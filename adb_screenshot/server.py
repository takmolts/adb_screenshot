"""scrcpy-server の起動と映像／制御ソケットの接続。

scrcpy 本体（app/src/server.c）の forward トンネル方式を Python で再現する。

  1. scrcpy-server を /data/local/tmp へ push
  2. adb forward tcp:PORT localabstract:scrcpy_XXXXXXXX
  3. adb shell app_process ... com.genymobile.scrcpy.Server VERSION key=value ...
  4. 映像ソケット接続 → ダミー1バイト → デバイス名64バイト
  5. 制御ソケット接続
"""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import adb

log = logging.getLogger(__name__)

DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server-adbss.jar"
DEVICE_NAME_FIELD_LENGTH = 64
DEFAULT_SERVER_VERSION = "4.1"

_VERSION_MISMATCH_RE = re.compile(r"The server version \(([^)]+)\) does not match the client")


class ServerError(RuntimeError):
    """scrcpy-server の起動／接続失敗。"""


@dataclass
class ServerOptions:
    """scrcpy-server に渡すオプション。"""

    max_size: int = 0  # 0 = サーバー側で縮小しない
    video_bit_rate: int = 16_000_000
    max_fps: float | None = None
    video_codec: str = "h264"
    display_id: int | None = None
    new_display: str | None = None  # 例: "1920x1080/420"
    allow_downsize: bool = False  # エンコード失敗時の自動縮小を許可するか
    stay_awake: bool = True
    log_level: str = "info"

    def to_params(self, scid: int) -> list[str]:
        """key=value 形式のサーバー引数へ変換する。"""
        params = [
            f"scid={scid:08x}",
            f"log_level={self.log_level}",
            "audio=false",
            "tunnel_forward=true",
            "clipboard_autosync=false",
            f"max_size={self.max_size}",
            f"video_bit_rate={self.video_bit_rate}",
            f"downsize_on_error={'true' if self.allow_downsize else 'false'}",
            f"stay_awake={'true' if self.stay_awake else 'false'}",
        ]
        if self.video_codec != "h264":
            params.append(f"video_codec={self.video_codec}")
        if self.max_fps:
            params.append(f"max_fps={self.max_fps:g}")
        if self.display_id is not None:
            params.append(f"display_id={self.display_id}")
        if self.new_display is not None:
            params.append(f"new_display={self.new_display}")
        return params


def find_server_file(explicit: str | None = None) -> Path:
    """scrcpy-server ファイルを探す。優先順: 引数 > 環境変数 > scrcpy 実行ファイルの隣 > 既定パス。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("SCRCPY_SERVER_PATH")
    if env:
        candidates.append(Path(env))
    scrcpy_exe = shutil.which("scrcpy")
    if scrcpy_exe:
        candidates.append(Path(os.path.realpath(scrcpy_exe)).parent / "scrcpy-server")
    candidates += [
        Path("/usr/share/scrcpy/scrcpy-server"),
        Path("/usr/local/share/scrcpy/scrcpy-server"),
        Path(__file__).resolve().parent.parent / "scrcpy-server",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise ServerError(
        "scrcpy-server が見つかりません。--server か SCRCPY_SERVER_PATH で指定してください。"
    )


def _free_tcp_port() -> int:
    """空いている TCP ポート番号を返す。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """ちょうど n バイト受信する。EOF なら None。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class ScrcpyServer:
    """scrcpy-server のライフサイクルを管理する。"""

    def __init__(
        self,
        serial: str,
        server_path: Path,
        options: ServerOptions | None = None,
        version: str = DEFAULT_SERVER_VERSION,
    ) -> None:
        self.serial = serial
        self.server_path = server_path
        self.options = options or ServerOptions()
        self.version = version
        self.local_port: int | None = None
        self.scid = random.randint(0, 0x7FFFFFFF)
        self.device_name = ""
        self.video_socket: socket.socket | None = None
        self.control_socket: socket.socket | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._output_lines: list[str] = []
        self._log_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        """サーバーを起動してソケットを接続する。バージョン不一致は 1 回だけ自動で追従する。"""
        adb.push(self.serial, self.server_path, DEVICE_SERVER_PATH)
        try:
            self._start_once()
        except ServerError as e:
            actual = self._detect_server_version()
            if actual and actual != self.version:
                log.warning("サーバーバージョン %s に合わせて再起動します（指定: %s）", actual, self.version)
                self._cleanup_process()
                self.version = actual
                self.scid = random.randint(0, 0x7FFFFFFF)
                self._start_once()
            else:
                raise e

    def _start_once(self) -> None:
        socket_name = f"scrcpy_{self.scid:08x}"
        self.local_port = _free_tcp_port()
        adb.forward(self.serial, self.local_port, socket_name)

        cmd = adb.adb_args(self.serial) + [
            "shell",
            f"CLASSPATH={DEVICE_SERVER_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            self.version,
            *self.options.to_params(self.scid),
        ]
        log.info("server: %s", " ".join(cmd[cmd.index("app_process") :]))
        self._output_lines = []
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        self._log_thread = threading.Thread(target=self._pump_output, daemon=True, name="scrcpy-server-log")
        self._log_thread.start()

        try:
            # サーバーは全ソケットの accept 完了後にデバイス名を送るため、
            # video → control の順に接続し終えてからデバイス名を読む。
            self.video_socket = self._connect_first(self.local_port)
            self.control_socket = socket.create_connection(("127.0.0.1", self.local_port), timeout=5)
            self.control_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.control_socket.settimeout(None)

            name = recv_exact(self.video_socket, DEVICE_NAME_FIELD_LENGTH)
            if name is None:
                raise ServerError("デバイス名の受信に失敗しました")
            self.device_name = name.split(b"\0", 1)[0].decode("utf-8", "replace")
            log.info("device: %s", self.device_name)
        except Exception:
            self._close_sockets()
            raise

    def _connect_first(self, port: int, attempts: int = 100, delay: float = 0.1) -> socket.socket:
        """最初のソケットへ接続し、ダミー1バイトを読めるまで再試行する。"""
        for _ in range(attempts):
            if self._proc is not None and self._proc.poll() is not None:
                raise ServerError("scrcpy-server が終了しました:\n" + self.output_tail())
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=2)
            except OSError:
                time.sleep(delay)
                continue
            try:
                byte = s.recv(1)
            except OSError:
                byte = b""
            if byte == b"\x00":
                s.settimeout(None)
                return s
            s.close()
            time.sleep(delay)
        raise ServerError("scrcpy-server へ接続できませんでした:\n" + self.output_tail())

    # ---------------------------------------------------------------- output
    def _pump_output(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip()
            self._output_lines.append(line)
            if len(self._output_lines) > 200:
                del self._output_lines[:-200]
            log.info("[server] %s", line)

    def output_tail(self, n: int = 20) -> str:
        """サーバー出力の末尾を返す（エラー表示用）。"""
        return "\n".join(self._output_lines[-n:])

    def _detect_server_version(self) -> str | None:
        for line in self._output_lines:
            m = _VERSION_MISMATCH_RE.search(line)
            if m:
                return m.group(1)
        return None

    # ------------------------------------------------------------------ stop
    def _close_sockets(self) -> None:
        for s in (self.video_socket, self.control_socket):
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
        self.video_socket = None
        self.control_socket = None

    def _cleanup_process(self) -> None:
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self.local_port is not None:
            adb.forward_remove(self.serial, self.local_port)
            self.local_port = None

    def stop(self) -> None:
        """ソケットを閉じてサーバーを終了させ、forward を解除する。"""
        self._close_sockets()
        self._cleanup_process()
