"""paper_bot.py — the protection shell (FRAMEWORK.md → "Rules") and the R-ladder.

Shell (checked before every NEW entry; open positions always keep being managed):
  • Risk $2.5 per position (= 1R); market orders.
  • At most 5 open positions ($12.50 max exposure); correlation cap 3 per side.
  • Daily LOSS stop (ARMED): halt new entries after −$15 realized OR 6 losing trades on the day.
  • PROTECT: once +$15 is realized (daily target) halt new entries for the rest of the day.
  • Daily gates reset at MYT midnight. "Realized" is net: fees and funding included, partial
    take-profits counted when they fill.

R-ladder (mark()): hard stop −1R · at +1R take 50% off, stop → breakeven, arm trailing · the runner
trails 1% behind the peak and locks +1R once +2R prints · target = the DOL (≥1:2) · 48h time-stop.

In paper mode the exchange's resting orders are simulated by replaying the finest closed candles since
the last mark, so stops/targets that traded while the machine was off still fill at their levels.
Within a candle the path is O→L→H→C for an up-close candle and O→H→L→C for a down-close candle.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import config
import timeutil as tu
from exchange import Candle, Contract

# --- the protection shell (FRAMEWORK.md) ----------------------------------------------------------
RISK_PER_TRADE = 2.5            # $ per position = 1R
MAX_OPEN_POSITIONS = 5          # $12.50 max exposure
CORR_MAX_PER_SIDE = 3           # correlation cap: max open longs / max open shorts
DAILY_MAX_LOSS = 15.0           # loss stop: halt new entries at −$15 realized …
MAX_LOSSES_PER_DAY = 6          # … or after 6 losing trades on the day
DAILY_TARGET = 15.0             # PROTECT mode at +$15 realized
MAX_TRADE_AGE_HOURS = 48        # time-stop

# --- R-ladder ------------------------------------------------------------------------------------
TP1_R = 1.0                     # first scale-out at +1R …
TP1_FRACTION = 0.5              # … of 50%; stop → breakeven; trailing armed
TRAIL_PCT = 0.01                # runner trails 1% behind the peak
LOCK_TRIGGER_R = 2.0            # once +2R prints …
LOCK_R = 1.0                    # … the stop locks at least +1R
FUNDING_EVERY_H = 8             # Bitget USDT-M funding settles 00:00 / 08:00 / 16:00 UTC

STOP_KINDS = ("stop", "breakeven", "trail", "lock")


# --- state ---------------------------------------------------------------------------------------

def new_state(balance: float = config.ACCOUNT_SIZE) -> dict:
    return {"version": 4, "account": {"start_balance": balance}, "positions": [], "closed": [],
            "traded_setups": {}}


def balance(state: dict) -> float:
    """Start balance + every realized fill (net of fees and funding), open positions included."""
    total = state["account"]["start_balance"]
    for p in state["positions"] + state["closed"]:
        total += sum(f["pnl"] - f["fee"] for f in p["fills"])
    return total


def unrealized(state: dict) -> float:
    out = 0.0
    for p in state["positions"]:
        price = p.get("mark_price") or p.get("last_price")
        if price:
            out += p["dir"] * (price - p["entry"]) * p["qty_open"]
    return out


def realized_between(state: dict, start: int, end: int) -> float:
    total = 0.0
    for p in state["positions"] + state["closed"]:
        for f in p["fills"]:
            if start <= f["ts"] < end:
                total += f["pnl"] - f["fee"]
    return total


# --- gates ---------------------------------------------------------------------------------------

@dataclass
class Gates:
    day: str
    start: int
    end: int
    realized: float
    wins: int
    losses: int
    open: int
    longs: int
    shorts: int
    loss_stop: bool          # triggered (the stop itself is always ARMED)
    protect: bool
    reason: str

    @property
    def entries_allowed(self) -> bool:
        return not (self.loss_stop or self.protect)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(entries_allowed=self.entries_allowed, max_loss=DAILY_MAX_LOSS, max_losses=MAX_LOSSES_PER_DAY,
                 target=DAILY_TARGET, max_open=MAX_OPEN_POSITIONS, corr_max=CORR_MAX_PER_SIDE)
        return d


def gates(state: dict, now_ms: int) -> Gates:
    start, end = tu.day_bounds(now_ms)
    realized = realized_between(state, start, end)
    today = [t for t in state["closed"] if start <= t["closed_at"] < end]
    wins = sum(1 for t in today if t["net_pnl"] > 0)
    losses = sum(1 for t in today if t["net_pnl"] < 0)
    longs = sum(1 for p in state["positions"] if p["direction"] == "long")
    shorts = len(state["positions"]) - longs
    loss_stop = realized <= -DAILY_MAX_LOSS or losses >= MAX_LOSSES_PER_DAY
    protect = realized >= DAILY_TARGET
    if loss_stop:
        reason = (f"daily loss stop hit ({realized:+.2f} ≤ −{DAILY_MAX_LOSS:.0f})" if realized <= -DAILY_MAX_LOSS
                  else f"daily loss stop hit ({losses} losing trades)")
    elif protect:
        reason = f"PROTECT: daily target reached ({realized:+.2f} ≥ +{DAILY_TARGET:.0f})"
    else:
        reason = "entries allowed"
    return Gates(tu.date_str(start), start, end, round(realized, 6), wins, losses, len(state["positions"]),
                 longs, shorts, loss_stop, protect, reason)


def check_entry(state: dict, g: Gates, direction: str, symbol: str, setup_id: Optional[str] = None) -> Tuple[bool, str]:
    if not g.entries_allowed:
        return False, g.reason
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False, f"max open positions ({MAX_OPEN_POSITIONS})"
    side = sum(1 for p in state["positions"] if p["direction"] == direction)
    if side >= CORR_MAX_PER_SIDE:
        return False, f"correlation cap ({CORR_MAX_PER_SIDE} {direction}s open)"
    if any(p["symbol"] == symbol for p in state["positions"]):
        return False, "already in a position on this symbol"
    if setup_id and setup_id in state["traded_setups"]:
        return False, "this setup was already traded"
    return True, "ok"


def size_position(entry: float, stop: float, contract: Optional[Contract] = None,
                  risk: float = RISK_PER_TRADE) -> Tuple[float, float, str]:
    """(qty, risk $, reject reason): qty = 1R / stop distance, rounded DOWN to the exchange step."""
    dist = abs(entry - stop)
    if dist <= 0:
        return 0.0, 0.0, "zero stop distance"
    qty = risk / dist
    if contract is not None:
        qty = contract.round_qty(qty)
        if qty <= 0 or qty < contract.min_qty or qty * entry < contract.min_usdt:
            return 0.0, 0.0, f"size below exchange minimum (qty {qty}, notional ${qty * entry:.2f})"
    return qty, qty * dist, ""


# --- positions -----------------------------------------------------------------------------------

def open_position(setup: dict, qty: float, fill: float, fee: float, now_ms: int, mode: str,
                  qty_step: float = 0.0, order: Optional[dict] = None) -> dict:
    """Book a filled entry. R is re-measured from the actual fill to the setup's stop; the 1:2 floor
    is re-applied from the fill, the DOL target is kept."""
    d = 1 if setup["direction"] == "long" else -1
    stop = setup["stop"]
    r = d * (fill - stop)
    if r <= 0:
        raise ValueError(f"fill {fill} is beyond the stop {stop}")
    floor = fill + d * 2.0 * r
    dol = setup.get("dol")
    target = dol if dol is not None and d * (dol - floor) > 0 else floor
    return {
        "id": f"{setup['symbol']}-{now_ms}", "symbol": setup["symbol"], "direction": setup["direction"], "dir": d,
        "tier": setup["tier"], "tag": setup["tag"], "family": setup["family"], "trigger": setup["trigger"],
        "setup_id": setup["setup_id"], "struct_tf": setup["struct_tf"], "entry_tf": setup["entry_tf"],
        "mode": mode, "opened_at": now_ms, "entry": fill, "qty": qty, "qty_open": qty, "qty_step": qty_step,
        "initial_stop": stop, "stop": stop, "stop_kind": "stop", "target": target, "dol": dol,
        "tp1": fill + d * TP1_R * r, "lock_trigger": fill + d * LOCK_TRIGGER_R * r, "lock_level": fill + d * LOCK_R * r,
        "r_price": r, "risk_usd": qty * r, "tp1_done": False, "trail_armed": False, "lock_done": False,
        "peak": fill, "trough": fill, "last_mark_ts": now_ms, "last_funding_ts": now_ms,
        "last_price": fill, "last_price_ts": now_ms, "funding": 0.0,
        "fills": [{"ts": now_ms, "kind": "entry", "qty": qty, "price": fill, "pnl": 0.0, "fee": fee}],
        "order": order or {},
        "setup": {k: setup.get(k) for k in ("status", "bias", "structure", "rr", "entry", "target", "raid", "mss",
                                            "range", "poi", "confirm", "checklist", "dol_kind", "risk_pct")},
    }


def r_now(pos: dict, price: Optional[float] = None) -> float:
    """Open R of the position at `price` (default: latest mark, else the ladder's last replayed close)."""
    price = (pos.get("mark_price") or pos["last_price"]) if price is None else price
    return pos["dir"] * (price - pos["entry"]) / pos["r_price"]


def _fill(pos: dict, price: float, slippage: float) -> float:
    """Exit fills are market orders: slippage always works against the position."""
    return price * (1 - pos["dir"] * slippage)


def _close_qty(pos: dict, qty: float, price: float, kind: str, reason: str, ts: int, fee_rate: float,
               slippage: float) -> dict:
    px = _fill(pos, price, slippage)
    pnl = pos["dir"] * (px - pos["entry"]) * qty
    fee = px * qty * fee_rate
    pos["fills"].append({"ts": ts, "kind": kind, "reason": reason, "qty": qty, "price": px, "pnl": pnl, "fee": fee})
    pos["qty_open"] = max(0.0, pos["qty_open"] - qty)
    if pos["qty_open"] <= 1e-12:
        pos["qty_open"] = 0.0
        pos["closed_at"], pos["exit_reason"], pos["exit_price"] = ts, reason, px
    return {"kind": kind, "reason": reason, "symbol": pos["symbol"], "ts": ts,
            "qty": qty, "price": px, "pnl": pnl - fee, "r": r_now(pos, px), "id": pos["id"]}


def close_all(pos: dict, price: float, reason: str, ts: int, fee_rate: float = config.TAKER_FEE,
              slippage: float = config.PAPER_SLIPPAGE) -> dict:
    return _close_qty(pos, pos["qty_open"], price, "exit", reason, ts, fee_rate, slippage)


def _tp1(pos: dict, price: float, ts: int, fee_rate: float, slippage: float) -> dict:
    step = pos.get("qty_step") or 0.0
    half = pos["qty"] * TP1_FRACTION
    if step > 0:
        half = math.floor(half / step + 1e-9) * step
    pos["tp1_done"] = True
    pos["trail_armed"] = True
    if pos["dir"] * (pos["entry"] - pos["stop"]) > 0:
        pos["stop"], pos["stop_kind"] = pos["entry"], "breakeven"
    if 0 < half < pos["qty_open"]:
        ev = _close_qty(pos, half, price, "tp1", "tp1", ts, fee_rate, slippage)
    else:                                   # cannot split at the exchange step → keep the full runner
        ev = {"kind": "tp1", "reason": "tp1", "symbol": pos["symbol"], "ts": ts, "qty": 0.0, "price": price,
              "pnl": 0.0, "r": r_now(pos, price), "id": pos["id"]}
    ev["note"] = "50% off at +1R · stop → breakeven · trailing armed"
    return ev


def _ratchet(pos: dict, price: float, ts: int, events: List[dict]) -> None:
    """New favourable extreme → move the peak, trail 1% behind it, lock +1R once +2R printed."""
    d = pos["dir"]
    if d * (price - pos["peak"]) > 0:
        pos["peak"] = price
    if pos["trail_armed"]:
        trail = pos["peak"] * (1 - d * TRAIL_PCT)
        if d * (trail - pos["stop"]) > 0:
            pos["stop"], pos["stop_kind"] = trail, "trail"
    if not pos["lock_done"] and d * (pos["peak"] - pos["lock_trigger"]) >= 0:
        pos["lock_done"] = True
        if d * (pos["lock_level"] - pos["stop"]) > 0:
            pos["stop"], pos["stop_kind"] = pos["lock_level"], "lock"
        events.append({"kind": "lock", "symbol": pos["symbol"], "ts": ts, "price": price, "id": pos["id"],
                       "note": f"+{LOCK_TRIGGER_R:.0f}R printed · stop locked ≥ +{LOCK_R:.0f}R"})


def _walk(pos: dict, pts: Sequence[float], ts: int, events: List[dict], fee_rate: float, slippage: float) -> bool:
    """Walk one candle's price path; the first segment (previous close → open) is the gap segment,
    where a crossed level fills at the open rather than at the level. Returns True once flat."""
    d = pos["dir"]
    for k in range(1, len(pts)):
        a, b = pts[k - 1], pts[k]
        gap = k == 1
        if d * (b - a) > 0:
            if not pos["tp1_done"] and d * (b - pos["tp1"]) >= 0:
                events.append(_tp1(pos, b if gap else pos["tp1"], ts, fee_rate, slippage))
                if pos["qty_open"] <= 0:
                    return True
            if d * (b - pos["target"]) >= 0:
                events.append(_close_qty(pos, pos["qty_open"], b if gap else pos["target"], "exit", "target", ts,
                                         fee_rate, slippage))
                return True
            _ratchet(pos, b, ts, events)
        elif d * (b - a) < 0:
            if d * (b - pos["stop"]) <= 0:
                events.append(_close_qty(pos, pos["qty_open"], b if gap else pos["stop"], "exit", pos["stop_kind"],
                                         ts, fee_rate, slippage))
                return True
            if d * (b - pos["trough"]) < 0:
                pos["trough"] = b
    return False


def mark(pos: dict, candles: Iterable[Candle], now_ms: int, bar_ms: int, fee_rate: float = config.TAKER_FEE,
         slippage: float = config.PAPER_SLIPPAGE) -> List[dict]:
    """Advance an open paper position through every closed candle since its last mark (R-ladder +
    48h time-stop). Mutates `pos`; returns the ladder events (tp1, lock, exit)."""
    events: List[dict] = []
    deadline = pos["opened_at"] + MAX_TRADE_AGE_HOURS * tu.HOUR
    for c in candles:
        if pos["qty_open"] <= 0:
            break
        if c.ts < pos["last_mark_ts"]:
            continue
        if c.ts + bar_ms > now_ms:
            break                                      # forming candle — never replayed
        if c.ts >= deadline:
            events.append(close_all(pos, c.o, "time", c.ts, fee_rate, slippage))
            pos["last_price"], pos["last_price_ts"] = c.o, c.ts
            break
        path = (c.o, c.l, c.h, c.c) if c.c >= c.o else (c.o, c.h, c.l, c.c)
        done = _walk(pos, (pos["last_price"],) + path, c.ts, events, fee_rate, slippage)
        pos["last_mark_ts"] = c.ts + bar_ms
        pos["last_price"], pos["last_price_ts"] = c.c, c.ts + bar_ms
        if done:
            break
    return events


def time_stop_due(pos: dict, now_ms: int) -> bool:
    return pos["qty_open"] > 0 and now_ms >= pos["opened_at"] + MAX_TRADE_AGE_HOURS * tu.HOUR


def ladder_stop(pos: dict, price: float, ts: int) -> Tuple[float, List[dict]]:
    """Exchange mode: fold the latest price into the ladder and return the stop the exchange should hold."""
    events: List[dict] = []
    _ratchet(pos, price, ts, events)
    return pos["stop"], events


def qty_at(pos: dict, ts: int) -> float:
    q = 0.0
    for f in pos["fills"]:                  # not chronological: funding is settled after the ladder
        if f["ts"] > ts:
            continue
        if f["kind"] == "entry":
            q += f["qty"]
        elif f["kind"] in ("tp1", "exit"):
            q -= f["qty"]
    return max(0.0, q)


def apply_funding(pos: dict, rate: float, until_ms: int, price: float) -> List[dict]:
    """Settle funding for every 8h boundary crossed since the last settlement (longs pay a positive rate)."""
    step = FUNDING_EVERY_H * tu.HOUR
    t = (pos["last_funding_ts"] // step + 1) * step
    events = []
    while t <= until_ms:
        q = qty_at(pos, t)
        if q > 0 and rate:
            amt = -pos["dir"] * q * price * rate
            pos["funding"] += amt
            pos["fills"].append({"ts": t, "kind": "funding", "qty": 0.0, "price": price, "pnl": amt, "fee": 0.0})
            events.append({"kind": "funding", "symbol": pos["symbol"], "ts": t, "pnl": amt, "rate": rate})
        t += step
    pos["last_funding_ts"] = max(pos["last_funding_ts"], until_ms)
    return events


def finalize(pos: dict) -> dict:
    """Closed-trade record: gross / fees / funding / net, R multiple, MFE/MAE, hold time."""
    gross = sum(f["pnl"] for f in pos["fills"] if f["kind"] in ("tp1", "exit"))
    fees = sum(f["fee"] for f in pos["fills"])
    funding = sum(f["pnl"] for f in pos["fills"] if f["kind"] == "funding")
    net = gross - fees + funding
    t = dict(pos)
    t.update(gross_pnl=gross, fees=fees, funding=funding, net_pnl=net, r_mult=net / pos["risk_usd"],
             mfe_r=pos["dir"] * (pos["peak"] - pos["entry"]) / pos["r_price"],
             mae_r=pos["dir"] * (pos["trough"] - pos["entry"]) / pos["r_price"],
             hold_h=(pos["closed_at"] - pos["opened_at"]) / tu.HOUR)
    return t


# --- statistics ----------------------------------------------------------------------------------

def trade_stats(closed: Sequence[dict]) -> dict:
    n = len(closed)
    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] < 0]
    gp = sum(t["net_pnl"] for t in wins)
    gl = -sum(t["net_pnl"] for t in losses)
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(closed, key=lambda x: x["closed_at"]):
        eq += t["net_pnl"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n if n else None,
        "net": sum(t["net_pnl"] for t in closed), "gross": sum(t["gross_pnl"] for t in closed),
        "fees": sum(t["fees"] for t in closed), "funding": sum(t["funding"] for t in closed),
        "profit_factor": (gp / gl) if gl > 0 else None,
        "expectancy_r": sum(t["r_mult"] for t in closed) / n if n else None,
        "avg_win_r": sum(t["r_mult"] for t in wins) / len(wins) if wins else None,
        "avg_loss_r": sum(t["r_mult"] for t in losses) / len(losses) if losses else None,
        "best_r": max((t["r_mult"] for t in closed), default=None),
        "worst_r": min((t["r_mult"] for t in closed), default=None),
        "avg_hold_h": sum(t["hold_h"] for t in closed) / n if n else None,
        "max_drawdown": dd,
    }


def breakdown(closed: Sequence[dict], key: str) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = {}
    for t in closed:
        groups.setdefault(str(t.get(key)), []).append(t)
    return {k: trade_stats(v) for k, v in sorted(groups.items())}


def equity_curve(state: dict) -> List[Tuple[int, float]]:
    """(ts, balance) after every realized fill, starting from the account's start balance."""
    fills = sorted((f["ts"], f["pnl"] - f["fee"]) for p in state["positions"] + state["closed"] for f in p["fills"])
    eq = state["account"]["start_balance"]
    out = [(fills[0][0] if fills else tu.now_ms(), eq)]
    for ts, amt in fills:
        eq += amt
        out.append((ts, eq))
    return out


def edge_gate(state: dict, min_trades: int = config.EDGE_MIN_TRADES,
              min_pf: float = config.EDGE_MIN_PROFIT_FACTOR) -> Tuple[bool, str]:
    """Live orders stay disabled until the demo/paper record proves positive (framework signal-edge note)."""
    s = trade_stats(state["closed"])
    if s["trades"] < min_trades:
        return False, f"only {s['trades']} closed demo trades (need ≥ {min_trades})"
    if s["net"] <= 0 or (s["expectancy_r"] or 0) <= 0:
        return False, f"net-of-fees result not positive ({s['net']:+.2f}, {s['expectancy_r'] or 0:+.2f}R/trade)"
    if s["profit_factor"] is not None and s["profit_factor"] < min_pf:
        return False, f"profit factor {s['profit_factor']:.2f} < {min_pf}"
    return True, f"{s['trades']} trades · {s['net']:+.2f} net · PF {s['profit_factor'] or 0:.2f}"


# --- paper execution -----------------------------------------------------------------------------

class PaperBroker:
    """Local fills against live (or replayed) prices; resting orders are simulated by mark()."""

    mode = "paper"

    def __init__(self, fee_rate: float = config.TAKER_FEE, slippage: float = config.PAPER_SLIPPAGE):
        self.fee_rate = fee_rate
        self.slippage = slippage

    def entry_price(self, direction: str, price: float) -> float:
        return price * (1 + (1 if direction == "long" else -1) * self.slippage)

    def open(self, setup: dict, qty: float, price: float, now_ms: int, contract: Optional[Contract] = None) -> dict:
        fill = self.entry_price(setup["direction"], price)
        step = (contract.size_mult or 10 ** -contract.volume_place) if contract else 0.0
        return open_position(setup, qty, fill, fill * qty * self.fee_rate, now_ms, self.mode, step)

    def close(self, pos: dict, price: float, reason: str, now_ms: int) -> dict:
        return close_all(pos, price, reason, now_ms, self.fee_rate, self.slippage)
