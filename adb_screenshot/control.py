"""scrcpy 制御ソケットへのタッチ／キー注入（scrcpy 4.x の ControlMessage 形式）。"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_BACK_OR_SCREEN_ON = 4

ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

POINTER_ID_GENERIC_FINGER = -2

KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_APP_SWITCH = 187


class ControlError(RuntimeError):
    """制御メッセージ送信の失敗。"""


class Controller:
    """タッチ・キーイベントを scrcpy-server へ送る。

    座標は映像（受信フレーム）座標系で指定する。サーバー側は
    (screen_w, screen_h) が現在の映像サイズと一致しないイベントを無視するため、
    size_getter で常に最新の映像サイズを渡す。
    """

    def __init__(self, sock: socket.socket, size_getter: Callable[[], tuple[int, int] | None]) -> None:
        self._sock = sock
        self._size_getter = size_getter
        self._lock = threading.Lock()
        self._drain = threading.Thread(target=self._drain_device_messages, daemon=True, name="control-drain")
        self._drain.start()

    def _drain_device_messages(self) -> None:
        """サーバーからのデバイスメッセージ（クリップボード等）を読み捨てる。"""
        try:
            while True:
                if not self._sock.recv(4096):
                    return
        except OSError:
            return

    def _send(self, payload: bytes) -> None:
        with self._lock:
            try:
                self._sock.sendall(payload)
            except OSError as e:
                raise ControlError(f"制御メッセージの送信に失敗しました: {e}") from e

    def _screen_size(self) -> tuple[int, int]:
        size = self._size_getter()
        if size is None:
            raise ControlError("映像サイズが未確定のためタッチを送れません")
        return size

    # ---------------------------------------------------------------- touch
    def touch(self, action: int, x: int, y: int, pressure: float = 1.0) -> None:
        """単一ポインタのタッチイベントを送る。"""
        w, h = self._screen_size()
        p = 0xFFFF if pressure >= 1.0 else int(max(0.0, pressure) * 0x10000)
        payload = struct.pack(
            ">BBqiiHHHii",
            TYPE_INJECT_TOUCH_EVENT,
            action,
            POINTER_ID_GENERIC_FINGER,
            int(x),
            int(y),
            w,
            h,
            p,
            0,  # action button
            0,  # buttons
        )
        self._send(payload)

    def tap(self, x: int, y: int, hold: float = 0.05) -> None:
        """指定座標をタップする。"""
        self.touch(ACTION_DOWN, x, y)
        time.sleep(hold)
        self.touch(ACTION_UP, x, y, pressure=0.0)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3, steps: int = 10) -> None:
        """2 点間をスワイプする。"""
        self.touch(ACTION_DOWN, x1, y1)
        for i in range(1, steps + 1):
            t = i / steps
            self.touch(ACTION_MOVE, int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
            time.sleep(duration / steps)
        self.touch(ACTION_UP, x2, y2, pressure=0.0)

    # ------------------------------------------------------------------ key
    def key(self, keycode: int) -> None:
        """キーを押して離す。"""
        for action in (ACTION_DOWN, ACTION_UP):
            self._send(struct.pack(">BBiii", TYPE_INJECT_KEYCODE, action, keycode, 0, 0))

    def back(self) -> None:
        self.key(KEYCODE_BACK)

    def home(self) -> None:
        self.key(KEYCODE_HOME)

    def app_switch(self) -> None:
        self.key(KEYCODE_APP_SWITCH)
