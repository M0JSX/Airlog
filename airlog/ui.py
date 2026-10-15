"""Airlog main window."""
from __future__ import annotations

import datetime as dt
import math
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__, osutil, sources
from .config import Config
from .engine import Engine, fmt_clock
from .settings import SettingsDialog

# ---------------------------------------------------------------- palette
BG = "#16181d"
PANEL = "#1e2128"
PANEL2 = "#262a33"
FG = "#e6e8ec"
DIM = "#8b93a1"
AMBER = "#f5a623"
AMBER_DIM = "#8a5c12"
RED = "#e5484d"
GREEN = "#46a758"
BLUE = "#3e7bfa"
SEL = "#24406f"
GRID = "#2c313b"
HOUR = "#56607a"

SPANS = [("1 min", 60), ("2 min", 120), ("5 min", 300), ("15 min", 900),
         ("30 min", 1800), ("1 hour", 3600), ("2 hours", 7200)]
TICKS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
DB_RANGE = 48.0


def peak_to_y(v: float) -> float:
    """0..1 linear peak -> 0..1 display height on a dB scale."""
    if v <= 0:
        return 0.0
    db = 20 * math.log10(v)
    return max(0.0, min(1.0, (db + DB_RANGE) / DB_RANGE))


def fmt_dur(s: float) -> str:
    s = max(0.0, s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{sec:04.1f}" if h else f"{int(m)}:{sec:04.1f}"


def fmt_hms(t: float) -> str:
    d = dt.datetime.fromtimestamp(t)
    return d.strftime("%H:%M:%S") + f".{d.microsecond // 100000}"


class App(tk.Tk):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.engine: Engine | None = None
        self.ui_q: queue.Queue = queue.Queue()

        self.span = cfg.view_minutes * 60 if cfg.view_minutes * 60 in [s for _, s in SPANS] else 900
        self.follow = tk.BooleanVar(value=True)
        self.view_end: float | None = None      # right edge when not following
        self.sel: list[float | None] = [None, None]
        self.drag_mode: str | None = None
        self.drag_anchor = 0.0
        self.frozen_end: float | None = None
        self.preview_proc: subprocess.Popen | None = None
        self.grabs: list[tuple[float, Path, float]] = []
        self.meter_hold = [0.0, 0.0]
        self.meter_peak = [0.0, 0.0, 0.0, 0.0]  # value, timestamp per channel

        self.title(f"Airlog — {cfg.station_name}")
        self.geometry("1180x760")
        self.minsize(900, 600)
        self.configure(bg=BG)
        try:
            self._icon = tk.PhotoImage(file=str(Path(__file__).with_name("assets") / "icon.png"))
            self.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        self._style()
        self._build()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._make_engine()
        if cfg.autostart and self._source_configured():
            self.start_recording()
        elif not self._source_configured():
            self.after(400, self.open_settings)

        self.after(50, self._tick_fast)
        self.after(250, self._tick_slow)

    # ================================================================ styling
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        base = ("Segoe UI", 10) if sys.platform.startswith("win") else ("Helvetica", 11) \
            if sys.platform == "darwin" else ("DejaVu Sans", 10)
        self.font = base
        self.mono = ("Consolas", 11) if sys.platform.startswith("win") else ("Menlo", 12) \
            if sys.platform == "darwin" else ("DejaVu Sans Mono", 10)
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2, font=base,
                    bordercolor=GRID, lightcolor=PANEL, darkcolor=PANEL, troughcolor=PANEL,
                    selectbackground=SEL, selectforeground=FG, insertcolor=FG)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.map("TLabel", background=[("disabled", BG)], foreground=[("disabled", "#5d6470")])
        s.map("TEntry", fieldbackground=[("disabled", PANEL)], foreground=[("disabled", "#5d6470")])
        s.configure("Panel.TLabel", background=PANEL)
        s.configure("Dim.TLabel", foreground=DIM)
        s.configure("PanelDim.TLabel", background=PANEL, foreground=DIM)
        s.configure("Big.TLabel", background=PANEL, font=(base[0], base[1] + 6, "bold"))
        s.configure("Clock.TLabel", background=PANEL, font=(self.mono[0], self.mono[1] + 8, "bold"))
        s.configure("Mono.TLabel", background=PANEL, font=self.mono)
        s.configure("TButton", background=PANEL2, foreground=FG, padding=(10, 5), borderwidth=1)
        s.map("TButton", background=[("active", "#323844"), ("disabled", PANEL)],
              foreground=[("disabled", DIM)])
        s.configure("Accent.TButton", background=AMBER, foreground="#1a1a1a", font=(base[0], base[1], "bold"))
        s.map("Accent.TButton", background=[("active", "#ffb945"), ("disabled", AMBER_DIM)])
        s.configure("Rec.TButton", background=RED, foreground="white", font=(base[0], base[1], "bold"))
        s.map("Rec.TButton", background=[("active", "#f0656a")])
        s.configure("Quick.TButton", padding=(8, 4))
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.configure("Panel.TCheckbutton", background=PANEL, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.map("Panel.TCheckbutton", background=[("active", PANEL)])
        s.configure("TRadiobutton", background=BG, foreground=FG)
        s.map("TRadiobutton", background=[("active", BG)])
        s.configure("TEntry", fieldbackground=PANEL2, foreground=FG, padding=4)
        s.configure("Time.TEntry", fieldbackground=PANEL2, foreground=FG, padding=4)
        s.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=FG, arrowcolor=FG)
        s.map("TCombobox", fieldbackground=[("disabled", PANEL), ("readonly", PANEL2)],
              foreground=[("disabled", "#5d6470"), ("readonly", FG)])
        s.configure("TSpinbox", fieldbackground=PANEL2, foreground=FG, arrowcolor=FG)
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL, foreground=DIM, padding=(14, 6))
        s.map("TNotebook.Tab", background=[("selected", PANEL2)], foreground=[("selected", FG)])
        s.configure("TLabelframe", background=BG, bordercolor=GRID)
        s.configure("TLabelframe.Label", background=BG, foreground=DIM)
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=24,
                    borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL2, foreground=DIM, relief="flat")
        s.map("Treeview", background=[("selected", SEL)])
        self.option_add("*TCombobox*Listbox.background", PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", SEL)

    # ================================================================ layout
    def _build(self):
        # ---- header
        head = ttk.Frame(self, style="Panel.TFrame", padding=(14, 10))
        head.pack(fill="x")
        left = ttk.Frame(head, style="Panel.TFrame")
        left.pack(side="left", fill="y")
        top = ttk.Frame(left, style="Panel.TFrame")
        top.pack(anchor="w")
        self.status_dot = tk.Canvas(top, width=16, height=16, bg=PANEL, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_lbl = ttk.Label(top, text="STOPPED", style="Big.TLabel")
        self.status_lbl.pack(side="left")
        self.station_lbl = ttk.Label(top, text="  " + self.cfg.station_name, style="Big.TLabel", foreground=DIM)
        self.station_lbl.pack(side="left")
        self.source_lbl = ttk.Label(left, text="", style="PanelDim.TLabel")
        self.source_lbl.pack(anchor="w", pady=(2, 0))
        self.file_lbl = ttk.Label(left, text="", style="PanelDim.TLabel")
        self.file_lbl.pack(anchor="w")

        right = ttk.Frame(head, style="Panel.TFrame")
        right.pack(side="right", fill="y")
        btnrow = ttk.Frame(right, style="Panel.TFrame")
        btnrow.pack(side="right", padx=(16, 0))
        self.rec_btn = ttk.Button(btnrow, text="● Record", style="Rec.TButton", command=self.toggle_recording)
        self.rec_btn.pack(fill="x")
        ttk.Button(btnrow, text="Settings", command=self.open_settings).pack(fill="x", pady=(6, 0))

        clockf = ttk.Frame(right, style="Panel.TFrame")
        clockf.pack(side="right", padx=(16, 0))
        self.clock_lbl = ttk.Label(clockf, text="--:--:--", style="Clock.TLabel")
        self.clock_lbl.pack(anchor="e")
        self.silence_lbl = ttk.Label(clockf, text="", style="Panel.TLabel", foreground=RED)
        self.silence_lbl.pack(anchor="e")

        self.meter = tk.Canvas(right, width=340, height=46, bg=PANEL, highlightthickness=0)
        self.meter.pack(side="right")

        # ---- timeline toolbar
        tb = ttk.Frame(self, padding=(14, 10, 14, 4))
        tb.pack(fill="x")
        ttk.Label(tb, text="Timeline", font=(self.font[0], self.font[1] + 1, "bold")).pack(side="left")
        ttk.Label(tb, text="   drag to select · wheel to scroll · Ctrl+wheel to zoom", style="Dim.TLabel").pack(side="left")
        self.span_var = tk.StringVar(value=next(n for n, s in SPANS if s == self.span))
        cb = ttk.Combobox(tb, textvariable=self.span_var, values=[n for n, _ in SPANS], state="readonly", width=9)
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_span(dict(SPANS)[self.span_var.get()]))
        ttk.Label(tb, text="View ", style="Dim.TLabel").pack(side="right")
        ttk.Button(tb, text="» Live", command=self.go_live, style="Quick.TButton").pack(side="right", padx=8)
        ttk.Checkbutton(tb, text="Follow live", variable=self.follow, command=self._follow_changed).pack(side="right")

        # ---- waveform
        wf = ttk.Frame(self, padding=(14, 0))
        wf.pack(fill="x")
        self.wave = tk.Canvas(wf, height=210, bg="#101216", highlightthickness=1, highlightbackground=GRID,
                              cursor="xterm")
        self.wave.pack(fill="x")
        self.wave.bind("<ButtonPress-1>", self._wave_press)
        self.wave.bind("<B1-Motion>", self._wave_drag)
        self.wave.bind("<ButtonRelease-1>", self._wave_release)
        self.wave.bind("<Double-Button-1>", self._wave_double)
        self.wave.bind("<Motion>", self._wave_hover)
        self.wave.bind("<Leave>", lambda e: self.wave.delete("hover"))
        self.wave.bind("<MouseWheel>", self._wheel)
        self.wave.bind("<Button-4>", lambda e: self._wheel(e, 120))
        self.wave.bind("<Button-5>", lambda e: self._wheel(e, -120))
        self.wave.bind("<Configure>", lambda e: self._draw_wave())

        # ---- grab controls
        gp = ttk.Frame(self, padding=(14, 10, 14, 6))
        gp.pack(fill="x")

        selbox = ttk.Frame(gp, style="Panel.TFrame", padding=10)
        selbox.pack(side="left", fill="y")
        ttk.Label(selbox, text="SELECTION", style="PanelDim.TLabel").grid(row=0, column=0, columnspan=6, sticky="w")
        self.in_var, self.out_var = tk.StringVar(), tk.StringVar()
        ttk.Label(selbox, text="In", style="Panel.TLabel").grid(row=1, column=0, padx=(0, 4), pady=(6, 0))
        self.in_entry = ttk.Entry(selbox, textvariable=self.in_var, width=11, font=self.mono, style="Time.TEntry")
        self.in_entry.grid(row=1, column=1, pady=(6, 0))
        ttk.Button(selbox, text="Mark In  F2", command=self.mark_in, style="Quick.TButton").grid(row=2, column=1, sticky="ew", pady=(4, 0))
        ttk.Label(selbox, text="Out", style="Panel.TLabel").grid(row=1, column=2, padx=(12, 4), pady=(6, 0))
        self.out_entry = ttk.Entry(selbox, textvariable=self.out_var, width=11, font=self.mono, style="Time.TEntry")
        self.out_entry.grid(row=1, column=3, pady=(6, 0))
        ttk.Button(selbox, text="Mark Out  F3", command=self.mark_out, style="Quick.TButton").grid(row=2, column=3, sticky="ew", pady=(4, 0))
        self.dur_lbl = ttk.Label(selbox, text="—", style="Mono.TLabel", width=12, anchor="center")
        self.dur_lbl.grid(row=1, column=4, padx=12, pady=(6, 0))
        ttk.Button(selbox, text="Clear  Esc", command=self.clear_sel, style="Quick.TButton").grid(row=2, column=4, padx=12, sticky="ew", pady=(4, 0))
        for e, idx in ((self.in_entry, 0), (self.out_entry, 1)):
            e.bind("<Return>", lambda ev, i=idx: self._entry_commit(i))
            e.bind("<FocusOut>", lambda ev, i=idx: self._entry_commit(i))

        actions = ttk.Frame(selbox, style="Panel.TFrame")
        actions.grid(row=1, column=5, rowspan=2, padx=(6, 0), sticky="ns")
        self.grab_btn = ttk.Button(actions, text="Grab selection  F4", style="Accent.TButton", command=self.grab_selection)
        self.grab_btn.pack(fill="x")
        prow = ttk.Frame(actions, style="Panel.TFrame")
        prow.pack(fill="x", pady=(4, 0))
        self.preview_btn = ttk.Button(prow, text="▶ Preview  P", command=self.toggle_preview, style="Quick.TButton")
        self.preview_btn.pack(side="left", fill="x", expand=True)

        quick = ttk.Frame(gp, style="Panel.TFrame", padding=10)
        quick.pack(side="left", fill="y", padx=(10, 0))
        ttk.Label(quick, text="QUICK GRAB — LAST…", style="PanelDim.TLabel").grid(row=0, column=0, columnspan=6, sticky="w")
        for i, (label, secs) in enumerate((("30s", 30), ("1m", 60), ("2m", 120), ("5m", 300), ("10m", 600), ("15m", 900))):
            ttk.Button(quick, text=label, width=4, style="Quick.TButton",
                       command=lambda s=secs: self.grab_last(s)).grid(row=1 + i // 3, column=i % 3, padx=2, pady=(6 if i < 3 else 2, 0))
        ttk.Button(quick, text="Since In → now  F5", style="Quick.TButton", command=self.grab_since_in
                   ).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0), padx=2)

        opts = ttk.Frame(gp, style="Panel.TFrame", padding=10)
        opts.pack(side="left", fill="both", expand=True, padx=(10, 0))
        ttk.Label(opts, text="OUTPUT", style="PanelDim.TLabel").pack(anchor="w")
        fr = ttk.Frame(opts, style="Panel.TFrame")
        fr.pack(anchor="w", pady=(6, 0))
        ttk.Label(fr, text="Format", style="Panel.TLabel").pack(side="left")
        self.grab_fmt = tk.StringVar(value=self.cfg.grab_format)
        ttk.Combobox(fr, textvariable=self.grab_fmt, values=["wav", "mp3", "flac"], state="readonly", width=6
                     ).pack(side="left", padx=6)
        self.open_after = tk.BooleanVar(value=self.cfg.open_after_grab)
        ttk.Checkbutton(opts, text="Open in editor after grab", variable=self.open_after,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))
        self.grab_msg = ttk.Label(opts, text="", style="PanelDim.TLabel", wraplength=300, justify="left")
        self.grab_msg.pack(anchor="w", pady=(6, 0))

        # ---- bottom notebook
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=14, pady=(4, 0))
        self.nb = nb

        g = ttk.Frame(nb)
        nb.add(g, text="Grabs")
        self.grab_tree = self._tree(g, ("time", "length", "file"), (("time", "Grabbed", 90), ("length", "Length", 80),
                                                                   ("file", "File", 600)))
        gb = ttk.Frame(g)
        gb.pack(fill="x", pady=4)
        ttk.Button(gb, text="Open in editor", command=lambda: self._tree_open(self.grab_tree)).pack(side="left")
        ttk.Button(gb, text="Show in folder", command=lambda: self._tree_reveal(self.grab_tree)).pack(side="left", padx=6)
        ttk.Button(gb, text="Copy path", command=lambda: self._tree_copy(self.grab_tree)).pack(side="left")
        ttk.Button(gb, text="Open grabs folder", command=lambda: self._open_folder(self.cfg.grabs_dir)).pack(side="right")

        a = ttk.Frame(nb)
        nb.add(a, text="Archive")
        self.arch_tree = self._tree(a, ("time", "file"), (("time", "Closed", 90), ("file", "File", 700)))
        ab = ttk.Frame(a)
        ab.pack(fill="x", pady=4)
        ttk.Button(ab, text="Open", command=lambda: self._tree_open(self.arch_tree)).pack(side="left")
        ttk.Button(ab, text="Show in folder", command=lambda: self._tree_reveal(self.arch_tree)).pack(side="left", padx=6)
        ttk.Button(ab, text="Open archive folder", command=lambda: self._open_folder(self.cfg.archive_dir)).pack(side="right")

        lg = ttk.Frame(nb)
        nb.add(lg, text="Log")
        self.log_text = tk.Text(lg, bg=PANEL, fg=DIM, insertbackground=FG, relief="flat", height=8,
                                font=(self.mono[0], self.mono[1] - 1), wrap="none")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("err", foreground=RED)
        self.log_text.configure(state="disabled")

        # ---- status bar
        sb = ttk.Frame(self, style="Panel.TFrame", padding=(14, 4))
        sb.pack(fill="x", side="bottom")
        self.buf_lbl = ttk.Label(sb, text="", style="PanelDim.TLabel")
        self.buf_lbl.pack(side="left")
        ttk.Label(sb, text=f"Airlog {__version__}", style="PanelDim.TLabel").pack(side="right")

    def _tree(self, parent, cols, heads):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        t = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse", height=5)
        for key, title, width in heads:
            t.heading(key, text=title, anchor="w")
            t.column(key, width=width, stretch=(key == "file"), anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        t.bind("<Double-1>", lambda e: self._tree_open(t))
        t.paths = {}
        return t

    def _bind_keys(self):
        def guard(fn):
            def h(e):
                if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox)) and e.keysym in ("p", "P"):
                    return
                fn()
                return "break"
            return h
        self.bind_all("<F2>", guard(self.mark_in))
        self.bind_all("<F3>", guard(self.mark_out))
        self.bind_all("<F4>", guard(self.grab_selection))
        self.bind_all("<F5>", guard(self.grab_since_in))
        self.bind_all("<Escape>", guard(self.clear_sel))
        self.bind("<p>", guard(self.toggle_preview))
        self.bind("<P>", guard(self.toggle_preview))

    # ================================================================ engine control
    def _source_configured(self) -> bool:
        c = self.cfg
        return bool((c.source_type == "stream" and c.stream_url) or (c.source_type == "device")
                    or (c.source_type == "custom" and c.custom_input_args))

    def _make_engine(self):
        self.engine = Engine(self.cfg)
        self.engine.on_log = lambda line, err: self.ui_q.put(("log", line, err))
        for line in list(self.engine.logs):
            self.ui_q.put(("log", line, False))

    def start_recording(self):
        if not self.engine.running:
            self.engine.start()

    def stop_recording(self):
        if self.engine.running:
            self.engine.stop()

    def toggle_recording(self):
        if self.engine.running:
            if messagebox.askyesno("Airlog", "Stop recording?\n\nThe current archive file will be closed.", parent=self):
                threading.Thread(target=self.stop_recording, daemon=True).start()
        else:
            if not self._source_configured():
                self.open_settings()
                return
            self.start_recording()

    def open_settings(self):
        SettingsDialog(self, self.cfg, self._settings_saved)

    def _settings_saved(self):
        was_running = self.engine.running
        self.title(f"Airlog — {self.cfg.station_name}")
        self.station_lbl.configure(text="  " + self.cfg.station_name)
        self.grab_fmt.set(self.cfg.grab_format)
        self.open_after.set(self.cfg.open_after_grab)

        def restart():
            self.engine.stop()
            self.ui_q.put(("restart", was_running or self.cfg.autostart))
        threading.Thread(target=restart, daemon=True).start()

    # ================================================================ timeline maths
    def _view(self) -> tuple[float, float]:
        if self.frozen_end is not None:
            end = self.frozen_end
        elif self.follow.get():
            lt = self.engine.live_time() if self.engine else None
            end = lt if lt is not None else time.time()
        else:
            end = self.view_end or time.time()
        return end - self.span, end

    def _t_to_x(self, t: float, w: int, t0: float, t1: float) -> float:
        return (t - t0) / (t1 - t0) * w

    def _x_to_t(self, x: float) -> float:
        w = max(1, self.wave.winfo_width())
        t0, t1 = self._view()
        return t0 + (x / w) * (t1 - t0)

    def _clamp_to_buffer(self, t: float) -> float:
        lo = self.engine.buffer_start()
        hi = self.engine.live_time()
        if hi is not None:
            t = min(t, hi)
        if lo is not None:
            t = max(t, lo)
        return t

    def _set_span(self, secs: int):
        if not self.follow.get():
            t0, t1 = self._view()
            mid = (t0 + t1) / 2
            self.view_end = mid + secs / 2
        self.span = secs
        self.span_var.set(next(n for n, s in SPANS if s == secs))
        self.cfg.view_minutes = max(1, secs // 60)
        self._draw_wave()

    def _follow_changed(self):
        if not self.follow.get():
            self.view_end = self._view()[1]
        self._draw_wave()

    def go_live(self):
        self.follow.set(True)
        self._draw_wave()

    def _wheel(self, e, delta=None):
        d = delta if delta is not None else e.delta
        if sys.platform == "darwin" and delta is None:
            d *= 40
        ctrl = bool(e.state & 0x0004)
        if ctrl:
            idx = [s for _, s in SPANS].index(self.span)
            idx = max(0, min(len(SPANS) - 1, idx + (-1 if d > 0 else 1)))
            self._set_span(SPANS[idx][1])
            return
        t0, t1 = self._view()
        shift = -(d / 120) * self.span * 0.1
        lt = self.engine.live_time() or time.time()
        new_end = min(lt, t1 + shift)
        if new_end >= lt - 0.01 and shift > 0:
            self.follow.set(True)
        else:
            self.follow.set(False)
            self.view_end = new_end
        self._draw_wave()

    # ================================================================ waveform interaction
    def _wave_press(self, e):
        self.frozen_end = self._view()[1]
        t = self._clamp_to_buffer(self._x_to_t(e.x))
        a, b = self.sel
        w = self.wave.winfo_width()
        t0, t1 = self._view()
        near = lambda tt: tt is not None and abs(self._t_to_x(tt, w, t0, t1) - e.x) <= 6
        if near(a) and b is not None:
            self.drag_mode, self.drag_anchor = "edge", b
        elif near(b) and a is not None:
            self.drag_mode, self.drag_anchor = "edge", a
        elif e.state & 0x0001 and a is not None:   # shift-click extends
            self.drag_mode, self.drag_anchor = "edge", a if abs(t - a) > abs(t - (b or a)) else (b or a)
            self._apply_drag(t)
        else:
            self.drag_mode, self.drag_anchor = "new", t
            self.sel = [t, None]
        self._sel_changed()

    def _apply_drag(self, t: float):
        lo, hi = sorted((self.drag_anchor, t))
        self.sel = [lo, hi if hi - lo > 0.05 else None]

    def _wave_drag(self, e):
        if not self.drag_mode:
            return
        self._apply_drag(self._clamp_to_buffer(self._x_to_t(e.x)))
        self._sel_changed()

    def _wave_release(self, e):
        self.drag_mode = None
        if not self.follow.get() and self.frozen_end is not None:
            self.view_end = self.frozen_end
        self.frozen_end = None
        self._sel_changed()

    def _wave_double(self, e):
        """Double-click: select the hour/segment block around the click."""
        t = self._x_to_t(e.x)
        d = dt.datetime.fromtimestamp(t)
        m = self.cfg.split_minutes
        start = d.replace(minute=(d.minute // m) * m if m < 60 else 0, second=0, microsecond=0)
        a = self._clamp_to_buffer(start.timestamp())
        b = self._clamp_to_buffer((start + dt.timedelta(minutes=m)).timestamp())
        self.sel = [a, b]
        self._sel_changed()

    def _wave_hover(self, e):
        self.wave.delete("hover")
        h = self.wave.winfo_height()
        t = self._x_to_t(e.x)
        self.wave.create_line(e.x, 0, e.x, h, fill="#5b6475", dash=(2, 3), tags="hover")
        anchor = "nw" if e.x < self.wave.winfo_width() - 90 else "ne"
        self.wave.create_text(e.x + (4 if anchor == "nw" else -4), 4, text=fmt_hms(t), fill=FG, anchor=anchor,
                              font=(self.mono[0], self.mono[1] - 1), tags="hover")

    # ================================================================ selection
    def _sel_changed(self):
        a, b = self.sel
        if self.focus_get() not in (self.in_entry, self.out_entry):
            self.in_var.set(fmt_hms(a) if a else "")
            self.out_var.set(fmt_hms(b) if b else "")
        if a and b:
            self.dur_lbl.configure(text=fmt_dur(b - a))
        elif a:
            lt = self.engine.live_time()
            self.dur_lbl.configure(text=("+" + fmt_dur(lt - a)) if lt else "—")
        else:
            self.dur_lbl.configure(text="—")
        self._draw_wave()

    def _parse_time(self, text: str) -> float | None:
        text = text.strip()
        if not text:
            return None
        parts = text.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 3:
            h, m, s = nums
        elif len(nums) == 2:
            lt = dt.datetime.fromtimestamp(self.engine.live_time() or time.time())
            h, (m, s) = lt.hour, nums
        else:
            return None
        ref = self.engine.live_time() or time.time()
        base = dt.datetime.fromtimestamp(ref).replace(hour=0, minute=0, second=0, microsecond=0)
        cand = base + dt.timedelta(hours=h, minutes=m, seconds=s)
        t = cand.timestamp()
        if t > ref + 1:            # e.g. typing 23:58 just after midnight -> yesterday
            t = (cand - dt.timedelta(days=1)).timestamp()
        return t

    def _entry_commit(self, idx: int):
        var = self.in_var if idx == 0 else self.out_var
        t = self._parse_time(var.get())
        if var.get().strip() and t is None:
            self.grab_msg.configure(text="Times are HH:MM:SS(.s) or MM:SS", foreground=RED)
            return
        self.sel[idx] = t
        a, b = self.sel
        if a and b and b < a:
            self.sel = [b, a]
        self.focus_set()
        self._sel_changed()
        if t and not self._visible(t):
            self._show_time(t)

    def _visible(self, t: float) -> bool:
        t0, t1 = self._view()
        return t0 <= t <= t1

    def _show_time(self, t: float):
        lt = self.engine.live_time() or time.time()
        end = min(lt, t + self.span * 0.3)
        if end >= lt - 0.1:
            self.follow.set(True)
        else:
            self.follow.set(False)
            self.view_end = end
        self._draw_wave()

    def mark_in(self):
        lt = self.engine.live_time()
        if lt is None:
            return
        self.sel = [lt, None]
        self._sel_changed()

    def mark_out(self):
        lt = self.engine.live_time()
        if lt is None:
            return
        a = self.sel[0]
        if a is None or lt <= a:
            self.grab_msg.configure(text="Mark In first.", foreground=AMBER)
            return
        self.sel = [a, lt]
        self._sel_changed()

    def clear_sel(self):
        self.sel = [None, None]
        self._sel_changed()

    # ================================================================ grabbing
    def grab_selection(self):
        a, b = self.sel
        if a is None:
            self.grab_msg.configure(text="Select a region on the timeline, or Mark In/Out.", foreground=AMBER)
            return
        if b is None:
            return self.grab_since_in()
        self._grab(a, b)

    def grab_since_in(self):
        a = self.sel[0]
        lt = self.engine.live_time()
        if a is None or lt is None:
            self.grab_msg.configure(text="Mark In first (F2).", foreground=AMBER)
            return
        self._grab(a, lt)

    def grab_last(self, secs: float):
        lt = self.engine.live_time()
        if lt is None:
            self.grab_msg.configure(text="Nothing recorded yet.", foreground=AMBER)
            return
        a = self._clamp_to_buffer(lt - secs)
        self.sel = [a, lt]
        self._sel_changed()
        self._grab(a, lt)

    def _grab(self, a: float, b: float):
        a = a - float(self.cfg.grab_preroll_s or 0)
        a = self._clamp_to_buffer(a)
        fmt = self.grab_fmt.get()
        self.grab_msg.configure(text=f"Grabbing {fmt_clock(a)}–{fmt_clock(b)}…", foreground=DIM)
        open_after = self.open_after.get()

        def work():
            try:
                p = self.engine.grab(a, b, fmt=fmt)
                self.ui_q.put(("grabbed", p, b - a, open_after))
            except Exception as ex:   # noqa: BLE001 - shown to the user
                self.ui_q.put(("grab_err", str(ex)))
        threading.Thread(target=work, daemon=True).start()

    # ---- preview with ffplay
    def toggle_preview(self):
        if self.preview_proc and self.preview_proc.poll() is None:
            self.preview_proc.terminate()
            self.preview_proc = None
            self.preview_btn.configure(text="▶ Preview  P")
            return
        a, b = self.sel
        lt = self.engine.live_time()
        if a is None or lt is None:
            self.grab_msg.configure(text="Select something to preview.", foreground=AMBER)
            return
        b = b or lt
        ffplay = osutil.find_ffplay(self.cfg.ffmpeg_path)
        if not ffplay:
            self.grab_msg.configure(text="Preview needs ffplay (ships with full ffmpeg builds).", foreground=AMBER)
            return

        def work():
            tmp = Path(tempfile.gettempdir()) / "airlog_preview.wav"
            try:
                self.engine.grab(a, b, out_path=tmp, fmt="wav")
                proc = subprocess.Popen([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(tmp)],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=sources.NO_WINDOW)
                self.ui_q.put(("preview", proc))
            except Exception as ex:   # noqa: BLE001
                self.ui_q.put(("grab_err", str(ex)))
        threading.Thread(target=work, daemon=True).start()

    # ---- lists
    def _tree_selected(self, tree) -> Path | None:
        sel = tree.selection()
        if not sel:
            kids = tree.get_children()
            if not kids:
                return None
            sel = (kids[0],)
        return tree.paths.get(sel[0])

    def _tree_open(self, tree):
        p = self._tree_selected(tree)
        if p:
            try:
                osutil.open_file(p, self.cfg.editor_path if tree is self.grab_tree else "")
            except OSError as e:
                messagebox.showerror("Airlog", str(e), parent=self)

    def _tree_reveal(self, tree):
        p = self._tree_selected(tree)
        if p:
            osutil.reveal(p)

    def _tree_copy(self, tree):
        p = self._tree_selected(tree)
        if p:
            self.clipboard_clear()
            self.clipboard_append(str(p))
            self.grab_msg.configure(text="Path copied.", foreground=DIM)

    def _open_folder(self, folder: str):
        Path(folder).mkdir(parents=True, exist_ok=True)
        osutil.reveal(Path(folder))

    # ================================================================ periodic updates
    def _tick_fast(self):
        try:
            self._drain_queue()
            self._draw_meter()
            self.clock_lbl.configure(text=time.strftime("%H:%M:%S"))
        finally:
            self.after(50, self._tick_fast)

    def _tick_slow(self):
        try:
            self._update_status()
            self._draw_wave()
            if self.sel[0] and not self.sel[1]:
                self._sel_changed()
        finally:
            self.after(250, self._tick_slow)

    def _drain_queue(self):
        while True:
            try:
                item = self.ui_q.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            if kind == "log":
                _, line, err = item
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n", ("err",) if err else ())
                if int(self.log_text.index("end-1c").split(".")[0]) > 600:
                    self.log_text.delete("1.0", "100.0")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                if "Archive: closed" in line:
                    self._refresh_archive()
            elif kind == "grabbed":
                _, path, secs, open_after = item
                iid = self.grab_tree.insert("", 0, values=(time.strftime("%H:%M:%S"), fmt_dur(secs), path.name))
                self.grab_tree.paths[iid] = path
                self.grab_tree.selection_set(iid)
                self.nb.select(0)
                self.grab_msg.configure(text=f"✓ Saved {path.name}", foreground=GREEN)
                if open_after:
                    try:
                        osutil.open_file(path, self.cfg.editor_path)
                    except OSError as e:
                        self.grab_msg.configure(text=f"Saved, but couldn't open editor: {e}", foreground=AMBER)
            elif kind == "grab_err":
                self.grab_msg.configure(text="✗ " + item[1], foreground=RED)
            elif kind == "preview":
                self.preview_proc = item[1]
                self.preview_btn.configure(text="■ Stop  P")
            elif kind == "restart":
                self._make_engine()
                self.sel = [None, None]
                if item[1] and self._source_configured():
                    self.start_recording()

    def _refresh_archive(self):
        t = self.arch_tree
        t.delete(*t.get_children())
        t.paths.clear()
        for p in list(self.engine.recent_archive):
            try:
                closed = dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%H:%M:%S")
            except OSError:
                continue
            iid = t.insert("", "end", values=(closed, str(p)))
            t.paths[iid] = p

    def _update_status(self):
        e = self.engine
        st = e.status if e else "stopped"
        colours = {"recording": RED, "starting": AMBER, "reconnecting": AMBER, "error": RED, "stopped": DIM}
        text = {"recording": "REC", "starting": "CONNECTING", "reconnecting": "RECONNECTING",
                "error": "ERROR", "stopped": "STOPPED"}[st]
        self.status_dot.delete("all")
        blink = st == "recording" and int(time.time() * 2) % 2 == 0
        self.status_dot.create_oval(2, 2, 14, 14, fill=colours[st] if not (st == "recording" and blink) else "#7a2023",
                                    outline="")
        self.status_lbl.configure(text=text, foreground=colours[st] if st != "stopped" else FG)
        self.rec_btn.configure(text="■ Stop" if e.running else "● Record",
                               style="TButton" if e.running else "Rec.TButton")
        desc = sources.describe(self.cfg)
        desc = ("Stream: " + desc) if self.cfg.source_type == "stream" else desc
        desc = desc if len(desc) <= 90 else desc[:87] + "…"
        self.source_lbl.configure(text=desc
                                  + (f"   ·   {e.last_error}" if st in ("error", "reconnecting") and e.last_error else ""))
        if e.archive is not None:
            lt = e.live_time() or time.time()
            self.file_lbl.configure(text=f"Writing {e.archive.path.name}   ·   {fmt_dur(lt - e.archive.start)} "
                                         f"   ·   next file at {dt.datetime.fromtimestamp(e.archive.boundary):%H:%M}")
        else:
            self.file_lbl.configure(text="Archive off" if not self.cfg.archive_enabled else "")
        sil = e.silence_seconds() if e.status == "recording" else 0
        self.silence_lbl.configure(text=f"⚠ SILENCE {int(sil)}s" if sil >= self.cfg.silence_alert_s else "")
        lo, hi = e.buffer_start(), e.live_time()
        if lo and hi:
            self.buf_lbl.configure(text=f"Grab buffer {fmt_clock(lo)} → {fmt_clock(hi)}  ({fmt_dur(hi - lo)} of "
                                        f"{self.cfg.buffer_minutes} min)   ·   {self.cfg.sample_rate} Hz "
                                        f"{'stereo' if self.cfg.channels == 2 else 'mono'}")
        else:
            self.buf_lbl.configure(text="Grab buffer empty")

    # ================================================================ drawing
    def _draw_meter(self):
        c = self.meter
        c.delete("all")
        w, h = int(c.winfo_width()), int(c.winfo_height())
        x0, x1 = 26, w - 6
        e = self.engine
        lvl = e.level if (e and e.status == "recording") else (0.0, 0.0)
        now = time.monotonic()
        for i, v in enumerate(lvl):
            y = 4 + i * 16
            # smooth fall
            self.meter_hold[i] = max(v, self.meter_hold[i] * 0.82)
            if v >= self.meter_peak[i * 2] or now - self.meter_peak[i * 2 + 1] > 1.5:
                self.meter_peak[i * 2], self.meter_peak[i * 2 + 1] = v, now
            c.create_text(4, y + 6, text="LR"[i], fill=DIM, anchor="w", font=(self.font[0], 8))
            c.create_rectangle(x0, y, x1, y + 12, fill="#101216", outline="")
            frac = peak_to_y(self.meter_hold[i])
            xs = x0 + (x1 - x0) * frac
            # three colour zones: green < -18, amber < -6, red above
            for lo_db, hi_db, col in ((-48, -18, GREEN), (-18, -6, AMBER), (-6, 0, RED)):
                zx0 = x0 + (x1 - x0) * (lo_db + 48) / 48
                zx1 = x0 + (x1 - x0) * (hi_db + 48) / 48
                if xs > zx0:
                    c.create_rectangle(zx0, y, min(xs, zx1), y + 12, fill=col, outline="")
            pk = peak_to_y(self.meter_peak[i * 2])
            if pk > 0:
                px = x0 + (x1 - x0) * pk
                c.create_line(px, y, px, y + 12, fill=FG, width=2)
        for db in (-48, -36, -24, -18, -12, -6, -3, 0):
            x = x0 + (x1 - x0) * (db + 48) / 48
            c.create_line(x, 34, x, 37, fill=DIM)
            if db in (-48, -36, -24, -12, -6, 0):
                c.create_text(x, 38, text=str(db), fill=DIM, anchor="n", font=(self.font[0], 7))

    def _draw_wave(self):
        c = self.wave
        if not self.engine:
            return
        w, h = int(c.winfo_width()), int(c.winfo_height())
        if w < 10:
            return
        c.delete("w")
        t0, t1 = self._view()
        axis_h = 18
        wh = h - axis_h
        mid = wh / 2

        lo, hi = self.engine.buffer_start(), self.engine.live_time()
        # outside-buffer shading
        if lo and lo > t0:
            c.create_rectangle(0, 0, self._t_to_x(lo, w, t0, t1), wh, fill="#0b0c0f", outline="", tags="w")

        # selection
        a, b = self.sel
        if a is not None:
            bb = b if b is not None else (hi or a)
            xa, xb = self._t_to_x(a, w, t0, t1), self._t_to_x(bb, w, t0, t1)
            c.create_rectangle(xa, 0, max(xa + 1, xb), wh, fill=SEL, outline="", tags="w")

        # grid ticks
        tick = next((s for s in TICKS if w / (self.span / s) >= 90), 3600)
        first = math.ceil(t0 / tick) * tick
        # align to local minute boundaries for ticks >= 60 s
        tz_off = dt.datetime.fromtimestamp(t0).astimezone().utcoffset().total_seconds()
        if tick >= 60:
            first = math.ceil((t0 + tz_off) / tick) * tick - tz_off
        t = first
        while t <= t1:
            x = self._t_to_x(t, w, t0, t1)
            d = dt.datetime.fromtimestamp(t)
            is_split = d.second == 0 and (d.minute % self.cfg.split_minutes == 0 if self.cfg.split_minutes < 60
                                          else d.minute == 0)
            c.create_line(x, 0, x, wh, fill=HOUR if is_split else GRID, width=2 if is_split else 1, tags="w")
            label = d.strftime("%H:%M") if tick >= 60 and d.second == 0 else d.strftime("%H:%M:%S")
            c.create_text(x + 3, wh + 2, text=label, fill=FG if is_split else DIM, anchor="nw",
                          font=(self.font[0], 8, "bold" if is_split else "normal"), tags="w")
            t += tick

        # waveform: max peak per pixel column, dB scale
        times, vals = self.engine.peaks_between(t0, t1)
        if len(times):
            cols = [0] * w
            scale = w / (t1 - t0)
            for tt, v in zip(times, vals):
                xi = int((tt - t0) * scale)
                if 0 <= xi < w and v > cols[xi]:
                    cols[xi] = v
            # columns with no sample in them (zoomed in) copy the previous one
            first_x = int((times[0] - t0) * scale)
            last_x = int((times[-1] - t0) * scale)
            # (only bridges spacing between adjacent blocks, never a real gap in the audio)
            bridge = int(scale / 10) + 1
            prev, last_real = 0, -10**9
            for x in range(max(0, first_x), min(w, last_x + 1)):
                if cols[x]:
                    last_real = x
                elif prev and x - last_real <= bridge:
                    cols[x] = prev
                    continue
                prev = cols[x]
            ytab = [peak_to_y(i / 255) * (mid - 4) for i in range(256)]
            top = []
            bot = []
            x_start, x_end = max(0, first_x), min(w - 1, last_x)
            for x in range(x_start, x_end + 1):
                y = ytab[cols[x]]
                top.extend((x, mid - y))
                bot.extend((x, mid + y))
            def poly(xa_: int, xb_: int, colour: str):
                xa_, xb_ = max(xa_, x_start), min(xb_, x_end)
                if xb_ - xa_ < 1:
                    return
                tp = top[(xa_ - x_start) * 2:(xb_ - x_start + 1) * 2]
                bt = bot[(xa_ - x_start) * 2:(xb_ - x_start + 1) * 2]
                pts = tp + [v for i in range(len(bt) - 2, -1, -2) for v in (bt[i], bt[i + 1])]
                c.create_polygon(pts, fill=colour, outline=colour, tags="w")
            poly(x_start, x_end, AMBER)
            if a is not None:
                bb = b if b is not None else (hi or a)
                poly(int(self._t_to_x(a, w, t0, t1)), int(self._t_to_x(bb, w, t0, t1)), "#a9c8ff")
        c.create_line(0, mid, w, mid, fill="#3a3220", tags="w")

        # selection edges + labels
        if a is not None:
            xa = self._t_to_x(a, w, t0, t1)
            c.create_line(xa, 0, xa, wh, fill=GREEN, width=2, tags="w")
            c.create_text(xa + 4, wh - 4, text="IN " + fmt_hms(a), fill=GREEN, anchor="sw",
                          font=(self.mono[0], self.mono[1] - 2, "bold"), tags="w")
        if b is not None:
            xb = self._t_to_x(b, w, t0, t1)
            c.create_line(xb, 0, xb, wh, fill=RED, width=2, tags="w")
            c.create_text(xb - 4, wh - 4, text="OUT " + fmt_hms(b), fill=RED, anchor="se",
                          font=(self.mono[0], self.mono[1] - 2, "bold"), tags="w")

        # live edge
        if hi and t0 <= hi <= t1 + 0.5:
            xl = min(w - 2, self._t_to_x(hi, w, t0, t1))
            c.create_line(xl, 0, xl, wh, fill=FG, width=1, tags="w")
            c.create_text(xl - 4, 4, text="LIVE", fill=FG, anchor="ne", font=(self.font[0], 8, "bold"), tags="w")
        elif hi and hi > t1:
            c.create_text(w - 6, 4, text=f"{fmt_dur(hi - t1)} behind live »", fill=AMBER, anchor="ne",
                          font=(self.font[0], 9, "bold"), tags="w")
        if not len(times) and self.engine.status != "recording":
            c.create_text(w / 2, mid, text="Not recording — press ● Record" if not self.engine.running
                          else "Waiting for audio…", fill=DIM, font=(self.font[0], 12), tags="w")
        c.tag_raise("hover")

    # ================================================================ shutdown
    def _on_close(self):
        if self.engine.running and not messagebox.askyesno(
                "Airlog", "Recording is running.\n\nStop recording and quit?", parent=self):
            return
        try:
            self.cfg.grab_format = self.grab_fmt.get()
            self.cfg.open_after_grab = self.open_after.get()
            self.cfg.save()
        except OSError:
            pass
        if self.preview_proc and self.preview_proc.poll() is None:
            self.preview_proc.terminate()
        self.engine.stop()
        self.destroy()
