"""DexScreener token-profile websocket, with reconnect backoff.

REST polling remains the source of truth. The stream only adds addresses the
next scan should look at. The documented handshake is
``{"limit": int, "data": [profiles...]}``. Later messages are accepted when
they look like a profile, a list of profiles, or another handshake.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict, deque

log = logging.getLogger(__name__)

WS_URL = "wss://api.dexscreener.com/token-profiles/latest/v1"


def backoff_seconds(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    return min(cap, base * (2 ** min(max(attempt, 0), 6)))


def profiles_from_message(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if payload.get("tokenAddress"):
            return [payload]
    return []


class ProfileStream:
    def __init__(self, url: str = WS_URL, chain: str = "solana", max_items: int = 200):
        self.url = url
        self.chain = chain
        self._addresses: deque[str] = deque(maxlen=max_items)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.state = "stopped"
        self.reconnects = 0
        self._thread: threading.Thread | None = None

    def ingest(self, payload) -> int:
        added = 0
        for row in profiles_from_message(payload):
            if str(row.get("chainId") or "").lower() != self.chain:
                continue
            address = row.get("tokenAddress")
            if not address:
                continue
            with self._lock:
                if address in self._seen:
                    continue
                self._seen[address] = None
                while len(self._seen) > 2000:
                    self._seen.popitem(last=False)
                self._addresses.append(address)
                added += 1
        return added

    def drain(self) -> list[str]:
        with self._lock:
            items = list(self._addresses)
            self._addresses.clear()
        return items

    def run(self) -> None:
        try:
            import websocket
        except ImportError:
            self.state = "unavailable"
            log.warning("websocket-client is not installed; profile stream disabled")
            return
        attempt = 0
        while not self._stop.is_set():
            ws = None
            try:
                self.state = "connecting"
                ws = websocket.create_connection(self.url, timeout=20)
                self.state = "connected"
                attempt = 0
                log.info("dexscreener profile stream connected")
                while not self._stop.is_set():
                    ws.settimeout(20)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("profile stream ignored a non-JSON message")
                        continue
                    added = self.ingest(payload)
                    if added:
                        log.info("profile stream queued %s solana token(s)", added)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.state = "reconnecting"
                self.reconnects += 1
                delay = backoff_seconds(attempt)
                attempt += 1
                log.warning("profile stream dropped (%s); reconnecting in %.0fs", exc, delay)
                self._stop.wait(delay)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        log.debug("profile stream close failed", exc_info=True)
                if self.state == "connected":
                    self.state = "reconnecting"
        self.state = "stopped"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="dex-profile-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.state = "stopped"


_stream: ProfileStream | None = None
_guard = threading.Lock()


def get_stream() -> ProfileStream | None:
    return _stream


def start_stream(url: str = WS_URL) -> ProfileStream:
    global _stream
    with _guard:
        if _stream is None:
            _stream = ProfileStream(url)
            _stream.start()
        return _stream


def stop_stream() -> None:
    global _stream
    with _guard:
        if _stream is not None:
            _stream.stop()
