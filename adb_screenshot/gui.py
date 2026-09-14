"""Tkinter によるミラーリング表示と操作 UI。

画面には縮小表示するが、保存は常に受信解像度のフレームから行う。
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import cli
from .capture import CaptureError, Region, SequenceConfig, SequenceRunner, save_frame
from .session import Session

log = logging.getLogger(__name__)

MODE_CONTROL = "control"
MODE_REGION = "region"
MODE_TAP = "tap"

RENDER_INTERVAL_MS = 33


class App:
    """メインウィンドウ。"""

    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.session: Session | None = None
        self.runner: SequenceRunner | None = None
        self._events: queue.Queue = queue.Queue()

        self._photo: tk.PhotoImage | None = None
        self._rendered_seq = -1
        self._layout = (1.0, 0.0, 0.0, 0, 0)  # scale, ox, oy, disp_w, disp_h
        self._drag_start: tuple[int, int] | None = None

        self.mode = tk.StringVar(value=MODE_CONTROL)
        self.region_var = tk.StringVar(value=str(args.region) if args.region else "")
        self.tap_var = tk.StringVar(value=f"{args.tap[0]},{args.tap[1]}" if args.tap else "")
        self.count_var = tk.StringVar(value=str(args.count))
        self.interval_var = tk.StringVar(value=f"{args.interval:g}")
        self.settle_var = tk.StringVar(value=f"{args.settle:g}")
        self.timeout_var = tk.StringVar(value=f"{args.timeout:g}")
        self.out_var = tk.StringVar(value=args.out)
        self.prefix_var = tk.StringVar(value=args.prefix)
        self.format_var = tk.StringVar(value=args.format)
        self.status_var = tk.StringVar(value="接続中...")
        self.progress_var = tk.StringVar(value="")

        root.title("adb_screenshot")
        root.geometry("1100x800")
        root.minsize(600, 400)
        self._build_widgets()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._connect)
        root.after(RENDER_INTERVAL_MS, self._render_loop)

    # ---------------------------------------------------------------- widgets
    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self.root, bg="#202020", highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Configure>", lambda _e: setattr(self, "_rendered_seq", -1))
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._region_item = self.canvas.create_rectangle(0, 0, 0, 0, outline="#ff4040", width=2, state="hidden")
        self._tap_h = self.canvas.create_line(0, 0, 0, 0, fill="#40e0ff", width=2, state="hidden")
        self._tap_v = self.canvas.create_line(0, 0, 0, 0, fill="#40e0ff", width=2, state="hidden")

        panel = ttk.Frame(self.root, padding=8)
        panel.grid(row=0, column=1, sticky="ns")

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

        add_label("マウス操作モード")
        for text, value in (("端末を操作", MODE_CONTROL), ("領域をドラッグ選択", MODE_REGION), ("タップ位置をクリック", MODE_TAP)):
            ttk.Radiobutton(panel, text=text, value=value, variable=self.mode).grid(row=row, column=0, columnspan=3, sticky="w")
            row += 1

        add_label("保存領域（x,y,w,h 映像座標）")
        ttk.Entry(panel, textvariable=self.region_var, width=18).grid(row=row, column=0, columnspan=2, sticky="we")
        ttk.Button(panel, text="クリア", command=lambda: self.region_var.set("")).grid(row=row, column=2)
        row += 1

        add_label("タップ位置（x,y 映像座標）")
        ttk.Entry(panel, textvariable=self.tap_var, width=18).grid(row=row, column=0, columnspan=2, sticky="we")
        ttk.Button(panel, text="クリア", command=lambda: self.tap_var.set("")).grid(row=row, column=2)
        row += 1

        add_label("自動実行")
        add_entry("回数", self.count_var)
        add_entry("タップ後待ち [s]", self.interval_var)
        add_entry("静止判定 [s]", self.settle_var)
        add_entry("タイムアウト [s]", self.timeout_var)

        add_label("保存先")
        ttk.Entry(panel, textvariable=self.out_var, width=18).grid(row=row, column=0, columnspan=2, sticky="we")
        ttk.Button(panel, text="参照", command=self._choose_dir).grid(row=row, column=2)
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
        ttk.Label(panel, textvariable=self.progress_var, wraplength=220).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        ttk.Label(panel, textvariable=self.status_var, wraplength=220).grid(row=row, column=0, columnspan=3, sticky="w")

    # ---------------------------------------------------------------- connect
    def _connect(self) -> None:
        def worker() -> None:
            try:
                session = cli.make_session(self.args)
                session.on_stream_error = lambda e: self._events.put(("stream_error", e))
                session.start()
                self._events.put(("connected", session))
            except Exception as e:  # noqa: BLE001 - GUI へ通知する
                self._events.put(("connect_error", e))

        threading.Thread(target=worker, daemon=True, name="connect").start()
        self.root.after(100, self._poll_events)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "connected":
                    self.session = payload
                    self.status_var.set(f"接続: {payload.server.device_name} ({payload.serial})")
                elif kind == "connect_error":
                    self.status_var.set(f"接続失敗: {payload}")
                    messagebox.showerror("接続失敗", str(payload))
                elif kind == "stream_error":
                    self.status_var.set(f"映像エラー: {payload}")
                elif kind == "progress":
                    done, total, path = payload
                    self.progress_var.set(f"{done}/{total} 保存: {Path(path).name}")
                elif kind == "sequence_done":
                    self._sequence_finished(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    # ----------------------------------------------------------------- render
    def _render_loop(self) -> None:
        try:
            self._render()
        except Exception as e:  # noqa: BLE001 - 描画エラーでループを止めない
            log.debug("render error: %s", e)
        self.root.after(RENDER_INTERVAL_MS, self._render_loop)

    def _render(self) -> None:
        if self.session is None:
            return
        frame, seq = self.session.store.get()
        if frame is None or seq == self._rendered_seq:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        scale = min(cw / frame.width, ch / frame.height)
        dw = max(1, int(frame.width * scale))
        dh = max(1, int(frame.height * scale))
        ox = (cw - dw) // 2
        oy = (ch - dh) // 2
        self._layout = (scale, ox, oy, dw, dh)

        small = frame.reformat(width=dw, height=dh, format="rgb24")
        rgb = small.to_ndarray()
        ppm = b"P6 %d %d 255\n" % (dw, dh) + rgb.tobytes()
        self._photo = tk.PhotoImage(data=ppm, format="PPM")
        self.canvas.itemconfigure(self._image_item, image=self._photo)
        self.canvas.coords(self._image_item, ox, oy)
        self._rendered_seq = seq

        fps = self.session.store.fps
        self.status_var.set(
            f"接続: {self.session.server.device_name}  受信 {frame.width}x{frame.height}  {fps:.0f} fps  表示 {dw}x{dh}"
        )
        self._draw_overlays()

    def _draw_overlays(self) -> None:
        region = self._parse_region()
        if region is not None:
            x0, y0 = self._to_canvas(region.x, region.y)
            x1, y1 = self._to_canvas(region.x + region.w, region.y + region.h)
            self.canvas.coords(self._region_item, x0, y0, x1, y1)
            self.canvas.itemconfigure(self._region_item, state="normal")
        else:
            self.canvas.itemconfigure(self._region_item, state="hidden")

        tap = self._parse_tap()
        if tap is not None:
            cx, cy = self._to_canvas(*tap)
            self.canvas.coords(self._tap_h, cx - 12, cy, cx + 12, cy)
            self.canvas.coords(self._tap_v, cx, cy - 12, cx, cy + 12)
            self.canvas.itemconfigure(self._tap_h, state="normal")
            self.canvas.itemconfigure(self._tap_v, state="normal")
        else:
            self.canvas.itemconfigure(self._tap_h, state="hidden")
            self.canvas.itemconfigure(self._tap_v, state="hidden")
        self.canvas.tag_raise(self._region_item)
        self.canvas.tag_raise(self._tap_h)
        self.canvas.tag_raise(self._tap_v)

    # ----------------------------------------------------------- coordinates
    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        scale, ox, oy, _, _ = self._layout
        return ox + x * scale, oy + y * scale

    def _to_source(self, cx: float, cy: float) -> tuple[int, int] | None:
        scale, ox, oy, dw, dh = self._layout
        if dw == 0 or not (ox <= cx < ox + dw and oy <= cy < oy + dh):
            return None
        return int((cx - ox) / scale), int((cy - oy) / scale)

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

    # -------------------------------------------------------------- mouse
    def _on_press(self, event: tk.Event) -> None:
        pos = self._to_source(event.x, event.y)
        if pos is None:
            return
        self._drag_start = pos
        mode = self.mode.get()
        if mode == MODE_TAP:
            self.tap_var.set(f"{pos[0]},{pos[1]}")
            self._draw_overlays()
        elif mode == MODE_REGION:
            self.region_var.set(f"{pos[0]},{pos[1]},1,1")
            self._draw_overlays()
        elif mode == MODE_CONTROL and self.session and self.session.controller:
            from .control import ACTION_DOWN

            self._safe_control(lambda: self.session.controller.touch(ACTION_DOWN, *pos))

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        pos = self._to_source(event.x, event.y)
        if pos is None:
            return
        mode = self.mode.get()
        if mode == MODE_REGION:
            self._set_region_from_drag(pos)
        elif mode == MODE_CONTROL and self.session and self.session.controller:
            from .control import ACTION_MOVE

            self._safe_control(lambda: self.session.controller.touch(ACTION_MOVE, *pos))

    def _on_release(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        start = self._drag_start
        self._drag_start = None
        pos = self._to_source(event.x, event.y) or start
        mode = self.mode.get()
        if mode == MODE_REGION:
            self._set_region_from_drag(pos)
        elif mode == MODE_CONTROL and self.session and self.session.controller:
            from .control import ACTION_UP

            self._safe_control(lambda: self.session.controller.touch(ACTION_UP, *pos, pressure=0.0))

    def _set_region_from_drag(self, pos: tuple[int, int]) -> None:
        assert self._drag_start is not None
        x0, y0 = self._drag_start
        x1, y1 = pos
        x, y = min(x0, x1), min(y0, y1)
        w, h = max(1, abs(x1 - x0)), max(1, abs(y1 - y0))
        self.region_var.set(f"{x},{y},{w},{h}")
        self._draw_overlays()

    def _safe_control(self, func) -> None:
        try:
            func()
        except Exception as e:  # noqa: BLE001 - ステータスへ表示する
            self.status_var.set(f"操作エラー: {e}")

    def _nav(self, name: str) -> None:
        if self.session and self.session.controller:
            self._safe_control(getattr(self.session.controller, name))

    # ------------------------------------------------------------ actions
    def _choose_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.out_var.get() or ".")
        if chosen:
            self.out_var.set(chosen)

    def _sequence_config(self, count: int) -> SequenceConfig:
        out_dir = Path(self.out_var.get() or "output")
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
        )

    def take_screenshot(self) -> None:
        if self.session is None:
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
        if self.session is None or self.runner is not None:
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
            self.session.close()
        self.root.destroy()


def run_gui(args: argparse.Namespace) -> int:
    """GUI を起動する。SIGINT/SIGTERM でもサーバー停止と forward 解除を行う。"""
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
