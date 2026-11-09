"""exchange.py — exchange adapters (stdlib only).

MarketData      read-only interface the engine, screens and paper broker consume
BitgetPublic    Bitget v2 public REST for USDT-M futures (tickers, contracts, candles) — no keys
BitgetPrivate   signed Bitget v2 REST (demo via the `paptrading: 1` header, or live)
universe()      the tradable crypto universe: normal perpetuals, TradFi/RWA excluded

Candles are `Candle(ts, o, h, l, c, v)`: ts = open time (epoch ms, UTC), v = quote volume (USDT),
ascending. Bitget returns the still-forming candle last; callers drop it with `closed_only()`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import config
import timeutil as tu

Candle = namedtuple("Candle", "ts o h l c v")

# Bitget granularities. 6H/12H/1D/1W default to UTC+8 boundaries (= MYT); the *utc variants use UTC.
TF_MS: Dict[str, int] = {
    "1m": tu.MIN, "3m": 3 * tu.MIN, "5m": 5 * tu.MIN, "15m": 15 * tu.MIN, "30m": 30 * tu.MIN,
    "1H": tu.HOUR, "2H": 2 * tu.HOUR, "4H": 4 * tu.HOUR, "6H": 6 * tu.HOUR, "12H": 12 * tu.HOUR,
    "1D": tu.DAY, "1Dutc": tu.DAY, "1W": 7 * tu.DAY,
}


def tf_ms(tf: str) -> int:
    try:
        return TF_MS[tf]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe {tf!r}") from exc


def bucket(ts: int, tf: str) -> int:
    """Open time of the `tf` candle containing ts (Bitget boundaries: 1D on MYT midnight)."""
    if tf == "1D":
        return tu.midnight(ts)
    if tf == "1W":
        return tu.midnight(ts) - ((tu.myt(ts).weekday()) * tu.DAY)
    step = tf_ms(tf)
    return ts // step * step


def closed_only(candles: Sequence[Candle], tf: str, now_ms: int) -> List[Candle]:
    """Drop the forming candle(s): keep candles whose close time is at or before now."""
    step = tf_ms(tf)
    out = list(candles)
    while out and out[-1].ts + step > now_ms:
        out.pop()
    return out


@dataclass
class Ticker:
    symbol: str
    last: float
    change24h: float          # fraction (0.0123 = +1.23%)
    volume_usd: float         # 24h quote volume (USDT)
    funding: float            # current funding rate (fraction per interval)
    high24h: float = 0.0
    low24h: float = 0.0
    mark: float = 0.0


@dataclass
class Contract:
    symbol: str
    base: str
    quote: str = "USDT"
    price_place: int = 4
    price_end_step: int = 1
    volume_place: int = 3
    size_mult: float = 0.001
    min_qty: float = 0.0
    min_usdt: float = 5.0
    status: str = "normal"
    kind: str = "perpetual"
    is_rwa: bool = False
    max_lever: int = 20

    @property
    def tick(self) -> float:
        return self.price_end_step / (10 ** self.price_place)

    def round_price(self, p: float, mode: str = "nearest") -> float:
        t = self.tick
        f = {"down": math.floor, "up": math.ceil}.get(mode, round)
        return round(f(p / t + (1e-9 if mode == "down" else -1e-9 if mode == "up" else 0)) * t, self.price_place)

    def round_qty(self, q: float) -> float:
        step = self.size_mult if self.size_mult > 0 else 10 ** -self.volume_place
        return round(math.floor(q / step + 1e-9) * step, self.volume_place)


class ExchangeError(Exception):
    """Transport failure or a non-success Bitget response."""

    def __init__(self, msg: str, code: str = "", status: int = 0):
        super().__init__(msg)
        self.code = code
        self.status = status


class RateLimiter:
    """Thread-safe pacing: at most `rate` calls per second across all worker threads."""

    def __init__(self, rate: float):
        self.interval = 1.0 / max(rate, 0.1)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], dict]


def http_json(method: str, url: str, headers: Dict[str, str] | None = None, body: bytes | None = None,
              timeout: float = config.HTTP_TIMEOUT, retries: int = 3) -> dict:
    """JSON over HTTPS with bounded retries on 429/5xx and network errors."""
    hdrs = {"User-Agent": "AITradingTerminal/4", "Accept": "application/json"}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:400]
            last = ExchangeError(f"HTTP {exc.code} {url.split('?')[0]}: {text}", status=exc.code)
            try:
                payload = json.loads(text)
                last.code = str(payload.get("code", ""))
            except ValueError:
                pass
            if exc.code not in (429, 500, 502, 503, 504):
                raise last from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = ExchangeError(f"network error {url.split('?')[0]}: {exc}")
        if attempt < retries:
            time.sleep(min(8.0, 0.6 * 2 ** attempt))
    assert last is not None
    raise last


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class MarketData:
    """Read-only market interface. `now_ms()` is the data clock (wall clock live, simulated in replays)."""

    name = "base"
    finest_tf = "1m"          # finest candles available (paper R-ladder replays use it)

    def now_ms(self) -> int:
        return tu.now_ms()

    def tickers(self) -> List[Ticker]:
        raise NotImplementedError

    def contracts(self) -> Dict[str, Contract]:
        raise NotImplementedError

    def candles(self, symbol: str, tf: str, limit: int = 200) -> List[Candle]:
        raise NotImplementedError


class BitgetPublic(MarketData):
    """Bitget v2 public market data for USDT-M futures."""

    name = "bitget"
    finest_tf = "1m"

    def __init__(self, base_url: str = config.BITGET_BASE_URL, product: str = config.PRODUCT_TYPE,
                 rps: float = config.HTTP_RPS, transport: Transport | None = None):
        self.base = base_url.rstrip("/")
        self.product = product
        self.limiter = RateLimiter(rps)
        self.transport = transport or (lambda m, u, h, b: http_json(m, u, h, b))
        self._contracts: Tuple[float, Dict[str, Contract]] = (0.0, {})

    def _get(self, path: str, params: dict):
        self.limiter.wait()
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        data = self.transport("GET", url, {}, None)
        if str(data.get("code")) != "00000":
            raise ExchangeError(f"{path}: {data.get('code')} {data.get('msg')}", code=str(data.get("code")))
        return data.get("data")

    def tickers(self) -> List[Ticker]:
        rows = self._get("/api/v2/mix/market/tickers", {"productType": self.product}) or []
        out = []
        for r in rows:
            last = _f(r.get("lastPr"))
            if last <= 0:
                continue
            out.append(Ticker(
                symbol=r.get("symbol", ""), last=last, change24h=_f(r.get("change24h")),
                volume_usd=_f(r.get("usdtVolume")) or _f(r.get("quoteVolume")),
                funding=_f(r.get("fundingRate")), high24h=_f(r.get("high24h")), low24h=_f(r.get("low24h")),
                mark=_f(r.get("markPrice")) or last))
        return out

    def contracts(self) -> Dict[str, Contract]:
        cached_at, cached = self._contracts
        if cached and time.time() - cached_at < 3600:
            return cached
        rows = self._get("/api/v2/mix/market/contracts", {"productType": self.product}) or []
        out = {}
        for r in rows:
            sym = r.get("symbol", "")
            out[sym] = Contract(
                symbol=sym, base=r.get("baseCoin", sym[:-4]), quote=r.get("quoteCoin", "USDT"),
                price_place=int(_f(r.get("pricePlace"), 4)), price_end_step=int(_f(r.get("priceEndStep"), 1)) or 1,
                volume_place=int(_f(r.get("volumePlace"), 3)), size_mult=_f(r.get("sizeMultiplier"), 0.0),
                min_qty=_f(r.get("minTradeNum")), min_usdt=_f(r.get("minTradeUSDT"), 5.0),
                status=str(r.get("symbolStatus", "normal")), kind=str(r.get("symbolType", "perpetual")),
                is_rwa=str(r.get("isRwa", "NO")).upper() in ("YES", "TRUE", "1"),
                max_lever=int(_f(r.get("maxLever"), 20)))
        self._contracts = (time.time(), out)
        return out

    def candles(self, symbol: str, tf: str, limit: int = 200) -> List[Candle]:
        rows = self._get("/api/v2/mix/market/candles", {
            "symbol": symbol, "productType": self.product, "granularity": tf, "limit": min(int(limit), 1000)}) or []
        return _parse_candles(rows)

    def history(self, symbol: str, tf: str, start_ms: int, end_ms: int) -> List[Candle]:
        """Older candles via /history-candles (200 per page), paged backwards from end_ms."""
        out: Dict[int, Candle] = {}
        end = end_ms
        for _ in range(500):
            rows = self._get("/api/v2/mix/market/history-candles", {
                "symbol": symbol, "productType": self.product, "granularity": tf,
                "endTime": end, "limit": 200}) or []
            page = _parse_candles(rows)
            if not page:
                break
            for c in page:
                out[c.ts] = c
            first = page[0].ts
            if first <= start_ms or first >= end:
                break
            end = first - 1
        return [out[k] for k in sorted(out) if start_ms <= k <= end_ms]


def _parse_candles(rows) -> List[Candle]:
    out = []
    for r in rows:
        o, h, l, c = _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4])
        vol = _f(r[6]) if len(r) > 6 else _f(r[5]) * c
        out.append(Candle(int(r[0]), o, h, l, c, vol))
    out.sort(key=lambda x: x.ts)
    return out


# --- universe ----------------------------------------------------------------------------------

def is_tradfi(contract: Contract) -> bool:
    """TradFi/RWA contracts are excluded from every routine (framework: crypto only)."""
    base = contract.base.upper()
    if base in config.TRADFI_ALLOW:
        return False
    if contract.is_rwa or base in config.TRADFI_BASES or base in config.TRADFI_EXTRA:
        return True
    for suf in config.TRADFI_SUFFIXES:
        stem = base[: -len(suf)]
        if base.endswith(suf) and stem and (stem in config.TRADFI_BASES or stem in config.TRADFI_EXTRA):
            return True
    return False


def universe(md: MarketData, min_volume: float = 0.0) -> Tuple[List[Ticker], List[str]]:
    """Tradable crypto perpetuals (normal status), TradFi/RWA excluded. Returns (tickers, excluded)."""
    contracts = md.contracts()
    keep, excluded = [], []
    for t in md.tickers():
        c = contracts.get(t.symbol)
        if c is None or c.kind != "perpetual" or c.status != "normal" or not t.symbol.endswith("USDT"):
            continue
        if is_tradfi(c):
            excluded.append(t.symbol)
            continue
        if t.volume_usd >= min_volume:
            keep.append(t)
    keep.sort(key=lambda t: t.symbol)
    return keep, sorted(excluded)


# --- private (demo / live) ---------------------------------------------------------------------

def sign(secret: str, ts: str, method: str, path: str, query: str, body: str) -> str:
    """Bitget ACCESS-SIGN: base64(HMAC-SHA256(secret, ts + METHOD + path[?query] + body))."""
    prehash = f"{ts}{method.upper()}{path}{'?' + query if query else ''}{body}"
    return base64.b64encode(hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()).decode()


class BitgetPrivate:
    """Signed Bitget v2 REST. demo=True adds `paptrading: 1` (Bitget demo trading keys)."""

    def __init__(self, key: str, secret: str, passphrase: str, demo: bool = True,
                 base_url: str = config.BITGET_BASE_URL, legacy_demo: bool = config.BITGET_DEMO_LEGACY,
                 transport: Transport | None = None, rps: float = 8.0):
        if not (key and secret and passphrase):
            raise SystemExit("Bitget API key/secret/passphrase are required for demo/live mode (.env).")
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.demo = demo
        self.base = base_url.rstrip("/")
        self.legacy = demo and legacy_demo
        self.product = "SUSDT-FUTURES" if self.legacy else config.PRODUCT_TYPE
        self.margin_coin = "SUSDT" if self.legacy else config.MARGIN_COIN
        self.transport = transport or (lambda m, u, h, b: http_json(m, u, h, b, retries=1))
        self.limiter = RateLimiter(rps)

    # symbol mapping for legacy demo accounts: BTCUSDT ↔ SBTCSUSDT
    def xsym(self, symbol: str) -> str:
        return f"S{symbol[:-4]}SUSDT" if self.legacy and not symbol.startswith("S") else symbol

    def usym(self, symbol: str) -> str:
        if self.legacy and symbol.startswith("S") and symbol.endswith("SUSDT"):
            return symbol[1:-5] + "USDT"
        return symbol

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        self.limiter.wait()
        query = urllib.parse.urlencode(sorted((params or {}).items()))
        body_s = json.dumps(body, separators=(",", ":")) if body else ""
        ts = str(tu.now_ms())
        headers = {
            "ACCESS-KEY": self.key, "ACCESS-SIGN": sign(self.secret, ts, method, path, query, body_s),
            "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json", "locale": "en-US",
        }
        if self.demo:
            headers["paptrading"] = "1"
        url = f"{self.base}{path}{'?' + query if query else ''}"
        data = self.transport(method, url, headers, body_s.encode() if body_s else None)
        if str(data.get("code")) != "00000":
            raise ExchangeError(f"{path}: {data.get('code')} {data.get('msg')}", code=str(data.get("code")))
        return data.get("data")

    # account ---------------------------------------------------------------------------------
    def account(self, symbol: str) -> dict:
        return self.request("GET", "/api/v2/mix/account/account", {
            "symbol": self.xsym(symbol), "productType": self.product, "marginCoin": self.margin_coin}) or {}

    def accounts(self) -> list:
        return self.request("GET", "/api/v2/mix/account/accounts", {"productType": self.product}) or []

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.request("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol": self.xsym(symbol), "productType": self.product, "marginCoin": self.margin_coin,
            "leverage": str(leverage)})

    def set_margin_mode(self, symbol: str, mode: str) -> None:
        self.request("POST", "/api/v2/mix/account/set-margin-mode", body={
            "symbol": self.xsym(symbol), "productType": self.product, "marginCoin": self.margin_coin,
            "marginMode": mode})

    def positions(self) -> list:
        rows = self.request("GET", "/api/v2/mix/position/all-position", {
            "productType": self.product, "marginCoin": self.margin_coin}) or []
        for r in rows:
            r["symbol"] = self.usym(r.get("symbol", ""))
        return rows

    # orders ------------------------------------------------------------------------------------
    def place_market(self, symbol: str, side: str, size: str, *, hedge: bool = False, close: bool = False,
                     stop_loss: str | None = None, take_profit: str | None = None,
                     client_oid: str | None = None) -> dict:
        body = {"symbol": self.xsym(symbol), "productType": self.product, "marginMode": config.MARGIN_MODE,
                "marginCoin": self.margin_coin, "size": size, "side": side, "orderType": "market"}
        if hedge:
            body["tradeSide"] = "close" if close else "open"
        elif close:
            body["reduceOnly"] = "YES"
        if stop_loss:
            body["presetStopLossPrice"] = stop_loss
        if take_profit:
            body["presetStopSurplusPrice"] = take_profit
        if client_oid:
            body["clientOid"] = client_oid
        return self.request("POST", "/api/v2/mix/order/place-order", body=body) or {}

    def place_tpsl(self, symbol: str, plan_type: str, trigger_price: str, hold_side: str,
                   size: str | None = None, range_rate: str | None = None, client_oid: str | None = None) -> dict:
        body = {"marginCoin": self.margin_coin, "productType": self.product, "symbol": self.xsym(symbol),
                "planType": plan_type, "triggerPrice": trigger_price, "triggerType": "fill_price",
                "executePrice": "0", "holdSide": hold_side}
        if size:
            body["size"] = size
        if range_rate:
            body["rangeRate"] = range_rate
        if client_oid:
            body["clientOid"] = client_oid
        return self.request("POST", "/api/v2/mix/order/place-tpsl-order", body=body) or {}

    def modify_tpsl(self, symbol: str, order_id: str, trigger_price: str, size: str | None = None) -> dict:
        body = {"orderId": order_id, "marginCoin": self.margin_coin, "productType": self.product,
                "symbol": self.xsym(symbol), "triggerPrice": trigger_price, "triggerType": "fill_price",
                "executePrice": "0"}
        if size:
            body["size"] = size
        return self.request("POST", "/api/v2/mix/order/modify-tpsl-order", body=body) or {}

    def pending_tpsl(self, symbol: str) -> list:
        data = self.request("GET", "/api/v2/mix/order/orders-plan-pending", {
            "productType": self.product, "planType": "profit_loss", "symbol": self.xsym(symbol)}) or {}
        return data.get("entrustedList") or [] if isinstance(data, dict) else list(data)

    def cancel_plan(self, symbol: str, order_id: str, plan_type: str) -> None:
        self.request("POST", "/api/v2/mix/order/cancel-plan-order", body={
            "symbol": self.xsym(symbol), "productType": self.product, "marginCoin": self.margin_coin,
            "orderIdList": [{"orderId": order_id}], "planType": plan_type})

    def close_position(self, symbol: str, hold_side: str | None = None) -> dict:
        body = {"symbol": self.xsym(symbol), "productType": self.product}
        if hold_side:
            body["holdSide"] = hold_side
        return self.request("POST", "/api/v2/mix/order/close-positions", body=body) or {}

    # history ---------------------------------------------------------------------------------
    def fills(self, start_ms: int, end_ms: int, symbol: str | None = None) -> list:
        params = {"productType": self.product, "startTime": start_ms, "endTime": end_ms, "limit": 100}
        if symbol:
            params["symbol"] = self.xsym(symbol)
        data = self.request("GET", "/api/v2/mix/order/fill-history", params) or {}
        rows = data.get("fillList") or [] if isinstance(data, dict) else list(data)
        for r in rows:
            r["symbol"] = self.usym(r.get("symbol", ""))
        return rows

    def position_history(self, start_ms: int, end_ms: int, symbol: str | None = None) -> list:
        """Closed positions with Bitget's own net-of-fees numbers (netProfit, openFee, closeFee, totalFunding)."""
        out, id_less_than = [], None
        for _ in range(20):
            params = {"productType": self.product, "startTime": start_ms, "endTime": end_ms, "limit": 100}
            if symbol:
                params["symbol"] = self.xsym(symbol)
            if id_less_than:
                params["idLessThan"] = id_less_than
            data = self.request("GET", "/api/v2/mix/position/history-position", params) or {}
            rows = data.get("list") or [] if isinstance(data, dict) else list(data)
            for r in rows:
                r["symbol"] = self.usym(r.get("symbol", ""))
            out.extend(rows)
            id_less_than = data.get("endId") if isinstance(data, dict) else None
            if len(rows) < 100 or not id_less_than:
                break
        return out
