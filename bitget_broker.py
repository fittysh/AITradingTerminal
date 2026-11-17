"""bitget_broker.py — demo/live execution on Bitget USDT-M (used when TRADING_MODE=demo or live).

Exchange-held protection, so the R-ladder keeps working with the machine off:
  • the market entry carries a preset stop-loss (−1R) and a preset take-profit (the DOL target)
  • right after the fill: a TP1 profit_plan closes 50% at +1R, and a moving_plan trails the runner
    1% behind the peak once +1R is reached
manage() (sync) then keeps the local ladder in step: TP1 detected → stop to breakeven; +2R printed →
stop locked at +1R; trailing ratchets; the 48h time-stop closes at market. Bitget's position history
stays the authoritative P&L (bitget_report.py).

The order path must be exercised on Bitget DEMO first; live is refused unless LIVE_CONFIRM matches and
the edge gate (paper_bot.edge_gate) passes.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional

import config
import paper_bot as pb
from exchange import BitgetPrivate, Contract, ExchangeError, MarketData, Ticker

STOP_UPDATE_MIN_R = 0.1       # only move the exchange stop when it improves by ≥ 0.1R (API budget)


def _fmt(x: float, places: int) -> str:
    return f"{x:.{max(0, places)}f}"


class BitgetBroker:
    def __init__(self, client: BitgetPrivate, md: MarketData, mode: str, log: logging.Logger):
        self.client, self.md, self.mode, self.log = client, md, mode, log
        self._prepared: set = set()
        self._hedge: Optional[bool] = None

    # -- helpers -----------------------------------------------------------------------------------
    def entry_price(self, direction: str, price: float) -> float:
        return price

    def hedge(self, symbol: str) -> bool:
        if self._hedge is None:
            try:
                self._hedge = self.client.account(symbol).get("posMode") == "hedge_mode"
            except ExchangeError as exc:
                self.log.warning("posMode lookup failed (%s) — assuming one-way", exc)
                self._hedge = False
        return self._hedge

    def hold_side(self, symbol: str, direction: str) -> str:
        if self.hedge(symbol):
            return direction
        return "buy" if direction == "long" else "sell"

    def _prepare(self, symbol: str) -> None:
        if symbol in self._prepared:
            return
        for call in (lambda: self.client.set_margin_mode(symbol, config.MARGIN_MODE),
                     lambda: self.client.set_leverage(symbol, config.LEVERAGE)):
            try:
                call()
            except ExchangeError as exc:          # "already set" style errors are harmless
                self.log.info("%s prepare: %s", symbol, exc)
        self._prepared.add(symbol)

    def _position(self, symbol: str, direction: str) -> Optional[dict]:
        for p in self.client.positions():
            side = p.get("holdSide", "")
            if p.get("symbol") == symbol and float(p.get("total") or 0) > 0 and side in (direction, "buy" if direction == "long" else "sell"):
                return p
        return None

    # -- orders --------------------------------------------------------------------------------------
    def open(self, setup: dict, qty: float, price: float, now_ms: int, contract: Optional[Contract] = None) -> dict:
        sym, d = setup["symbol"], setup["direction"]
        contract = contract or Contract(sym, sym[:-4])
        self._prepare(sym)
        pp, vp = contract.price_place, contract.volume_place
        stop = contract.round_price(setup["stop"], "down" if d == "long" else "up")
        target = contract.round_price(setup["target"], "down" if d == "long" else "up")
        oid = f"att{now_ms}{sym[:8]}".lower()[:40]
        res = self.client.place_market(sym, "buy" if d == "long" else "sell", _fmt(qty, vp), hedge=self.hedge(sym),
                                       stop_loss=_fmt(stop, pp), take_profit=_fmt(target, pp), client_oid=oid)
        fill, size = price, qty
        for _ in range(4):
            time.sleep(0.5)
            p = self._position(sym, d)
            if p:
                fill = float(p.get("openPriceAvg") or price)
                size = float(p.get("total") or qty)
                break
        setup = dict(setup, stop=stop)
        step = contract.size_mult or 10 ** -vp
        pos = pb.open_position(setup, size, fill, fill * size * config.TAKER_FEE, now_ms, self.mode, step,
                               order={"entry_id": res.get("orderId"), "client_oid": oid})
        half = math.floor(size * pb.TP1_FRACTION / step + 1e-9) * step
        hold = self.hold_side(sym, d)
        tp1 = _fmt(contract.round_price(pos["tp1"]), pp)
        try:
            if 0 < half < size:
                r1 = self.client.place_tpsl(sym, "profit_plan", tp1, hold, size=_fmt(half, vp))
                r2 = self.client.place_tpsl(sym, "moving_plan", tp1, hold, size=_fmt(size - half, vp),
                                            range_rate=config.BITGET_TRAIL_RANGE_RATE)
                pos["order"].update(tp1_plan=r1.get("orderId"), trail_plan=r2.get("orderId"))
        except ExchangeError as exc:
            self.log.error("%s: ladder plan orders failed (%s) — position keeps its preset SL/TP", sym, exc)
            pos["order"]["plan_error"] = str(exc)[:200]
        return pos

    def close(self, pos: dict, price: float, reason: str, now_ms: int) -> dict:
        sym = pos["symbol"]
        self.client.close_position(sym, pos["direction"] if self.hedge(sym) else None)
        return pb.close_all(pos, price, reason, now_ms, config.TAKER_FEE, 0.0)

    def set_stop(self, pos: dict, stop: float, contract: Optional[Contract] = None) -> bool:
        sym = pos["symbol"]
        contract = contract or Contract(sym, sym[:-4])
        try:
            for plan in self.client.pending_tpsl(sym):
                if plan.get("planType") in ("pos_loss", "loss_plan"):
                    self.client.modify_tpsl(sym, plan["orderId"], _fmt(contract.round_price(stop), contract.price_place))
                    return True
            self.client.place_tpsl(sym, "pos_loss", _fmt(contract.round_price(stop), contract.price_place),
                                   self.hold_side(sym, pos["direction"]))
            return True
        except ExchangeError as exc:
            self.log.error("%s: stop update to %s failed: %s", sym, stop, exc)
            return False

    # -- reconciliation (the manage job in demo/live) -------------------------------------------------
    def sync(self, state: dict, tick: Dict[str, Ticker], now_ms: int) -> List[dict]:
        events: List[dict] = []
        contracts = self.md.contracts()
        live = {}
        for p in self.client.positions():
            if float(p.get("total") or 0) > 0:
                live[p["symbol"]] = p
        for pos in list(state["positions"]):
            sym = pos["symbol"]
            t = tick.get(sym)
            price = t.last if t else pos["last_price"]
            ex = live.get(sym)
            if ex is None:                                    # SL / TP / trail filled on the exchange
                exit_px = self._last_fill_price(pos, now_ms) or price
                d = pos["dir"]
                if d * (exit_px - pos["target"]) >= -abs(pos["r_price"]) * 0.05:
                    reason = "target"
                elif abs(exit_px - pos["stop"]) <= abs(pos["r_price"]) * 0.25:
                    reason = pos["stop_kind"]
                else:
                    reason = "trail" if pos["trail_armed"] else "exchange"
                events.append(pb.close_all(pos, exit_px, reason, now_ms, config.TAKER_FEE, 0.0))
                continue
            size = float(ex.get("total") or 0)
            if not pos["tp1_done"] and size < pos["qty_open"] * 0.95:
                q = pos["qty_open"] - size
                ev = pb._close_qty(pos, q, pos["tp1"], "tp1", "tp1", now_ms, config.TAKER_FEE, 0.0)
                pos["tp1_done"] = pos["trail_armed"] = True
                pos["stop"], pos["stop_kind"] = pos["entry"], "breakeven"
                self.set_stop(pos, pos["entry"], contracts.get(sym))
                events.append(dict(ev, note="TP1 filled on exchange · stop → breakeven"))
            before = pos["stop"]
            new_stop, evs = pb.ladder_stop(pos, price, now_ms)
            events.extend(evs)
            if pos["dir"] * (new_stop - before) >= STOP_UPDATE_MIN_R * pos["r_price"]:
                self.set_stop(pos, new_stop, contracts.get(sym))
            pos["mark_price"], pos["mark_ts"] = price, now_ms
            if pb.time_stop_due(pos, now_ms):
                events.append(self.close(pos, price, "time", now_ms))
        return events

    def _last_fill_price(self, pos: dict, now_ms: int) -> Optional[float]:
        try:
            fills = [f for f in self.client.fills(pos["opened_at"], now_ms, pos["symbol"])
                     if int(f.get("cTime") or 0) > pos["opened_at"] + 1000]
        except ExchangeError as exc:
            self.log.warning("%s: fill history unavailable: %s", pos["symbol"], exc)
            return None
        if not fills:
            return None
        last = max(fills, key=lambda f: int(f.get("cTime") or 0))
        return float(last.get("price") or 0) or None
