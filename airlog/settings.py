"""Settings dialog."""
from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import sources
from .config import Config


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, cfg: Config, on_save):
        super().__init__(master)
        self.title("Airlog settings")
        self.configure(bg="#16181d")
        self.transient(master)
        self.resizable(True, False)
        self.cfg = cfg
        self.on_save = on_save
        self.v: dict[str, tk.Variable] = {}

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        nb.add(self._source_tab(nb), text="Source")
        nb.add(self._archive_tab(nb), text="Hourly archive")
        nb.add(self._grab_tab(nb), text="Grabs")
        nb.add(self._general_tab(nb), text="General")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=10)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=6)
        ttk.Label(btns, text="Saving restarts the recorder (a gap of a second or two).",
                  style="Dim.TLabel").pack(side="left")
        self._sync_source()
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())

    # ------------------------------------------------------------- helpers
    def var(self, key, kind=tk.StringVar):
        v = kind(value=getattr(self.cfg, key))
        self.v[key] = v
        return v

    def row(self, parent, r, label, widget, hint=None):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=4)
        widget.grid(row=r, column=1, sticky="ew", pady=4)
        if hint:
            ttk.Label(parent, text=hint, style="Dim.TLabel").grid(row=r, column=2, sticky="w", padx=8)
        parent.columnconfigure(1, weight=1)

    def path_entry(self, parent, key, folder=True):
        f = ttk.Frame(parent)
        e = ttk.Entry(f, textvariable=self.var(key), width=46)
        e.pack(side="left", fill="x", expand=True)

        def browse():
            if folder:
                p = filedialog.askdirectory(parent=self, initialdir=self.v[key].get() or None)
            else:
                p = filedialog.askopenfilename(parent=self)
            if p:
                self.v[key].set(p)
        ttk.Button(f, text="Browse…", command=browse).pack(side="left", padx=(6, 0))
        return f

    # ------------------------------------------------------------- tabs
    def _source_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        st = self.var("source_type")
        rb = ttk.Frame(f)
        for val, text in (("stream", "Internet stream"), ("device", "Sound card input"), ("custom", "Custom ffmpeg input")):
            ttk.Radiobutton(rb, text=text, value=val, variable=st, command=self._sync_source).pack(side="left", padx=(0, 14))
        rb.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # stream
        self.stream_frame = sf = ttk.LabelFrame(f, text="Stream", padding=10)
        e = ttk.Entry(sf, textvariable=self.var("stream_url"), width=56)
        self.row(sf, 0, "URL", e)
        tf = ttk.Frame(sf)
        ttk.Button(tf, text="Test stream", command=self._test_stream).pack(side="left")
        self.test_lbl = ttk.Label(tf, text="Icecast, Shoutcast, HLS (.m3u8) and .pls/.m3u playlists all work.",
                                  style="Dim.TLabel", wraplength=420)
        self.test_lbl.pack(side="left", padx=8)
        tf.grid(row=1, column=1, sticky="w")
        sf.grid(row=1, column=0, columnspan=3, sticky="ew")

        # device
        self.device_frame = df = ttk.LabelFrame(f, text="Sound card", padding=10)
        bv = self.var("device_backend")
        if not bv.get():
            bv.set(sources.default_backend())
        cb = ttk.Combobox(df, textvariable=bv, values=sources.BACKENDS, state="readonly", width=16)
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_devices())
        self.row(df, 0, "Driver", cb)
        dv = self.var("device_name")
        rf = ttk.Frame(df)
        self.dev_combo = ttk.Combobox(rf, textvariable=dv, width=50)
        self.dev_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(rf, text="Refresh", command=self._refresh_devices).pack(side="left", padx=(6, 0))
        self.row(df, 1, "Input", rf)
        ttk.Label(df, text="Windows: dshow · macOS: avfoundation · Linux: pulse or alsa",
                  style="Dim.TLabel").grid(row=2, column=1, sticky="w")
        self._device_values: dict[str, str] = {}
        df.grid(row=1, column=0, columnspan=3, sticky="ew")

        # custom
        self.custom_frame = cf = ttk.LabelFrame(f, text="Custom", padding=10)
        self.row(cf, 0, "Input args", ttk.Entry(cf, textvariable=self.var("custom_input_args"), width=56))
        ttk.Label(cf, text='Everything before the output, e.g.  -f lavfi -i "sine=f=440"  or  -i srt://host:9000',
                  style="Dim.TLabel").grid(row=1, column=1, sticky="w")
        cf.grid(row=1, column=0, columnspan=3, sticky="ew")

        common = ttk.LabelFrame(f, text="Capture", padding=10)
        fp = self.path_entry(common, "ffmpeg_path", folder=False)
        self.row(common, 0, "ffmpeg", fp)
        self.ff_lbl = ttk.Label(common, text="", style="Dim.TLabel")
        self.ff_lbl.grid(row=1, column=1, sticky="w")
        self.row(common, 2, "Sample rate",
                 ttk.Combobox(common, textvariable=self.var("sample_rate", tk.IntVar), values=[44100, 48000],
                              state="readonly", width=10))
        self.row(common, 3, "Channels",
                 ttk.Combobox(common, textvariable=self.var("channels", tk.IntVar), values=[1, 2],
                              state="readonly", width=10), "1 = mono, 2 = stereo")
        self.row(common, 4, "Source delay",
                 ttk.Spinbox(common, from_=0, to=120, increment=0.5, textvariable=self.var("source_delay_s", tk.DoubleVar),
                             width=10), "seconds the source runs behind air (streams)")
        common.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        f.columnconfigure(0, weight=1)
        self.after(50, self._check_ffmpeg)
        return f

    def _archive_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        ttk.Checkbutton(f, text="Record hourly archive files", variable=self.var("archive_enabled", tk.BooleanVar)
                        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.row(f, 1, "Folder", self.path_entry(f, "archive_dir"))
        self.row(f, 2, "File name", ttk.Entry(f, textvariable=self.var("archive_pattern"), width=46),
                 "strftime + {station}")
        ttk.Label(f, text="Use / for sub-folders. The time is each file's start time, e.g. 13-00.",
                  style="Dim.TLabel").grid(row=3, column=1, sticky="w")
        self.row(f, 4, "Format", ttk.Combobox(f, textvariable=self.var("archive_format"),
                                              values=["mp3", "flac", "wav", "ogg"], state="readonly", width=10))
        self.row(f, 5, "Bitrate (kbps)", ttk.Combobox(f, textvariable=self.var("archive_bitrate_kbps", tk.IntVar),
                                                     values=[96, 128, 160, 192, 256, 320], width=10), "mp3/ogg only")
        self.row(f, 6, "Split every", ttk.Combobox(f, textvariable=self.var("split_minutes", tk.IntVar),
                                                   values=[60, 30, 15], state="readonly", width=10),
                 "minutes, on the clock")
        self.row(f, 7, "Delete after", ttk.Spinbox(f, from_=0, to=3650, textvariable=self.var("archive_keep_days", tk.IntVar),
                                                   width=10), "days (0 = never)")
        return f

    def _grab_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        self.row(f, 0, "Grabs folder", self.path_entry(f, "grabs_dir"))
        self.row(f, 1, "File name", ttk.Entry(f, textvariable=self.var("grab_pattern"), width=46),
                 "time = grab in-point")
        self.row(f, 2, "Format", ttk.Combobox(f, textvariable=self.var("grab_format"),
                                              values=["wav", "mp3", "flac"], state="readonly", width=10))
        self.row(f, 3, "Pre-roll", ttk.Spinbox(f, from_=0, to=30, increment=0.5,
                                               textvariable=self.var("grab_preroll_s", tk.DoubleVar), width=10),
                 "seconds added before the in-point")
        self.row(f, 4, "Grab buffer", ttk.Spinbox(f, from_=5, to=1440, increment=5,
                                                  textvariable=self.var("buffer_minutes", tk.IntVar), width=10),
                 "minutes you can grab back (≈ 690 MB/hour at 48k stereo)")
        self.row(f, 5, "Buffer folder", self.path_entry(f, "spool_dir"))
        ttk.Label(f, text="Blank = ~/Airlog/.spool. A local SSD is best.", style="Dim.TLabel").grid(row=6, column=1, sticky="w")
        self.row(f, 7, "Audio editor", self.path_entry(f, "editor_path", folder=False))
        ttk.Label(f, text="Blank = system default app. e.g. Audition.exe, Audacity, Audemo",
                  style="Dim.TLabel").grid(row=8, column=1, sticky="w")
        ttk.Checkbutton(f, text="Open in editor after each grab", variable=self.var("open_after_grab", tk.BooleanVar)
                        ).grid(row=9, column=1, sticky="w", pady=4)
        return f

    def _general_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        self.row(f, 0, "Station name", ttk.Entry(f, textvariable=self.var("station_name"), width=30))
        ttk.Checkbutton(f, text="Start recording when Airlog opens", variable=self.var("autostart", tk.BooleanVar)
                        ).grid(row=1, column=1, sticky="w", pady=4)
        self.row(f, 2, "Silence alert below", ttk.Spinbox(f, from_=-90, to=-10, textvariable=self.var("silence_alert_db", tk.DoubleVar),
                                                          width=10), "dBFS")
        self.row(f, 3, "…for longer than", ttk.Spinbox(f, from_=3, to=600, textvariable=self.var("silence_alert_s", tk.IntVar),
                                                       width=10), "seconds")
        ttk.Checkbutton(f, text="Identify songs with Shazam (sends a 12-second sample at each check)",
                        variable=self.var("identify_songs", tk.BooleanVar)).grid(row=4, column=1, sticky="w", pady=4)
        self.row(f, 5, "Check every", ttk.Spinbox(f, from_=15, to=600, textvariable=self.var("identify_interval_s", tk.IntVar),
                                                  width=10), "seconds")
        return f

    # ------------------------------------------------------------- behaviour
    def _sync_source(self):
        st = self.v["source_type"].get()
        for frame, key in ((self.stream_frame, "stream"), (self.device_frame, "device"), (self.custom_frame, "custom")):
            if key == st:
                frame.grid()
            else:
                frame.grid_remove()
        if st == "device" and not self._device_values:
            self._refresh_devices()

    def _refresh_devices(self):
        backend = self.v["device_backend"].get()
        ff = self.v["ffmpeg_path"].get() or "ffmpeg"
        self.dev_combo.configure(values=["(searching…)"])

        def work():
            devs = sources.list_devices(backend, ff)
            self.after(0, lambda: self._set_devices(devs))
        threading.Thread(target=work, daemon=True).start()

    def _set_devices(self, devs):
        if not self.winfo_exists():
            return
        self._device_values = {label: value for value, label in devs}
        self.dev_combo.configure(values=list(self._device_values) or ["(no inputs found)"])
        cur = self.v["device_name"].get()
        if not cur and devs:
            self.v["device_name"].set(devs[0][1])

    def _check_ffmpeg(self):
        ff = self.v["ffmpeg_path"].get() or "ffmpeg"

        def work():
            ver = sources.ffmpeg_version(ff)
            text = f"ffmpeg {ver} found" if ver else "ffmpeg not found — install it or browse to ffmpeg(.exe)"
            self.after(0, lambda: self.winfo_exists() and self.ff_lbl.configure(text=text))
        threading.Thread(target=work, daemon=True).start()

    def _test_stream(self):
        url = self.v["stream_url"].get().strip()
        if not url:
            return
        ff = self.v["ffmpeg_path"].get() or "ffmpeg"
        fp = Path(ff)
        probe = str(fp.with_name(fp.name.replace("ffmpeg", "ffprobe"))) if "ffmpeg" in fp.name else "ffprobe"
        self.test_lbl.configure(text="Testing…")

        def work():
            try:
                p = subprocess.run(stdin=subprocess.DEVNULL, args=[probe, "-v", "error", "-rw_timeout", "10000000", "-select_streams", "a:0",
                                    "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate",
                                    "-of", "default=nw=1", url],
                                   capture_output=True, text=True, timeout=20, creationflags=sources.NO_WINDOW)
                out = (p.stdout.strip().replace("\n", "  ") or p.stderr.strip()[-200:] or "No audio stream found")
                ok = p.returncode == 0 and "codec_name" in p.stdout
            except (OSError, subprocess.TimeoutExpired) as e:
                out, ok = str(e), False
            msg = ("✓ " if ok else "✗ ") + out
            self.after(0, lambda: self.winfo_exists() and self.test_lbl.configure(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def _save(self):
        try:
            for k, v in self.v.items():
                val = v.get()
                if k == "device_name":
                    val = self._device_values.get(val, val)
                if isinstance(val, str):
                    val = val.strip()
                setattr(self.cfg, k, val)
        except tk.TclError as e:
            messagebox.showerror("Airlog", f"Invalid value: {e}", parent=self)
            return
        if self.cfg.source_type == "stream" and not self.cfg.stream_url:
            messagebox.showwarning("Airlog", "Enter a stream URL.", parent=self)
            return
        self.cfg.save()
        self.destroy()
        self.on_save()
