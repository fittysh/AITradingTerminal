"""sim.py — clocks and replayable markets.

SimClock          settable data clock; with no time set it follows the wall clock
SeriesMarket      serves candles/tickers from stored series *as of the clock* — no lookahead:
                  only fully closed 5m bars are visible, and every higher-TF forming candle is
                  rebuilt from those bars
SimMarket         deterministic synthetic Bitget-like USDT-M market (regime drift, volatility
                  clustering, market beta, scripted stop-hunts). Offline demo, tests, CI.
HistoricalMarket  real Bitget history (cached on disk) behind the same interface, for replays
"""
from __future__ import annotations

import bisect
import json
import math
import random
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import timeutil as tu
from exchange import BitgetPublic, Candle, Contract, MarketData, Ticker, bucket, tf_ms

BASE_TF = "5m"
BASE_MS = 5 * tu.MIN
BARS_PER_DAY = tu.DAY // BASE_MS


class SimClock:
    """Callable clock. `set(ms)` pins simulated time; unset it follows the wall clock."""

    def __init__(self, ms: Optional[int] = None):
        self.ms = ms

    def __call__(self) -> int:
        return self.ms if self.ms is not None else tu.now_ms()

    def set(self, ms: int) -> None:
        self.ms = int(ms)


def aggregate(bars: Sequence[Candle], tf: str) -> List[Candle]:
    out: List[Candle] = []
    cur: Optional[list] = None
    for b in bars:
        k = bucket(b.ts, tf)
        if cur is None or k != cur[0]:
            if cur is not None:
                out.append(Candle(*cur))
            cur = [k, b.o, b.h, b.l, b.c, b.v]
        else:
            cur[2] = max(cur[2], b.h)
            cur[3] = min(cur[3], b.l)
            cur[4] = b.c
            cur[5] += b.v
    if cur is not None:
        out.append(Candle(*cur))
    return out


class SeriesMarket(MarketData):
    """Clock-bound market over stored series. Subclasses provide the data."""

    finest_tf = BASE_TF

    def __init__(self, clock: SimClock):
        self.clock = clock
        self._base: Dict[str, Tuple[List[int], List[Candle], List[float]]] = {}
        self._tfs: Dict[Tuple[str, str], Tuple[List[int], List[Candle]]] = {}
        self._tick_cache: Tuple[int, List[Ticker]] = (-1, [])

    # -- subclass hooks --
    def symbols(self) -> List[str]:
        raise NotImplementedError

    def _load_base(self, sym: str) -> List[Candle]:
        raise NotImplementedError

    def _load_tf(self, sym: str, tf: str) -> Optional[List[Candle]]:
        return None

    def funding(self, sym: str, now: int) -> float:
        return 0.0

    # -- MarketData --
    def now_ms(self) -> int:
        return self.clock()

    def _series(self, sym: str):
        if sym not in self._base:
            bars = self._load_base(sym)
            pre = [0.0]
            for b in bars:
                pre.append(pre[-1] + b.v)
            self._base[sym] = ([b.ts for b in bars], bars, pre)
        return self._base[sym]

    def _tf_series(self, sym: str, tf: str):
        key = (sym, tf)
        if key not in self._tfs:
            bars = self._load_tf(sym, tf)
            if bars is None:
                bars = aggregate(self._series(sym)[1], tf)
            self._tfs[key] = ([b.ts for b in bars], bars)
        return self._tfs[key]

    def candles(self, symbol: str, tf: str, limit: int = 200) -> List[Candle]:
        if symbol not in self.symbols():
            return []
        now = self.now_ms()
        ts, bars, _ = self._series(symbol)
        idx = bisect.bisect_right(ts, now - BASE_MS)          # bars[:idx] are closed
        if idx == 0:
            return []
        if tf_ms(tf) <= BASE_MS:                             # finer requests are served at 5m
            return bars[max(0, idx - limit): idx]
        cur = bucket(now, tf)
        tts, tbars = self._tf_series(symbol, tf)
        j = bisect.bisect_left(tts, cur)                      # tbars[:j] closed before `cur`
        out = tbars[max(0, j - limit): j]
        part = bars[bisect.bisect_left(ts, cur): idx]
        if part:
            forming = Candle(cur, part[0].o, max(b.h for b in part), min(b.l for b in part), part[-1].c,
                             sum(b.v for b in part))
            out = (out[-(limit - 1):] if limit > 1 else []) + [forming]
        return out

    def tickers(self) -> List[Ticker]:
        now = self.now_ms()
        if self._tick_cache[0] == now:
            return list(self._tick_cache[1])
        out = []
        for sym in self.symbols():
            ts, bars, pre = self._series(sym)
            idx = bisect.bisect_right(ts, now - BASE_MS)
            if idx == 0:
                continue
            j = max(0, idx - BARS_PER_DAY)
            window = bars[j:idx]
            last = bars[idx - 1].c
            out.append(Ticker(sym, last, last / window[0].o - 1.0, pre[idx] - pre[j], self.funding(sym, now),
                              max(b.h for b in window), min(b.l for b in window), last))
        self._tick_cache = (now, out)
        return list(out)


# --- synthetic market ------------------------------------------------------------------------------

# symbol, reference price, 5m sigma, 24h volume (USD), market beta, TradFi/RWA flag
UNIVERSE: Tuple[Tuple[str, float, float, float, float, bool], ...] = (
    ("BTCUSDT", 64000, 0.0016, 2.8e9, 0.95, False), ("ETHUSDT", 2600, 0.0020, 1.4e9, 0.85, False),
    ("SOLUSDT", 148, 0.0028, 5.2e8, 0.75, False), ("XRPUSDT", 0.58, 0.0026, 3.1e8, 0.70, False),
    ("DOGEUSDT", 0.121, 0.0032, 2.6e8, 0.70, False), ("BNBUSDT", 560, 0.0017, 1.2e8, 0.70, False),
    ("ADAUSDT", 0.36, 0.0028, 9.0e7, 0.70, False), ("AVAXUSDT", 26.4, 0.0032, 8.0e7, 0.70, False),
    ("LINKUSDT", 11.6, 0.0030, 7.0e7, 0.70, False), ("SUIUSDT", 1.62, 0.0040, 2.1e8, 0.60, False),
    ("TONUSDT", 5.4, 0.0026, 5.0e7, 0.50, False), ("DOTUSDT", 4.3, 0.0029, 4.0e7, 0.70, False),
    ("NEARUSDT", 4.6, 0.0034, 5.0e7, 0.65, False), ("APTUSDT", 8.2, 0.0035, 4.0e7, 0.65, False),
    ("ARBUSDT", 0.55, 0.0035, 4.0e7, 0.65, False), ("OPUSDT", 1.55, 0.0036, 3.5e7, 0.65, False),
    ("INJUSDT", 21.0, 0.0038, 3.0e7, 0.60, False), ("TIAUSDT", 5.2, 0.0042, 3.0e7, 0.55, False),
    ("SEIUSDT", 0.33, 0.0044, 3.0e7, 0.55, False), ("WIFUSDT", 1.9, 0.0058, 1.2e8, 0.50, False),
    ("PEPEUSDT", 0.0000085, 0.0050, 1.5e8, 0.55, False), ("ORDIUSDT", 32.0, 0.0047, 4.0e7, 0.50, False),
    ("FETUSDT", 1.35, 0.0044, 6.0e7, 0.55, False), ("RENDERUSDT", 5.1, 0.0042, 3.0e7, 0.60, False),
    ("JUPUSDT", 0.85, 0.0044, 2.5e7, 0.55, False), ("ENAUSDT", 0.45, 0.0052, 6.0e7, 0.50, False),
    ("LTCUSDT", 68.0, 0.0024, 4.0e7, 0.70, False), ("BCHUSDT", 340, 0.0026, 3.0e7, 0.70, False),
    ("AAVEUSDT", 145, 0.0032, 3.0e7, 0.65, False), ("HYPEUSDT", 24.0, 0.0047, 1.0e8, 0.45, False),
    ("ZKJUSDT", 0.9, 0.0050, 6.0e5, 0.30, False),          # below the $1M routine-1 volume floor
    ("XAUUSDT", 2650, 0.0006, 1.8e8, 0.10, True),           # TradFi: excluded everywhere
    ("TSLAUSDT", 245, 0.0018, 4.0e7, 0.20, True),
    ("NVDAUSDT", 120, 0.0019, 3.0e7, 0.20, True),
)

HUNT_P = 1 / 480          # chance per 5m bar that a stop-hunt script starts (≈0.6/day/coin)
SIGMA_SCALE = 0.6         # calibrates the table's sigmas to realistic daily ranges (BTC ≈ 3%)
SIM_EPOCH = tu.parse_date("2024-01-01")    # every synthetic series is anchored here (MYT midnight)
MEAN_REVERSION = 1 / 120                     # daily pull of log-price toward the reference price


def _rng(*key) -> random.Random:
    return random.Random(zlib.crc32(":".join(map(str, key)).encode()))


class SimMarket(SeriesMarket):
    """Deterministic synthetic market, addressable by absolute time.

    A daily anchor walk runs from a fixed epoch (mean-reverting log price, market beta, regime drift,
    volatility clustering). Each MYT day is then an independent 5-minute Brownian bridge between that
    day's two anchors, seeded by (symbol, day), with scripted stop hunts. So any two instances with the
    same seed agree on every candle whatever window they load, and a live --sim run can extend forever.
    """

    name = "sim"

    def __init__(self, clock: Optional[SimClock] = None, seed: int = 7, start_ms: Optional[int] = None,
                 end_ms: Optional[int] = None, days: int = 75, spec=UNIVERSE):
        clock = clock or SimClock()
        super().__init__(clock)
        self.seed = seed
        self.spec = {s[0]: s for s in spec}
        self.auto_extend = end_ms is None
        now = clock()
        self.end = tu.midnight(end_ms or (now + 2 * tu.DAY)) + tu.DAY
        self.start = max(SIM_EPOCH + tu.DAY, tu.midnight(start_ms or (self.end - days * tu.DAY)))
        self._daily: Dict[str, tuple] = {}
        self._mkt_days: List[float] = []

    def symbols(self) -> List[str]:
        return list(self.spec)

    def contracts(self) -> Dict[str, Contract]:
        out = {}
        for sym, p0, *_rest in self.spec.values():
            mag = math.floor(math.log10(p0))
            vp = max(0, min(6, mag + 2))
            out[sym] = Contract(symbol=sym, base=sym[:-4], price_place=max(1, min(10, 5 - mag)),
                                volume_place=vp, size_mult=10 ** -vp, min_qty=10 ** -vp, min_usdt=5.0,
                                is_rwa=bool(_rest[-1]))
        return out

    def funding(self, sym: str, now: int) -> float:
        period = now // (8 * tu.HOUR)
        h = zlib.crc32(f"{sym}:{period}:{self.seed}".encode()) / 0xFFFFFFFF
        return round(0.0001 + 0.0004 * (h - 0.35), 6)

    # -- horizon ---------------------------------------------------------------------------------------
    def _maybe_extend(self) -> None:
        now = self.clock()
        if self.auto_extend and now > self.end - tu.DAY:
            self.end = tu.midnight(now + 2 * tu.DAY) + tu.DAY
            self._base.clear()
            self._tfs.clear()
            self._tick_cache = (-1, [])

    def candles(self, symbol: str, tf: str, limit: int = 200) -> List[Candle]:
        self._maybe_extend()
        return super().candles(symbol, tf, limit)

    def tickers(self) -> List[Ticker]:
        self._maybe_extend()
        return super().tickers()

    # -- generation ------------------------------------------------------------------------------------
    def _market(self, n: int) -> List[float]:
        if len(self._mkt_days) < n:
            rng = _rng("mkt-daily", self.seed)
            self._mkt_days = [rng.gauss(0.0, 1.0) for _ in range(n + 64)]
        return self._mkt_days

    def _anchors(self, sym: str, upto: int) -> tuple:
        """(log price at each MYT midnight since the epoch, per-day volatility multiplier)."""
        have = self._daily.get(sym)
        if have and len(have[0]) > upto + 1:
            return have
        _, p0, sigma, _, beta, _ = self.spec[sym]
        sd = sigma * SIGMA_SCALE * math.sqrt(BARS_PER_DAY)
        idio = math.sqrt(max(0.0, 1.0 - beta * beta))
        n = upto + 64
        mkt = self._market(n)
        rng = _rng("daily", sym, self.seed)
        ref = math.log(p0)
        x = ref + rng.gauss(0.0, 0.1)
        v, drift, left = 1.0, 0.0, 0
        anchors, vols = [x], []
        for d in range(n):
            if left <= 0:
                left = rng.randint(2, 7)
                drift = rng.choice((-1, -1, 0, 0, 0, 1, 1)) * sd * rng.uniform(0.2, 0.6)
            left -= 1
            v = min(2.2, max(0.55, v + 0.15 * (1.0 - v) + 0.18 * rng.gauss(0.0, 1.0)))
            vols.append(v)
            x += drift + MEAN_REVERSION * (ref - x) + sd * v * (beta * mkt[d] + idio * rng.gauss(0.0, 1.0))
            anchors.append(x)
        self._daily[sym] = (anchors, vols)
        return anchors, vols

    def _day(self, sym: str, d: int) -> List[Candle]:
        anchors, vols = self._anchors(sym, d + 1)
        a, b, v = anchors[d], anchors[d + 1], vols[d]
        _, _, sigma, vol24, beta, _ = self.spec[sym]
        s = sigma * SIGMA_SCALE * v
        idio = math.sqrt(max(0.0, 1.0 - beta * beta))
        rng = _rng("day", sym, d, self.seed)
        mrng = _rng("mkt-bars", d, self.seed)
        mkt = [mrng.gauss(0.0, 1.0) for _ in range(BARS_PER_DAY)]
        walk, eps_l, vm_l = [0.0], [], []
        queue: List[Tuple[float, float]] = []
        for k in range(BARS_PER_DAY):
            if queue:
                r, vm = queue.pop(0)
                eps = r / s
            else:
                eps = beta * mkt[k] + idio * rng.gauss(0.0, 1.0)
                r, vm = s * eps, 1.0
                if k >= 48 and rng.random() < HUNT_P:
                    queue = self._hunt(rng, walk, s, b - a)
            walk.append(walk[-1] + r)
            eps_l.append(eps)
            vm_l.append(vm)
        corr = (b - a) - walk[-1]                      # Brownian bridge: land exactly on tomorrow's anchor
        xs = [a + walk[k] + corr * k / BARS_PER_DAY for k in range(BARS_PER_DAY + 1)]
        t0 = SIM_EPOCH + d * tu.DAY
        per_bar = vol24 / BARS_PER_DAY
        bars = []
        for k in range(BARS_PER_DAY):
            o, c = math.exp(xs[k]), math.exp(xs[k + 1])
            h = max(o, c) * math.exp(abs(rng.gauss(0.0, 0.5 * s)))
            lo = min(o, c) * math.exp(-abs(rng.gauss(0.0, 0.5 * s)))
            vol = per_bar * (0.35 + 0.5 * min(abs(eps_l[k]), 6.0) + 0.3 * rng.random()) * vm_l[k]
            bars.append(Candle(t0 + k * BASE_MS, o, h, lo, c, vol))
        return bars

    def _load_base(self, sym: str) -> List[Candle]:
        d0 = (self.start - SIM_EPOCH) // tu.DAY
        d1 = (self.end - SIM_EPOCH) // tu.DAY
        self._anchors(sym, d1 + 1)
        out: List[Candle] = []
        for d in range(d0, d1):
            out.extend(self._day(sym, d))
        return out

    @staticmethod
    def _hunt(rng: random.Random, walk: List[float], s: float, day_move: float) -> List[Tuple[float, float]]:
        """Script a raid of a recent swing extreme (log space), then a displacement the other way."""
        raid_lows = rng.random() < (0.7 if day_move > 0 else 0.3)
        look = walk[-rng.randint(36, 144):]
        cur = walk[-1]
        target = min(look) - rng.uniform(0.0008, 0.003) if raid_lows else max(look) + rng.uniform(0.0008, 0.003)
        total = target - cur
        if abs(total) > 0.03 or (raid_lows and total >= 0) or (not raid_lows and total <= 0):
            return []
        n1, n2 = rng.randint(2, 4), rng.randint(4, 9)
        steps = [(total / n1 + rng.gauss(0.0, 0.2 * s), 1.8) for _ in range(n1)]
        rev = -total * rng.uniform(1.1, 1.9) + (2 * s if raid_lows else -2 * s)
        steps += [(rev / n2 + rng.gauss(0.0, 0.25 * s), 2.2) for _ in range(n2)]
        return steps


# --- real history -------------------------------------------------------------------------------

class HistoricalMarket(SeriesMarket):
    """Bitget history served through the clock (no lookahead). Candles cached under cache_dir."""

    name = "bitget-history"
    # history each timeframe needs before the replay start (engine lookbacks + margin)
    WARMUP = {"5m": 1 * tu.DAY, "15m": 2 * tu.DAY, "1H": 14 * tu.DAY, "4H": 52 * tu.DAY, "1D": 12 * tu.DAY}

    def __init__(self, public: BitgetPublic, clock: SimClock, symbols: Sequence[str], start_ms: int,
                 end_ms: int, cache_dir: Path, log=None):
        super().__init__(clock)
        self.public = public
        self.syms = list(symbols)
        self.start, self.end = start_ms, end_ms
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        all_contracts = public.contracts()
        self._contracts = {s: all_contracts[s] for s in self.syms if s in all_contracts}

    def symbols(self) -> List[str]:
        return self.syms

    def contracts(self) -> Dict[str, Contract]:
        return dict(self._contracts)

    def _fetch(self, sym: str, tf: str) -> List[Candle]:
        start = self.start - self.WARMUP.get(tf, 2 * tu.DAY)
        path = self.cache_dir / f"{sym}_{tf}_{start}_{self.end}.json"
        if path.is_file():
            return [Candle(*row) for row in json.loads(path.read_text())]
        if self.log:
            self.log.info("history %s %s (%s → %s)", sym, tf, tu.fmt(start), tu.fmt(self.end))
        rows = self.public.history(sym, tf, start, self.end)
        path.write_text(json.dumps([list(c) for c in rows]))
        return rows

    def _load_base(self, sym: str) -> List[Candle]:
        return self._fetch(sym, BASE_TF)

    def _load_tf(self, sym: str, tf: str) -> Optional[List[Candle]]:
        return self._fetch(sym, tf) if tf in self.WARMUP else None
