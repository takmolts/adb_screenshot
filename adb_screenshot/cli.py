"""コマンドライン引数の解釈と、GUI／ヘッドレス実行の起点。"""

from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import adb
from .capture import DEFAULT_JPEG_QUALITY, CaptureError, Region, SequenceConfig, SequenceRunner, book_dir
from .presets import DEFAULT_PRESETS_PATH, PresetStore
from .server import DEFAULT_SERVER_VERSION, ServerOptions
from .session import Session

log = logging.getLogger(__name__)


def parse_point(text: str) -> tuple[int, int]:
    """'x,y' を解釈する。"""
    try:
        x, y = (int(v) for v in text.split(","))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"座標の書式が不正です（x,y）: {text!r}") from e
    return x, y


def parse_bit_rate(text: str) -> int:
    """'16M' / '8000K' / '8000000' を bps へ変換する。"""
    m = re.fullmatch(r"(\d+)([kKmM]?)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"ビットレートの書式が不正です: {text!r}")
    value = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "k":
        value *= 1000
    elif unit == "m":
        value *= 1_000_000
    return value


def next_index(out_dir: Path, prefix: str, ext: str) -> int:
    """既存ファイルと衝突しない次の連番を返す。"""
    pattern = re.compile(re.escape(prefix) + r"(\d{4,})\." + re.escape(ext) + "$")
    highest = 0
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            m = pattern.fullmatch(p.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adb_screenshot",
        description="scrcpy の映像を受信解像度のまま保存するミラーリング／スクリーンショットツール",
    )
    g = p.add_argument_group("接続")
    g.add_argument("-s", "--serial", help="対象端末のシリアル（adb devices の値）")
    g.add_argument("--server", help="scrcpy-server ファイルのパス")
    g.add_argument("--server-version", default=DEFAULT_SERVER_VERSION, help="scrcpy-server のバージョン文字列")
    g.add_argument("--tap-method", choices=["scrcpy", "adb"], default="scrcpy",
                   help="タップ手段: scrcpy 制御ソケット（既定）/ adb shell input tap")

    g = p.add_argument_group("映像")
    g.add_argument("--bit-rate", type=parse_bit_rate, default="16M", help="映像ビットレート（例: 16M, 8000K）")
    g.add_argument("--max-fps", type=float, help="最大フレームレート")
    g.add_argument("--max-size", type=int, default=0, help="サーバー側の最大辺サイズ（0 = 縮小しない）")
    g.add_argument("--video-codec", choices=["h264", "h265", "av1"], default="h264")
    g.add_argument("--display-id", type=int, help="ミラーリングするディスプレイ ID")
    g.add_argument("--new-display", help="仮想ディスプレイを作成して映す（例: 1920x1080/420）")
    g.add_argument("--allow-downsize", action="store_true",
                   help="エンコード失敗時にサーバー側の自動縮小を許可する（既定は失敗扱い）")
    g.add_argument("--width", type=int, help="端末の論理解像度を幅で上書き（wm size、高さは比率から自動計算）")
    g.add_argument("--height", type=int, help="端末の論理解像度を高さで上書き（wm size、幅は比率から自動計算）")

    g = p.add_argument_group("保存")
    g.add_argument("-o", "--out", default="output", help="保存先のベースディレクトリ")
    g.add_argument("--book", default="", help="書籍名（保存先が out/『書籍名 巻数』になる）")
    g.add_argument("--volume", default="", help="巻数・号数（例: 12, 2026年40号）")
    g.add_argument("--prefix", default="", help="ファイル名プレフィックス")
    g.add_argument("--digits", type=int, default=5, help="連番の桁数")
    g.add_argument("--format", choices=["png", "jpg"], default="png")
    g.add_argument(
        "--quality", type=int, default=DEFAULT_JPEG_QUALITY, help="JPEG 品質 1〜95（--format jpg のときのみ有効）"
    )
    g.add_argument("--region", type=Region.parse, help="切り出し領域 x,y,w,h（映像座標）")

    g = p.add_argument_group("プリセット")
    g.add_argument("--presets", help=f"プリセットファイル（既定: {DEFAULT_PRESETS_PATH}）")
    g.add_argument("--preset", help="読み込むプリセット名（書籍名）。--region/--tap/--interval/--settle の未指定分を補う")

    g = p.add_argument_group("自動実行")
    g.add_argument("-n", "--count", type=int, default=1, help="スクリーンショット回数")
    g.add_argument("--tap", type=parse_point, help="各ショット後にタップする座標 x,y（映像座標）")
    g.add_argument("--interval", type=float, default=None, help="タップ後の最小待ち時間 [s]（既定 1.0）")
    g.add_argument("--settle", type=float, default=None, help="画面静止とみなす継続時間 [s]（既定 0.5、0 で無効）")
    g.add_argument("--timeout", type=float, default=10.0, help="静止待ちの上限 [s]")
    g.add_argument("--start-delay", type=float, default=0.0, help="ヘッドレス実行開始前の待ち時間 [s]")

    p.add_argument("--no-gui", action="store_true", help="GUI を開かずに自動実行だけ行う")
    p.add_argument("-v", "--verbose", action="store_true", help="デバッグログを表示")
    return p


def make_options(args: argparse.Namespace) -> ServerOptions:
    return ServerOptions(
        max_size=args.max_size,
        video_bit_rate=args.bit_rate if isinstance(args.bit_rate, int) else parse_bit_rate(args.bit_rate),
        max_fps=args.max_fps,
        video_codec=args.video_codec,
        display_id=args.display_id,
        new_display=args.new_display,
        allow_downsize=args.allow_downsize,
        log_level="debug" if args.verbose else "info",
    )


def make_session(
    args: argparse.Namespace,
    serial: str | None = None,
    display_size: tuple[int, int] | None = None,
) -> Session:
    """引数から Session を組み立てる。serial / display_size は GUI からの上書き用。"""
    serial = serial or args.serial
    if display_size is None and (args.width or args.height):
        physical, _ = adb.display_size(adb.resolve_serial(serial))
        display_size = adb.scaled_size(physical, args.width, args.height)
    return Session(
        serial,
        make_options(args),
        server_path=args.server,
        server_version=args.server_version,
        tap_method=args.tap_method,
        display_size=display_size,
    )


DEFAULT_INTERVAL = 1.0
DEFAULT_SETTLE = 0.5


@dataclass
class CaptureParams:
    """引数とプリセットを合成したキャプチャ設定。優先順: 明示引数 > プリセット > 既定値。"""

    region: Region | None
    tap: tuple[int, int] | None
    interval: float
    settle: float
    book: str


def resolve_capture_params(args: argparse.Namespace, store: PresetStore) -> CaptureParams:
    preset = None
    if args.preset:
        preset = store.get(args.preset)
        if preset is None:
            raise CaptureError(f"プリセットが見つかりません: {args.preset!r}（登録済み: {', '.join(store.names()) or 'なし'}）")
    region = args.region
    tap = args.tap
    interval = args.interval
    settle = args.settle
    book = args.book
    if preset is not None:
        if region is None and preset.region:
            region = Region.parse(preset.region)
        if tap is None and preset.tap:
            tap = parse_point(preset.tap)
        if interval is None:
            interval = preset.interval
        if settle is None:
            settle = preset.settle
        if not book:
            book = preset.name
    return CaptureParams(
        region=region,
        tap=tap,
        interval=DEFAULT_INTERVAL if interval is None else interval,
        settle=DEFAULT_SETTLE if settle is None else settle,
        book=book,
    )


def run_headless(args: argparse.Namespace) -> int:
    """GUI なしで自動実行する。"""
    store = PresetStore(args.presets)
    params = resolve_capture_params(args, store)
    out_dir = book_dir(Path(args.out), params.book, args.volume)
    session = make_session(args)
    session.start()
    try:
        if not session.wait_first_frame(timeout=15.0):
            log.error("映像を受信できませんでした")
            return 1
        size = session.store.frame_size
        log.info("受信解像度: %dx%d", *size)  # type: ignore[misc]
        if args.start_delay > 0:
            log.info("%.1f 秒後に開始します", args.start_delay)
            time.sleep(args.start_delay)

        cfg = SequenceConfig(
            count=args.count,
            out_dir=out_dir,
            prefix=args.prefix,
            ext=args.format,
            start_index=next_index(out_dir, args.prefix, args.format),
            region=params.region,
            tap=params.tap,
            interval=params.interval,
            settle=params.settle,
            timeout=args.timeout,
            digits=args.digits,
            quality=args.quality,
        )
        log.info("保存先: %s（領域 %s / タップ %s）", out_dir, params.region or "全体", params.tap or "なし")
        runner = SequenceRunner(session.store, cfg, session.tap)
        signal.signal(signal.SIGINT, lambda *_: runner.stop())
        saved = runner.run()
        log.info("%d 枚保存しました: %s", len(saved), out_dir)
        return 0
    except CaptureError as e:
        log.error("%s", e)
        return 1
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        if args.no_gui:
            return run_headless(args)
        from .gui import run_gui

        return run_gui(args)
    except Exception as e:  # noqa: BLE001 - 最上位でユーザー向けに表示する
        log.error("%s", e)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
