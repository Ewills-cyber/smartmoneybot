"""HTTP helper with backoff for rate limits and transient failures."""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

_SECRET_QUERY = {"api-key", "api_key", "apikey", "key", "token"}
_BOT_PATH = re.compile(r"/bot[^/?#]+")
_PEM_BLOCK = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def redact(text: str) -> str:
    """Remove bot tokens, API keys, signatures, and PEM blocks from a log line."""
    cleaned = _BOT_PATH.sub("/bot<redacted>", str(text))
    cleaned = _PEM_BLOCK.sub("-----BEGIN PRIVATE KEY-----REDACTED-----END PRIVATE KEY-----", cleaned)
    cleaned = re.sub(
        r"(api-key|api_key|apikey|key|token)=([^&\s]+)",
        r"\1=REDACTED",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"((?:X-APIKEY|X-Signature)['\"]?\s*[:=]\s*)\S+",
        r"\1REDACTED",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def safe_url(url: str) -> str:
    """Drop query secrets before a URL is written to a log."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "invalid-url"
    path = _BOT_PATH.sub("/bot<redacted>", parts.path)
    query = [
        (key, "REDACTED" if key.lower() in _SECRET_QUERY else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def request_json(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    retries: int = 3,
    timeout: float = 20,
    sleeper=time.sleep,
    **kwargs,
):
    """Return parsed JSON. Retries network errors, HTTP 429, and HTTP 5xx."""
    sess = session or requests.Session()
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = sess.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last = exc
            delay = min(30.0, 1.5 * (2**attempt))
            log.warning("%s %s failed (%s); retry in %.1fs", method, safe_url(url), redact(exc), delay)
            sleeper(delay)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            delay = _retry_delay(response, attempt)
            last = ApiError(f"HTTP {response.status_code} from {safe_url(url)}", response.status_code)
            log.warning("HTTP %s from %s; backing off %.1fs", response.status_code, safe_url(url), delay)
            sleeper(delay)
            continue
        if response.status_code >= 400:
            raise ApiError(f"HTTP {response.status_code} from {safe_url(url)}", response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"invalid JSON from {safe_url(url)}") from exc
    raise ApiError(f"gave up on {safe_url(url)}: {redact(last)}")


def _retry_delay(response: requests.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After") if response.headers else None
    if header:
        try:
            return min(60.0, max(0.5, float(header)))
        except ValueError:
            pass
    return min(30.0, 2.0 * (2**attempt))
