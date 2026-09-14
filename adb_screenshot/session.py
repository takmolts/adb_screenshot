"""サーバー起動・映像受信・制御をひとまとめにしたセッション。GUI と CLI の両方から使う。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from . import adb
from .control import Controller
from .server import ScrcpyServer, ServerOptions, find_server_file
from .stream import FrameStore, VideoStream, frame_signature

log = logging.getLogger(__name__)


class Session:
    """接続中の端末 1 台分の状態。

    display_size を指定すると接続時に `wm size` で論理解像度を上書きし、
    close() で `wm size reset` により元に戻す。
    """

    def __init__(
        self,
        serial: str | None,
        options: ServerOptions,
        *,
        server_path: str | None = None,
        server_version: str = "4.1",
        tap_method: str = "scrcpy",
        display_size: tuple[int, int] | None = None,
    ) -> None:
        self.serial = adb.resolve_serial(serial)
        self.options = options
        self.server_path: Path = find_server_file(server_path)
        self.server_version = server_version
        self.tap_method = tap_method
        self.display_size = display_size
        self._size_overridden = False
        self.physical_size: tuple[int, int] | None = None
        self.store = FrameStore()
        self.server: ScrcpyServer | None = None
        self.stream: VideoStream | None = None
        self.controller: Controller | None = None
        self.on_stream_error: Callable[[Exception], None] | None = None

    def start(self) -> None:
        """必要なら解像度を上書きし、サーバーを起動して映像受信と制御を準備する。"""
        self.physical_size, current_override = adb.display_size(self.serial)
        if self.display_size is not None and self.display_size != self.physical_size:
            if current_override != self.display_size:
                adb.set_display_size(self.serial, *self.display_size)
                time.sleep(0.5)  # 解像度変更直後の再レイアウトを待つ
            self._size_overridden = True

        try:
            self.server = ScrcpyServer(self.serial, self.server_path, self.options, self.server_version)
            self.server.start()
        except Exception:
            self._restore_display_size()
            raise
        assert self.server.video_socket is not None and self.server.control_socket is not None
        self.stream = VideoStream(self.server.video_socket, self.store, on_error=self._stream_error)
        self.stream.start()
        self.controller = Controller(self.server.control_socket, lambda: self.store.video_size)

    def _stream_error(self, e: Exception) -> None:
        if self.on_stream_error:
            self.on_stream_error(e)

    def _restore_display_size(self) -> None:
        if not self._size_overridden:
            return
        try:
            adb.reset_display_size(self.serial)
        except adb.AdbError as e:
            log.warning("wm size reset に失敗しました: %s", e)
        finally:
            self._size_overridden = False

    def tap(self, x: int, y: int) -> None:
        """設定されたタップ手段で座標をタップする。"""
        if self.tap_method == "adb":
            adb.shell_input_tap(self.serial, x, y)
        else:
            assert self.controller is not None
            self.controller.tap(x, y)

    def wait_first_frame(self, timeout: float = 10.0, blank_grace: float = 1.0) -> bool:
        """最初の有効なフレームを受信するまで待つ。

        エンコーダ起動直後の 1〜2 フレームは真っ黒になることがあるため、
        一様でないフレームが来るまで最大 blank_grace 秒だけ追加で待つ。
        """
        frame, seq = self.store.wait_new(0, timeout)
        if frame is None:
            return False
        deadline = time.monotonic() + blank_grace
        while time.monotonic() < deadline:
            if float(frame_signature(frame).std()) > 2.0:
                break
            frame, seq = self.store.wait_new(seq, max(0.0, deadline - time.monotonic()))
            if frame is None:
                return False
        return True

    def close(self) -> None:
        """映像受信とサーバーを停止し、上書きした解像度を元に戻す。"""
        if self.stream is not None:
            self.stream.stop()
        if self.server is not None:
            self.server.stop()
        if self.stream is not None:
            self.stream.join(timeout=2)
        self._restore_display_size()
        log.info("session closed")
