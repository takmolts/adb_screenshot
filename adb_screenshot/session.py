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
    """接続中の端末 1 台分の状態。"""

    def __init__(
        self,
        serial: str | None,
        options: ServerOptions,
        *,
        server_path: str | None = None,
        server_version: str = "4.1",
        tap_method: str = "scrcpy",
    ) -> None:
        self.serial = adb.resolve_serial(serial)
        self.options = options
        self.server_path: Path = find_server_file(server_path)
        self.server_version = server_version
        self.tap_method = tap_method
        self.store = FrameStore()
        self.server: ScrcpyServer | None = None
        self.stream: VideoStream | None = None
        self.controller: Controller | None = None
        self.on_stream_error: Callable[[Exception], None] | None = None

    def start(self) -> None:
        """サーバーを起動し、映像受信スレッドと制御を準備する。"""
        self.server = ScrcpyServer(self.serial, self.server_path, self.options, self.server_version)
        self.server.start()
        assert self.server.video_socket is not None and self.server.control_socket is not None
        self.stream = VideoStream(self.server.video_socket, self.store, on_error=self._stream_error)
        self.stream.start()
        self.controller = Controller(self.server.control_socket, lambda: self.store.video_size)

    def _stream_error(self, e: Exception) -> None:
        if self.on_stream_error:
            self.on_stream_error(e)

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
        if self.stream is not None:
            self.stream.stop()
        if self.server is not None:
            self.server.stop()
        if self.stream is not None:
            self.stream.join(timeout=2)
        log.info("session closed")
