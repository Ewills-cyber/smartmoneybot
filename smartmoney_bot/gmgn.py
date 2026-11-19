"""GMGN OpenAPI client.

Signing matches GMGNAI/gmgn-skills `src/client/signer.ts` and `OpenApiClient.ts`
(main branch as of the implementation):

* Read routes send `X-APIKEY`. `timestamp` (Unix seconds, accepted within ±5s)
  and `client_id` (UUID, rejected if replayed within 7s) go in the query.
* Swap and order routes also send `X-Signature`.
* The signed string is `{sub_path}:{sorted_query}:{body}:{timestamp}`.
* Query keys are sorted. Values use `encodeURIComponent` (space is `%20`).
  Array values become repeated `key=value` pairs sorted by value.
* POST body is `JSON.stringify` of the object (no spaces, insertion order).
  A GET signs an empty body, which leaves `::` before the timestamp.
* Ed25519 signs the raw UTF-8 bytes with no extra hash. The header is
  standard base64. The private key never leaves this process.
* The request URL uses URLSearchParams order (not the sorted signature order)
  and form-encoding (space is `+`).

Host: `https://openapi.gmgn.ai`, from `src/config.ts`.
Swap route: POST `/v1/trade/swap`. Order status: GET `/v1/trade/query_order`.
Success is response `code === 0`, from `parseResponse`.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .http import redact

log = logging.getLogger(__name__)

HOST = "https://openapi.gmgn.ai"
SWAP_PATH = "/v1/trade/swap"
QUERY_ORDER_PATH = "/v1/trade/query_order"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000

# encodeURIComponent leaves these unescaped. URLSearchParams encodes the extra marks.
_URI_SAFE = "-_.!~*'()"
_FORM_SAFE = "-_.*"
_TERMINAL_OK = {"confirmed", "successful", "success"}
_TERMINAL_BAD = {"failed", "expired", "failure"}


class GmgnError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status: str = "failed"):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class SignedRequest:
    method: str
    url: str
    headers: dict
    body: str | None
    message: str


@dataclass
class TradeResult:
    ok: bool
    status: str
    order_id: str | None = None
    tx_hash: str | None = None
    strategy_order_id: str | None = None
    error: str | None = None
    occupies_slot: bool = False
    retryable: bool = False
    input_amount: str | None = None
    output_amount: str | None = None
    terminal: bool = False


def normalize_pem(raw: str | None) -> str | None:
    """Accept a PEM block or the same block with escaped newlines."""
    if not raw:
        return None
    text = raw.strip().strip('"').strip("'")
    if "\\n" in text:
        text = text.replace("\\n", "\n")
    if "BEGIN" not in text or "PRIVATE KEY" not in text:
        return None
    if not text.endswith("\n"):
        text += "\n"
    return text


def encode_uri_component(value: str) -> str:
    """Match JavaScript encodeURIComponent (space is %20)."""
    return quote(str(value), safe=_URI_SAFE)


def encode_form_component(value: str) -> str:
    """Match URLSearchParams / application/x-www-form-urlencoded (space is +)."""
    return quote(str(value), safe=_FORM_SAFE).replace("%20", "+")


def dumps_js(value) -> str:
    """Match JSON.stringify for the objects this bot sends: no spaces, stable key order."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def sorted_query(params: dict) -> str:
    """Canonical query string used inside the signature."""
    parts: list[str] = []
    for key in sorted(params):
        encoded_key = encode_uri_component(str(key))
        value = params[key]
        if isinstance(value, (list, tuple)):
            for item in sorted(str(piece) for piece in value):
                parts.append(f"{encoded_key}={encode_uri_component(item)}")
        else:
            parts.append(f"{encoded_key}={encode_uri_component(str(value))}")
    return "&".join(parts)


def build_message(sub_path: str, query: dict, body: str, timestamp: int) -> str:
    return f"{sub_path}:{sorted_query(query)}:{body}:{int(timestamp)}"


def request_url(host: str, sub_path: str, query: dict) -> str:
    """URLSearchParams insertion order, not the sorted signature order."""
    parts: list[str] = []
    for key, value in query.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                parts.append(f"{encode_form_component(str(key))}={encode_form_component(str(item))}")
        else:
            parts.append(f"{encode_form_component(str(key))}={encode_form_component(str(value))}")
    base = host.rstrip("/") + sub_path
    if not parts:
        return base
    return base + "?" + "&".join(parts)


def sign_message(message: str, private_key_pem: str) -> str:
    """Ed25519 over the raw UTF-8 message. Header value is standard base64."""
    key = load_pem_private_key(normalize_pem(private_key_pem).encode("utf-8"), password=None)
    signature = key.sign(message.encode("utf-8"))
    return base64.b64encode(signature).decode("ascii")


def build_request(
    *,
    method: str,
    sub_path: str,
    query_extra: dict | None,
    body: dict | None,
    api_key: str,
    private_key_pem: str | None,
    timestamp: int,
    client_id: str,
    host: str = HOST,
    sign: bool = True,
) -> SignedRequest:
    query = dict(query_extra or {})
    query["timestamp"] = int(timestamp)
    query["client_id"] = client_id
    body_str = dumps_js(body) if body is not None else ""
    message = build_message(sub_path, query, body_str, int(timestamp))
    headers = {
        "X-APIKEY": api_key,
        "Content-Type": "application/json",
        "User-Agent": "smartmoneybot/1.1.0",
    }
    if sign:
        if not private_key_pem:
            raise GmgnError("GMGN_PRIVATE_KEY is required to sign this request")
        headers["X-Signature"] = sign_message(message, private_key_pem)
    return SignedRequest(
        method=method.upper(),
        url=request_url(host, sub_path, query),
        headers=headers,
        body=body_str if body is not None else None,
        message=message,
    )


def lamports_for(sol_amount) -> str:
    from decimal import Decimal, ROUND_DOWN

    amount = (Decimal(str(sol_amount)) * Decimal(LAMPORTS)).to_integral_value(ROUND_DOWN)
    if amount <= 0:
        raise GmgnError("trade size is too small to buy")
    return str(amount)


def condition_orders(take_profit_multiple: float, stop_loss_multiple: float) -> list[dict]:
    """TP/SL sub-orders attached to a buy.

    `price_scale` follows the gmgn-swap skill: take-profit is the gain percent
    (`"150"` is +150%, a 2.5x), stop-loss is the drop percent (`"35"` triggers
    at 65% of entry). `sell_ratio` `"100"` sells the whole buy.
    """
    from decimal import Decimal, ROUND_HALF_UP

    gain = (Decimal(str(take_profit_multiple)) - 1) * 100
    drop = (Decimal("1") - Decimal(str(stop_loss_multiple))) * 100
    gain_text = str(int(gain.to_integral_value(ROUND_HALF_UP)))
    drop_text = str(int(drop.to_integral_value(ROUND_HALF_UP)))
    orders = []
    if int(gain_text) > 0:
        orders.append(
            {
                "order_type": "profit_stop",
                "side": "sell",
                "price_scale": gain_text,
                "sell_ratio": "100",
            }
        )
    if int(drop_text) > 0:
        orders.append(
            {
                "order_type": "loss_stop",
                "side": "sell",
                "price_scale": drop_text,
                "sell_ratio": "100",
            }
        )
    return orders


def build_buy_body(cfg: dict, output_token: str) -> dict:
    body = {
        "chain": "sol",
        "from_address": cfg["gmgn_wallet"],
        "input_token": SOL_MINT,
        "output_token": output_token,
        "input_amount": lamports_for(cfg["trade_size_sol"]),
    }
    _slippage(body, cfg)
    if cfg.get("gmgn_anti_mev", True):
        body["is_anti_mev"] = True
    orders = condition_orders(cfg["take_profit_multiple"], cfg["stop_loss_multiple"])
    if orders:
        # The swap skill requires both fees on SOL when condition_orders is set.
        body["priority_fee"] = str(cfg.get("gmgn_priority_fee") or "0.00001")
        body["tip_fee"] = str(cfg.get("gmgn_tip_fee") or "0.00001")
        body["condition_orders"] = orders
        body["sell_ratio_type"] = "buy_amount"
    return body


def build_sell_body(cfg: dict, input_token: str, percent: int = 100) -> dict:
    """Sell `percent` of the token. 100% is `input_amount_bps` 10000, matching the CLI."""
    bounded = max(1, min(100, int(percent)))
    body = {
        "chain": "sol",
        "from_address": cfg["gmgn_wallet"],
        "input_token": input_token,
        "output_token": SOL_MINT,
        "input_amount": "0",
        "input_amount_bps": str(bounded * 100),
    }
    _slippage(body, cfg)
    if cfg.get("gmgn_anti_mev", True):
        body["is_anti_mev"] = True
    return body


def _slippage(body: dict, cfg: dict) -> None:
    slippage = cfg.get("gmgn_slippage")
    if slippage is None:
        body["auto_slippage"] = True
        return
    body["slippage"] = int(slippage)


def parse_order(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        raise GmgnError("GMGN returned a response that was not an object")
    code = payload.get("code")
    if code != 0:
        message = payload.get("message") or payload.get("error") or f"code {code}"
        raise GmgnError(f"GMGN rejected the order: {message}")
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = {}
    return data


def result_from_order(data: dict, *, fallback: str = "pending") -> TradeResult:
    status = str(data.get("status") or fallback).lower()
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    error = data.get("error_status") or data.get("error_code") or None
    result = TradeResult(
        ok=status in _TERMINAL_OK,
        status=status,
        order_id=_text(data.get("order_id")),
        tx_hash=_text(data.get("hash") or data.get("tx_hash")),
        strategy_order_id=_text(data.get("strategy_order_id")),
        error=_text(error),
        input_amount=_text(report.get("input_amount") if report else None),
        output_amount=_text(report.get("output_amount") if report else None),
        terminal=status in _TERMINAL_OK or status in _TERMINAL_BAD,
        occupies_slot=status in _TERMINAL_OK or status in {"pending", "processed", ""},
    )
    if status in _TERMINAL_BAD:
        result.ok = False
        result.occupies_slot = False
        result.terminal = True
    if not result.order_id and not result.ok:
        result.occupies_slot = False
    return result


def entry_decision(cfg: dict, *, paused: bool, open_count: int, daily_loss: float) -> tuple[str, str]:
    """Decide whether a gate pass becomes a paper call, a live buy, or a skip."""
    from .paper import can_open

    mode = cfg.get("trade_mode", "paper")
    if paused:
        return "skip", "paused"
    if mode == "off":
        return "skip", "trading off"
    allowed, why = can_open(open_count, daily_loss, cfg)
    if not allowed:
        return "skip", why
    if mode == "live":
        if not cfg.get("gmgn_ready"):
            return "skip", "gmgn credentials missing"
        if not cfg.get("gmgn_live_storage_ok", True):
            return "skip", "live mode needs a persistent database path"
        return "live", "pass"
    return "paper", "pass"


def should_submit_sell(position: dict, action: dict) -> bool:
    """Live exits.

    Take-profit and stop-loss attached as condition_orders stay on GMGN.
    A time stop is not a condition order, so this bot sells that itself.
    A second sell is not submitted while an exit order id is already stored.
    """
    if action.get("type") != "close":
        return False
    if position.get("exit_order_id"):
        return False
    owner = position.get("exit_owner") or "bot"
    if owner == "gmgn" and action.get("reason") != "time_stop":
        return False
    return True


def _text(value) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


class GmgnClient:
    """Signs and sends swap / query_order calls. `transport` replaces HTTP in tests."""

    def __init__(
        self,
        cfg: dict,
        *,
        transport=None,
        clock=None,
        client_ids=None,
        sleeper=time.sleep,
        confirm_attempts: int | None = None,
        confirm_wait: float | None = None,
    ):
        self.cfg = cfg
        self.transport = transport or _default_transport
        self.clock = clock or time.time
        self._ids = iter(client_ids) if client_ids is not None else None
        self.sleeper = sleeper
        self.confirm_attempts = confirm_attempts or int(cfg.get("gmgn_confirm_attempts") or 3)
        self.confirm_wait = 5.0 if confirm_wait is None else confirm_wait

    def buy(self, token: str, symbol: str, size_sol: float) -> TradeResult:
        del symbol, size_sol  # size comes from cfg so the signed body matches TRADE_SIZE_SOL
        try:
            body = build_buy_body(self.cfg, token)
            data = self._signed("POST", SWAP_PATH, {}, body)
            return self._confirm(result_from_order(data))
        except GmgnError as exc:
            log.warning("gmgn buy failed: %s", redact(exc))
            return TradeResult(
                ok=False,
                status=exc.status,
                error=str(exc),
                retryable=exc.retryable,
                occupies_slot=False,
                terminal=not exc.retryable,
            )
        except (ValueError, TypeError) as exc:
            log.warning("gmgn buy rejected the signing key: %s", redact(exc))
            return TradeResult(ok=False, status="failed", error="signing key could not be read", retryable=False, terminal=True)

    def sell(self, token: str, symbol: str, reason: str) -> TradeResult:
        del symbol, reason
        try:
            body = build_sell_body(self.cfg, token, 100)
            data = self._signed("POST", SWAP_PATH, {}, body)
            return self._confirm(result_from_order(data))
        except GmgnError as exc:
            log.warning("gmgn sell failed: %s", redact(exc))
            return TradeResult(
                ok=False,
                status=exc.status,
                error=str(exc),
                retryable=exc.retryable,
                terminal=not exc.retryable,
            )
        except (ValueError, TypeError) as exc:
            log.warning("gmgn sell rejected the signing key: %s", redact(exc))
            return TradeResult(ok=False, status="failed", error="signing key could not be read", retryable=False, terminal=True)

    def refresh(self, order_id: str) -> TradeResult:
        try:
            data = self._signed("GET", QUERY_ORDER_PATH, {"order_id": order_id, "chain": "sol"}, None)
            return result_from_order(data)
        except GmgnError as exc:
            log.warning("gmgn order query failed: %s", redact(exc))
            return TradeResult(ok=False, status="pending", order_id=order_id, error=str(exc), retryable=True, occupies_slot=True)

    def _confirm(self, result: TradeResult) -> TradeResult:
        if result.terminal or not result.order_id:
            return result
        last = result
        for attempt in range(self.confirm_attempts):
            polled = self.refresh(result.order_id)
            if not polled.strategy_order_id:
                polled.strategy_order_id = result.strategy_order_id
            last = polled
            if last.terminal:
                return last
            if attempt + 1 < self.confirm_attempts:
                self.sleeper(self.confirm_wait)
        last.occupies_slot = True
        last.terminal = False
        return last

    def _signed(self, method: str, path: str, query_extra: dict, body: dict | None) -> dict:
        request = build_request(
            method=method,
            sub_path=path,
            query_extra=query_extra,
            body=body,
            api_key=self.cfg["gmgn_api_key"],
            private_key_pem=self.cfg.get("gmgn_private_key"),
            timestamp=int(self.clock()),
            client_id=self._client_id(),
            host=self.cfg.get("gmgn_host") or HOST,
            sign=True,
        )
        try:
            payload = self.transport(request)
        except GmgnError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GmgnError(f"GMGN request failed: {redact(exc)}", retryable=True, status="pending") from exc
        return parse_order(payload)

    def _client_id(self) -> str:
        if self._ids is not None:
            return next(self._ids)
        return str(uuid.uuid4())


def _default_transport(request: SignedRequest) -> dict:
    import requests

    try:
        response = requests.request(
            request.method,
            request.url,
            headers=request.headers,
            data=request.body.encode("utf-8") if request.body is not None else None,
            timeout=20,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise GmgnError(f"GMGN network error: {redact(exc)}", retryable=True, status="pending") from exc
    if 300 <= response.status_code < 400:
        raise GmgnError(f"GMGN redirected the request (HTTP {response.status_code})", retryable=False)
    try:
        payload = response.json()
    except ValueError as exc:
        raise GmgnError(f"GMGN returned HTTP {response.status_code} without JSON", retryable=response.status_code >= 500) from exc
    if response.status_code == 429:
        raise GmgnError("GMGN rate limited the request", retryable=False, status="failed")
    return payload
