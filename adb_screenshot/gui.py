"""Tkinter によるミラーリング表示と操作 UI。

- 起動時は未接続。上部の接続パネルで端末と受信解像度を指定して「接続」する
- 画面には縮小／拡大表示するが、保存は常に受信解像度のフレームから行う
- 表示エリアはズーム（ボタン・ホイール）と移動（中ボタン／右ボタンドラッグ）ができる
- 保存領域の赤枠は四隅のハンドルでリサイズ、枠内ドラッグで移動できる
"""

from __future__ import annotations

import argparse
import logging
import math
import queue
import signal
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image

from . import adb, cli
from .capture import CaptureError, Region, SequenceConfig, SequenceRunner, book_dir, save_frame
from .control import ACTION_DOWN, ACTION_MOVE, ACTION_UP
from .presets import Preset, PresetStore
from .session import Session

log = logging.getLogger(__name__)

MODE_CONTROL = "control"
MODE_REGION = "region"
MODE_TAP = "tap"
MODE_PAN = "pan"

MODE_CURSORS = {MODE_CONTROL: "crosshair", MODE_REGION: "crosshair", MODE_TAP: "crosshair", MODE_PAN: "fleur"}

RENDER_INTERVAL_MS = 33
HANDLE_HALF = 5  # 四隅ハンドルの半径 [px]
HANDLE_HIT = 9  # ハンドル判定の許容距離 [px]
ZOOM_STEP = 1.5
ZOOM_MAX = 32.0


class App:
    """メインウィンドウ。"""

    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.session: Session | None = None
        self.runner: SequenceRunner | None = None
        self._events: queue.Queue = queue.Queue()
        self._connecting = False

        # 表示ビュー: canvas = ox + source * scale
        self.scale = 1.0
        self.ox = 0.0
        self.oy = 0.0
        self.fit = True
        self.frame_size: tuple[int, int] | None = None
        self._photo: tk.PhotoImage | None = None
        self._rendered_seq = -1
        self._view_dirty = True
        self._rgb_cache: tuple[int, np.ndarray] | None = None

        self._drag: dict | None = None
        self._pan_start: tuple[float, float, float, float] | None = None
        self._updating_size = False
        self.physical_size: tuple[int, int] | None = None

        # 接続パネル
        self.device_var = tk.StringVar(value=args.serial or "")
        self.physical_var = tk.StringVar(value="元解像度: -")
        self.width_var = tk.StringVar(value=str(args.width) if args.width else "")
        self.height_var = tk.StringVar(value=str(args.height) if args.height else "")

        # プリセット（引数 > プリセット > 既定値 の順で初期値を決める）
        self.presets = PresetStore(args.presets)
        try:
            params = cli.resolve_capture_params(args, self.presets)
            preset_error: str | None = None
        except CaptureError as e:
            preset_error = str(e)
            args_no_preset = argparse.Namespace(**{**vars(args), "preset": None})
            params = cli.resolve_capture_params(args_no_preset, self.presets)
        self.preset_var = tk.StringVar(value=args.preset or "")
        self.book_var = tk.StringVar(value=params.book)
        self.volume_var = tk.StringVar(value=args.volume)
        self.save_path_var = tk.StringVar(value="")

        # 操作パネル
        self.mode = tk.StringVar(value=MODE_CONTROL)
        self.region_var = tk.StringVar(value=str(params.region) if params.region else "")
        self.tap_var = tk.StringVar(value=f"{params.tap[0]},{params.tap[1]}" if params.tap else "")
        self.count_var = tk.StringVar(value=str(args.count))
        self.interval_var = tk.StringVar(value=f"{params.interval:g}")
        self.settle_var = tk.StringVar(value=f"{params.settle:g}")
        self.timeout_var = tk.StringVar(value=f"{args.timeout:g}")
        self.out_var = tk.StringVar(value=args.out)
        self.prefix_var = tk.StringVar(value=args.prefix)
        self.format_var = tk.StringVar(value=args.format)
        for var in (self.out_var, self.book_var, self.volume_var, self.prefix_var, self.format_var):
            var.trace_add("write", lambda *_: self._update_save_path())
        self.status_var = tk.StringVar(value="未接続")
        self.progress_var = tk.StringVar(value="")
        self.zoom_var = tk.StringVar(value="全体")
        self.coord_var = tk.StringVar(value="")

        root.title("adb_screenshot")
        root.geometry("1200x850")
        root.minsize(700, 500)
        self._build_widgets()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._update_save_path()
        root.after(100, self._poll_events)
        root.after(RENDER_INTERVAL_MS, self._render_loop)
        root.after(50, self._refresh_devices)
        if preset_error:
            root.after(200, lambda: messagebox.showwarning("プリセット", preset_error))

    # ================================================================ widgets
    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._build_connect_bar()
        self._build_canvas()
        self._build_zoom_bar()
        self._build_side_panel()

    def _build_connect_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.grid(row=0, column=0, columnspan=2, sticky="we")
        ttk.Label(bar, text="端末").pack(side="left")
        self.device_combo = ttk.Combobox(bar, textvariable=self.device_var, width=26)
        self.device_combo.pack(side="left", padx=(4, 2))
        self.device_combo.bind("<<ComboboxSelected>>", lambda _e: self._fetch_physical_size())
        self.device_combo.bind("<Return>", lambda _e: self._fetch_physical_size())
        ttk.Button(bar, text="更新", width=5, command=self._refresh_devices).pack(side="left")
        ttk.Label(bar, textvariable=self.physical_var).pack(side="left", padx=(12, 8))

        ttk.Label(bar, text="受信解像度 幅").pack(side="left")
        w_entry = ttk.Entry(bar, textvariable=self.width_var, width=7)
        w_entry.pack(side="left", padx=2)
        w_entry.bind("<KeyRelease>", lambda _e: self._on_size_edited("width"))
        ttk.Label(bar, text="高さ").pack(side="left")
        h_entry = ttk.Entry(bar, textvariable=self.height_var, width=7)
        h_entry.pack(side="left", padx=2)
        h_entry.bind("<KeyRelease>", lambda _e: self._on_size_edited("height"))
        ttk.Label(bar, text="（片方入力で比率維持、空欄で端末そのまま）").pack(side="left", padx=(2, 8))

        self.disconnect_btn = ttk.Button(bar, text="切断", command=self.disconnect, state="disabled")
        self.disconnect_btn.pack(side="right")
        self.connect_btn = ttk.Button(bar, text="接続", command=self.connect)
        self.connect_btn.pack(side="right", padx=4)

    def _build_canvas(self) -> None:
        self.canvas = tk.Canvas(self.root, bg="#202020", highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        for btn in (2, 3):
            self.canvas.bind(f"<ButtonPress-{btn}>", self._on_pan_start)
            self.canvas.bind(f"<B{btn}-Motion>", self._on_pan_move)
        self.canvas.bind("<Button-4>", lambda e: self._zoom_at(ZOOM_STEP, e.x, e.y))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_at(1 / ZOOM_STEP, e.x, e.y))
        self.canvas.bind("<MouseWheel>", lambda e: self._zoom_at(ZOOM_STEP if e.delta > 0 else 1 / ZOOM_STEP, e.x, e.y))
        self.canvas.bind("<Configure>", lambda _e: self._mark_view_dirty())

        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._region_item = self.canvas.create_rectangle(0, 0, 0, 0, outline="#ff4040", width=2, state="hidden")
        self._handles = [
            self.canvas.create_rectangle(0, 0, 0, 0, fill="#ffffff", outline="#ff4040", state="hidden")
            for _ in range(4)
        ]
        self._tap_h = self.canvas.create_line(0, 0, 0, 0, fill="#40e0ff", width=2, state="hidden")
        self._tap_v = self.canvas.create_line(0, 0, 0, 0, fill="#40e0ff", width=2, state="hidden")

    def _build_zoom_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 4))
        bar.grid(row=2, column=0, sticky="we")
        ttk.Button(bar, text="🔍+", width=5, command=lambda: self._zoom_center(ZOOM_STEP)).pack(side="left")
        ttk.Button(bar, text="🔍−", width=5, command=lambda: self._zoom_center(1 / ZOOM_STEP)).pack(side="left", padx=2)
        ttk.Button(bar, text="全体", width=5, command=self._zoom_fit).pack(side="left")
        ttk.Label(bar, textvariable=self.zoom_var, width=8).pack(side="left", padx=(8, 0))
        ttk.Label(bar, textvariable=self.coord_var, width=22).pack(side="left", padx=(8, 0))
        ttk.Label(bar, text="ホイール: ズーム / 中・右ボタンドラッグ（または「表示を移動」モードで左ドラッグ）: 移動", foreground="#666").pack(side="right")

    def _build_side_panel(self) -> None:
        panel = ttk.Frame(self.root, padding=8)
        panel.grid(row=1, column=1, rowspan=2, sticky="ns")
        row = 0

        def add_label(text: str) -> None:
            nonlocal row
            ttk.Label(panel, text=text, font=("", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 2))
            row += 1

        def add_entry(text: str, var: tk.StringVar, width: int = 14) -> None:
            nonlocal row
            ttk.Label(panel, text=text).grid(row=row, column=0, sticky="w")
            ttk.Entry(panel, textvariable=var, width=width).grid(row=row, column=1, columnspan=2, sticky="we")
            row += 1

        add_label("書籍プリセット")
        self.preset_combo = ttk.Combobox(panel, textvariable=self.preset_var, values=self.presets.names(), width=16)
        self.preset_combo.grid(row=row, column=0, columnspan=3, sticky="we")
        self.preset_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_preset())
        row += 1
        btns = ttk.Frame(panel)
        btns.grid(row=row, column=0, columnspan=3, sticky="we")
        ttk.Button(btns, text="読込", width=6, command=self.load_preset).pack(side="left")
        ttk.Button(btns, text="登録/更新", width=9, command=self.save_preset).pack(side="left", padx=2)
        ttk.Button(btns, text="削除", width=6, command=self.delete_preset).pack(side="left")
        row += 1
        add_entry("書籍名", self.book_var)
        add_entry("巻数・号", self.volume_var)

        add_label("マウス操作モード")
        for text, value in (
            ("端末を操作", MODE_CONTROL),
            ("領域を選択（ドラッグ／四隅で調整）", MODE_REGION),
            ("タップ位置をクリック", MODE_TAP),
            ("表示を移動（ドラッグ）", MODE_PAN),
        ):
            ttk.Radiobutton(panel, text=text, value=value, variable=self.mode, command=self._on_mode_changed).grid(
                row=row, column=0, columnspan=3, sticky="w"
            )
            row += 1

        add_label("保存領域（x,y,w,h 映像座標）")
        entry = ttk.Entry(panel, textvariable=self.region_var, width=18)
        entry.grid(row=row, column=0, columnspan=2, sticky="we")
        entry.bind("<KeyRelease>", lambda _e: self._draw_overlays())
        ttk.Button(panel, text="クリア", command=lambda: (self.region_var.set(""), self._draw_overlays())).grid(row=row, column=2)
        row += 1

        add_label("タップ位置（x,y 映像座標）")
        entry = ttk.Entry(panel, textvariable=self.tap_var, width=18)
        entry.grid(row=row, column=0, columnspan=2, sticky="we")
        entry.bind("<KeyRelease>", lambda _e: self._draw_overlays())
        ttk.Button(panel, text="クリア", command=lambda: (self.tap_var.set(""), self._draw_overlays())).grid(row=row, column=2)
        row += 1

        add_label("自動実行")
        add_entry("回数", self.count_var)
        add_entry("タップ後待ち [s]", self.interval_var)
        add_entry("静止判定 [s]", self.settle_var)
        add_entry("タイムアウト [s]", self.timeout_var)

        add_label("保存先ベース")
        ttk.Entry(panel, textvariable=self.out_var, width=18).grid(row=row, column=0, columnspan=2, sticky="we")
        ttk.Button(panel, text="参照", command=self._choose_dir).grid(row=row, column=2)
        row += 1
        ttk.Label(panel, textvariable=self.save_path_var, wraplength=240, foreground="#555").grid(
            row=row, column=0, columnspan=3, sticky="w"
        )
        row += 1
        add_entry("プレフィックス", self.prefix_var)
        ttk.Label(panel, text="形式").grid(row=row, column=0, sticky="w")
        ttk.Combobox(panel, textvariable=self.format_var, values=("png", "jpg"), width=6, state="readonly").grid(row=row, column=1, sticky="w")
        row += 1

        row += 1
        self.shot_btn = ttk.Button(panel, text="スクリーンショット", command=self.take_screenshot)
        self.shot_btn.grid(row=row, column=0, columnspan=3, sticky="we", pady=2)
        row += 1
        self.start_btn = ttk.Button(panel, text="自動実行 開始", command=self.start_sequence)
        self.start_btn.grid(row=row, column=0, columnspan=2, sticky="we", pady=2)
        self.stop_btn = ttk.Button(panel, text="停止", command=self.stop_sequence, state="disabled")
        self.stop_btn.grid(row=row, column=2, sticky="we", pady=2)
        row += 1

        add_label("端末操作")
        nav = ttk.Frame(panel)
        nav.grid(row=row, column=0, columnspan=3, sticky="we")
        ttk.Button(nav, text="戻る", width=6, command=lambda: self._nav("back")).pack(side="left")
        ttk.Button(nav, text="ホーム", width=6, command=lambda: self._nav("home")).pack(side="left")
        ttk.Button(nav, text="タスク", width=6, command=lambda: self._nav("app_switch")).pack(side="left")
        row += 1

        panel.rowconfigure(row, weight=1)
        row += 1
        ttk.Label(panel, textvariable=self.progress_var, wraplength=240).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        ttk.Label(panel, textvariable=self.status_var, wraplength=240).grid(row=row, column=0, columnspan=3, sticky="w")

    # ============================================================ connection
    def _refresh_devices(self) -> None:
        """adb devices の一覧をコンボボックスへ反映する。"""

        def worker() -> None:
            try:
                devices = [s for s, state in adb.list_devices() if state == "device"]
                self._events.put(("devices", devices))
            except adb.AdbError as e:
                self._events.put(("status", f"adb devices 失敗: {e}"))

        threading.Thread(target=worker, daemon=True, name="adb-devices").start()

    def _fetch_physical_size(self) -> None:
        """選択中の端末の物理解像度を取得して表示する。"""
        serial = self.device_var.get().strip()
        if not serial:
            return

        def worker() -> None:
            try:
                physical, override = adb.display_size(serial)
                self._events.put(("physical", (serial, physical, override)))
            except adb.AdbError as e:
                self._events.put(("status", f"解像度取得失敗: {e}"))

        threading.Thread(target=worker, daemon=True, name="wm-size").start()

    def _on_size_edited(self, which: str) -> None:
        """幅・高さの片方が編集されたら、物理解像度の比率でもう片方を埋める。"""
        if self._updating_size or self.physical_size is None:
            return
        src_var, dst_var = (self.width_var, self.height_var) if which == "width" else (self.height_var, self.width_var)
        text = src_var.get().strip()
        self._updating_size = True
        try:
            if not text:
                dst_var.set("")
                return
            value = int(text)
            if value <= 0:
                return
            w, h = adb.scaled_size(self.physical_size, width=value if which == "width" else None,
                                   height=value if which == "height" else None)
            dst_var.set(str(h if which == "width" else w))
        except ValueError:
            pass
        finally:
            self._updating_size = False

    def _requested_size(self) -> tuple[int | None, int | None]:
        def parse(var: tk.StringVar) -> int | None:
            text = var.get().strip()
            if not text:
                return None
            value = int(text)
            if value <= 0:
                raise ValueError(f"解像度は正の整数で指定してください: {text}")
            return value

        return parse(self.width_var), parse(self.height_var)

    def connect(self) -> None:
        if self.session is not None or self._connecting:
            return
        address = self.device_var.get().strip()
        if not address:
            messagebox.showerror("接続", "端末を選択するか IP:port を入力してください")
            return
        try:
            width, height = self._requested_size()
        except ValueError as e:
            messagebox.showerror("接続", str(e))
            return

        self._connecting = True
        self.connect_btn.configure(state="disabled")
        self.status_var.set(f"接続中: {address}")

        def worker() -> None:
            try:
                online = [s for s, state in adb.list_devices() if state == "device"]
                if address not in online and ":" in address:
                    adb.connect(address)
                physical, _ = adb.display_size(address)
                size = adb.scaled_size(physical, width, height) if (width or height) else None
                session = cli.make_session(self.args, serial=address, display_size=size)
                session.on_stream_error = lambda e: self._events.put(("stream_error", e))
                session.start()
                self._events.put(("connected", session))
            except Exception as e:  # noqa: BLE001 - GUI へ通知する
                self._events.put(("connect_error", e))

        threading.Thread(target=worker, daemon=True, name="connect").start()

    def disconnect(self) -> None:
        if self.session is None:
            return
        if self.runner is not None:
            self.runner.stop()
        session = self.session
        self.session = None
        self.disconnect_btn.configure(state="disabled")
        self.status_var.set("切断中...")
        self.canvas.itemconfigure(self._image_item, image="")
        self._photo = None
        self.frame_size = None
        self._rgb_cache = None

        def worker() -> None:
            try:
                session.close()
            finally:
                self._events.put(("disconnected", None))

        threading.Thread(target=worker, daemon=True, name="disconnect").start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "devices":
            self.device_combo["values"] = payload
            if not self.device_var.get() and payload:
                self.device_var.set(payload[0])
            if self.device_var.get():
                self._fetch_physical_size()
        elif kind == "physical":
            serial, physical, override = payload
            if serial == self.device_var.get().strip():
                self.physical_size = physical
                text = f"元解像度: {physical[0]}x{physical[1]}"
                if override:
                    text += f"（現在 {override[0]}x{override[1]} に上書き中）"
                self.physical_var.set(text)
                if self.width_var.get().strip():
                    self._on_size_edited("width")
                elif self.height_var.get().strip():
                    self._on_size_edited("height")
        elif kind == "status":
            self.status_var.set(str(payload))
        elif kind == "connected":
            self._connecting = False
            self.session = payload
            self.disconnect_btn.configure(state="normal")
            self.fit = True
            self._mark_view_dirty()
            size = payload.display_size or payload.physical_size
            self.status_var.set(f"接続: {payload.server.device_name} ({payload.serial}) 要求 {size[0]}x{size[1]}")
        elif kind == "connect_error":
            self._connecting = False
            self.connect_btn.configure(state="normal")
            self.status_var.set(f"接続失敗: {payload}")
            messagebox.showerror("接続失敗", str(payload))
        elif kind == "disconnected":
            self.connect_btn.configure(state="normal")
            self.status_var.set("未接続")
            self._fetch_physical_size()
        elif kind == "stream_error":
            self.status_var.set(f"映像エラー: {payload}")
        elif kind == "progress":
            done, total, path = payload
            self.progress_var.set(f"{done}/{total} 保存: {Path(path).name}")
        elif kind == "sequence_done":
            self._sequence_finished(payload)

    # ================================================================= view
    def _mark_view_dirty(self) -> None:
        self._view_dirty = True

    def _fit_scale(self, cw: int, ch: int, fw: int, fh: int) -> float:
        return min(cw / fw, ch / fh)

    def _clamp_view(self, cw: int, ch: int, fw: int, fh: int) -> None:
        """ズーム時にフレームが画面外へ逃げないよう原点を制限する。"""
        sw, sh = fw * self.scale, fh * self.scale
        self.ox = (cw - sw) / 2 if sw <= cw else min(0.0, max(cw - sw, self.ox))
        self.oy = (ch - sh) / 2 if sh <= ch else min(0.0, max(ch - sh, self.oy))

    def _update_layout(self) -> None:
        if self.frame_size is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        fw, fh = self.frame_size
        if self.fit:
            self.scale = self._fit_scale(cw, ch, fw, fh)
        self._clamp_view(cw, ch, fw, fh)
        self.zoom_var.set("全体" if self.fit else f"{self.scale * 100:.0f}%")

    def _zoom_at(self, factor: float, cx: float, cy: float) -> None:
        """キャンバス座標 (cx, cy) を固定点としてズームする。"""
        if self.frame_size is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        fw, fh = self.frame_size
        fit_scale = self._fit_scale(cw, ch, fw, fh)
        new_scale = min(ZOOM_MAX, self.scale * factor)
        if new_scale <= fit_scale:
            self._zoom_fit()
            return
        sx = (cx - self.ox) / self.scale
        sy = (cy - self.oy) / self.scale
        self.fit = False
        self.scale = new_scale
        self.ox = cx - sx * new_scale
        self.oy = cy - sy * new_scale
        self._mark_view_dirty()
        self._render_now()

    def _zoom_center(self, factor: float) -> None:
        self._zoom_at(factor, self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)

    def _zoom_fit(self) -> None:
        self.fit = True
        self._mark_view_dirty()
        self._render_now()

    def _on_pan_start(self, event: tk.Event) -> None:
        self._pan_start = (event.x, event.y, self.ox, self.oy)

    def _on_pan_move(self, event: tk.Event) -> None:
        if self._pan_start is None or self.fit:
            return
        x0, y0, ox0, oy0 = self._pan_start
        self.ox = ox0 + (event.x - x0)
        self.oy = oy0 + (event.y - y0)
        self._mark_view_dirty()
        self._render_now()

    # --------------------------------------------------------- coordinates
    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return self.ox + x * self.scale, self.oy + y * self.scale

    def _to_source_float(self, cx: float, cy: float) -> tuple[float, float] | None:
        if self.frame_size is None:
            return None
        fw, fh = self.frame_size
        sx = (cx - self.ox) / self.scale
        sy = (cy - self.oy) / self.scale
        if not (0 <= sx < fw and 0 <= sy < fh):
            return None
        return sx, sy

    def _to_source(self, cx: float, cy: float) -> tuple[int, int] | None:
        pos = self._to_source_float(cx, cy)
        if pos is None:
            return None
        return int(math.floor(pos[0])), int(math.floor(pos[1]))

    # =============================================================== render
    def _render_loop(self) -> None:
        self._render_now()
        self.root.after(RENDER_INTERVAL_MS, self._render_loop)

    def _render_now(self) -> None:
        try:
            self._render()
        except Exception as e:  # noqa: BLE001 - 描画エラーでループを止めない
            log.debug("render error: %s", e)

    def _render(self) -> None:
        if self.session is None:
            return
        frame, seq = self.session.store.get()
        if frame is None:
            return
        if self.frame_size != (frame.width, frame.height):
            self.frame_size = (frame.width, frame.height)
            self.fit = True
            self._view_dirty = True
        if seq == self._rendered_seq and not self._view_dirty:
            return

        self._update_layout()
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        fw, fh = self.frame_size
        scale = self.scale

        # 可視部分の映像座標範囲
        sx0 = max(0, int(math.floor(-self.ox / scale)))
        sy0 = max(0, int(math.floor(-self.oy / scale)))
        sx1 = min(fw, int(math.ceil((cw - self.ox) / scale)))
        sy1 = min(fh, int(math.ceil((ch - self.oy) / scale)))
        if sx1 <= sx0 or sy1 <= sy0:
            self.canvas.itemconfigure(self._image_item, image="")
            return

        if scale <= 1.0:
            # 縮小: swscale で全体を縮小してから可視部分を切り出す
            full = frame.reformat(width=max(1, round(fw * scale)), height=max(1, round(fh * scale)), format="rgb24").to_ndarray()
            cx0, cy0 = round(sx0 * scale), round(sy0 * scale)
            cx1, cy1 = round(sx1 * scale), round(sy1 * scale)
            rgb = np.ascontiguousarray(full[cy0:cy1, cx0:cx1])
            pos = (self.ox + cx0, self.oy + cy0)
        else:
            # 拡大: 可視部分だけ切り出して最近傍で拡大（ピクセル境界が見える）
            if self._rgb_cache is None or self._rgb_cache[0] != seq:
                self._rgb_cache = (seq, frame.to_ndarray(format="rgb24"))
            crop = self._rgb_cache[1][sy0:sy1, sx0:sx1]
            dw = max(1, round((sx1 - sx0) * scale))
            dh = max(1, round((sy1 - sy0) * scale))
            rgb = np.asarray(Image.fromarray(crop).resize((dw, dh), Image.NEAREST))
            pos = (self.ox + sx0 * scale, self.oy + sy0 * scale)

        h, w = rgb.shape[:2]
        ppm = b"P6 %d %d 255\n" % (w, h) + rgb.tobytes()
        self._photo = tk.PhotoImage(data=ppm, format="PPM")
        self.canvas.itemconfigure(self._image_item, image=self._photo)
        self.canvas.coords(self._image_item, pos[0], pos[1])
        self._rendered_seq = seq
        self._view_dirty = False

        fps = self.session.store.fps
        self.status_var.set(f"接続: {self.session.server.device_name}  受信 {fw}x{fh}  {fps:.0f} fps")
        self._draw_overlays()

    def _draw_overlays(self) -> None:
        region = self._parse_region()
        if region is not None and self.frame_size is not None:
            x0, y0 = self._to_canvas(region.x, region.y)
            x1, y1 = self._to_canvas(region.x + region.w, region.y + region.h)
            self.canvas.coords(self._region_item, x0, y0, x1, y1)
            self.canvas.itemconfigure(self._region_item, state="normal")
            for item, (hx, hy) in zip(self._handles, ((x0, y0), (x1, y0), (x0, y1), (x1, y1))):
                self.canvas.coords(item, hx - HANDLE_HALF, hy - HANDLE_HALF, hx + HANDLE_HALF, hy + HANDLE_HALF)
                self.canvas.itemconfigure(item, state="normal")
        else:
            self.canvas.itemconfigure(self._region_item, state="hidden")
            for item in self._handles:
                self.canvas.itemconfigure(item, state="hidden")

        tap = self._parse_tap()
        if tap is not None and self.frame_size is not None:
            cx, cy = self._to_canvas(tap[0] + 0.5, tap[1] + 0.5)
            self.canvas.coords(self._tap_h, cx - 12, cy, cx + 12, cy)
            self.canvas.coords(self._tap_v, cx, cy - 12, cx, cy + 12)
            self.canvas.itemconfigure(self._tap_h, state="normal")
            self.canvas.itemconfigure(self._tap_v, state="normal")
        else:
            self.canvas.itemconfigure(self._tap_h, state="hidden")
            self.canvas.itemconfigure(self._tap_v, state="hidden")
        for item in (self._region_item, *self._handles, self._tap_h, self._tap_v):
            self.canvas.tag_raise(item)

    def _parse_region(self) -> Region | None:
        text = self.region_var.get().strip()
        if not text:
            return None
        try:
            return Region.parse(text)
        except CaptureError:
            return None

    def _parse_tap(self) -> tuple[int, int] | None:
        text = self.tap_var.get().strip()
        if not text:
            return None
        try:
            return cli.parse_point(text)
        except argparse.ArgumentTypeError:
            return None

    # ================================================================ mouse
    def _on_motion(self, event: tk.Event) -> None:
        pos = self._to_source(event.x, event.y)
        self.coord_var.set(f"x={pos[0]}, y={pos[1]}" if pos else "")

    def _on_mode_changed(self) -> None:
        self.canvas.configure(cursor=MODE_CURSORS.get(self.mode.get(), "crosshair"))

    def _on_press(self, event: tk.Event) -> None:
        mode = self.mode.get()
        if mode == MODE_PAN:
            self._drag = {"kind": "pan"}
            self._on_pan_start(event)
            return
        if mode == MODE_REGION:
            self._region_press(event.x, event.y)
            return
        pos = self._to_source(event.x, event.y)
        if pos is None:
            return
        if mode == MODE_TAP:
            self.tap_var.set(f"{pos[0]},{pos[1]}")
            self._draw_overlays()
        elif mode == MODE_CONTROL and self.session and self.session.controller:
            self._drag = {"kind": "touch"}
            self._safe_control(lambda: self.session.controller.touch(ACTION_DOWN, *pos))

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        if self._drag["kind"] == "pan":
            self._on_pan_move(event)
        elif self._drag["kind"] == "touch":
            pos = self._to_source(event.x, event.y)
            if pos and self.session and self.session.controller:
                self._safe_control(lambda: self.session.controller.touch(ACTION_MOVE, *pos))
        else:
            self._region_drag(event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        drag, self._drag = self._drag, None
        if drag["kind"] == "pan":
            self._pan_start = None
        elif drag["kind"] == "touch":
            pos = self._to_source(event.x, event.y) or drag.get("last")
            if self.session and self.session.controller:
                if pos is None:
                    pos = (0, 0)
                self._safe_control(lambda: self.session.controller.touch(ACTION_UP, *pos, pressure=0.0))
        else:
            self._drag = drag
            self._region_drag(event.x, event.y)
            self._drag = None

    # ------------------------------------------------------------ region
    def _region_press(self, cx: float, cy: float) -> None:
        if self.frame_size is None:
            return
        region = self._parse_region()
        if region is not None:
            x0, y0 = self._to_canvas(region.x, region.y)
            x1, y1 = self._to_canvas(region.x + region.w, region.y + region.h)
            corners = (
                ((x0, y0), (region.x + region.w, region.y + region.h)),
                ((x1, y0), (region.x, region.y + region.h)),
                ((x0, y1), (region.x + region.w, region.y)),
                ((x1, y1), (region.x, region.y)),
            )
            for (hx, hy), anchor in corners:
                if abs(cx - hx) <= HANDLE_HIT and abs(cy - hy) <= HANDLE_HIT:
                    self._drag = {"kind": "resize", "anchor": anchor}
                    return
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                pos = self._to_source_float(cx, cy) or ((cx - self.ox) / self.scale, (cy - self.oy) / self.scale)
                self._drag = {"kind": "move", "start": pos, "orig": region}
                return
        pos = self._to_source_float(cx, cy)
        if pos is None:
            return
        self._drag = {"kind": "new", "start": (math.floor(pos[0]), math.floor(pos[1]))}

    def _region_drag(self, cx: float, cy: float) -> None:
        assert self._drag is not None and self.frame_size is not None
        fw, fh = self.frame_size
        # 枠外へドラッグしてもフレーム範囲内に収める
        sx = min(max((cx - self.ox) / self.scale, 0.0), float(fw))
        sy = min(max((cy - self.oy) / self.scale, 0.0), float(fh))
        kind = self._drag["kind"]
        if kind == "new":
            x0, y0 = self._drag["start"]
            x1 = min(fw, math.floor(sx) + 1)
            y1 = min(fh, math.floor(sy) + 1)
            region = self._region_from_edges(x0, y0, x1, y1)
        elif kind == "resize":
            ax, ay = self._drag["anchor"]
            region = self._region_from_edges(ax, ay, round(sx), round(sy))
        else:
            start = self._drag["start"]
            orig: Region = self._drag["orig"]
            dx = round(sx - start[0])
            dy = round(sy - start[1])
            nx = min(max(orig.x + dx, 0), fw - orig.w)
            ny = min(max(orig.y + dy, 0), fh - orig.h)
            region = Region(nx, ny, orig.w, orig.h)
        self.region_var.set(str(region))
        self._draw_overlays()

    @staticmethod
    def _region_from_edges(x0: int, y0: int, x1: int, y1: int) -> Region:
        x, y = min(x0, x1), min(y0, y1)
        return Region(x, y, max(1, abs(x1 - x0)), max(1, abs(y1 - y0)))

    def _safe_control(self, func) -> None:
        try:
            func()
        except Exception as e:  # noqa: BLE001 - ステータスへ表示する
            self.status_var.set(f"操作エラー: {e}")

    def _nav(self, name: str) -> None:
        if self.session and self.session.controller:
            self._safe_control(getattr(self.session.controller, name))

    def _choose_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
        if chosen:
            self.out_var.set(chosen)

    # ============================================================== presets
    def _output_dir(self) -> Path:
        return book_dir(Path(self.out_var.get() or "output"), self.book_var.get(), self.volume_var.get())

    def _update_save_path(self) -> None:
        self.save_path_var.set(f"→ {self._output_dir()}/{self.prefix_var.get()}00001.{self.format_var.get()}")

    def load_preset(self) -> None:
        name = self.preset_var.get().strip()
        preset = self.presets.get(name)
        if preset is None:
            messagebox.showerror("プリセット", f"プリセットがありません: {name!r}")
            return
        self.book_var.set(preset.name)
        self.region_var.set(preset.region)
        self.tap_var.set(preset.tap)
        if preset.interval is not None:
            self.interval_var.set(f"{preset.interval:g}")
        if preset.settle is not None:
            self.settle_var.set(f"{preset.settle:g}")
        self._draw_overlays()
        self.progress_var.set(f"プリセット読込: {preset.name}")

    def save_preset(self) -> None:
        name = self.book_var.get().strip() or self.preset_var.get().strip()
        if not name:
            messagebox.showerror("プリセット", "書籍名を入力してください")
            return
        try:
            preset = Preset(
                name=name,
                region=self.region_var.get().strip(),
                tap=self.tap_var.get().strip(),
                interval=float(self.interval_var.get()),
                settle=float(self.settle_var.get()),
            )
            if preset.region:
                Region.parse(preset.region)
            if preset.tap:
                cli.parse_point(preset.tap)
            self.presets.put(preset)
        except (ValueError, CaptureError, argparse.ArgumentTypeError, OSError) as e:
            messagebox.showerror("プリセット", f"登録できません: {e}")
            return
        self.preset_combo["values"] = self.presets.names()
        self.preset_var.set(name)
        self.book_var.set(name)
        self.progress_var.set(f"プリセット登録: {name}")

    def delete_preset(self) -> None:
        name = self.preset_var.get().strip()
        if not name or self.presets.get(name) is None:
            return
        if not messagebox.askyesno("プリセット", f"『{name}』を削除しますか？"):
            return
        self.presets.delete(name)
        self.preset_combo["values"] = self.presets.names()
        self.preset_var.set("")
        self.progress_var.set(f"プリセット削除: {name}")

    # ============================================================== actions
    def _sequence_config(self, count: int) -> SequenceConfig:
        out_dir = self._output_dir()
        prefix = self.prefix_var.get()
        ext = self.format_var.get()
        region_text = self.region_var.get().strip()
        region = Region.parse(region_text) if region_text else None
        tap_text = self.tap_var.get().strip()
        tap = cli.parse_point(tap_text) if tap_text else None
        return SequenceConfig(
            count=count,
            out_dir=out_dir,
            prefix=prefix,
            ext=ext,
            start_index=cli.next_index(out_dir, prefix, ext),
            region=region,
            tap=tap,
            interval=float(self.interval_var.get()),
            settle=float(self.settle_var.get()),
            timeout=float(self.timeout_var.get()),
            digits=self.args.digits,
        )

    def take_screenshot(self) -> None:
        if self.session is None:
            self.progress_var.set("未接続です")
            return
        frame, _ = self.session.store.get()
        if frame is None:
            self.progress_var.set("フレーム未受信")
            return
        try:
            cfg = self._sequence_config(1)
            path = save_frame(frame, cfg.path_for(cfg.start_index), cfg.region)
        except (CaptureError, ValueError, argparse.ArgumentTypeError, OSError) as e:
            messagebox.showerror("保存失敗", str(e))
            return
        self.progress_var.set(f"保存: {path.name} ({frame.width}x{frame.height}{' 切り出し' if cfg.region else ''})")

    def start_sequence(self) -> None:
        if self.session is None:
            self.progress_var.set("未接続です")
            return
        if self.runner is not None:
            return
        try:
            cfg = self._sequence_config(int(self.count_var.get()))
        except (CaptureError, ValueError, argparse.ArgumentTypeError) as e:
            messagebox.showerror("設定エラー", str(e))
            return
        if cfg.count > 1 and cfg.tap is None:
            if not messagebox.askyesno("確認", "タップ位置が未設定です。タップせずに連続保存しますか？"):
                return

        session = self.session
        runner = SequenceRunner(
            session.store,
            cfg,
            session.tap,
            on_progress=lambda d, t, p: self._events.put(("progress", (d, t, p))),
        )
        self.runner = runner
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress_var.set(f"自動実行開始: {cfg.count} 回")

        def worker() -> None:
            try:
                runner.run()
                self._events.put(("sequence_done", None))
            except Exception as e:  # noqa: BLE001 - GUI へ通知する
                self._events.put(("sequence_done", e))

        threading.Thread(target=worker, daemon=True, name="sequence").start()

    def stop_sequence(self) -> None:
        if self.runner is not None:
            self.runner.stop()

    def _sequence_finished(self, error: Exception | None) -> None:
        runner = self.runner
        self.runner = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        n = len(runner.saved) if runner else 0
        if error is not None:
            self.progress_var.set(f"自動実行エラー（{n} 枚保存）: {error}")
            messagebox.showerror("自動実行エラー", str(error))
        elif runner is not None and runner.stop_event.is_set():
            self.progress_var.set(f"停止しました（{n} 枚保存）")
        else:
            self.progress_var.set(f"完了: {n} 枚保存")

    def on_close(self) -> None:
        if self.runner is not None:
            self.runner.stop()
        if self.session is not None:
            session, self.session = self.session, None
            session.close()
        self.root.destroy()


def run_gui(args: argparse.Namespace) -> int:
    """GUI を起動する。SIGINT/SIGTERM でもサーバー停止と解像度復元を行う。"""
    root = tk.Tk()
    app = App(root, args)

    def _on_signal(*_: object) -> None:
        root.after(0, app.on_close)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # シグナルハンドラは Python のバイトコード実行中にしか走らないため、定期的に起こす
    def _tick() -> None:
        root.after(200, _tick)

    _tick()
    root.mainloop()
    return 0
