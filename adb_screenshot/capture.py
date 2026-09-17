"""スクリーンショット保存と「スクショ → タップ → スクショ …」の自動実行。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import av
import numpy as np

from .stream import FrameStore, frame_signature, frame_to_pil

log = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    """キャプチャ処理の失敗。"""


@dataclass(frozen=True)
class Region:
    """映像座標系での切り出し領域。"""

    x: int
    y: int
    w: int
    h: int

    @classmethod
    def parse(cls, text: str) -> "Region":
        """'x,y,w,h' 形式を解釈する。"""
        try:
            x, y, w, h = (int(v) for v in text.split(","))
        except ValueError as e:
            raise CaptureError(f"領域の書式が不正です（x,y,w,h）: {text!r}") from e
        if w <= 0 or h <= 0:
            raise CaptureError(f"領域の幅・高さは正である必要があります: {text!r}")
        return cls(x, y, w, h)

    def clamp(self, width: int, height: int) -> "Region":
        """フレームの範囲に収める。"""
        x0 = min(max(self.x, 0), width)
        y0 = min(max(self.y, 0), height)
        x1 = min(max(self.x + self.w, 0), width)
        y1 = min(max(self.y + self.h, 0), height)
        if x1 <= x0 or y1 <= y0:
            raise CaptureError(f"領域がフレーム外です: {self} / frame {width}x{height}")
        return Region(x0, y0, x1 - x0, y1 - y0)

    def __str__(self) -> str:
        return f"{self.x},{self.y},{self.w},{self.h}"


def frame_to_image(frame: av.VideoFrame, region: Region | None = None):
    """フレームを PIL Image（RGB）へ変換し、必要なら切り出す。"""
    image = frame_to_pil(frame)
    if region is not None:
        r = region.clamp(frame.width, frame.height)
        image = image.crop((r.x, r.y, r.x + r.w, r.y + r.h))
    return image


DEFAULT_JPEG_QUALITY = 75  # adb screencap ベースのツールと同程度のファイルサイズになる値


def save_frame(
    frame: av.VideoFrame, path: Path, region: Region | None = None, quality: int = DEFAULT_JPEG_QUALITY
) -> Path:
    """フレームをファイルへ保存する。拡張子で形式を決める。quality は JPEG のみ有効（1〜95）。"""
    image = frame_to_image(frame, region)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".jpg", ".jpeg"):
        image.save(path, quality=max(1, min(95, quality)), optimize=True)
    else:
        image.save(path)
    return path


def wait_settled(
    store: FrameStore,
    *,
    after_seq: int,
    min_wait: float,
    settle: float,
    timeout: float,
    stop: threading.Event | None = None,
    threshold: float = 1.0,
) -> bool:
    """タップ後に画面が落ち着くまで待つ。

    - after_seq より新しいフレームを受信し、かつ min_wait 秒経過するまで待つ
    - settle > 0 なら、縮小画像の差分が threshold 未満の状態が settle 秒続くまで待つ
    - timeout 秒で打ち切り（False を返す）
    """
    t0 = time.monotonic()
    deadline = t0 + timeout

    def _stopped() -> bool:
        return stop is not None and stop.is_set()

    # 新しいフレーム + 最小待ち時間
    _, seq = store.wait_new(after_seq, timeout=max(0.0, deadline - time.monotonic()))
    while time.monotonic() - t0 < min_wait:
        if _stopped():
            return False
        time.sleep(min(0.05, max(0.0, min_wait - (time.monotonic() - t0))))

    if settle <= 0:
        return True

    last_sig: np.ndarray | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline and not _stopped():
        frame, seq = store.get()
        if frame is None:
            time.sleep(0.05)
            continue
        sig = frame_signature(frame)
        now = time.monotonic()
        if last_sig is not None and sig.shape == last_sig.shape:
            diff = float(np.abs(sig - last_sig).mean())
            log.debug("settle: seq=%d diff=%.2f elapsed=%.2fs", seq, diff, now - t0)
            if diff < threshold:
                stable_since = stable_since if stable_since is not None else now
                if now - stable_since >= settle:
                    return True
            else:
                stable_since = None
        last_sig = sig
        store.wait_new(seq, timeout=min(0.5, max(0.0, deadline - now)))
    return False


@dataclass
class SequenceConfig:
    """自動実行の設定。"""

    count: int
    out_dir: Path
    prefix: str = ""
    ext: str = "png"
    start_index: int = 1
    region: Region | None = None
    tap: tuple[int, int] | None = None
    interval: float = 1.0  # タップ後の最小待ち時間 [s]
    settle: float = 0.5  # 画面が静止しているとみなす継続時間 [s]（0 で無効）
    timeout: float = 10.0  # 静止待ちの上限 [s]
    digits: int = 5  # 連番の桁数
    quality: int = DEFAULT_JPEG_QUALITY  # JPEG 品質（1〜95）

    def path_for(self, index: int) -> Path:
        return self.out_dir / f"{self.prefix}{index:0{self.digits}d}.{self.ext}"


def book_dir(base: Path, book: str = "", volume: str = "") -> Path:
    """保存先ディレクトリ `base/{書籍名} {巻数}` を返す。書籍名・巻数が空ならその分を省く。"""
    name = " ".join(part.strip() for part in (book, volume) if part and part.strip())
    name = name.replace("/", "／").replace("\\", "＼")
    return base / name if name else base


@dataclass(frozen=True)
class Progress:
    """自動実行の進捗。1 枚保存するごとに on_progress へ渡される。"""

    done: int  # 保存済み枚数（今保存した分を含む）
    total: int
    path: Path
    elapsed: float  # 開始からの経過秒
    eta: float | None  # 残り目安秒（1 枚目の直後など推定できないときは None）

    @property
    def remaining(self) -> int:
        return self.total - self.done

    def summary(self) -> str:
        """'12/300 枚  残り約 4 分' のような短い表示。"""
        text = f"{self.done}/{self.total} 枚"
        if self.remaining > 0:
            text += f"  残り約 {format_duration(self.eta)}" if self.eta is not None else "  残り時間 計測中"
        return text


def format_duration(seconds: float) -> str:
    """秒数を '30 秒' / '4 分' / '1 時間 20 分' 形式にする。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(round(seconds))} 秒"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 時間 {minutes} 分" if minutes else f"{hours} 時間"


class SequenceRunner:
    """スクショ → タップ → 待機 を count 回繰り返す。"""

    def __init__(
        self,
        store: FrameStore,
        config: SequenceConfig,
        tap_func: Callable[[int, int], None] | None,
        on_progress: Callable[[Progress], None] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._tap = tap_func
        self._on_progress = on_progress
        self.stop_event = threading.Event()
        self.saved: list[Path] = []
        self.progress: Progress | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def _make_progress(self, done: int, path: Path, elapsed: float) -> Progress:
        """経過時間から残り目安を出す。1 枚目はタップ待ちを含まないので推定しない。"""
        total = self.config.count
        eta = None
        if done >= 2 and done < total:
            # 1 枚目は「保存だけ」なので除き、2 枚目以降の「タップ→待機→保存」の平均で見積もる
            eta = elapsed / (done - 1) * (total - done)
        elif done >= total:
            eta = 0.0
        return Progress(done=done, total=total, path=path, elapsed=elapsed, eta=eta)

    def run(self) -> list[Path]:
        cfg = self.config
        if cfg.tap is not None and self._tap is None:
            raise CaptureError("タップ手段が設定されていません")

        frame, seq = self.store.get()
        if frame is None:
            frame, seq = self.store.wait_new(0, timeout=cfg.timeout)
            if frame is None:
                raise CaptureError("映像フレームをまだ受信していません")

        started = time.monotonic()
        for i in range(cfg.count):
            if self.stop_event.is_set():
                break
            if self.store.closed:
                raise CaptureError(f"映像ストリームが切断されました（{len(self.saved)} 枚保存済み）")
            frame, seq = self.store.get()
            assert frame is not None
            path = save_frame(frame, cfg.path_for(cfg.start_index + i), cfg.region, cfg.quality)
            self.saved.append(path)
            progress = self._make_progress(i + 1, path, time.monotonic() - started)
            self.progress = progress
            log.info("saved %s: %s (%dx%d)", progress.summary(), path, frame.width, frame.height)
            if self._on_progress:
                self._on_progress(progress)

            if i == cfg.count - 1:
                break
            if cfg.tap is None:
                # タップなしの連続保存: interval だけ待つ
                if self.stop_event.wait(cfg.interval):
                    break
                self.store.wait_new(seq, timeout=1.0)
                continue
            assert self._tap is not None
            self._tap(*cfg.tap)
            ok = wait_settled(
                self.store,
                after_seq=seq,
                min_wait=cfg.interval,
                settle=cfg.settle,
                timeout=cfg.timeout,
                stop=self.stop_event,
            )
            if not ok and not self.stop_event.is_set():
                log.warning("画面の静止を %.1fs 以内に確認できませんでした（そのまま続行）", cfg.timeout)
        return self.saved
