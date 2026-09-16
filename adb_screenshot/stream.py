"""scrcpy 映像ストリームの受信・デコードと最新フレームの保持。

ストリーム形式（send_stream_meta / send_frame_meta 有効時）:
  - 先頭 4 バイト: コーデック ID（0 / 1 はストリーム無効の通知）
  - 以降 12 バイトヘッダの繰り返し
      MSB=1: セッションヘッダ  [flags:4][width:4][height:4]
      MSB=0: パケットヘッダ    [pts|flags:8][size:4] + データ size バイト
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable

import av
import numpy as np

from .server import recv_exact

log = logging.getLogger(__name__)

PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61
PACKET_PTS_MASK = PACKET_FLAG_KEY_FRAME - 1

CODEC_IDS = {
    0x68323634: "h264",
    0x68323635: "hevc",
    0x00617631: "av1",
}


class StreamError(RuntimeError):
    """映像ストリームのエラー。"""


class FrameStore:
    """最新のデコード済みフレームをスレッドセーフに保持する。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: av.VideoFrame | None = None
        self._seq = 0
        self._time = 0.0
        self.video_size: tuple[int, int] | None = None  # セッションヘッダの (width, height)
        self._fps_window: list[float] = []
        self.closed = False  # 受信スレッドが終了したら True（以降のフレームは更新されない）

    def mark_closed(self) -> None:
        """受信終了を記録し、待機中のスレッドを起こす。"""
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def put(self, frame: av.VideoFrame) -> None:
        now = time.monotonic()
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._time = now
            self._fps_window.append(now)
            self._fps_window = [t for t in self._fps_window if now - t < 1.0]
            self._cond.notify_all()

    def get(self) -> tuple[av.VideoFrame | None, int]:
        """(最新フレーム, シーケンス番号) を返す。"""
        with self._cond:
            return self._frame, self._seq

    def wait_new(self, after_seq: int, timeout: float) -> tuple[av.VideoFrame | None, int]:
        """after_seq より新しいフレームが来るまで待つ。タイムアウト時は現状を返す。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= after_seq and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._frame, self._seq

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    @property
    def fps(self) -> float:
        with self._cond:
            return float(len(self._fps_window))

    @property
    def frame_size(self) -> tuple[int, int] | None:
        """デコード済みフレームの (width, height)。"""
        with self._cond:
            if self._frame is None:
                return None
            return self._frame.width, self._frame.height


# PyAV の VideoFrame はフレームごとに 1 つの変換コンテキスト (SwsContext) を共有しており、
# reformat() / to_image() / to_ndarray() を複数スレッド（GUI 描画と撮影ループなど）から
# 同時に呼ぶと libswscale 内でデッドロックする。フレーム変換は必ずこのロックの中で行う。
CONVERT_LOCK = threading.Lock()


def frame_to_rgb(frame: av.VideoFrame, width: int | None = None, height: int | None = None) -> np.ndarray:
    """フレームを RGB24 の ndarray へ変換する（必要なら縮小）。"""
    with CONVERT_LOCK:
        if width is not None and height is not None:
            return frame.reformat(width=width, height=height, format="rgb24").to_ndarray()
        return frame.to_ndarray(format="rgb24")


def frame_to_pil(frame: av.VideoFrame):
    """フレームを PIL Image（RGB）へ変換する。"""
    with CONVERT_LOCK:
        return frame.to_image()


def frame_signature(frame: av.VideoFrame, shrink: int = 8) -> np.ndarray:
    """変化検出用に縮小したグレースケール配列を返す。"""
    w = max(1, frame.width // shrink)
    h = max(1, frame.height // shrink)
    with CONVERT_LOCK:
        small = frame.reformat(width=w, height=h, format="gray").to_ndarray()
    return small.astype(np.int16)


class VideoStream(threading.Thread):
    """映像ソケットを読み、PyAV でデコードして FrameStore へ格納するスレッド。"""

    def __init__(
        self,
        sock: socket.socket,
        store: FrameStore,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        super().__init__(daemon=True, name="video-stream")
        self._sock = sock
        self.store = store
        self._on_error = on_error
        self._stop_event = threading.Event()
        self.error: Exception | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001 - スレッド境界でまとめて通知する
            if not self._stop_event.is_set():
                self.error = e
                log.error("video stream error: %s", e)
                if self._on_error:
                    self._on_error(e)
        finally:
            self.store.mark_closed()

    def _run(self) -> None:
        raw = recv_exact(self._sock, 4)
        if raw is None:
            raise StreamError("コーデック ID を受信できませんでした")
        codec_id = int.from_bytes(raw, "big")
        if codec_id == 0:
            raise StreamError("端末が映像ストリームを無効化しました")
        if codec_id == 1:
            raise StreamError("端末側で設定エラーが発生しました（サーバーログを確認してください）")
        codec_name = CODEC_IDS.get(codec_id)
        if codec_name is None:
            raise StreamError(f"未知のコーデック ID: 0x{codec_id:08x}")
        log.info("codec: %s", codec_name)

        decoder = av.CodecContext.create(codec_name, "r")
        try:
            decoder.flags |= "LOW_DELAY"
        except Exception:  # noqa: BLE001 - PyAV のバージョン差異は無視してよい
            pass

        pending_config = b""
        while not self._stop_event.is_set():
            header = recv_exact(self._sock, 12)
            if header is None:
                if self._stop_event.is_set():
                    return
                raise StreamError("映像ソケットが閉じられました")

            if header[0] & 0x80:
                width = int.from_bytes(header[4:8], "big")
                height = int.from_bytes(header[8:12], "big")
                self.store.video_size = (width, height)
                log.info("video session: %dx%d", width, height)
                continue

            pts_flags = int.from_bytes(header[0:8], "big")
            size = int.from_bytes(header[8:12], "big")
            if size == 0:
                raise StreamError("パケット長 0 を受信しました")
            data = recv_exact(self._sock, size)
            if data is None:
                raise StreamError("パケット本体の受信に失敗しました")

            if pts_flags & PACKET_FLAG_CONFIG:
                # SPS/PPS は次のメディアパケットに連結して渡す（scrcpy 本体と同じ扱い）
                pending_config = data
                continue
            if pending_config:
                data = pending_config + data
                pending_config = b""

            packet = av.Packet(data)
            packet.pts = pts_flags & PACKET_PTS_MASK
            packet.dts = packet.pts
            try:
                frames = decoder.decode(packet)
            except av.error.InvalidDataError as e:
                log.warning("decode error (skipped): %s", e)
                continue
            for frame in frames:
                self.store.put(frame)
