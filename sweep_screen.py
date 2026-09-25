"""sweep_screen.py — Module A: liquidity-sweep screener over the whole universe.

Spec: MODULE_A_liquidity_sweep.md. For every coin and both structure TFs (4H, 1H) the most recent
stop raid (strategy.liquidity_sweep over the last SWEEP_LOOKBACK bars) is scored on its acceptance
clauses:

  oi            open interest flushed through the sweep      UNAVAILABLE — Bitget public data has no OI history
  cvd           cumulative volume delta diverged             UNAVAILABLE — no historical trade tape / CVD
  volume        sweep volume ≥ 1.5× the prior 20-bar average  (stops triggering = volume)
  displacement  a reversal body ≥ 1.5× the prior 20-bar average body, within 3 bars of the reclaim
  time_decay    freshness weight 0.5^(age / 6 bars) ≥ 0.25   (≤ 12 bars old)

Degraded mode: the mandatory OI gate is OFF, so a sweep is PROMOTED when volume ∧ displacement ∧
time_decay pass. Module A is a research screen: promoted candidates are reported, they do not gate the
engine (the framework-signal scan is the only input to the trade loop).
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

import strategy
from exchange import Candle

SWEEP_TFS = ("4H", "1H")
SWEEP_LOOKBACK = 24
BASELINE_BARS = 20
VOLUME_MIN = 1.5
DISPLACEMENT_MIN = 1.5
DISPLACEMENT_BARS = 3
DECAY_HALF_LIFE = 6.0
DECAY_MIN = 0.25
MODE = "degraded"
CLAUSES = ("oi", "cvd", "volume", "displacement", "time_decay")
UNAVAILABLE = ("oi", "cvd")


def score_sweep(c: Sequence[Candle], direction: str, trig: strategy.Trigger) -> Optional[dict]:
    n = len(c)
    j, r = trig.idx, trig.confirm_idx
    base = c[max(0, j - BASELINE_BARS):j]
    if len(base) < 5:
        return None
    sg = 1 if direction == "long" else -1
    avg_vol = sum(x.v for x in base) / len(base) or 1e-12
    avg_body = sum(abs(x.c - x.o) for x in base) / len(base) or 1e-12
    vol_ratio = max(x.v for x in c[j:r + 1]) / avg_vol
    disp_end = min(n, r + DISPLACEMENT_BARS + 1)
    disp_ratio = max(sg * (x.c - x.o) for x in c[j:disp_end]) / avg_body
    age = n - 1 - j
    decay = 0.5 ** (age / DECAY_HALF_LIFE)
    clauses = {"oi": None, "cvd": None, "volume": vol_ratio >= VOLUME_MIN,
               "displacement": disp_ratio >= DISPLACEMENT_MIN, "time_decay": decay >= DECAY_MIN}
    score = 100.0 * decay * (0.5 * min(vol_ratio, 3.0) / 3.0 + 0.5 * min(max(disp_ratio, 0.0), 3.0) / 3.0)
    return {"vol_ratio": round(vol_ratio, 2), "disp_ratio": round(disp_ratio, 2), "decay": round(decay, 3),
            "age_bars": age, "clauses": clauses, "score": round(score, 1),
            "promoted": all(clauses[k] for k in ("volume", "displacement", "time_decay"))}


def screen_symbol(symbol: str, get: Callable[[str, int], List[Candle]], price: float) -> List[dict]:
    out = []
    for tf in SWEEP_TFS:
        c = get(tf, 300)
        if len(c) < BASELINE_BARS + 10:
            continue
        for direction in ("long", "short"):
            trig = strategy.liquidity_sweep(c, direction, lookback=SWEEP_LOOKBACK)
            if trig is None:
                continue
            s = score_sweep(c, direction, trig)
            if s is None:
                continue
            s.update(symbol=symbol, tf=tf, direction=direction,
                     pool="sell-side" if direction == "long" else "buy-side",
                     level=trig.level, extreme=trig.extreme, pierce_ts=c[trig.idx].ts,
                     reclaim_ts=c[trig.confirm_idx].ts, price=price,
                     from_extreme=(price - trig.extreme) / trig.extreme)
            out.append(s)
    return out


def screen(symbols: Sequence[str], getters: Dict[str, Callable], prices: Dict[str, float]) -> dict:
    sweeps: List[dict] = []
    for sym in symbols:
        try:
            sweeps.extend(screen_symbol(sym, getters[sym], prices[sym]))
        except Exception as exc:  # one bad symbol never kills the screen
            sweeps.append({"symbol": sym, "error": str(exc)[:200]})
    good = [s for s in sweeps if "error" not in s]
    good.sort(key=lambda s: -s["score"])
    return {"mode": MODE, "clauses": list(CLAUSES), "unavailable": list(UNAVAILABLE),
            "gate": "volume ∧ displacement ∧ time_decay (OI gate off: no historical OI)",
            "sweeps": good, "promoted": [s for s in good if s["promoted"]],
            "errors": [s for s in sweeps if "error" in s]}
