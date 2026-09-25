#!/usr/bin/env python3
"""run_bot.py — the 24/7 bot (FRAMEWORK.md → Core Responsibilities). Market day from 07:00 MYT.

  scan      routine 1 (top 20 movers + 20 losers, vol ≥ $1M) + Module A (liquidity sweeps) +
            routine 2 (PDH/PDL daily failure, research only) → Telegram + Gmail
  signals   the v4 engine over ALL coins → signals_live.json (signals + in-zone watch) → Telegram + Gmail
  cycle     scan → signals → trade (what the daemon runs every 4 hours)
  trade     every 30 min: enter ONLY coins listed in signals_live.json, re-validated live
  manage    ~every 5 min: R-ladder + 48h time-stop on open positions
  report    07:00 & 19:00 MYT: net-of-fees report + PDF → Telegram + Gmail; journal every day
  daemon    all of the above on the MYT schedule, with catch-up after downtime
  replay    fast-forward the whole bot over past days (synthetic market or Bitget history)
  serve · tui · status · dashboard · journal — the UIs

Everything is DEMO by default (paper fills on live Bitget public data). See README.md.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import bitget_report
import config
import dashboard
import journal
import paper_bot as pb
import schedule
import strategy
import sweep_screen
import timeutil as tu
from exchange import (BitgetPrivate, BitgetPublic, Candle, ExchangeError, MarketData, Ticker, closed_only, tf_ms,
                      universe)
from notify import Notifier, esc
from sim import HistoricalMarket, SimClock, SimMarket
from store import Paths, Store, prune_outbox
from util import compact, pct, px, rmult, usd

log = logging.getLogger("bot")


# --- context ---------------------------------------------------------------------------------------

@dataclass
class Ctx:
    md: MarketData
    broker: object
    store: Store
    notifier: Notifier
    mode: str                      # paper | demo | live
    sim: bool = False
    replay: bool = False
    client: Optional[BitgetPrivate] = None
    workers: int = config.HTTP_WORKERS
    stop: threading.Event = field(default_factory=threading.Event)

    def now(self) -> int:
        return self.md.now_ms()

    @property
    def label(self) -> str:
        if self.sim:
            return ("REPLAY" if self.replay else "SIM") + "·PAPER"
        return self.mode.upper()


def setup_logging(paths: Paths, console: bool = True, verbose: bool = False) -> logging.Logger:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(paths.state / "bot.log", maxBytes=2_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    return log


def check_live_allowed(store: Store) -> None:
    if config.LIVE_CONFIRM != config.LIVE_CONFIRM_PHRASE:
        raise SystemExit(f"LIVE refused: set LIVE_CONFIRM={config.LIVE_CONFIRM_PHRASE} to acknowledge real-money risk.")
    ok, why = pb.edge_gate(store.load_state())
    if not ok:
        raise SystemExit(f"LIVE refused by the edge gate — {why}. Run paper/demo in this home until the signal "
                         f"proves positive (framework signal-edge note).")


def build_ctx(home: Optional[str] = None, sim: bool = False, seed: int = 7, mode: Optional[str] = None,
              quiet: bool = False, console: bool = True, verbose: bool = False) -> Ctx:
    home_path = Path(home) if home else (config.ROOT / "sim" if sim else config.BOT_HOME)
    paths = Paths(home_path)
    store = Store(paths, config.ACCOUNT_SIZE)
    setup_logging(paths, console, verbose)
    mode = (mode or config.TRADING_MODE).lower()
    if mode not in ("paper", "demo", "live"):
        raise SystemExit(f"unknown TRADING_MODE {mode!r} (paper | demo | live)")
    if sim:
        md: MarketData = SimMarket(SimClock(), seed=seed)
        mode = "paper"
    else:
        md = BitgetPublic()
    client = None
    if mode == "paper":
        broker = pb.PaperBroker()
    else:
        if mode == "live":
            check_live_allowed(store)
        from bitget_broker import BitgetBroker
        client = BitgetPrivate(config.BITGET_API_KEY, config.BITGET_API_SECRET, config.BITGET_API_PASSPHRASE,
                               demo=(mode == "demo"))
        broker = BitgetBroker(client, md, mode, log)
    notifier = Notifier(log, paths.outbox, enabled=not (quiet or sim), clock=md.now_ms)
    return Ctx(md, broker, store, notifier, mode, sim=sim, client=client, workers=1 if sim else config.HTTP_WORKERS)


# --- market data plumbing ---------------------------------------------------------------------------

class CandleCache:
    """One fetch per (symbol, timeframe) per job; hands the engine closed candles only."""

    def __init__(self, md: MarketData, now: int):
        self.md, self.now = md, now
        self._raw: Dict[tuple, tuple] = {}
        self._lock = threading.Lock()
        self.errors: Dict[str, str] = {}

    def raw(self, sym: str, tf: str, limit: int) -> List[Candle]:
        key = (sym, tf)
        with self._lock:
            have = self._raw.get(key)
        if have is None or have[0] < limit:
            candles = self.md.candles(sym, tf, limit + 1)
            with self._lock:
                self._raw[key] = (limit, candles)
            return candles
        return have[1]

    def closed(self, sym: str, tf: str, limit: int) -> List[Candle]:
        return closed_only(self.raw(sym, tf, limit), tf, self.now)[-limit:]

    def forming(self, sym: str, tf: str) -> Optional[Candle]:
        c = self.raw(sym, tf, 10)
        return c[-1] if c and c[-1].ts + tf_ms(tf) > self.now else None

    def getter(self, sym: str) -> Callable[[str, int], List[Candle]]:
        return lambda tf, limit: self.closed(sym, tf, limit)

    def prefetch(self, symbols: Sequence[str], plan: Sequence[tuple], workers: int) -> None:
        def one(args):
            sym, tf, limit = args
            try:
                self.raw(sym, tf, limit)
            except Exception as exc:
                self.errors[sym] = f"{tf}: {exc}"
        pmap(one, [(s, tf, n) for s in symbols for tf, n in plan], workers)


def pmap(fn: Callable, items: Sequence, workers: int) -> list:
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def get_universe(ctx: Ctx):
    return universe(ctx.md, config.UNIVERSE_MIN_VOLUME_USD)


STRUCT_PLAN = (("1D", 10), ("4H", 300), ("1H", 300))


# --- jobs ------------------------------------------------------------------------------------------

def _pd_state(last: float, level: float, extreme: float, above: bool) -> str:
    if above:
        return "broken" if last > level else "swept" if extreme > level else "respected"
    return "broken" if last < level else "swept" if extreme < level else "respected"


def routine1_row(t: Ticker, cache: CandleCache) -> dict:
    daily = cache.closed(t.symbol, "1D", 10)
    today = cache.forming(t.symbol, "1D")
    row = {"symbol": t.symbol, "last": t.last, "change": t.change24h, "volume": t.volume_usd, "funding": t.funding,
           "pdh": None, "pdl": None, "pdh_state": "—", "pdl_state": "—"}
    if daily:
        pdh, pdl = daily[-1].h, daily[-1].l
        hi = max(today.h, t.last) if today else t.last
        lo = min(today.l, t.last) if today else t.last
        row.update(pdh=pdh, pdl=pdl, pdh_state=_pd_state(t.last, pdh, hi, True), pdl_state=_pd_state(t.last, pdl, lo, False))
    return row


def job_scan(ctx: Ctx, cache: Optional[CandleCache] = None) -> dict:
    """Routine 1 + Module A + routine 2 over the whole (crypto-only) universe."""
    now = ctx.now()
    tickers, excluded = get_universe(ctx)
    syms = [t.symbol for t in tickers]
    cache = cache or CandleCache(ctx.md, now)
    cache.prefetch(syms, STRUCT_PLAN, ctx.workers)
    liquid = [t for t in tickers if t.volume_usd >= config.MIN_VOLUME_USD]
    movers = sorted((t for t in liquid if t.change24h > 0), key=lambda t: -t.change24h)[:config.MOVERS_N]
    losers = sorted((t for t in liquid if t.change24h < 0), key=lambda t: t.change24h)[:config.MOVERS_N]
    scan = {"generated_at": now, "universe": len(tickers), "excluded": excluded, "liquid": len(liquid),
            "min_volume": config.MIN_VOLUME_USD,
            "movers": [routine1_row(t, cache) for t in movers], "losers": [routine1_row(t, cache) for t in losers]}
    ok_syms = [s for s in syms if s not in cache.errors]
    sweeps = sweep_screen.screen(ok_syms, {s: cache.getter(s) for s in ok_syms}, {t.symbol: t.last for t in tickers})
    sweeps["generated_at"] = now
    failures = []
    for t in tickers:
        if t.symbol in cache.errors:
            continue
        f = strategy.daily_failure(cache.closed(t.symbol, "1D", 10))
        if f:
            failures.append(dict(f, symbol=t.symbol, last=t.last, volume=t.volume_usd))
    failures.sort(key=lambda f: -f["volume"])
    dfail = {"generated_at": now, "note": "research watchlist only — no backtested edge, never gates entries",
             "items": failures}
    ctx.store.write("scan", scan)
    ctx.store.write("sweeps", sweeps)
    ctx.store.write("daily_failure", dfail)
    ctx.store.event("scan", now, universe=len(tickers), excluded=len(excluded), movers=len(movers),
                    losers=len(losers), sweeps=len(sweeps["sweeps"]), promoted=len(sweeps["promoted"]),
                    failures=len(failures), errors=len(cache.errors))
    log.info("scan: %d coins (%d TradFi excluded) · %d sweeps (%d promoted) · %d daily-failure · %d fetch errors",
             len(tickers), len(excluded), len(sweeps["sweeps"]), len(sweeps["promoted"]), len(failures), len(cache.errors))
    subject, tg, html = fmt_scan(ctx, scan, sweeps, dfail)
    ctx.notifier.send(subject, tg, html)
    return {"scan": scan, "sweeps": sweeps, "daily_failure": dfail}


def _rank(s: dict) -> tuple:
    return (0 if s["status"] == "signal" else 1, s["tier"], -s["rr"])


def job_signals(ctx: Ctx, cache: Optional[CandleCache] = None) -> dict:
    """The v4 engine over ALL coins → signals_live.json = signals + in-zone watch."""
    now = ctx.now()
    tickers, excluded = get_universe(ctx)
    cache = cache or CandleCache(ctx.md, now)
    cache.prefetch([t.symbol for t in tickers], STRUCT_PLAN, ctx.workers)

    def one(t: Ticker):
        try:
            return strategy.evaluate(t.symbol, cache.getter(t.symbol), t.last, now)
        except Exception as exc:
            return strategy.Result(t.symbol, None, 0, [f"error: {exc}"])

    results = pmap(one, tickers, ctx.workers)
    setups = [r.setup for r in results if r.setup]
    signals = sorted((s.to_dict() for s in setups if s.status == "signal"), key=_rank)
    watch = sorted((s.to_dict() for s in setups if s.status == "in-zone"), key=_rank)
    armed = [{k: v for k, v in s.to_dict().items() if k != "chart"} for s in setups if s.status == "armed"]
    funnel = {"universe": len(tickers)}
    for i, name in enumerate(strategy.STAGES[1:], start=1):
        funnel[name] = sum(1 for r in results if r.stage >= i)
    payload = {"generated_at": now, "engine": config.ENGINE, "universe": len(tickers), "excluded": len(excluded),
               "funnel": funnel, "signals": signals, "watch": watch,
               "note": "trade enters ONLY coins listed here (signals + in-zone watch), re-validated live"}
    ctx.store.write("signals", payload)
    ctx.store.write("armed", {"generated_at": now, "items": sorted(armed, key=lambda s: -s["rr"])})
    ctx.store.event("signals", now, signals=[f"{s['symbol']} {s['direction']} {s['tag']}" for s in signals],
                    in_zone=[f"{s['symbol']} {s['direction']}" for s in watch], armed=len(armed), funnel=funnel)
    log.info("signals: %d meet the framework · %d in-zone · %d armed (universe %d)", len(signals), len(watch),
             len(armed), len(tickers))
    subject, tg, html = fmt_signals(ctx, payload, len(armed))
    ctx.notifier.send(subject, tg, html)
    return payload


def job_cycle(ctx: Ctx) -> None:
    """The 4-hour cycle: scan + research routines, the framework-signal scan, then a trade pass."""
    cache = CandleCache(ctx.md, ctx.now())
    job_scan(ctx, cache)
    job_signals(ctx, cache)
    job_trade(ctx)


def job_trade(ctx: Ctx) -> List[dict]:
    """Enter only coins listed in signals_live.json, each re-validated on fresh candles right now."""
    now = ctx.now()
    entered: List[dict] = []
    with ctx.store.lock():
        st = ctx.store.load_state()
        sig = ctx.store.read("signals")
        if not sig:
            log.info("trade: no signals_live.json yet — run a scan cycle first")
            return entered
        age_h = (now - sig["generated_at"]) / tu.HOUR
        if age_h > config.SIGNALS_MAX_AGE_H:
            log.warning("trade: signals_live.json is %.1fh old (> %.0fh) — skipping", age_h, config.SIGNALS_MAX_AGE_H)
            return entered
        g = pb.gates(st, now)
        if not g.entries_allowed:
            day_start = tu.day_bounds(now)[0]
            gate = "loss_stop" if g.loss_stop else "protect"
            if not any(e.get("gate") == gate for e in ctx.store.events(day_start, kinds=["gate"])):
                ctx.store.event("gate", now, gate=gate, reason=g.reason)
                ctx.notifier.send("Entries halted", f"⛔ <b>ENTRIES HALTED</b> · {esc(g.reason)}\n"
                                  f"Open positions keep being managed.", email=False)
            log.info("trade: %s", g.reason)
            return entered
        listed, seen = [], set()
        for s in sorted(sig["signals"] + sig["watch"], key=_rank):
            if s["symbol"] not in seen:
                seen.add(s["symbol"])
                listed.append(s)
        if not listed:
            return entered
        tick = {t.symbol: t for t in ctx.md.tickers()}
        contracts = ctx.md.contracts()
        cache = CandleCache(ctx.md, now)
        checked, skipped = 0, []
        for item in listed:
            sym = item["symbol"]
            g = pb.gates(st, now)
            ok, why = pb.check_entry(st, g, item["direction"], sym)
            if not ok:
                skipped.append([sym, why])
                if not g.entries_allowed or len(st["positions"]) >= pb.MAX_OPEN_POSITIONS:
                    break
                continue
            t = tick.get(sym)
            if t is None:
                skipped.append([sym, "no ticker"])
                continue
            try:
                res = strategy.evaluate(sym, cache.getter(sym), t.last, now)
            except Exception as exc:
                skipped.append([sym, f"evaluate error: {exc}"])
                continue
            checked += 1
            s = res.setup
            if s is None or s.status != "signal" or s.direction != item["direction"]:
                skipped.append([sym, f"not valid now ({s.status if s else strategy.STAGES[res.stage]})"])
                continue
            ok, why = pb.check_entry(st, g, s.direction, sym, s.setup_id)
            if not ok:
                skipped.append([sym, why])
                continue
            contract = contracts.get(sym)
            qty, risk_usd, why = pb.size_position(ctx.broker.entry_price(s.direction, s.entry), s.stop, contract)
            if why:
                skipped.append([sym, why])
                continue
            try:
                pos = ctx.broker.open(s.to_dict(), qty, t.last, now, contract)
            except (ExchangeError, ValueError) as exc:
                log.error("entry %s failed: %s", sym, exc)
                ctx.store.event("error", now, job="trade", error=f"{sym} entry failed: {exc}")
                continue
            pos["mark_price"], pos["mark_ts"] = t.last, now
            st["positions"].append(pos)
            st["traded_setups"][s.setup_id] = now
            ctx.store.save_state(st)
            ev = ctx.store.event("entry", now, id=pos["id"], symbol=sym, direction=s.direction, tag=s.tag, tier=s.tier,
                                 struct_tf=s.struct_tf, entry_tf=s.entry_tf, entry=pos["entry"], stop=pos["stop"],
                                 target=pos["target"], qty=pos["qty"], risk_usd=pos["risk_usd"],
                                 risk_pct=pos["r_price"] / pos["entry"], rr=(pos["target"] - pos["entry"]) * pos["dir"] / pos["r_price"],
                                 checklist=s.checklist, mode=ctx.mode)
            entered.append(ev)
            log.info("ENTRY %s %s %s @ %s stop %s target %s qty %.6g", sym, s.direction, s.tag, px(pos["entry"]),
                     px(pos["stop"]), px(pos["target"]), pos["qty"])
            ctx.notifier.send(f"Entry {sym}", fmt_entry(ctx, ev), email=False)
        ctx.store.event("trade", now, checked=checked, entered=[e["symbol"] for e in entered], skipped=skipped[:25])
    return entered


def fetch_path(md: MarketData, sym: str, since: int, now: int) -> tuple:
    """Finest candles that cover [since, now] within one request (1m live, 5m synthetic)."""
    for tf in (md.finest_tf, "5m", "15m", "1H"):
        if tf_ms(tf) < tf_ms(md.finest_tf):
            continue
        need = (now - since) // tf_ms(tf) + 3
        if need <= 1000:
            return md.candles(sym, tf, int(need)), tf_ms(tf)
    return md.candles(sym, "1H", 1000), tf_ms("1H")


def job_manage(ctx: Ctx) -> List[dict]:
    """R-ladder + 48h time-stop on every open position (paper: candle replay; demo/live: exchange sync)."""
    now = ctx.now()
    events: List[dict] = []
    with ctx.store.lock():
        st = ctx.store.load_state()
        if not st["positions"]:
            return events
        tick = {t.symbol: t for t in ctx.md.tickers()}
        if ctx.mode == "paper":
            for pos in st["positions"]:
                t = tick.get(pos["symbol"])
                try:
                    candles, bar = fetch_path(ctx.md, pos["symbol"], pos["last_mark_ts"], now)
                except ExchangeError as exc:
                    log.warning("manage %s: candles unavailable (%s)", pos["symbol"], exc)
                    continue
                events += pb.mark(pos, candles, now, bar, ctx.broker.fee_rate, ctx.broker.slippage)
                if pos["qty_open"] > 0 and t is not None:
                    pos["mark_price"], pos["mark_ts"] = t.last, now
                    if pb.time_stop_due(pos, now):
                        events.append(ctx.broker.close(pos, t.last, "time", now))
                rate = t.funding if t else 0.0
                until = pos.get("closed_at") or now
                price = pos.get("exit_price") or (t.last if t else pos["last_price"])
                pb.apply_funding(pos, rate, until, price)
        else:
            events += ctx.broker.sync(st, tick, now)
        for pos in [p for p in st["positions"] if p["qty_open"] <= 0]:
            st["positions"].remove(pos)
            trade = pb.finalize(pos)
            st["closed"].append(trade)
            for e in events:
                if e.get("id") == pos["id"] and e["kind"] == "exit":
                    e.update(net_pnl=trade["net_pnl"], r_trade=trade["r_mult"])
        ctx.store.save_state(st)
    for e in events:
        rec = {k: v for k, v in e.items() if k not in ("ts", "kind")}
        ctx.store.event(e["kind"], e["ts"], **rec)
        if e["kind"] in ("exit", "tp1"):
            log.info("%s %s %s @ %s (%s)", e["kind"].upper(), e["symbol"], e.get("reason", ""), px(e.get("price")),
                     rmult(e.get("r_trade", e.get("r"))))
            ctx.notifier.send(f"{e['kind']} {e['symbol']}", fmt_ladder(ctx, e), email=False)
    return events


def job_report(ctx: Ctx, slot: Optional[int] = None) -> dict:
    now = ctx.now()
    slot = slot or (schedule.last_due("report", now) - schedule.REPORT_DELAY)
    start, end, label = bitget_report.window_for(slot)
    with ctx.store.lock():
        st = ctx.store.load_state()
    trades, source = None, None
    if ctx.client is not None:
        try:
            trades = bitget_report.exchange_trades(ctx.client, st, start, end)
            source = "Bitget position history (authoritative)"
        except ExchangeError as exc:
            log.warning("report: Bitget history unavailable (%s) — falling back to the local ledger", exc)
    rep = bitget_report.build(st, start, end, now, ctx.label, label, trades, source)
    files = bitget_report.render_all(rep, ctx.store.paths.reports, tu.fmt(slot, "%Y-%m-%d_%H%M"))
    journal.write(ctx.store, st, now, ctx.label, day_ms=start)
    ctx.store.event("report", now, slot=slot, label=label, pdf=files["pdf"].name,
                    net=rep["stats"]["net"], trades=rep["stats"]["trades"])
    ctx.notifier.send(f"Daily report — {label}", bitget_report.to_telegram(rep), bitget_report.to_html(rep),
                      attachments=[files["pdf"]])
    log.info("report %s: %d trades, net %s → %s", label, rep["stats"]["trades"], usd(rep["stats"]["net"], True),
             files["pdf"].name)
    return rep


def refresh_views(ctx: Ctx, write_dashboard: bool = True) -> None:
    st = ctx.store.load_state()
    journal.write(ctx.store, st, ctx.now(), ctx.label)
    if write_dashboard:
        dashboard.write(ctx)
    prune_outbox(ctx.store.paths)


JOB_FUNCS = {"manage": job_manage, "trade": job_trade, "cycle": job_cycle, "scan": job_scan,
             "signals": job_signals, "report": job_report}


def run_job(ctx: Ctx, name: str, slot: Optional[int] = None, refresh: bool = True) -> bool:
    t0 = time.time()
    ok, note = True, ""
    try:
        out = JOB_FUNCS[name](ctx, slot) if name == "report" else JOB_FUNCS[name](ctx)
        if isinstance(out, list):
            note = f"{len(out)} event(s)"
    except Exception as exc:
        ok, note = False, f"{type(exc).__name__}: {exc}"
        if isinstance(exc, ExchangeError):          # expected operational failure: one line is enough
            log.error("job %s failed: %s", name, note)
            log.debug("%s", traceback.format_exc())
        else:
            log.error("job %s failed: %s\n%s", name, note, traceback.format_exc())
        ctx.store.event("error", ctx.now(), job=name, error=note[:300])
        if not ctx.replay:
            ctx.notifier.send(f"Bot error in {name}", f"⚠️ <b>{esc(name)}</b> failed: {esc(note[:500])}", email=False)
    ctx.store.mark_run(name, ctx.now(), ok, note or f"{time.time() - t0:.1f}s")
    if refresh:
        try:
            refresh_views(ctx, write_dashboard=not ctx.replay)
        except Exception as exc:
            log.error("view refresh failed: %s", exc)
    return ok


# --- daemon ------------------------------------------------------------------------------------------

def daemon(ctx: Ctx) -> None:
    """Run every job on the MYT grid until ctx.stop is set. Catches up once after downtime."""
    now = ctx.now()
    log.info("daemon up · mode %s · notifications: %s · home %s", ctx.label, ctx.notifier.channels(),
             ctx.store.paths.home)
    run_job(ctx, "manage")
    if ctx.store.last_run("cycle") < schedule.last_due("cycle", now):
        log.info("catch-up: missed scan cycle — running it now")
        run_job(ctx, "cycle")
    rep_due = schedule.last_due("report", now)
    if ctx.store.last_run("report") < rep_due and now - rep_due < 12 * tu.HOUR:
        log.info("catch-up: missed %s report", tu.hm(schedule.report_slot(rep_due)))
        run_job(ctx, "report", schedule.report_slot(rep_due))
    nxt = schedule.upcoming(ctx.now())
    while not ctx.stop.is_set():
        name = min(nxt, key=lambda n: (nxt[n], schedule.JOBS.index(n)))
        due = nxt[name]
        while not ctx.stop.is_set() and ctx.now() < due:
            ctx.stop.wait(min(1.0, (due - ctx.now()) / 1000))
        if ctx.stop.is_set():
            break
        run_job(ctx, name, schedule.report_slot(due) if name == "report" else None)
        nxt[name] = schedule.next_due(name, max(ctx.now(), due))
    log.info("daemon stopped")


# --- replay ------------------------------------------------------------------------------------------

def replay(days: float, source: str = "sim", home: Optional[str] = None, seed: int = 7,
           symbols: Optional[List[str]] = None, top: int = 40, end_ms: Optional[int] = None, quiet: bool = False) -> dict:
    """Fast-forward the complete bot (same jobs, same schedule) over the past `days`, no lookahead."""
    end = (end_ms or tu.now_ms()) // (5 * tu.MIN) * (5 * tu.MIN)
    start = end - int(days * tu.DAY)
    home_path = Path(home) if home else config.ROOT / ("sim" if source == "sim" else "replay")
    if home_path.resolve() == config.BOT_HOME.resolve():
        raise SystemExit("replay refuses to write into the live paper home — choose another --home")
    paths = Paths(home_path)
    for d in (paths.state, paths.journal, paths.reports):
        shutil.rmtree(d, ignore_errors=True)
    paths.ensure()
    store = Store(paths, config.ACCOUNT_SIZE)
    setup_logging(paths, console=not quiet)
    clock = SimClock(start)
    if source == "sim":
        md: MarketData = SimMarket(clock, seed=seed, start_ms=start - 60 * tu.DAY, end_ms=end + tu.DAY)
    else:
        public = BitgetPublic()
        if not symbols:
            live, _ = universe(public, config.MIN_VOLUME_USD)
            symbols = [t.symbol for t in sorted(live, key=lambda t: -t.volume_usd)[:top]]
        md = HistoricalMarket(public, clock, symbols, start, end, paths.cache, log)
    notifier = Notifier(log, None, enabled=False, clock=clock)
    ctx = Ctx(md, pb.PaperBroker(), store, notifier, "paper", sim=True, replay=True, workers=1)
    log.info("replay %s · %s → %s MYT · %s", source, tu.fmt(start), tu.fmt(end), paths.home)
    nxt = schedule.upcoming(start)
    last_day = None
    t0 = time.time()
    while True:
        name = min(nxt, key=lambda n: (nxt[n], schedule.JOBS.index(n)))
        due = nxt[name]
        if due > end:
            break
        clock.set(due)
        day = tu.date_str(due)
        if day != last_day:
            st = store.load_state()
            log.info("replay day %s · balance %s · open %d · closed %d", day, usd(pb.balance(st)),
                     len(st["positions"]), len(st["closed"]))
            last_day = day
        changed_before = store.events_path.stat().st_size if store.events_path.exists() else 0
        run_job(ctx, name, schedule.report_slot(due) if name == "report" else None, refresh=False)
        size = store.events_path.stat().st_size if store.events_path.exists() else 0
        if size != changed_before:
            journal.write(store, store.load_state(), due, ctx.label)
        nxt[name] = schedule.next_due(name, due)
    clock.set(end)
    st = store.load_state()
    rep = bitget_report.build(st, start, end, end, ctx.label, f"replay {tu.date_str(start)} → {tu.date_str(end)}")
    files = bitget_report.render_all(rep, paths.reports, f"replay_{tu.fmt(start, '%Y%m%d')}_{tu.fmt(end, '%Y%m%d')}")
    dashboard.write(ctx)
    s = rep["stats"]
    log.info("replay done in %.0fs · %d trades · win %s · expectancy %s · PF %s · net %s · report %s",
             time.time() - t0, s["trades"], pct(s["win_rate"], 1, False) if s["win_rate"] is not None else "—",
             rmult(s["expectancy_r"]), f"{s['profit_factor']:.2f}" if s["profit_factor"] else "—",
             usd(s["net"], True), files["md"].name)
    return rep


# --- message formatting ------------------------------------------------------------------------------

def _pre(rows: List[str]) -> str:
    return "<pre>" + esc("\n".join(rows)) + "</pre>"


def _html_table(headers: List[str], rows: List[list]) -> str:
    th = "".join(f"<th style='text-align:left;padding:4px 8px;border-bottom:2px solid #ccd;font-size:12px'>{esc(h)}</th>"
                 for h in headers)
    body = "".join("<tr>" + "".join(f"<td style='padding:3px 8px;border-bottom:1px solid #eee;font-size:12px'>{esc(c)}"
                                    f"</td>" for c in r) + "</tr>" for r in rows)
    return f"<table style='border-collapse:collapse'><tr>{th}</tr>{body}</table>"


def _pd(s: str) -> str:
    return {"broken": "brk", "swept": "swp", "respected": "rsp"}.get(s, "—")


def fmt_scan(ctx: Ctx, scan: dict, sweeps: dict, dfail: dict) -> tuple:
    when = tu.fmt(scan["generated_at"])
    head = ["SYMBOL", "LAST", "24H", "VOL", "FUND", "PDH", "PDL"]

    def rows(items):
        return [[r["symbol"], px(r["last"]), pct(r["change"]), compact(r["volume"]), pct(r["funding"], 3),
                 _pd(r["pdh_state"]), _pd(r["pdl_state"])] for r in items]

    def text(items):
        return [f"{'SYMBOL':<12}{'LAST':>11}{'24H':>9}{'VOL':>8}{'FUND':>9} PDH PDL"] + [
            f"{r[0][:12]:<12}{r[1]:>11}{r[2]:>9}{r[3]:>8}{r[4]:>9} {r[5]} {r[6]}" for r in rows(items)]

    prom = sweeps["promoted"][:15]
    sw_rows = [f"{'SYMBOL':<12}{'TF':>3} {'POOL':<9}{'VOL':>5}{'DISP':>6}{'AGE':>4}{'SCORE':>6}"] + [
        f"{s['symbol'][:12]:<12}{s['tf']:>3} {s['pool']:<9}{s['vol_ratio']:>5.1f}{s['disp_ratio']:>6.1f}"
        f"{s['age_bars']:>4}{s['score']:>6.0f}" for s in prom]
    fails = dfail["items"][:15]
    f_rows = [f"{'SYMBOL':<12}{'FAILED':>8}{'LEVEL':>12}{'CLOSE':>12}"] + [
        f"{f['symbol'][:12]:<12}{f['kind']:>8}{px(f['prior_high'] if f['failed_high'] else f['prior_low']):>12}"
        f"{px(f['close']):>12}" for f in fails]
    tg = (f"📊 <b>SCAN</b> · {when} MYT · {ctx.label}\n"
          f"Universe {scan['universe']} crypto perps · {len(scan['excluded'])} TradFi/RWA excluded · "
          f"{scan['liquid']} with 24h vol ≥ $1M\n\n"
          f"<b>Top movers</b>\n{_pre(text(scan['movers']))}\n<b>Top losers</b>\n{_pre(text(scan['losers']))}\n"
          f"🧲 <b>Module A</b> — {len(sweeps['sweeps'])} sweeps · {len(sweeps['promoted'])} promoted "
          f"(degraded: no OI/CVD history → gate {esc(sweeps['gate'])})\n{_pre(sw_rows) if prom else 'none promoted'}\n"
          f"📉 <b>Routine 2</b> daily-failure watchlist ({len(dfail['items'])}) — research only, never gates entries\n"
          f"{_pre(f_rows) if fails else 'none'}")
    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif'><h2 style='margin:0'>Scan · {esc(when)} MYT</h2>"
            f"<p>Universe {scan['universe']} · {len(scan['excluded'])} TradFi excluded · mode {ctx.label}</p>"
            f"<h3>Top movers</h3>{_html_table(head, rows(scan['movers']))}"
            f"<h3>Top losers</h3>{_html_table(head, rows(scan['losers']))}"
            "<h3>Module A — promoted sweeps</h3>" + _html_table(
                ["Symbol", "TF", "Pool", "Vol×", "Disp×", "Age", "Score"],
                [[s["symbol"], s["tf"], s["pool"], s["vol_ratio"], s["disp_ratio"], s["age_bars"], s["score"]]
                 for s in prom]) +
            "<h3>Routine 2 — daily failure (research only)</h3>" + _html_table(
                ["Symbol", "Failed", "Level", "Close"],
                [[f["symbol"], f["kind"], px(f["prior_high"] if f["failed_high"] else f["prior_low"]), px(f["close"])]
                 for f in fails]) + "</div>")
    return f"Scan {when} MYT", tg, html


def fmt_signals(ctx: Ctx, p: dict, armed: int) -> tuple:
    when = tu.fmt(p["generated_at"])
    f = p["funnel"]

    def lines(items):
        return [f"{'SYMBOL':<11}{'D':>2} {'SETUP':<16}{'ENTRY':>11}{'STOP':>11}{'TARGET':>11}{'RR':>5}"] + [
            f"{s['symbol'][:11]:<11}{s['direction'][0].upper():>2} {s['tag'][:16]:<16}{px(s['entry']):>11}"
            f"{px(s['stop']):>11}{px(s['target']):>11}{s['rr']:>5.1f}" for s in items]

    st = ctx.store.load_state()
    g = pb.gates(st, p["generated_at"])
    gl = (f"Gates: loss stop {'HIT' if g.loss_stop else 'ARMED'} · PROTECT {'ON' if g.protect else 'off'} · "
          f"open {g.open}/{pb.MAX_OPEN_POSITIONS} (L{g.longs}/S{g.shorts})")
    funnel = " → ".join(f"{k} {v}" for k, v in f.items())
    tg = (f"🎯 <b>FRAMEWORK SIGNALS</b> · {esc(config.ENGINE)} · {when} MYT · {ctx.label}\n"
          f"{esc(funnel)}\n\n<b>Meet the framework ({len(p['signals'])})</b>\n"
          + (_pre(lines(p["signals"][:15])) if p["signals"] else "none") +
          f"\n<b>In-zone watch ({len(p['watch'])})</b> — in the POI, waiting for a solid-body entry candle\n"
          + (_pre(lines(p["watch"][:15])) if p["watch"] else "none") +
          f"\nArmed (waiting for price to retrace to the POI): {armed}\n{esc(gl)}")
    head = ["Symbol", "Dir", "Setup", "Entry", "Stop", "Target", "RR"]

    def rows(items):
        return [[s["symbol"], s["direction"], s["tag"], px(s["entry"]), px(s["stop"]), px(s["target"]), s["rr"]]
                for s in items]

    html = (f"<div style='font-family:Segoe UI,Arial,sans-serif'><h2 style='margin:0'>Framework signals · {esc(when)} MYT"
            f"</h2><p>{esc(funnel)}</p><h3>Meet the framework</h3>{_html_table(head, rows(p['signals']))}"
            f"<h3>In-zone watch</h3>{_html_table(head, rows(p['watch']))}<p>Armed: {armed} · {esc(gl)}</p></div>")
    return f"Signals {when} MYT — {len(p['signals'])} signal(s)", tg, html


def fmt_entry(ctx: Ctx, e: dict) -> str:
    return (f"🟢 <b>ENTRY</b> {esc(e['symbol'])} {e['direction'].upper()} · {esc(e['tag'])} · {ctx.label}\n"
            f"entry {px(e['entry'])} · stop {px(e['stop'])} ({e['risk_pct']:.2%}) · target {px(e['target'])} "
            f"({e['rr']:.2f}R)\nqty {e['qty']:.6g} · 1R {usd(e['risk_usd'])}")


def fmt_ladder(ctx: Ctx, e: dict) -> str:
    if e["kind"] == "tp1":
        return (f"🟡 <b>TP1</b> {esc(e['symbol'])} · 50% off at +1R ({px(e['price'])}) · stop → breakeven · "
                f"trailing armed")
    net = e.get("net_pnl", e.get("pnl", 0.0))
    icon = "✅" if net > 0 else "❌" if net < 0 else "➖"
    return (f"{icon} <b>EXIT</b> {esc(e['symbol'])} · {esc(e.get('reason', ''))} @ {px(e['price'])} · "
            f"{rmult(e.get('r_trade', e.get('r')))} · {usd(net, True)} net")


# --- CLI ---------------------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    # global options work before or after the subcommand (subparser copies default to SUPPRESS)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS, help="data directory (default: repo dir; ./sim with --sim)")
    common.add_argument("--sim", action="store_true", default=argparse.SUPPRESS, help="synthetic market (offline)")
    common.add_argument("--seed", type=int, default=argparse.SUPPRESS, help="synthetic market seed")
    common.add_argument("--mode", choices=("paper", "demo", "live"), default=argparse.SUPPRESS, help="override TRADING_MODE")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS, help="no Telegram/Gmail (outbox only)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(description="AI Trading Terminal — v4 sweep-reversal bot (Bitget USDT-M, DEMO by default)",
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("scan", "routine 1 + Module A + routine 2"), ("signals", "framework-signal scan (all coins)"),
                       ("cycle", "scan → signals → trade"), ("trade", "enter from signals_live.json"),
                       ("manage", "R-ladder + time-stop"), ("journal", "rewrite today's journal"),
                       ("dashboard", "write dashboard/index.html"), ("daemon", "run 24/7 on the MYT schedule"),
                       ("status", "one-shot terminal snapshot")):
        sub.add_parser(name, help=text, parents=[common])
    rp = sub.add_parser("report", help="daily report + PDF", parents=[common])
    rp.add_argument("--slot", choices=config.REPORT_TIMES, help="report slot (default: the latest one)")
    sp = sub.add_parser("serve", help="live dashboard (read-only)", parents=[common])
    sp.add_argument("--port", type=int, default=config.DASHBOARD_PORT)
    sp.add_argument("--host", default=config.DASHBOARD_HOST)
    sp.add_argument("--run", action="store_true", help="also run the daemon in the background")
    tp = sub.add_parser("tui", help="terminal UI", parents=[common])
    tp.add_argument("--run", action="store_true", help="also run the daemon inside the TUI")
    rpl = sub.add_parser("replay", help="fast-forward the bot over past days (no lookahead)", parents=[common])
    rpl.add_argument("--days", type=float, default=7)
    rpl.add_argument("--source", choices=("sim", "bitget"), default="sim")
    rpl.add_argument("--symbols", nargs="*", help="bitget source: symbols to replay (default: top by volume)")
    rpl.add_argument("--top", type=int, default=40)
    args = ap.parse_args(argv)
    # defaults filled after parsing: set_defaults() would leak into the shared parent actions
    for key, value in (("home", None), ("sim", False), ("seed", 7), ("mode", None), ("quiet", False), ("verbose", False)):
        if not hasattr(args, key):
            setattr(args, key, value)

    if args.cmd == "replay":
        replay(args.days, args.source, args.home, args.seed, args.symbols, args.top)
        return 0
    ctx = build_ctx(args.home, args.sim, args.seed, args.mode, args.quiet, console=args.cmd != "tui",
                    verbose=args.verbose)
    if args.cmd in ("scan", "signals", "cycle", "trade", "manage"):
        return 0 if run_job(ctx, args.cmd) else 1
    if args.cmd == "report":
        slot = None
        if args.slot:
            slot = tu.last_daily(ctx.now(), [args.slot])
        return 0 if run_job(ctx, "report", slot) else 1
    if args.cmd == "journal":
        print(journal.write(ctx.store, ctx.store.load_state(), ctx.now(), ctx.label))
        return 0
    if args.cmd == "dashboard":
        print(dashboard.write(ctx))
        return 0
    if args.cmd == "daemon":
        import signal
        signal.signal(signal.SIGTERM, lambda *_: ctx.stop.set())       # systemd / docker stop
        try:
            daemon(ctx)
        except KeyboardInterrupt:
            ctx.stop.set()
            log.info("daemon stopped (Ctrl+C)")
        return 0
    if args.cmd == "serve":
        if args.run:
            threading.Thread(target=daemon, args=(ctx,), daemon=True, name="daemon").start()
        dashboard.serve(ctx, args.host, args.port)
        return 0
    if args.cmd == "tui":
        import tui
        return tui.main(ctx, run_daemon=args.run, daemon_fn=daemon, job_fn=run_job)
    if args.cmd == "status":
        import tui
        tui.print_status(dashboard.snapshot(ctx))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
