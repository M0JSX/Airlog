"""An Icecast-compatible input: encoders push a stream *to* Airlog (like Liquidsoap's harbor).

BUTT, Mixxx, OBS, ffmpeg, another Airlog's Icecast output, or a playout system's "stream out"
connect with the source protocol (HTTP PUT, or the legacy SOURCE method) and any mount point.
One sender at a time; the source password is required. Whatever format arrives (MP3, AAC, Ogg,
Opus, FLAC) is decoded by the engine's ffmpeg from a pipe.
"""
from __future__ import annotations

import base64
import hmac
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

FORMAT_HINTS = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/aac": "aac", "audio/aacp": "aac",
                "audio/x-aac": "aac", "audio/flac": "flac", "audio/x-flac": "flac"}


@dataclass
class Sender:
    sock: socket.socket
    peer: str
    content_type: str
    chunked: bool
    pending: bytes            # body bytes received with the headers

    @property
    def format_hint(self) -> str | None:
        return FORMAT_HINTS.get(self.content_type.split(";")[0].strip().lower())

    def read_body(self):
        """Yield the raw stream bytes (de-chunked if needed) until the sender goes away."""
        self.sock.settimeout(20)          # a sender that stalls for 20 s is treated as gone
        if not self.chunked:
            if self.pending:
                yield self.pending
            while True:
                try:
                    data = self.sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                yield data
        buf = self.pending
        while True:
            while b"\r\n" not in buf:
                more = self._recv()
                if not more:
                    return
                buf += more
            size_line, _, buf = buf.partition(b"\r\n")
            try:
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                return
            if size == 0:
                return
            while len(buf) < size + 2:
                more = self._recv()
                if not more:
                    if buf:
                        yield buf[:size]
                    return
                buf += more
            yield buf[:size]
            buf = buf[size + 2:]

    def _recv(self) -> bytes:
        try:
            return self.sock.recv(65536)
        except OSError:
            return b""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class PushServer:
    def __init__(self, port: int, password: str, log: Callable[[str], None]):
        self.port = port
        self.password = password
        self.log = log
        self.senders: "queue.Queue[Sender]" = queue.Queue(maxsize=1)
        self.busy = threading.Event()        # set while a sender is being recorded
        self.current_peer = ""
        self.connected_at = 0.0
        self.closed = False
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.srv.bind(("0.0.0.0", port))
        except OSError as e:
            self.srv.close()
            raise OSError(f"can't listen on port {port}: {e.strerror or e}") from None
        self.srv.listen(4)
        # accept() wakes every half second to check `closed`, and the accept thread closes the socket
        # itself. Closing a socket from another thread while accept() blocks on it doesn't wake the
        # accept, and the port can stay listening (answering, with this server's replies) afterwards.
        self.srv.settimeout(0.5)
        self.thread = threading.Thread(target=self._accept_loop, name="receive-accept", daemon=True)
        self.thread.start()

    def close(self) -> None:
        """Stop listening. Returns once the port is free (a restart may listen on it again at once)."""
        self.closed = True
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def next_sender(self, timeout: float) -> Sender | None:
        try:
            return self.senders.get(timeout=timeout)
        except queue.Empty:
            return None

    def _accept_loop(self) -> None:
        try:
            while not self.closed:
                try:
                    conn, addr = self.srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.closed:
                        return
                    time.sleep(0.5)
                    continue
                if self.closed:                      # arrived as we were closing: not ours to answer
                    conn.close()
                    return
                conn.settimeout(None)
                threading.Thread(target=self._handshake, args=(conn, addr[0]), daemon=True).start()
        finally:
            try:
                self.srv.close()
            except OSError:
                pass

    def _reply(self, conn: socket.socket, status: str, text: str = "", extra: str = "") -> None:
        body = text.encode()
        try:
            conn.sendall((f"HTTP/1.0 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n{extra}"
                          f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _handshake(self, conn: socket.socket, peer: str) -> None:
        conn.settimeout(10)
        buf = b""
        try:
            while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
                got = conn.recv(4096)
                if not got:
                    conn.close()
                    return
                buf += got
                if len(buf) > 16384:
                    return self._reply(conn, "400 Bad Request", "Headers too large\n")
        except OSError:
            conn.close()
            return
        sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
        head, _, rest = buf.partition(sep)
        lines = head.decode("latin-1").replace("\r\n", "\n").split("\n")
        parts = lines[0].split()
        method = parts[0].upper() if parts else ""
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

        if method in ("GET", "HEAD"):
            state = f"receiving from {self.current_peer}" if self.busy.is_set() else "waiting for a sender"
            return self._reply(conn, "200 OK", f"Airlog stream input: {state}.\n"
                               "Send with the Icecast source protocol (PUT or SOURCE) to any mount.\n")
        if method not in ("PUT", "SOURCE", "POST"):
            return self._reply(conn, "405 Method Not Allowed", "Use the Icecast source protocol (PUT or SOURCE)\n")
        if not self.password:
            return self._reply(conn, "403 Forbidden", "Set a receive password in Airlog first\n")
        given = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                given = base64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")[2]
            except Exception:
                given = ""
        if not hmac.compare_digest(given.encode(), self.password.encode()):
            time.sleep(1)
            self.log(f"Stream input: wrong password from {peer}")
            return self._reply(conn, "401 Unauthorized", "Wrong source password\n",
                               'WWW-Authenticate: Basic realm="Airlog"\r\n')
        if self.busy.is_set() or self.senders.full():
            return self._reply(conn, "403 Forbidden", "Mountpoint in use\n")

        try:
            if headers.get("expect", "").lower() == "100-continue":
                conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            else:
                conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        except OSError:
            conn.close()
            return
        sender = Sender(conn, peer, headers.get("content-type", ""),
                        "chunked" in headers.get("transfer-encoding", "").lower(), rest)
        try:
            self.senders.put_nowait(sender)
        except queue.Full:
            sender.close()
