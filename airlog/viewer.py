"""Airlog Viewer: a small app for the machines that *use* a recorder rather than run one.

It lists the recorders you've saved plus any it finds on the network (mDNS), shows whether
each is up and recording, keeps passwords in the OS keychain, and opens a recorder's web UI
signed in, in a Chrome/Edge app window or the default browser. It records nothing itself.

    AirlogViewer                     the recorder list
    AirlogViewer --open NAME|URL     open one recorder straight away (for shortcuts)
"""
from __future__ import annotations

import base64
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__, discovery
from .config import config_dir
from .servicecore import app_window_browser, open_url

APP_NAME = "Airlog Viewer"
KEYRING_SERVICE = "Airlog Viewer"
STORE = config_dir() / "viewer.json"
DEFAULT_PORT = 8080
POLL_S = 4
TIMEOUT_S = 3


# ---------------------------------------------------------------- addresses
def normalize_url(text: str) -> str:
    """'studio', 'studio:9000', '192.168.1.20', 'http://studio:8080/x' -> 'http://host:port/'."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Enter the recorder's address, e.g. 192.168.1.20 or studio-pc")
    if "://" not in t:
        t = "http://" + t
    u = urllib.parse.urlsplit(t)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"Not a recorder address: {text}")
    host = u.hostname
    if ":" in host:                       # IPv6 literal
        host = f"[{host}]"
    try:
        port = u.port
    except ValueError:
        raise ValueError(f"Bad port in {text}") from None
    if port is None:          # https: assume a reverse proxy on 443; plain http: Airlog's default port
        port = 443 if u.scheme == "https" else DEFAULT_PORT
    return f"{u.scheme}://{host}:{port}/"


def short_address(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    s = u.netloc
    return s if u.scheme == "http" else f"{u.scheme}://{s}"


def same_place(url_a: str, url_b: str) -> bool:
    a, b = urllib.parse.urlsplit(url_a), urllib.parse.urlsplit(url_b)
    return (a.hostname or "").lower() == (b.hostname or "").lower() and a.port == b.port


# ---------------------------------------------------------------- saved recorders
@dataclass
class Recorder:
    name: str
    url: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)   # keychain key: survives address changes
    mdns: str = ""                                               # discovery name, to follow IP changes


class Store:
    def __init__(self, path: Path = STORE):
        self.path = path
        self.recorders: list[Recorder] = []
        self.app_window = True
        self.geometry = ""
        self.load()

    def load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        known = {"name", "url", "id", "mdns"}
        self.recorders = [Recorder(**{k: v for k, v in r.items() if k in known})
                          for r in d.get("recorders", []) if r.get("url")]
        self.app_window = bool(d.get("app_window", True))
        self.geometry = str(d.get("geometry", ""))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"recorders": [asdict(r) for r in self.recorders],
                                   "app_window": self.app_window, "geometry": self.geometry}, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def find(self, name_or_url: str) -> Recorder | None:
        for r in self.recorders:
            if r.name.lower() == name_or_url.lower():
                return r
        try:
            url = normalize_url(name_or_url)
        except ValueError:
            return None
        return next((r for r in self.recorders if same_place(r.url, url)), None)


# ---------------------------------------------------------------- keychain
class Secrets:
    """Passwords in Windows Credential Manager / macOS Keychain / Secret Service, cached in memory."""

    def __init__(self):
        self.cache: dict[str, str] = {}
        self.kr = None
        self.why = ""
        try:
            import keyring
            backend = keyring.get_keyring()
            name = type(backend).__module__ + "." + type(backend).__name__
            if "fail" in name or "null" in name.lower():
                self.why = "no keychain is available on this system"
            else:
                self.kr = keyring
                self.backend = getattr(backend, "name", name)
        except Exception as e:
            self.why = f"keychain unavailable ({type(e).__name__})"

    @property
    def available(self) -> bool:
        return self.kr is not None

    def get(self, rid: str) -> str | None:
        if rid in self.cache:
            return self.cache[rid]
        pw = None
        if self.kr:
            try:
                pw = self.kr.get_password(KEYRING_SERVICE, rid)
            except Exception:
                pw = None
        if pw is not None:
            self.cache[rid] = pw
        return pw

    def set(self, rid: str, pw: str, remember: bool) -> None:
        self.cache[rid] = pw
        if remember and self.kr:
            try:
                self.kr.set_password(KEYRING_SERVICE, rid, pw)
            except Exception:
                pass
        elif not remember:
            self.forget(rid, keep_session=True)

    def forget(self, rid: str, keep_session: bool = False) -> None:
        if not keep_session:
            self.cache.pop(rid, None)
        if self.kr:
            try:
                self.kr.delete_password(KEYRING_SERVICE, rid)
            except Exception:
                pass

    def stored(self, rid: str) -> bool:
        if not self.kr:
            return False
        try:
            return self.kr.get_password(KEYRING_SERVICE, rid) is not None
        except Exception:
            return False


# ---------------------------------------------------------------- talking to a recorder
class Unreachable(Exception):
    pass


class PasswordNeeded(Exception):
    pass


class WrongPassword(Exception):
    pass


@dataclass
class Probe:
    state: str                 # online | locked | offline
    station: str = ""
    status: str = ""           # recording | stopped | error | starting…
    version: str = ""
    error: str = ""
    with_password: bool = False   # whether this check sent a password (so "locked" means it was refused)


def _request(url: str, password: str | None = None, data: dict | None = None, timeout: float = TIMEOUT_S):
    headers = {"User-Agent": f"AirlogViewer/{__version__}", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"viewer:{password}".encode()).decode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    # No proxies: recorders are on the LAN, and a system proxy would hide them.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def probe(url: str, password: str | None) -> Probe:
    try:
        d = _request(url + "api/status", password)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return Probe("locked", with_password=bool(password))
        return Probe("offline", error=f"HTTP {e.code}")
    except (OSError, ValueError) as e:
        return Probe("offline", error=str(getattr(e, "reason", e)))
    return Probe("online", station=str(d.get("station", "")), status=str(d.get("status", "")),
                 version=str(d.get("version", "")), error=str(d.get("last_error") or ""))


def signed_in_url(url: str, password: str | None) -> str:
    """The URL that opens the recorder's UI signed in (a one-time link), or the plain URL if no
    password is set. Older recorders without /api/login get the plain URL (the browser asks)."""
    try:
        d = _request(url + "api/login", data={"password": password or ""}, timeout=8)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise (WrongPassword() if password else PasswordNeeded()) from None
        if e.code in (404, 405):
            return url
        raise Unreachable(f"HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise Unreachable(str(getattr(e, "reason", e))) from None
    tok = d.get("token")
    return url + "login?t=" + urllib.parse.quote(tok) if tok else url


# ---------------------------------------------------------------- the window
def run_gui(open_target: str | None = None) -> int:
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox, ttk

    store = Store()
    secrets = Secrets()
    events: "queue.Queue[tuple]" = queue.Queue()

    root = tk.Tk()
    root.title(APP_NAME)
    root.minsize(620, 320)
    root.geometry(store.geometry or "760x400")
    try:
        root.iconphoto(True, tk.PhotoImage(file=str(Path(__file__).with_name("assets") / "viewer.png")))
    except Exception:
        pass
    style = ttk.Style(root)
    if sys.platform.startswith("linux") and "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("Treeview", rowheight=26)

    # rows: iid -> ("saved", Recorder) | ("found", discovery.Found)
    rows: dict[str, tuple] = {}
    probes: dict[str, Probe] = {}
    found: list[discovery.Found] = []

    # ---- layout
    top = ttk.Frame(root, padding=(12, 10, 12, 4))
    top.pack(fill="x")
    heading = tkfont.nametofont("TkDefaultFont").copy()
    heading.configure(weight="bold", size=heading.cget("size") + 2 if heading.cget("size") > 0 else heading.cget("size") - 3)
    ttk.Label(top, text="Recorders", font=heading).pack(side="left")
    netlbl = ttk.Label(top, text="", foreground="#777")
    netlbl.pack(side="right")

    msg = ttk.Label(root, text="", foreground="#777", padding=(12, 0, 12, 8))
    msg.pack(side="bottom", fill="x")
    bottom = ttk.Frame(root, padding=12)
    bottom.pack(side="bottom", fill="x")
    mid = ttk.Frame(root, padding=(12, 0))
    mid.pack(fill="both", expand=True)
    tree = ttk.Treeview(mid, columns=("addr", "status", "ver"), selectmode="browse")
    tree.heading("#0", text="Recorder", anchor="w")
    tree.heading("addr", text="Address", anchor="w")
    tree.heading("status", text="Status", anchor="w")
    tree.heading("ver", text="Version", anchor="w")
    tree.column("#0", width=200, stretch=True)
    tree.column("addr", width=170, stretch=False)
    tree.column("status", width=250, stretch=True)
    tree.column("ver", width=70, stretch=False)
    sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    for tag, colour in (("rec", "#1a8f3a"), ("err", "#c0392b"), ("off", "#888888"), ("lock", "#b7791f"),
                        ("found", "#2b6cb0")):
        tree.tag_configure(tag, foreground=colour)

    open_btn = ttk.Button(bottom, text="Open", width=10)
    add_btn = ttk.Button(bottom, text="Add…")
    edit_btn = ttk.Button(bottom, text="Edit…")
    rm_btn = ttk.Button(bottom, text="Remove")
    for b in (open_btn, add_btn, edit_btn, rm_btn):
        b.pack(side="left", padx=(0, 6))
    appwin = tk.BooleanVar(value=store.app_window)
    browser = app_window_browser()
    appwin_cb = ttk.Checkbutton(bottom, variable=appwin,
                                text=f"Open in an app window ({browser})" if browser else "Open in an app window")
    if not browser:
        appwin_cb.state(["disabled"])
    appwin_cb.pack(side="right")

    def say(text: str) -> None:
        msg.configure(text=text)

    # ---- table
    def status_text(p: Probe | None) -> tuple[str, str]:
        if p is None:
            return "Checking…", "off"
        if p.state == "offline":
            return "Offline" + (f" ({p.error})" if p.error and len(p.error) < 60 else ""), "off"
        if p.state == "locked":
            return "Password needed", "lock"
        station = f" · {p.station}" if p.station else ""
        if p.status == "recording":
            return f"● Recording{station}", "rec"
        if p.status == "error":
            return f"⚠ {p.error or 'Error'}"[:80], "err"
        return f"■ {p.status.capitalize() or 'Stopped'}{station}", "off"

    def refresh_table() -> None:
        sel = tree.selection()
        keep = sel[0] if sel else None
        want: dict[str, tuple] = {}
        for r in store.recorders:
            want["s:" + r.id] = ("saved", r)
        for f in sorted(found, key=lambda f: f.station.lower()):
            if any(r.mdns == f.key or same_place(r.url, f.url) for r in store.recorders):
                continue
            want["f:" + f.key] = ("found", f)
        for iid in list(rows):
            if iid not in want:
                tree.delete(iid)
                rows.pop(iid)
        for i, (iid, row) in enumerate(want.items()):
            kind, obj = row
            url = obj.url
            p = probes.get(url)
            text, tag = status_text(p)
            if kind == "saved":
                name = obj.name
                if p and p.state == "locked":
                    if p.with_password:
                        text = "Password refused"
                    elif secrets.cache.get(obj.id):
                        text = "Checking…"          # a password was entered since this check started
            else:
                name = f"{obj.station}  (found)"
                tag = "found" if not p or p.state != "online" or p.status != "recording" else tag
            values = (short_address(url), text, (p.version if p else "") or (obj.version if kind == "found" else ""))
            if iid in rows:
                tree.item(iid, text=name, values=values, tags=(tag,))
                tree.move(iid, "", i)
            else:
                tree.insert("", i, iid=iid, text=name, values=values, tags=(tag,))
            rows[iid] = row
        if keep in rows:
            tree.selection_set(keep)
        elif rows and not tree.selection():
            first = next(iter(rows))
            tree.selection_set(first)
            tree.focus(first)
        update_buttons()

    def selected():
        sel = tree.selection()
        return rows.get(sel[0]) if sel else None

    def update_buttons(*_):
        row = selected()
        for b in (open_btn, edit_btn, rm_btn):
            b.state(["!disabled"] if row else ["disabled"])
        if row and row[0] == "found":
            edit_btn.configure(text="Save…")
            rm_btn.state(["disabled"])
        else:
            edit_btn.configure(text="Edit…")

    # ---- background: status polling and discovery
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")
    stop = threading.Event()

    def poll_loop():
        while not stop.is_set():
            targets: dict[str, str | None] = {}
            for r in list(store.recorders):
                targets[r.url] = secrets.cache.get(r.id)
            for f in list(found):
                targets.setdefault(f.url, None)
            results = dict(zip(targets, pool.map(lambda kv: probe(*kv), targets.items())))
            events.put(("probes", results))
            stop.wait(POLL_S)

    def prime_secrets():
        for r in list(store.recorders):
            secrets.get(r.id)            # keychain lookups can be slow (or prompt): off the UI thread

    def on_found(items):
        events.put(("found", items))

    browser_obj = discovery.Browser(on_found)

    def start_background():
        prime_secrets()
        if browser_obj.start():
            events.put(("net", "Searching the network for recorders…"))
        else:
            events.put(("net", "Network search off: " + browser_obj.error))
        poll_loop()

    threading.Thread(target=start_background, daemon=True, name="background").start()

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                kind = ev[0]
                if kind == "probes":
                    probes.clear()
                    probes.update(ev[1])
                    refresh_table()
                elif kind == "found":
                    found[:] = ev[1]
                    follow_moves()
                    refresh_table()
                    n = len(found)
                    netlbl.configure(text=f"{n} on the network" if n else "Searching the network…")
                elif kind == "net":
                    netlbl.configure(text=ev[1])
                elif kind == "call":
                    ev[1]()
        except queue.Empty:
            pass
        root.after(150, pump)

    def follow_moves():
        """A saved recorder whose IP changed (DHCP) is still found by its mDNS name: update it."""
        changed = False
        for f in found:
            for r in store.recorders:
                if r.mdns == f.key and not same_place(r.url, f.url) and r.url.startswith("http://"):
                    r.url = f.url
                    changed = True
        if changed:
            store.save()

    # ---- dialogs
    def password_dialog(title: str, prompt: str) -> tuple[str, bool] | None:
        d = tk.Toplevel(root)
        d.title(title)
        d.transient(root)
        d.resizable(False, False)
        f = ttk.Frame(d, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=prompt, wraplength=340, justify="left").pack(anchor="w", pady=(0, 8))
        pw = tk.StringVar()
        e = ttk.Entry(f, textvariable=pw, show="•", width=36)
        e.pack(fill="x")
        rem = tk.BooleanVar(value=secrets.available)
        cb = ttk.Checkbutton(f, text="Remember in the keychain" if secrets.available
                             else f"Can't remember it: {secrets.why}", variable=rem)
        if not secrets.available:
            cb.state(["disabled"])
        cb.pack(anchor="w", pady=(8, 0))
        result: list = []
        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=(14, 0))

        def ok(*_):
            result.append((pw.get(), bool(rem.get())))
            d.destroy()
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="right")
        ttk.Button(bf, text="Sign in", command=ok).pack(side="right", padx=6)
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda *_: d.destroy())
        e.focus_set()
        d.grab_set()
        root.wait_window(d)
        return result[0] if result else None

    def recorder_dialog(title: str, rec: Recorder | None, prefill: discovery.Found | None = None) -> bool:
        d = tk.Toplevel(root)
        d.title(title)
        d.transient(root)
        d.resizable(False, False)
        f = ttk.Frame(d, padding=16)
        f.pack(fill="both", expand=True)
        name = tk.StringVar(value=rec.name if rec else (prefill.station if prefill else ""))
        addr = tk.StringVar(value=short_address(rec.url) if rec else (short_address(prefill.url) if prefill else ""))
        pw = tk.StringVar()
        has_pw = bool(rec and secrets.stored(rec.id))
        rem = tk.BooleanVar(value=secrets.available)
        ttk.Label(f, text="Name").grid(row=0, column=0, sticky="w", pady=3)
        en = ttk.Entry(f, textvariable=name, width=34)
        en.grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="Address").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 10))
        ttk.Entry(f, textvariable=addr, width=34).grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="e.g. 192.168.1.20, studio-pc or studio-pc:9000 (port 8080 if left out)",
                  foreground="#777").grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="Password").grid(row=3, column=0, sticky="w", pady=(10, 3))
        ttk.Entry(f, textvariable=pw, show="•", width=34).grid(row=3, column=1, sticky="ew", pady=(10, 3))
        hint = ("Saved in the keychain. Leave blank to keep it." if has_pw else
                "Leave blank if the recorder has no web password.")
        ttk.Label(f, text=hint, foreground="#777").grid(row=4, column=1, sticky="w")
        cb = ttk.Checkbutton(f, text="Remember the password in the keychain" if secrets.available
                             else f"Passwords can't be saved: {secrets.why}", variable=rem)
        if not secrets.available:
            cb.state(["disabled"])
        cb.grid(row=5, column=1, sticky="w", pady=(6, 0))
        forget = tk.BooleanVar(value=False)
        if has_pw:
            ttk.Checkbutton(f, text="Forget the saved password", variable=forget).grid(row=6, column=1, sticky="w")
        err = ttk.Label(f, text="", foreground="#c0392b")
        err.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        saved: list = []

        def ok(*_):
            try:
                url = normalize_url(addr.get())
            except ValueError as ex:
                err.configure(text=str(ex))
                return
            nm = name.get().strip() or short_address(url)
            dup = next((r for r in store.recorders if r is not rec and same_place(r.url, url)), None)
            if dup:
                err.configure(text=f"Already saved as \"{dup.name}\"")
                return
            target = rec or Recorder(name=nm, url=url, mdns=prefill.key if prefill else "")
            if rec and not same_place(rec.url, url):
                target.mdns = ""                  # moved by hand: stop following the old mDNS name
            target.name, target.url = nm, url
            if rec is None:
                store.recorders.append(target)
            if forget.get():
                secrets.forget(target.id)
            if pw.get():
                secrets.set(target.id, pw.get(), bool(rem.get()))
            store.save()
            saved.append(target)
            d.destroy()

        bf = ttk.Frame(f)
        bf.grid(row=8, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="right")
        ttk.Button(bf, text="Save", command=ok).pack(side="right", padx=6)
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda *_: d.destroy())
        en.focus_set()
        d.grab_set()
        root.wait_window(d)
        if saved:
            refresh_table()
            iid = "s:" + saved[0].id
            if tree.exists(iid):
                tree.selection_set(iid)
                tree.see(iid)
            kick_poll()
        return bool(saved)

    def kick_poll():
        def one():
            targets = {r.url: secrets.cache.get(r.id) for r in store.recorders}
            for f in found:
                targets.setdefault(f.url, None)
            events.put(("probes", {u: probe(u, p) for u, p in targets.items()}))
        threading.Thread(target=one, daemon=True).start()

    # ---- actions
    def open_selected(*_):
        row = selected()
        if not row:
            return
        kind, obj = row
        if kind == "saved":
            open_recorder(obj.url, obj.name, rid=obj.id)
        else:
            open_recorder(obj.url, obj.station, rid=None)

    def open_recorder(url: str, label: str, rid: str | None, retry_prompt: str | None = None) -> None:
        pw = secrets.get(rid) if rid else secrets.cache.get("found:" + url)
        say(f"Opening {label}…")
        open_btn.state(["disabled"])

        def work():
            try:
                target = signed_in_url(url, pw)
                events.put(("call", lambda: launched(target, label)))
            except PasswordNeeded:
                events.put(("call", lambda: ask_pw(f"{label} needs its web password.")))
            except WrongPassword:
                events.put(("call", lambda: ask_pw(f"The password for {label} was refused. Enter it again.")))
            except Unreachable as e:
                events.put(("call", lambda: failed(f"Can't reach {label} at {short_address(url)}: {e}")))

        def ask_pw(prompt: str):
            update_buttons()
            say("")
            got = password_dialog("Sign in", prompt)
            if not got:
                return
            if rid:
                secrets.set(rid, got[0], got[1])
            else:
                secrets.cache["found:" + url] = got[0]
            open_recorder(url, label, rid)

        threading.Thread(target=work, daemon=True).start()

    def launched(target: str, label: str):
        open_url(target, app_window=bool(appwin.get()) and app_window_browser() is not None)
        say(f"Opened {label}.")
        update_buttons()
        kick_poll()
        root.after(4000, lambda: say(""))

    def failed(text: str):
        say("")
        update_buttons()
        messagebox.showerror(APP_NAME, text, parent=root)

    def add(*_):
        row = selected()
        recorder_dialog("Add recorder", None, prefill=row[1] if row and row[0] == "found" else None)

    def edit(*_):
        row = selected()
        if not row:
            return
        if row[0] == "found":
            recorder_dialog("Save recorder", None, prefill=row[1])
        else:
            recorder_dialog("Edit recorder", row[1])

    def remove(*_):
        row = selected()
        if not row or row[0] != "saved":
            return
        r = row[1]
        if not messagebox.askyesno(APP_NAME, f"Remove \"{r.name}\" from the list?\n\nIts saved password is "
                                   "deleted too. Nothing on the recorder changes.", parent=root):
            return
        store.recorders.remove(r)
        secrets.forget(r.id)
        store.save()
        refresh_table()

    open_btn.configure(command=open_selected)
    add_btn.configure(command=add)
    edit_btn.configure(command=edit)
    rm_btn.configure(command=remove)
    tree.bind("<Double-1>", open_selected)
    tree.bind("<Return>", open_selected)
    tree.bind("<Delete>", remove)
    tree.bind("<BackSpace>", remove)
    tree.bind("<<TreeviewSelect>>", update_buttons)

    def on_close():
        store.app_window = bool(appwin.get())
        store.geometry = root.geometry()
        try:
            store.save()
        except OSError:
            pass
        stop.set()
        threading.Thread(target=browser_obj.close, daemon=True).start()
        pool.shutdown(wait=False, cancel_futures=True)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_table()
    if not store.recorders:
        say("Recorders on this network appear here automatically. Use Add… for one at a known address.")
    if open_target:
        r = store.find(open_target)
        if r:
            root.after(300, lambda: open_recorder(r.url, r.name, r.id))
        else:
            try:
                url = normalize_url(open_target)
                root.after(300, lambda: open_recorder(url, short_address(url), None))
            except ValueError as e:
                messagebox.showerror(APP_NAME, str(e), parent=root)
    root.after(150, pump)
    root.mainloop()
    return 0


# ---------------------------------------------------------------- entry point
def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="airlog-viewer", description="Find and open Airlog recorders")
    ap.add_argument("--open", metavar="NAME_OR_URL", help="open this recorder straight away")
    ap.add_argument("--selftest", type=Path, metavar="REPORT", help=argparse.SUPPRESS)
    args = ap.parse_args([a for a in (sys.argv[1:] if argv is None else argv) if not a.startswith("-psn_")])
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    if args.selftest:
        sys.exit(selftest(args.selftest))
    sys.exit(run_gui(args.open))


def selftest(report: Path) -> int:
    import traceback
    lines: list[str] = []
    ok = True

    def check(name, fn, required=True):
        nonlocal ok
        try:
            lines.append(f"PASS  {name}  {fn() or ''}".rstrip())
        except Exception as e:  # noqa: BLE001
            if required:
                ok = False
            lines.append(f"{'FAIL' if required else 'WARN'}  {name}  {type(e).__name__}: {e}")
            if required:
                lines.append(traceback.format_exc())

    def tk_check():
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.withdraw()
        img = tk.PhotoImage(file=str(Path(__file__).with_name("assets") / "viewer.png"))
        ttk.Treeview(root, columns=("a",)).insert("", 0, text="x")
        v = f"Tk {tk.TkVersion}, icon {img.width()}x{img.height()}"
        root.destroy()
        return v

    def url_check():
        cases = {"studio": "http://studio:8080/", "10.0.0.5:9000": "http://10.0.0.5:9000/",
                 "http://h/x": "http://h:8080/", "https://air.example.org": "https://air.example.org:443/"}
        for given, want in cases.items():
            got = normalize_url(given)
            if got != want:
                raise AssertionError(f"{given} -> {got}, want {want}")
        return f"{len(cases)} address forms"

    def keyring_check():
        s = Secrets()
        if not s.available:
            raise RuntimeError(s.why)
        return f"backend {s.backend}"

    def zeroconf_check():
        import zeroconf
        from zeroconf import ServiceInfo
        ServiceInfo(discovery.SERVICE_TYPE, "Selftest." + discovery.SERVICE_TYPE, port=8080,
                    parsed_addresses=["127.0.0.1"], properties={"station": "x"})
        return f"zeroconf {zeroconf.__version__}"

    check("tkinter", tk_check)
    check("addresses", url_check)
    check("zeroconf", zeroconf_check)
    check("keyring module", lambda: __import__("keyring").__version__ if hasattr(__import__("keyring"), "__version__")
          else "imported")
    check("keychain", keyring_check, required=False)      # CI runners may have no keychain to talk to
    check("app window browser", lambda: app_window_browser() or "none (default browser)", required=False)
    lines.append("RESULT  " + ("OK" if ok else "FAILED"))
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
