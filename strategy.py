"""strategy.py — the v5 sweep-reversal engine (FRAMEWORK.md → "Entry: all seven steps must pass").

evaluate() runs every enabled tier in order (T1 → T2); the first *signal* wins. _reversal() is the
per-tier sequence (setup TF = 4H for T1 / 1H for T2; entry TF = 15m / 5m):

  1  Context: market structure (ITH/ITL on the setup TF) + daily bias (PDH/PDL) decide which direction
     may trade. The opposing liquidity pool is the Draw on Liquidity (DOL) = take-profit.
  2  Trigger: the most recent pool (swing low for longs / swing high for shorts) pierced and then
     reclaimed by a close, within ~30 bars — OR, when there is no raid, a CISD: the origin of the last
     delivery leg is closed through the other way. The raid extreme is the stop reference.
  3  Structure shift (MSS/CHoCH): a close through the last opposing pivot formed before the raid.
  4  Premium/discount: dealing range = raid extreme → post-shift extreme. Longs only below
     equilibrium (discount), shorts only above (premium).
  5  Confirmation (new in v5): a candle-2 or candle-3 closure at the reversal extreme on the setup TF
     (TTrades fractal model). C2 = the candle that printed the extreme, C1 = the one before it.
     C2 closure: C2 trades through C1's low and closes back above it. C3 closure: C2 closed below C1's
     low, and the next candle (C3) closes back above it. Neither → a continuation, no trade.
  6  Zone in that half: a fresh FVG (a Unicorn when a breaker overlaps it) or, failing that, the
     order block. Tags: sweep-* (FVG), unicorn-*, OB-*.
  7  Entry candle on the entry TF: the pullback ("series" of down-closes for a long) traded into the
     zone, then a candle with a solid body (≥ 50% of its range) closes its body above the series —
     above the highest open of that series (mirror for shorts). Combined with an FVG: when the
     move off the pullback low leaves an entry-TF FVG, the setup is flagged +FVG (ranked first), and
     a solid retest of that FVG after the series close is a second entry opportunity.
     Stop beyond the raid extreme (floored ≥0.8%, setup rejected beyond 12%); target = the DOL with
     a 1:2 floor.

Primitives read CLOSED candles only — the caller's `get(tf, limit)` drops the forming candle.

Setup status:  signal   all seven steps pass → tradable
               in-zone  steps 1–6 pass, price reached the zone, the entry candle has not printed yet
               armed    steps 1–6 pass, waiting for price to retrace into the zone

Interpretation notes (where the framework text leaves a choice, this is the choice made):
  • Daily bias = the previous day's close vs the day before's range: close above its high →
    bullish, below its low → bearish, otherwise neutral. The daily-*failure* pattern is NOT part of
    the bias — it has no backtested edge and stays research-only (daily_failure, routine 2).
  • Liquidity pools (raids, DOL) are 5-bar fractal swings; the structure shift and the ITH/ITL
    hierarchy use 3-bar short-term swings (ICT STH/STL → ITH/ITL).
  • The zone is read on the setup TF (where the raid and the displacement live); the entry TF supplies
    the series and the entry candle.
  • CISD (step 2) needs a series (≥2) of against-closes delivering price into a 5-bar swing, walked from
    the most recent leg backwards — a single engulfing candle at a minor wiggle is noise, not a CISD.
  • C2/C3 closures are read on the setup TF (4H with a 15m entry, 1H with a 5m entry — the fractal
    model's pairs). "Back inside C1's range" = a close beyond C1's low (high for shorts); a close
    beyond C1's other extreme counts too.
  • The entry-TF series is the most recent run of down-closes (≥1 candle) whose pullback reached the
    zone without closing through its far edge, ending within the last 20 entry bars. The level is the
    highest open in the run (the top of its bodies). The candle that first closes above it is the
    entry candle; it must close in the trade direction with a ≥50% body, and no close may fall back
    below the level afterwards.
  • The trade loop runs every 30 minutes but entry candles close every 15m / 5m, so an entry candle
    (or FVG retest) counts while it closed within the last 30 minutes. Older ones are spent.
  • "≥ 1:2 floor" is a floor, like the stop's 0.8% floor: target = max(DOL, entry + 2R).
  • The stop sits 0.1% beyond the raid extreme.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from exchange import Candle, tf_ms
from util import px, sig

LONG, SHORT = "long", "short"

# --- engine constants (FRAMEWORK.md) ----------------------------------------------------------------
RAID_LOOKBACK = 30          # stop raid / CISD "within ~30 bars" of the stop-raid TF
RECLAIM_BARS = 3            # the pierce must be reclaimed by a close within 3 bars (incl. the pierce bar)
CISD_MIN_RUN = 2            # CISD delivery leg: a series ("down-close bodies"), not a single candle
BODY_MIN = 0.50             # entry candle: body ≥ 50% of range (a wick/shadow is a likely fake)
SERIES_LOOKBACK = 20        # entry TF: the series that delivered price into the zone ends within 20 bars
SERIES_FRESH_MS = 30 * 60_000   # an entry candle / FVG retest counts for one trade-check interval (30 min)
STOP_FLOOR_PCT = 0.008      # stop floored at ≥ 0.8% from entry
STOP_MAX_PCT = 0.12         # whole setup rejected if the stop is > 12% away (spike / poor-RR guard)
RR_FLOOR = 2.0              # target = DOL, never closer than 1:2
STOP_BUFFER_PCT = 0.001     # stop placed 0.1% beyond the raid extreme
POI_TOLERANCE = 0.0015      # "inside the POI" tolerance (0.15%)
FVG_MIN_PCT = 0.0005        # ignore FVGs thinner than 0.05% of price
POOL_WIDTH = 2              # liquidity pools: 5-bar fractal swings
STH_WIDTH = 1               # short-term swings: 3-bar fractals (MSS pivots, ITH/ITL base)
OB_LOOKBACK = 5             # bars searched back from the raid extreme for the order block
BREAKER_LOOKBACK = 3        # bars searched back from the broken pivot for the breaker candle
MIN_STRUCT_BARS = 40
CHART_BARS = 90
DAILY_TF = "1D"             # Bitget 1D = UTC+8 day, i.e. MYT midnight — matches the gates' day

STAGES = ("no data", "context", "trigger", "shift", "closure", "armed", "in-zone", "signal")
STAGE = {name: i for i, name in enumerate(STAGES)}
FAMILY = {"FVG": "sweep", "unicorn": "unicorn", "OB": "OB"}
POI_PRIORITY = {"unicorn": 0, "FVG": 1, "OB": 2}


@dataclass(frozen=True)
class Tier:
    name: str
    struct_tf: str           # structure + stop raid (+ daily bias)
    entry_tf: str            # execution / confirmation candle
    enabled: bool = True
    struct_bars: int = 300
    entry_bars: int = 60


TIERS: Tuple[Tier, ...] = (
    Tier("T1", "4H", "15m"),
    Tier("T2", "1H", "5m"),
    # T3 is carried for reference and OFF: its S15 entry would be floored at M1 (Bitget public
    # candles stop at 1m — 15s/1s return HTTP 400) and the framework focuses on 1H/4H.
    Tier("T3", "15m", "1m", enabled=False),
)


# --- data types ----------------------------------------------------------------------------------

@dataclass
class Trigger:
    kind: str            # "raid" | "cisd"
    level: float         # raid: the swept pool; cisd: the delivery origin that was closed through
    extreme: float       # raid extreme / swing extreme → stop reference
    idx: int             # raid: pierce bar; cisd: swing-extreme bar
    ext_idx: int         # bar that printed the extreme
    confirm_idx: int     # raid: reclaim close; cisd: close through the origin
    pool_idx: int        # raid: pivot bar of the swept pool; cisd: first candle of the delivery run


@dataclass
class Shift:
    level: float         # the broken pivot (prior lower-high for longs / higher-low for shorts)
    pivot_idx: int
    idx: int             # bar whose close broke it


@dataclass
class Zone:
    kind: str            # "FVG" | "unicorn" | "OB"
    lo: float
    hi: float
    idx: int
    src_lo: float = 0.0  # zone before clipping to the premium/discount half
    src_hi: float = 0.0
    fresh: bool = True
    overlap: Optional[Tuple[float, float]] = None


@dataclass
class Setup:
    symbol: str
    tier: str
    struct_tf: str
    entry_tf: str
    direction: str
    status: str
    trigger: str
    family: str
    tag: str
    setup_id: str
    bias: str
    structure: str
    price: float
    entry: float
    stop: float
    target: float
    dol: Optional[float]
    dol_kind: str
    rr: float
    risk_pct: float
    raid: dict
    mss: dict
    range: dict
    poi: dict
    confirm: Optional[dict]
    checklist: List[dict]
    chart: dict
    found_at: int
    closure: str = ""                # "C2" | "C3" (step 5)
    entry_kind: str = ""             # "close" (series close) | "fvg" (FVG retest) — set on a signal
    fvg: bool = False                # +FVG: the move off the pullback low left an entry-TF FVG
    candle: dict = field(default_factory=dict)    # step 5 detail: C1/C2/C3 timestamps and the C1 level
    series: Optional[dict] = None    # step 7 detail: the entry-TF series and its level

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    symbol: str
    setup: Optional[Setup]
    stage: int                      # deepest step reached (index into STAGES)
    notes: List[str] = field(default_factory=list)


Getter = Callable[[str, int], List[Candle]]


# --- small helpers -----------------------------------------------------------------------------

def _sign(d: str) -> int:
    return 1 if d == LONG else -1


def _pool_side(c: Candle, d: str) -> float:
    """The extreme on the liquidity side a long/short raid takes (lows for longs)."""
    return c.l if d == LONG else c.h


def _target_side(c: Candle, d: str) -> float:
    return c.h if d == LONG else c.l


def _against(c: Candle, d: str) -> bool:
    """A delivery candle against the trade (down-close for a long)."""
    return c.c < c.o if d == LONG else c.c > c.o


def _with(c: Candle, d: str) -> bool:
    return c.c > c.o if d == LONG else c.c < c.o


# --- 1 · structure + daily bias ------------------------------------------------------------------

def _swing_seq(c: Sequence[Candle], width: int = STH_WIDTH) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Fractal swings → (highs, lows) as (index, price). A pivot needs `width` bars on each side:
    strictly beyond the left bars, at least as extreme as the right bars (equal highs count once)."""
    highs, lows = [], []
    for i in range(width, len(c) - width):
        hi, lo = c[i].h, c[i].l
        left, right = range(i - width, i), range(i + 1, i + width + 1)
        if all(hi > c[j].h for j in left) and all(hi >= c[j].h for j in right):
            highs.append((i, hi))
        if all(lo < c[j].l for j in left) and all(lo <= c[j].l for j in right):
            lows.append((i, lo))
    return highs, lows


def _intermediate(points: List[Tuple[int, float]], highs: bool) -> List[Tuple[int, float]]:
    """ITH = a short-term high with lower short-term highs on both sides (ITL mirrors it)."""
    out = []
    for k in range(1, len(points) - 1):
        p, a, b = points[k][1], points[k - 1][1], points[k + 1][1]
        if (p > a and p > b) if highs else (p < a and p < b):
            out.append(points[k])
    return out


def market_structure(c: Sequence[Candle]) -> str:
    """Intermediate-term structure: higher ITH + higher ITL → up, lower + lower → down, else range."""
    highs, lows = _swing_seq(c, STH_WIDTH)
    ith, itl = _intermediate(highs, True), _intermediate(lows, False)
    if len(ith) < 2 or len(itl) < 2:
        return "range"
    hh, hl = ith[-1][1] > ith[-2][1], itl[-1][1] > itl[-2][1]
    lh, ll = ith[-1][1] < ith[-2][1], itl[-1][1] < itl[-2][1]
    if hh and hl:
        return "up"
    if lh and ll:
        return "down"
    return "range"


def daily_bias(daily: Sequence[Candle]) -> str:
    """PDH/PDL bias from closed daily candles: yesterday closed above the prior day's high →
    bullish, below its low → bearish, inside → neutral."""
    if len(daily) < 2:
        return "neutral"
    d2, d1 = daily[-2], daily[-1]
    if d1.c > d2.h:
        return "bullish"
    if d1.c < d2.l:
        return "bearish"
    return "neutral"


def daily_failure(daily: Sequence[Candle]) -> Optional[dict]:
    """Routine 2 (research only — no backtested edge, never gates entries): the previous day traded
    beyond the day-before's high/low but FAILED to close beyond it."""
    if len(daily) < 2:
        return None
    d2, d1 = daily[-2], daily[-1]
    failed_high = d1.h > d2.h and d1.c <= d2.h
    failed_low = d1.l < d2.l and d1.c >= d2.l
    if not (failed_high or failed_low):
        return None
    kind = "both" if failed_high and failed_low else ("bearish" if failed_high else "bullish")
    return {"kind": kind, "day": d1.ts, "prior_high": d2.h, "prior_low": d2.l, "high": d1.h, "low": d1.l,
            "close": d1.c, "failed_high": failed_high, "failed_low": failed_low}


def daily_context(daily: Sequence[Candle]) -> dict:
    if not daily:
        return {"bias": "neutral", "pdh": None, "pdl": None}
    d1 = daily[-1]
    return {"bias": daily_bias(daily), "pdh": d1.h, "pdl": d1.l, "day": d1.ts}


# --- 2 · stop raid / CISD --------------------------------------------------------------------

def liquidity_sweep(c: Sequence[Candle], direction: str, lookback: int = RAID_LOOKBACK,
                    width: int = POOL_WIDTH) -> Optional[Trigger]:
    """Most recent stop raid: a fresh pool (first pierced inside the lookback) whose pierce was reclaimed
    by a close within RECLAIM_BARS, and whose raid extreme has not been taken out since."""
    n = len(c)
    if n < 2 * width + 3:
        return None
    sg = _sign(direction)
    highs, lows = _swing_seq(c, width)
    pools = lows if direction == LONG else highs
    start = n - lookback
    best: Optional[Trigger] = None
    for pi, lvl in pools:
        j = next((k for k in range(pi + 1, n) if sg * (lvl - _pool_side(c[k], direction)) > 0), None)
        if j is None or j < start:
            continue                      # never taken, or taken before the window (not fresh)
        r = next((k for k in range(j, min(n, j + RECLAIM_BARS)) if sg * (c[k].c - lvl) > 0), None)
        if r is None:
            continue                      # pierced but not reclaimed → a breakdown, not a raid
        ext_idx = min(range(j, r + 1), key=lambda k: sg * _pool_side(c[k], direction))
        extreme = _pool_side(c[ext_idx], direction)
        if any(sg * (_pool_side(c[k], direction) - extreme) < 0 for k in range(r + 1, n)):
            continue                      # raid extreme taken out afterwards → failed raid
        cand = Trigger("raid", lvl, extreme, j, ext_idx, r, pi)
        if best is None or (j, -sg * lvl) > (best.idx, -sg * best.level):
            best = cand
    return best


def _cisd(c: Sequence[Candle], direction: str, lookback: int = RAID_LOOKBACK) -> Optional[Trigger]:
    """Change in State of Delivery on the LAST delivery leg: a series of against-candles (≥ CISD_MIN_RUN
    down-closes for a long) that delivered price into a pool-grade swing, whose origin — the top of the
    down-close bodies (bottom of the up-close bodies for a short) — is later closed through the other
    way. Legs are walked from the most recent backwards; one not yet closed through, one whose swing is
    only a minor wiggle, or one whose swing was taken out since is skipped. Stop ref = that swing."""
    n = len(c)
    start = max(1, n - lookback)
    sg = _sign(direction)
    i = n - 1
    while i >= start:
        while i >= start and not _against(c[i], direction):
            i -= 1
        if i < start:
            break
        end = i
        while i >= 0 and _against(c[i], direction):
            i -= 1
        first = i + 1
        if end - first + 1 < CISD_MIN_RUN:
            continue
        origin = max(c[j].o for j in range(first, end + 1)) if direction == LONG else \
            min(c[j].o for j in range(first, end + 1))
        t = next((j for j in range(end + 1, n) if sg * (c[j].c - origin) > 0), None)
        if t is None:
            continue
        k = min(range(first, t + 1), key=lambda j: (sg * _pool_side(c[j], direction), -j))
        extreme = _pool_side(c[k], direction)
        if any(sg * (_pool_side(c[j], direction) - extreme) < 0 for j in range(k + 1, n)):
            continue
        left = range(max(0, k - POOL_WIDTH), k)
        right = range(k + 1, min(n, k + POOL_WIDTH + 1))
        if len(left) < POOL_WIDTH or any(sg * (_pool_side(c[j], direction) - extreme) <= 0 for j in left) \
                or len(right) < POOL_WIDTH:
            continue                      # not a pool-grade (5-bar) swing — a wiggle, not a delivery low
        return Trigger("cisd", origin, extreme, k, k, t, first)
    return None


# --- 3 · structure shift ---------------------------------------------------------------------

def structure_shift(c: Sequence[Candle], direction: str, trig: Trigger) -> Optional[Shift]:
    """Close through the last opposing short-term pivot that formed before the raid
    (the prior lower-high for longs / higher-low for shorts) — the failure to manipulate."""
    highs, lows = _swing_seq(c, STH_WIDTH)
    prior = [p for p in (highs if direction == LONG else lows) if p[0] < trig.idx]
    if not prior:
        return None
    pi, lvl = prior[-1]
    sg = _sign(direction)
    t = next((j for j in range(trig.idx, len(c)) if sg * (c[j].c - lvl) > 0), None)
    return Shift(lvl, pi, t) if t is not None else None


# --- 4 · premium / discount --------------------------------------------------------------------

def dealing_range(c: Sequence[Candle], direction: str, trig: Trigger) -> Tuple[float, float, float]:
    """Raid extreme → post-shift extreme; returns (low, high, equilibrium)."""
    seg = c[trig.idx:]
    if direction == LONG:
        lo, hi = trig.extreme, max(x.h for x in seg)
    else:
        lo, hi = min(x.l for x in seg), trig.extreme
    return lo, hi, (lo + hi) / 2.0


# --- 5 · points of interest ------------------------------------------------------------------

def find_fvgs(c: Sequence[Candle], direction: str, start: int = 1, end: Optional[int] = None,
              min_pct: float = FVG_MIN_PCT) -> List[Zone]:
    """Three-candle fair value gaps in `direction` whose middle candle index is in [start, end].
    Fresh = never fully filled afterwards (for a bullish gap: no later low at or below its bottom)."""
    n = len(c)
    last = n - 2 if end is None else min(end, n - 2)
    out = []
    for i in range(max(1, start), last + 1):
        a, b = c[i - 1], c[i + 1]
        if direction == LONG and b.l > a.h:
            lo, hi = a.h, b.l
        elif direction == SHORT and b.h < a.l:
            lo, hi = b.h, a.l
        else:
            continue
        if hi - lo < min_pct * c[i].c:
            continue
        later = c[i + 2:]
        filled = any(x.l <= lo for x in later) if direction == LONG else any(x.h >= hi for x in later)
        out.append(Zone("FVG", lo, hi, i, lo, hi, fresh=not filled))
    return out


def order_block(c: Sequence[Candle], direction: str, ext_idx: int, lookback: int = OB_LOOKBACK) -> Optional[Zone]:
    """Last opposite-close candle before the displacement leg (at or just before the raid extreme).
    Invalid once a later candle closes through its far side."""
    for i in range(ext_idx, max(-1, ext_idx - lookback), -1):
        if _against(c[i], direction):
            z = Zone("OB", c[i].l, c[i].h, i, c[i].l, c[i].h)
            broken = any(x.c < z.lo for x in c[ext_idx + 1:]) if direction == LONG else \
                any(x.c > z.hi for x in c[ext_idx + 1:])
            return None if broken else z
    return None


def breaker_block(c: Sequence[Candle], direction: str, pivot_idx: int,
                  lookback: int = BREAKER_LOOKBACK) -> Optional[Zone]:
    """The failed order block at the pivot the shift broke: for a long, the last up-close candle at the
    lower-high that preceded the raid (mirror for shorts)."""
    for i in range(pivot_idx, max(-1, pivot_idx - lookback), -1):
        if _with(c[i], direction):
            return Zone("breaker", c[i].l, c[i].h, i, c[i].l, c[i].h)
    return None


def breaker_overlap(c: Sequence[Candle], direction: str, pivot_idx: int, zone: Zone) -> Optional[Tuple[float, float]]:
    """Unicorn: the intersection of a breaker block with the FVG zone, if any."""
    bb = breaker_block(c, direction, pivot_idx)
    if bb is None:
        return None
    lo, hi = max(bb.lo, zone.lo), min(bb.hi, zone.hi)
    return (lo, hi) if hi > lo else None


def _clip(z: Zone, lo: float, hi: float) -> Optional[Zone]:
    a, b = max(z.lo, lo), min(z.hi, hi)
    return Zone(z.kind, a, b, z.idx, z.src_lo, z.src_hi, z.fresh, z.overlap) if b > a else None


def poi_zones(c: Sequence[Candle], direction: str, trig: Trigger, shift: Shift, lo: float, hi: float,
              eq: float) -> List[Zone]:
    """Every candidate POI in the correct half: fresh FVGs from the raid onwards (plus their unicorn
    overlap), then the order block."""
    half_lo, half_hi = (lo, eq) if direction == LONG else (eq, hi)
    zones: List[Zone] = []
    for f in find_fvgs(c, direction, start=trig.idx):
        if not f.fresh:
            continue
        z = _clip(f, half_lo, half_hi)
        if z is None:
            continue
        ov = breaker_overlap(c, direction, shift.pivot_idx, z)
        if ov:
            zones.append(Zone("unicorn", ov[0], ov[1], f.idx, f.src_lo, f.src_hi, True, ov))
        zones.append(z)
    ob = order_block(c, direction, trig.ext_idx)
    if ob is not None:
        z = _clip(ob, half_lo, half_hi)
        if z is not None:
            zones.append(z)
    return zones


def select_poi(zones: List[Zone], price: float, direction: str) -> Tuple[Optional[Zone], bool]:
    """(zone, price_inside). Price inside → the best zone containing it (unicorn > FVG > OB). Otherwise the
    first zone price will meet on its retrace. None when price is already through every zone."""
    if not zones:
        return None, False
    tol = POI_TOLERANCE
    inside = [z for z in zones if z.lo * (1 - tol) <= price <= z.hi * (1 + tol)]
    if inside:
        return min(inside, key=lambda z: (POI_PRIORITY[z.kind], -z.idx)), True
    if direction == LONG:
        ahead = [z for z in zones if z.hi < price]
        return (max(ahead, key=lambda z: (z.hi, -POI_PRIORITY[z.kind])), False) if ahead else (None, False)
    ahead = [z for z in zones if z.lo > price]
    return (min(ahead, key=lambda z: (z.lo, POI_PRIORITY[z.kind])), False) if ahead else (None, False)


# --- 5 · candle 2 / candle 3 closure ---------------------------------------------------------------

def candle_closure(c: Sequence[Candle], direction: str, ext_idx: int) -> Tuple[Optional[dict], str]:
    """Step 5 on the setup TF. C2 = the candle that printed the reversal extreme, C1 = the candle before it.
    C2 closure: C2 trades through C1's low (high for shorts) and closes back above it.
    C3 closure: C2 closed below C1's low; the next candle (C3) closes back above it.
    Returns (closure, reason): closure = {"kind", "level" (C1's low), "c1", "c2", "close" (index of the
    closing candle)}; reason says why there is none (or that C3 has not closed yet)."""
    side = "low" if direction == LONG else "high"
    if ext_idx < 1:
        return None, "no candle before the extreme"
    sg = _sign(direction)
    c1, c2 = c[ext_idx - 1], c[ext_idx]
    level = _pool_side(c1, direction)
    if sg * (level - _pool_side(c2, direction)) <= 0:
        return None, f"the extreme candle did not trade through the previous candle's {side}"
    out = {"level": level, "c1": ext_idx - 1, "c2": ext_idx}
    if sg * (c2.c - level) > 0:
        return dict(out, kind="C2", close=ext_idx), ""
    if ext_idx + 1 >= len(c):
        return None, f"C2 closed beyond C1's {side} {px(level)}; waiting for C3 to close"
    if sg * (c[ext_idx + 1].c - level) > 0:
        return dict(out, kind="C3", close=ext_idx + 1), ""
    return None, f"neither C2 nor C3 closed back inside C1's {side} {px(level)} (continuation, not a reversal)"


# --- 7 · entry candle: series close (+FVG) -------------------------------------------------------

def _runs(c: Sequence[Candle], direction: str, start: int):
    """Runs of against-closes (the "series"), most recent first, that end at or after `start`:
    yields (first, last) indices; a run may begin before `start`."""
    i = len(c) - 1
    while i >= start:
        while i >= start and not _against(c[i], direction):
            i -= 1
        if i < start:
            return
        last = i
        while i >= 0 and _against(c[i], direction):
            i -= 1
        yield i + 1, last


def _reached(c: Sequence[Candle], direction: str, first: int, zones: List[Zone]) -> Optional[Zone]:
    """The best zone (unicorn > FVG > OB, then the most recent) that the pullback c[first:] traded into
    without any close through the zone's far edge."""
    tol = POI_TOLERANCE
    seg = c[first:]
    sg = _sign(direction)
    ext = sg * min(sg * _pool_side(x, direction) for x in seg)
    hits = []
    for z in zones:
        near, far = (z.hi * (1 + tol), z.lo * (1 - tol)) if direction == LONG else (z.lo * (1 - tol), z.hi * (1 + tol))
        if sg * (ext - near) > 0 or any(sg * (x.c - far) < 0 for x in seg):
            continue
        hits.append(z)
    return min(hits, key=lambda z: (POI_PRIORITY[z.kind], -z.idx)) if hits else None


def _body(x: Candle, direction: str) -> float:
    rng = x.h - x.l
    return _sign(direction) * (x.c - x.o) / rng if rng > 0 else 0.0


def _bar(x: Candle, tf: str, **extra) -> dict:
    return dict({"ts": x.ts, "tf": tf, "o": x.o, "h": x.h, "l": x.l, "c": x.c}, **extra)


def entry_trigger(e: Sequence[Candle], tf: str, direction: str, zones: List[Zone], now_ms: int) -> dict:
    """Step 7 on the entry TF. Returns {"state": "signal" | "in-zone" | "none", "zone", "confirm", "series",
    "fvg", "kind", "wait"}.

    The series = the most recent run of against-closes whose pullback reached a zone (ending within
    SERIES_LOOKBACK bars). Entry when either
      close  the first candle to close beyond the series level has a ≥50% body in the trade direction,
             closed within the last 30 minutes, and nothing has closed back through the level since; or
      fvg    after that close, the move off the pullback low left an entry-TF FVG, and a candle closed within
             the last 30 minutes traded into it, closed with a ≥50% body in the trade direction and stayed
             beyond the series level (nothing closed back through the level from the series close on).
    +FVG = an entry-TF FVG formed in the move off the pullback low (always true for an FVG retest)."""
    out = {"state": "none", "zone": None, "confirm": None, "series": None, "fvg": False, "kind": "", "wait": ""}
    n = len(e)
    if n < 2 or not zones:
        return out
    tfm = tf_ms(tf)
    if e[-1].ts + 2 * tfm <= now_ms:
        out["wait"] = f"{tf} data is stale"
        return out
    # The most recent series that reached a zone decides what we are waiting for; an older series whose
    # close is still fresh and unbroken (a shallow pullback printed since) can still give the entry.
    first_state = None
    for first, last in _runs(e, direction, max(0, n - 1 - SERIES_LOOKBACK)):
        st = _series_state(e, tf, tfm, direction, zones, now_ms, first, last)
        if st is None:
            continue
        if st["state"] == "signal":
            return st
        if first_state is None:
            first_state = st
    return first_state or out


def _series_state(e: Sequence[Candle], tf: str, tfm: int, direction: str, zones: List[Zone], now_ms: int,
                  first: int, last: int) -> Optional[dict]:
    """entry_trigger() for one series e[first..last]; None when its pullback did not reach a zone."""
    zone = _reached(e, direction, first, zones)
    if zone is None:
        return None
    n = len(e)
    sg = _sign(direction)
    side = "above" if direction == LONG else "below"
    fresh_from = now_ms - SERIES_FRESH_MS

    def fresh(k: int) -> bool:
        return e[k].ts + tfm > fresh_from

    opens = [e[j].o for j in range(first, last + 1)]
    level = max(opens) if direction == LONG else min(opens)
    series = {"first_ts": e[first].ts, "last_ts": e[last].ts, "bars": last - first + 1, "level": level, "tf": tf}
    out = {"state": "in-zone", "zone": zone, "confirm": None, "series": series, "fvg": False, "kind": "", "wait": ""}
    t = next((k for k in range(last + 1, n) if sg * (e[k].c - level) > 0), None)
    if t is None:
        out["wait"] = f"waiting for a {tf} body close {side} the series at {px(level)}"
        return out
    low_idx = min(range(first, t + 1), key=lambda j: (sg * _pool_side(e[j], direction), -j))
    gaps = [g for g in find_fvgs(e, direction, start=low_idx) if g.idx + 1 <= n - 1]
    out["fvg"] = bool(gaps)
    series["close_ts"] = e[t].ts
    if not all(sg * (e[k].c - level) > 0 for k in range(t, n)):
        out["wait"] = f"the {tf} close {side} {px(level)} failed (closed back through it); waiting for a new series"
        return out
    x = e[t]
    if fresh(t) and _body(x, direction) >= BODY_MIN:
        out.update(state="signal", kind="close",
                   confirm=_bar(x, tf, body=round(_body(x, direction), 3), kind="close", level=level,
                                fvg=[gaps[-1].lo, gaps[-1].hi] if gaps else None))
        return out
    tol = POI_TOLERANCE
    for y in range(n - 1, t, -1):
        if not fresh(y):
            break
        yb = e[y]
        if _body(yb, direction) < BODY_MIN:
            continue
        for g in reversed(gaps):
            if g.idx + 2 > y:
                continue                          # the gap completed after this candle
            near, far = (g.hi * (1 + tol), g.lo * (1 - tol)) if direction == LONG else (g.lo * (1 - tol), g.hi * (1 + tol))
            if sg * (_pool_side(yb, direction) - near) <= 0 and sg * (yb.c - far) >= 0:
                out.update(state="signal", kind="fvg", fvg=True,
                           confirm=_bar(yb, tf, body=round(_body(yb, direction), 3), kind="fvg", level=level,
                                        fvg=[g.lo, g.hi]))
                return out
    why = "older than 30 min" if not fresh(t) else f"body {_body(x, direction):.0%} < {BODY_MIN:.0%}"
    out["wait"] = (f"the {tf} close {side} the series at {px(level)} is spent ({why}); "
                   f"waiting for an FVG retest or a new series")
    return out


# --- stop + target ---------------------------------------------------------------------------------

def stop_for(direction: str, entry: float, extreme: float) -> Tuple[Optional[float], float, str]:
    """(stop, risk distance, reject reason). Beyond the raid extreme, floored at 0.8%, rejected > 12%."""
    sg = _sign(direction)
    stop = extreme * (1 - sg * STOP_BUFFER_PCT)
    dist = sg * (entry - stop)
    if dist <= 0:
        return None, 0.0, "price is beyond the raid extreme"
    dist = max(dist, entry * STOP_FLOOR_PCT)
    if dist > entry * STOP_MAX_PCT:
        return None, 0.0, f"stop {dist / entry:.1%} away > {STOP_MAX_PCT:.0%} (spike / poor-RR guard)"
    return entry - sg * dist, dist, ""


def draw_on_liquidity(c: Sequence[Candle], direction: str, entry: float, range_extreme: float,
                      dctx: dict) -> Tuple[Optional[float], str]:
    """Nearest opposing pool beyond entry: unswept structure-TF swing, the dealing-range extreme, or PDH/PDL."""
    sg = _sign(direction)
    highs, lows = _swing_seq(c, POOL_WIDTH)
    cands: List[Tuple[float, str]] = []
    for i, lvl in (highs if direction == LONG else lows):
        if sg * (lvl - entry) <= 0:
            continue
        if not any(sg * (_target_side(c[k], direction) - lvl) > 0 for k in range(i + 1, len(c))):
            cands.append((lvl, "swing high" if direction == LONG else "swing low"))
    if sg * (range_extreme - entry) > 0:
        cands.append((range_extreme, "range high" if direction == LONG else "range low"))
    pd = dctx.get("pdh" if direction == LONG else "pdl")
    if pd and sg * (pd - entry) > 0:
        cands.append((pd, "PDH" if direction == LONG else "PDL"))
    if not cands:
        return None, "none"
    return min(cands, key=lambda x: sg * (x[0] - entry))


# --- the per-tier sequence -----------------------------------------------------------------------

def _reversal(symbol: str, tier: Tier, get: Getter, price: float, now_ms: int,
              dctx: dict) -> Tuple[List[Setup], int, List[str]]:
    S = get(tier.struct_tf, tier.struct_bars)
    if len(S) < MIN_STRUCT_BARS:
        return [], 0, [f"{tier.name}: only {len(S)} closed {tier.struct_tf} bars"]
    structure = market_structure(S)
    bias = dctx["bias"]
    setups: List[Setup] = []
    notes: List[str] = []
    stage = 0
    for d in (LONG, SHORT):
        lbl = f"{tier.name} {d}"
        allowed = (bias != "bearish" and structure != "down") if d == LONG else \
            (bias != "bullish" and structure != "up")
        if not allowed:
            notes.append(f"{lbl}: context blocks it ({tier.struct_tf} structure {structure}, daily {bias})")
            continue
        stage = max(stage, 1)
        trig = liquidity_sweep(S, d) or _cisd(S, d)
        if trig is None:
            notes.append(f"{lbl}: no stop raid or CISD within {RAID_LOOKBACK} bars")
            continue
        stage = max(stage, 2)
        shift = structure_shift(S, d, trig)
        if shift is None:
            notes.append(f"{lbl}: {trig.kind} found, no structure shift yet")
            continue
        stage = max(stage, 3)
        closure, why = candle_closure(S, d, trig.ext_idx)
        if closure is None:
            notes.append(f"{lbl}: {trig.kind} + shift found, no candle closure — {why}")
            continue
        stage = max(stage, 4)
        lo, hi, eq = dealing_range(S, d, trig)
        zones = poi_zones(S, d, trig, shift, lo, hi, eq)
        zone, inside = select_poi(zones, price, d)
        half = "discount" if d == LONG else "premium"
        if zone is None:
            notes.append(f"{lbl}: no fresh FVG/OB left in the {half} half")
            continue
        sg = _sign(d)
        in_half = sg * (eq - price) > 0
        et = None
        if in_half:
            et = entry_trigger(get(tier.entry_tf, tier.entry_bars), tier.entry_tf, d, zones, now_ms)
            if et["zone"] is not None:
                zone = et["zone"]
        in_zone = in_half and (inside or (et is not None and et["zone"] is not None))
        entry = price if in_zone else (zone.hi if d == LONG else zone.lo)
        stop, risk, why = stop_for(d, entry, trig.extreme)
        if stop is None:
            notes.append(f"{lbl}: rejected — {why}")
            continue
        dol, dol_kind = draw_on_liquidity(S, d, entry, hi if d == LONG else lo, dctx)
        target = entry + sg * RR_FLOOR * risk
        if dol is not None and sg * (dol - target) > 0:
            target = dol
        stage = max(stage, STAGE["armed"])
        status = "armed"
        if in_zone:
            stage = max(stage, STAGE["in-zone"])
            status = "in-zone"
            if et is not None and et["state"] == "signal":
                status = "signal"
                stage = STAGE["signal"]
        setups.append(_build(symbol, tier, d, status, S, trig, shift, closure, (lo, hi, eq), zone, price, entry,
                             stop, risk, target, dol, dol_kind, et if in_zone else None, bias, structure, in_half,
                             now_ms))
    return setups, stage, notes


def evaluate(symbol: str, get: Getter, price: float, now_ms: int, tiers: Sequence[Tier] = TIERS) -> Result:
    """Run T1 → T2 (enabled tiers). The first tier with a signal wins; otherwise the best watch setup
    (in-zone before armed, earlier tier first) is returned for the watchlist."""
    dctx = daily_context(get(DAILY_TF, 10))
    best: Optional[Setup] = None
    stage, notes = 0, []
    for tier in tiers:
        if not tier.enabled:
            continue
        setups, st, tnotes = _reversal(symbol, tier, get, price, now_ms, dctx)
        stage, notes = max(stage, st), notes + tnotes
        signals = [s for s in setups if s.status == "signal"]
        if signals:
            return Result(symbol, max(signals, key=lambda s: (s.fvg, s.raid["ts"], s.rr)), STAGE["signal"], notes)
        for s in setups:
            if best is None or (s.status == "in-zone" and best.status == "armed"):
                best = s
    return Result(symbol, best, stage, notes)


# --- presentation --------------------------------------------------------------------------------

def _build(symbol, tier, d, status, S, trig, shift, closure, rng, zone, price, entry, stop, risk, target, dol,
           dol_kind, et, bias, structure, in_half, now_ms) -> Setup:
    lo, hi, eq = rng
    sg = _sign(d)
    rr = sg * (target - entry) / risk
    family = FAMILY[zone.kind]
    tag = f"{family}-{tier.name}-{trig.kind}-{closure['kind']}"
    n = len(S)
    side_word = "low" if d == LONG else "high"
    half = "discount" if d == LONG else "premium"
    beyond = "above" if d == LONG else "below"
    if trig.kind == "raid":
        trig_txt = (f"swept swing {side_word} {px(trig.level)} to {px(trig.extreme)}, reclaimed "
                    f"{trig.confirm_idx - trig.idx} bar(s) later · {n - 1 - trig.idx} bars ago")
    else:
        trig_txt = (f"CISD: close through delivery origin {px(trig.level)} · swing {side_word} "
                    f"{px(trig.extreme)} · {n - 1 - trig.idx} bars ago")
    c2 = S[closure["c2"]]
    if closure["kind"] == "C2":
        closure_txt = (f"C2 closure: C2 took C1's {side_word} {px(closure['level'])} to {px(_pool_side(c2, d))} "
                       f"and closed back {beyond} it at {px(c2.c)}")
    else:
        closure_txt = (f"C3 closure: C2 closed {px(c2.c)}, through C1's {side_word} {px(closure['level'])}; "
                       f"C3 closed back {beyond} it at {px(S[closure['close']].c)}")
    zone_txt = f"{zone.kind} {px(zone.lo)}–{px(zone.hi)}"
    if zone.kind == "unicorn":
        zone_txt += f" (breaker ∩ FVG {px(zone.src_lo)}–{px(zone.src_hi)})"
    confirm = et["confirm"] if et and et["state"] == "signal" else None
    series = et["series"] if et else None
    fvg_flag = bool(et and et["fvg"])
    if confirm:
        word = "bull" if d == LONG else "bear"
        head = (f"{tier.entry_tf} {word} candle closed {px(confirm['c'])} {beyond} the series {px(confirm['level'])}"
                if confirm["kind"] == "close" else
                f"FVG retest: {tier.entry_tf} {word} candle in the FVG {px(confirm['fvg'][0])}–{px(confirm['fvg'][1])}, "
                f"closed {px(confirm['c'])}, series {px(confirm['level'])} held")
        entry_txt = (f"{head} · body {confirm['body']:.0%}{' · +FVG' if fvg_flag else ''} · stop {px(stop)} "
                     f"({risk / entry:.2%}) · target {px(target)} ({rr:.2f}R)")
    elif status == "in-zone":
        entry_txt = (et or {}).get("wait") or (f"price in the zone — waiting for a {tier.entry_tf} series and a "
                                               f"≥{BODY_MIN:.0%} body close {beyond} it")
    else:
        entry_txt = f"waiting for price to reach the zone ({abs(price - entry) / price:.2%} away)"
    checklist = [
        {"step": 1, "name": "Context", "ok": True,
         "detail": f"{tier.struct_tf} structure {structure} · daily {bias} → {d}s allowed · DOL {px(dol)} ({dol_kind})"},
        {"step": 2, "name": "Trigger", "ok": True, "detail": trig_txt},
        {"step": 3, "name": "Structure shift", "ok": True,
         "detail": f"close {px(S[shift.idx].c)} through {'lower-high' if d == LONG else 'higher-low'} "
                   f"{px(shift.level)} · {shift.idx - trig.idx} bar(s) after the {trig.kind}"},
        {"step": 4, "name": "Discount / premium", "ok": in_half,
         "detail": f"range {px(lo)}–{px(hi)} · EQ {px(eq)} · price {px(price)} "
                   f"{'in ' + half if in_half else 'not yet in ' + half}"},
        {"step": 5, "name": "C2 / C3 closure", "ok": True, "detail": f"{tier.struct_tf} {closure_txt}"},
        {"step": 6, "name": "Zone", "ok": True, "detail": zone_txt},
        {"step": 7, "name": "Entry candle", "ok": confirm is not None, "detail": entry_txt},
    ]
    tail = S[-CHART_BARS:]
    return Setup(
        symbol=symbol, tier=tier.name, struct_tf=tier.struct_tf, entry_tf=tier.entry_tf, direction=d,
        status=status, trigger=trig.kind, family=family, tag=tag,
        setup_id=f"{symbol}:{tier.name}:{d}:{S[trig.idx].ts}", bias=bias, structure=structure,
        price=price, entry=entry, stop=stop, target=target, dol=dol, dol_kind=dol_kind, rr=round(rr, 2),
        risk_pct=round(risk / entry, 5),
        raid={"kind": trig.kind, "level": trig.level, "extreme": trig.extreme, "ts": S[trig.idx].ts,
              "ext_ts": S[trig.ext_idx].ts, "confirm_ts": S[trig.confirm_idx].ts, "pool_ts": S[trig.pool_idx].ts},
        mss={"level": shift.level, "pivot_ts": S[shift.pivot_idx].ts, "ts": S[shift.idx].ts},
        range={"lo": lo, "hi": hi, "eq": eq},
        poi={"kind": zone.kind, "lo": zone.lo, "hi": zone.hi, "ts": S[zone.idx].ts, "src_lo": zone.src_lo,
             "src_hi": zone.src_hi},
        confirm=confirm, checklist=checklist,
        chart={"tf": tier.struct_tf, "t": [x.ts for x in tail], "o": [sig(x.o) for x in tail],
               "h": [sig(x.h) for x in tail], "l": [sig(x.l) for x in tail], "c": [sig(x.c) for x in tail]},
        found_at=now_ms,
        closure=closure["kind"], entry_kind=confirm["kind"] if confirm else "", fvg=fvg_flag,
        candle={"kind": closure["kind"], "level": closure["level"], "c1_ts": S[closure["c1"]].ts,
                "c2_ts": S[closure["c2"]].ts, "close_ts": S[closure["close"]].ts},
        series=series,
    )
