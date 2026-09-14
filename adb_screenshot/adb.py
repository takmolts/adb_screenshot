"""adb コマンドの薄いラッパー。"""

from __future__ import annotations

import logging
import re
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


def connect(address: str) -> None:
    """`adb connect IP:port` を実行する。既に接続済みなら何もしない。"""
    out = run([adb_executable(), "connect", address], timeout=20.0).stdout.strip()
    log.info("adb connect: %s", out)
    if "connected" not in out:
        raise AdbError(f"adb connect に失敗しました: {out}")


_PHYSICAL_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
_OVERRIDE_SIZE_RE = re.compile(r"Override size:\s*(\d+)x(\d+)")


def display_size(serial: str) -> tuple[tuple[int, int], tuple[int, int] | None]:
    """`wm size` の (物理サイズ, 上書きサイズ or None) を返す。"""
    out = run(adb_args(serial) + ["shell", "wm", "size"]).stdout
    m = _PHYSICAL_SIZE_RE.search(out)
    if not m:
        raise AdbError(f"wm size の出力を解釈できません: {out.strip()!r}")
    physical = (int(m.group(1)), int(m.group(2)))
    o = _OVERRIDE_SIZE_RE.search(out)
    override = (int(o.group(1)), int(o.group(2))) if o else None
    return physical, override


def set_display_size(serial: str, width: int, height: int) -> None:
    """論理解像度を上書きする（`wm size WxH`）。"""
    run(adb_args(serial) + ["shell", "wm", "size", f"{width}x{height}"])
    log.info("wm size %dx%d を適用しました", width, height)


def reset_display_size(serial: str) -> None:
    """論理解像度の上書きを解除する（`wm size reset`）。"""
    run(adb_args(serial) + ["shell", "wm", "size", "reset"])
    log.info("wm size reset を実行しました")


def scaled_size(
    physical: tuple[int, int], width: int | None = None, height: int | None = None
) -> tuple[int, int]:
    """幅か高さの片方（または両方）から、物理解像度の縦横比を保ったサイズを返す。

    未指定側は比率から計算し、エンコーダ互換のため偶数に丸める。
    """
    pw, ph = physical
    if width is None and height is None:
        raise ValueError("幅か高さのどちらかを指定してください")
    if width is not None and height is not None:
        w, h = width, height
    elif width is not None:
        w = width
        h = round(width * ph / pw)
    else:
        assert height is not None
        h = height
        w = round(height * pw / ph)
    w += w % 2
    h += h % 2
    if w <= 0 or h <= 0:
        raise ValueError(f"解像度が不正です: {w}x{h}")
    return w, h
