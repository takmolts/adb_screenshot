"""adb コマンドの薄いラッパー。"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class AdbError(RuntimeError):
    """adb コマンドの失敗。"""


def adb_executable() -> str:
    """PATH 上の adb を返す。見つからなければ AdbError。"""
    exe = shutil.which("adb")
    if exe is None:
        raise AdbError("adb が見つかりません。PATH を確認してください。")
    return exe


def adb_args(serial: str | None) -> list[str]:
    """adb 呼び出しの共通プレフィックス（-s シリアル付き）を返す。"""
    args = [adb_executable()]
    if serial:
        args += ["-s", serial]
    return args


def run(args: list[str], *, timeout: float | None = 30.0) -> subprocess.CompletedProcess[str]:
    """adb サブコマンドを実行し、失敗時は AdbError を送出する。"""
    log.debug("run: %s", " ".join(args))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise AdbError(f"adb がタイムアウトしました: {' '.join(args)}") from e
    if proc.returncode != 0:
        raise AdbError(f"adb 失敗 ({proc.returncode}): {' '.join(args)}\n{proc.stderr.strip()}")
    return proc


def list_devices() -> list[tuple[str, str]]:
    """`adb devices` の (serial, state) 一覧を返す。"""
    out = run([adb_executable(), "devices"]).stdout
    devices: list[tuple[str, str]] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def resolve_serial(serial: str | None) -> str:
    """シリアル未指定なら接続中の唯一の端末を選ぶ。複数ある場合はエラー。"""
    if serial:
        return serial
    online = [s for s, state in list_devices() if state == "device"]
    if not online:
        raise AdbError("接続中の端末がありません（adb devices を確認してください）。")
    if len(online) > 1:
        raise AdbError(f"端末が複数あります。--serial で指定してください: {', '.join(online)}")
    return online[0]


def push(serial: str, local: Path, remote: str) -> None:
    """ファイルを端末へ転送する。"""
    run(adb_args(serial) + ["push", str(local), remote], timeout=120.0)


def forward(serial: str, local_port: int, abstract_name: str) -> None:
    """tcp:local_port を端末の localabstract ソケットへ転送する。"""
    run(adb_args(serial) + ["forward", f"tcp:{local_port}", f"localabstract:{abstract_name}"])


def forward_remove(serial: str, local_port: int) -> None:
    """forward 設定を解除する（失敗はログのみ）。"""
    try:
        run(adb_args(serial) + ["forward", "--remove", f"tcp:{local_port}"])
    except AdbError as e:
        log.debug("forward --remove failed: %s", e)


def shell_input_tap(serial: str, x: int, y: int) -> None:
    """`input tap` によるタップ（scrcpy 制御ソケットを使わない代替手段）。"""
    run(adb_args(serial) + ["shell", "input", "tap", str(x), str(y)])
