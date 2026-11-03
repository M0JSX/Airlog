"""Finding recorders on the LAN with mDNS / DNS-SD (Bonjour).

The recorder advertises  _airlog._tcp.local.  with TXT records station / version / auth;
the viewer browses for it. Both use the `zeroconf` package and quietly do nothing without it.
"""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Callable

SERVICE_TYPE = "_airlog._tcp.local."


def available() -> bool:
    try:
        import zeroconf  # noqa: F401
        return True
    except Exception:
        return False


def _lan_ipv4() -> list[str]:
    """This machine's IPv4 addresses, without loopback or link-local."""
    ips: set[str] = set()
    try:
        import ifaddr                      # installed with zeroconf
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                    ips.add(ip.ip)
    except Exception:
        pass
    if not ips:                            # fallback: the address used for the default route
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))   # TEST-NET: nothing is sent
                ips.add(s.getsockname()[0])
        except OSError:
            pass
    return sorted(ips)


def _hostname() -> str:
    return socket.gethostname().split(".")[0] or "airlog"


# ---------------------------------------------------------------- recorder side
class Advertiser:
    """Announces this recorder. update() re-announces after a settings change."""

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.zc = None
        self.info = None
        self.lock = threading.Lock()

    def update(self, station: str, port: int, version: str, password: bool, enabled: bool = True) -> None:
        threading.Thread(target=self._update, args=(station, port, version, password, enabled),
                         daemon=True, name="mdns").start()

    def _update(self, station, port, version, password, enabled) -> None:
        with self.lock:
            try:
                from zeroconf import IPVersion, ServiceInfo, Zeroconf
            except Exception:
                return
            try:
                if self.zc is None and enabled:
                    self.zc = Zeroconf(ip_version=IPVersion.V4Only)
                if self.info is not None:
                    self.zc.unregister_service(self.info)
                    self.info = None
                if not enabled:
                    if self.zc is not None:
                        self.zc.close()
                        self.zc = None
                    return
                host = _hostname()
                label = f"{station} on {host}"[:63].replace(".", " ")
                self.info = ServiceInfo(
                    SERVICE_TYPE, f"{label}.{SERVICE_TYPE}", port=int(port),
                    parsed_addresses=_lan_ipv4() or ["127.0.0.1"], server=f"{host}.local.",
                    properties={"station": station, "version": version, "path": "/",
                                "auth": "password" if password else "open"})
                self.zc.register_service(self.info, allow_name_change=True)
                self.log(f"Announcing on the network as \"{label}\" (port {port})")
            except Exception as e:           # no multicast route, port 5353 blocked…
                self.log(f"Network announcement unavailable: {type(e).__name__}: {e}")

    def close(self) -> None:
        with self.lock:
            try:
                if self.zc is not None:
                    if self.info is not None:
                        self.zc.unregister_service(self.info)
                    self.zc.close()
            except Exception:
                pass
            self.zc = self.info = None


# ---------------------------------------------------------------- viewer side
@dataclass
class Found:
    key: str              # mDNS instance name
    station: str
    host: str             # address to connect to
    port: int
    version: str
    password: bool

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


class Browser:
    """Calls on_change(list[Found]) from a background thread whenever the set changes."""

    def __init__(self, on_change: Callable[[list[Found]], None]):
        self.on_change = on_change
        self.found: dict[str, Found] = {}
        self.zc = None
        self.browser = None
        self.error = ""

    def start(self) -> bool:
        try:
            from zeroconf import IPVersion, ServiceBrowser, Zeroconf
            self.zc = Zeroconf(ip_version=IPVersion.V4Only)
            self.browser = ServiceBrowser(self.zc, SERVICE_TYPE, handlers=[self._event])
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}" if available() else "zeroconf not installed"
            return False

    def _event(self, zeroconf, service_type, name, state_change) -> None:
        from zeroconf import ServiceStateChange
        if state_change is ServiceStateChange.Removed:
            if self.found.pop(name, None) is not None:
                self.on_change(list(self.found.values()))
            return
        threading.Thread(target=self._resolve, args=(name,), daemon=True).start()

    def _resolve(self, name: str) -> None:
        try:
            info = self.zc.get_service_info(SERVICE_TYPE, name, timeout=3000)
        except Exception:
            info = None
        if info is None:
            return
        props = {k.decode(errors="replace"): (v or b"").decode(errors="replace")
                 for k, v in (info.properties or {}).items()}
        addrs = info.parsed_addresses()
        if not addrs:
            return
        self.found[name] = Found(key=name, station=props.get("station") or name.split(".")[0],
                                 host=addrs[0], port=int(info.port), version=props.get("version", ""),
                                 password=props.get("auth") == "password")
        self.on_change(list(self.found.values()))

    def close(self) -> None:
        try:
            if self.zc is not None:
                self.zc.close()
        except Exception:
            pass
