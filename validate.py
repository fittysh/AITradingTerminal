"""validate.py — is the framework valid? Two separate questions, answered separately.

1. Does the bot follow the framework?  audit() takes every entry of a replay and re-checks it against the
   seven steps and the protection shell from the raw candles at the moment of entry. It does not trust the
   engine's own checklist: each rule is re-derived from candles with its own short implementation (only
   the ITH/ITL structure read reuses the engine's primitive).
2. Does the framework make money?  verdict() applies the framework's proof rule — at least 30 closed
   trades, net result above zero, profit factor at least 1.2 (the same bar as the live-trading lock).
   Only a replay on real Bitget history can answer this. The synthetic market is a random walk with stop
   hunts: rules can be checked on it, but any profit or loss there says nothing about a real edge.

    python run_bot.py validate --source bitget --days 30 --top 30     real history (needs api.bitget.com)
    python run_bot.py validate --days 14 --seed 7                     synthetic, rules only
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import config
import paper_bot as pb
import strategy as st
import timeutil as tu
from exchange import Candle, tf_ms
from util import atomic_write, pct, rmult, usd

RULES = ("listed by the signal scan", "1 context", "2 trigger", "3 structure shift", "4 discount / premium",
         "5 C2 / C3 closure", "6 zone", "7 entry candle", "stop + target", "risk per trade", "protection shell")
EPS = 1e-9


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


class Candles:
    """Closed candles of one symbol at one moment, read through the bot's own candle cache."""

    def __init__(self, md, clock, sym: str, t: int):
        import run_bot
        clock.set(t)
        self.cache = run_bot.CandleCache(md, t)
        self.sym = sym

    def get(self, tf: str, limit: int) -> List[Candle]:
        return self.cache.closed(self.sym, tf, limit)


def _tier(name: str) -> st.Tier:
    return next(t for t in st.TIERS if t.name == name)


def audit_entry(md, clock, p: dict) -> List[Tuple[str, bool, str]]:
    """[(rule, ok, detail)] for one position, re-derived from the candles at its open time."""
    out: List[Tuple[str, bool, str]] = []

    def check(rule: str, ok, detail: str = "") -> None:
        out.append((rule, bool(ok), detail))

    long = p["direction"] == "long"
    sg = 1 if long else -1
    t = p["opened_at"]
    s = p["setup"]
    tier = _tier(p["tier"])
    cs = Candles(md, clock, p["symbol"], t)
    S = cs.get(tier.struct_tf, tier.struct_bars)
    E = cs.get(tier.entry_tf, tier.entry_bars)
    D = cs.get(st.DAILY_TF, 10)
    si = {c.ts: i for i, c in enumerate(S)}
    ei = {c.ts: i for i, c in enumerate(E)}

    def ext(c: Candle) -> float:            # the liquidity side: lows for a long
        return c.l if long else c.h

    def against(c: Candle) -> bool:
        return c.c < c.o if long else c.c > c.o

    # 1 · context: daily bias (yesterday's close vs the day before's range) + setup-TF structure
    if len(D) >= 2:
        y, yy = D[-1], D[-2]
        bias = "bullish" if y.c > yy.h else "bearish" if y.c < yy.l else "neutral"
        structure = st.market_structure(S)
        allowed = (bias != "bearish" and structure != "down") if long else (bias != "bullish" and structure != "up")
        check("1 context", allowed, f"daily {bias}, {tier.struct_tf} structure {structure}")
    else:
        check("1 context", False, "fewer than 2 daily candles")

    # 2 · trigger
    r = s["raid"]
    ip, ix, ic, ipool = (si.get(r[k]) for k in ("ts", "ext_ts", "confirm_ts", "pool_ts"))
    if None in (ip, ix, ic, ipool):
        check("2 trigger", False, "trigger candles are not in the setup-TF history")
        return out
    extreme = ext(S[ix])
    untaken = all(sg * (ext(S[k]) - extreme) >= 0 for k in range(ix + 1, len(S)))
    if r["kind"] == "raid":
        lvl = r["level"]
        w = st.POOL_WIDTH
        left, right = S[max(0, ipool - w):ipool], S[ipool + 1:ipool + 1 + w]
        pool_ok = (_close(ext(S[ipool]), lvl) and len(left) == w and len(right) == w
                   and all(sg * (ext(c) - lvl) > 0 for c in left) and all(sg * (ext(c) - lvl) >= 0 for c in right))
        first_pierce = next((k for k in range(ipool + 1, len(S)) if sg * (lvl - ext(S[k])) > 0), None)
        ok = (pool_ok and first_pierce == ip and len(S) - 1 - ip < st.RAID_LOOKBACK
              and 0 <= ic - ip < st.RECLAIM_BARS and sg * (S[ic].c - lvl) > 0
              and all(sg * (S[k].c - lvl) <= 0 for k in range(ip, ic))
              and ip <= ix <= ic and _close(extreme, min((ext(S[k]) for k in range(ip, ic + 1)), key=lambda v: sg * v))
              and _close(extreme, r["extreme"]) and untaken)
        check("2 trigger", ok, f"pool {lvl:g} pierced {len(S) - 1 - ip} bars ago, reclaimed after {ic - ip} bar(s)")
    else:
        run_end = ipool
        while run_end + 1 < len(S) and against(S[run_end + 1]):
            run_end += 1
        opens = [S[k].o for k in range(ipool, run_end + 1)]
        origin = max(opens) if long else min(opens)
        ok = (run_end - ipool + 1 >= st.CISD_MIN_RUN and against(S[ipool]) and _close(origin, r["level"])
              and sg * (S[ic].c - origin) > 0 and all(sg * (S[k].c - origin) <= 0 for k in range(run_end + 1, ic))
              and ipool <= ix <= ic and _close(extreme, r["extreme"]) and untaken and len(S) - 1 - run_end < st.RAID_LOOKBACK
              and ix >= st.POOL_WIDTH and ix + st.POOL_WIDTH < len(S)
              and all(sg * (ext(S[k]) - extreme) > 0 for k in range(ix - st.POOL_WIDTH, ix)))
        check("2 trigger", ok, f"CISD: {run_end - ipool + 1} against-closes, origin {origin:g} closed through")

    # 3 · structure shift: the last opposing 3-bar pivot before the trigger, closed through afterwards
    m = s["mss"]
    iv, ish = si.get(m["pivot_ts"]), si.get(m["ts"])
    if iv is None or ish is None:
        check("3 structure shift", False, "shift candles are not in the history")
    else:
        piv = S[iv].h if long else S[iv].l
        is_pivot = (0 < iv < len(S) - 1 and sg * (piv - (S[iv - 1].h if long else S[iv - 1].l)) > 0
                    and sg * (piv - (S[iv + 1].h if long else S[iv + 1].l)) >= 0)
        first_close = next((k for k in range(ip, len(S)) if sg * (S[k].c - piv) > 0), None)

        def pivot_at(k: int) -> bool:
            v, a, b = (S[k].h, S[k - 1].h, S[k + 1].h) if long else (S[k].l, S[k - 1].l, S[k + 1].l)
            return sg * (v - a) > 0 and sg * (v - b) >= 0

        last = not any(pivot_at(k) for k in range(iv + 1, ip) if 0 < k < len(S) - 1)
        check("3 structure shift", is_pivot and last and iv < ip and _close(piv, m["level"]) and first_close == ish,
              f"pivot {piv:g} closed through {ish - ip} bar(s) after the trigger")

    # 4 · discount / premium: dealing range = trigger extreme → extreme since the trigger
    if long:
        lo, hi = extreme, max(c.h for c in S[ip:])
    else:
        lo, hi = min(c.l for c in S[ip:]), extreme
    eq = (lo + hi) / 2
    price = s["entry"]                      # the live price the engine entered on
    check("4 discount / premium", sg * (eq - price) > 0 and _close(eq, s["range"]["eq"]),
          f"price {price:g} vs EQ {eq:g} ({'discount' if long else 'premium'} needed)")

    # 5 · candle 2 / candle 3 closure at the trigger extreme
    cd = s.get("candle") or {}
    i1, i2 = si.get(cd.get("c1_ts")), si.get(cd.get("c2_ts"))
    if i1 is None or i2 is None:
        check("5 C2 / C3 closure", False, "no closure recorded")
    else:
        c1 = ext(S[i1])
        took = sg * (c1 - ext(S[i2])) > 0
        c2_back = sg * (S[i2].c - c1) > 0
        c3_back = i2 + 1 < len(S) and sg * (S[i2 + 1].c - c1) > 0
        kind = "C2" if c2_back else "C3" if c3_back else "none"
        ok = (i2 == ix and i1 == i2 - 1 and took and kind in ("C2", "C3") and kind == cd.get("kind")
              and kind == p.get("closure") and p["tag"].endswith("-" + kind))
        check("5 C2 / C3 closure", ok, f"{kind}: C1 {'low' if long else 'high'} {c1:g}, C2 closed {S[i2].c:g}")

    # 6 · zone inside the correct half: a fresh FVG (unicorn = breaker overlap) or the order block
    z = s["poi"]
    iz = si.get(z["ts"])
    half_lo, half_hi = (lo, eq) if long else (eq, hi)
    in_half = z["lo"] >= half_lo * (1 - EPS) and z["hi"] <= half_hi * (1 + EPS) and z["hi"] > z["lo"]
    family = p["tag"].split("-")[0]
    if iz is None:
        check("6 zone", False, "zone candle not in history")
    elif z["kind"] in ("FVG", "unicorn"):
        a, b = S[iz - 1], S[iz + 1] if iz + 1 < len(S) else None
        gap = b is not None and ((b.l > a.h and _close(z["src_lo"], a.h) and _close(z["src_hi"], b.l)) if long else
                                 (b.h < a.l and _close(z["src_lo"], b.h) and _close(z["src_hi"], a.l)))
        fresh = all((c.l > z["src_lo"]) if long else (c.h < z["src_hi"]) for c in S[iz + 2:])
        inside_gap = z["lo"] >= z["src_lo"] * (1 - EPS) and z["hi"] <= z["src_hi"] * (1 + EPS)
        fam_ok = family == ("unicorn" if z["kind"] == "unicorn" else "sweep")
        check("6 zone", gap and fresh and inside_gap and in_half and iz >= ip and fam_ok,
              f"{z['kind']} {z['lo']:g}–{z['hi']:g}, {'fresh' if fresh else 'filled'}")
    else:
        c = S[iz]
        unbroken = all((x.c >= c.l) if long else (x.c <= c.h) for x in S[ix + 1:])
        first_ob = not any(against(S[k]) for k in range(iz + 1, ix + 1))
        within = z["lo"] >= c.l * (1 - EPS) and z["hi"] <= c.h * (1 + EPS)
        check("6 zone", against(c) and first_ob and within and ix - st.OB_LOOKBACK < iz <= ix and unbroken and in_half
              and family == "OB",
              f"OB {c.l:g}–{c.h:g}, {'unbroken' if unbroken else 'closed through'}")

    # 7 · entry candle: pullback series into the zone, ≥50% body closing beyond the series (or an FVG retest)
    cf, ser = s.get("confirm") or {}, s.get("series") or {}
    ix_e, f, l = ei.get(cf.get("ts")), ei.get(ser.get("first_ts")), ei.get(ser.get("last_ts"))
    tfm = tf_ms(tier.entry_tf)
    if None in (ix_e, f, l):
        check("7 entry candle", False, "entry candle or series not in the entry-TF history")
    else:
        x = E[ix_e]
        body = sg * (x.c - x.o) / (x.h - x.l) if x.h > x.l else 0.0
        run_ok = (all(against(E[k]) for k in range(f, l + 1)) and (f == 0 or not against(E[f - 1]))
                  and (l + 1 >= len(E) or not against(E[l + 1])))
        opens = [E[k].o for k in range(f, l + 1)]
        level = max(opens) if long else min(opens)
        fc = next((k for k in range(l + 1, len(E)) if sg * (E[k].c - level) > 0), None)
        held = fc is not None and all(sg * (E[k].c - level) > 0 for k in range(fc, len(E)))
        fresh = x.ts + tfm <= t < x.ts + tfm + st.SERIES_FRESH_MS
        tol = st.POI_TOLERANCE
        seg = E[f:]
        pull = min(c.l for c in seg) if long else max(c.h for c in seg)
        touched = (pull <= z["hi"] * (1 + tol)) if long else (pull >= z["lo"] * (1 - tol))
        no_close_through = all((c.c >= z["lo"] * (1 - tol)) if long else (c.c <= z["hi"] * (1 + tol)) for c in seg)
        recent = len(E) - 1 - l <= st.SERIES_LOOKBACK
        ok = (run_ok and _close(level, ser["level"]) and _close(level, cf["level"]) and held and fresh
              and body >= st.BODY_MIN and touched and no_close_through and recent)
        if cf.get("kind") == "close":
            ok = ok and fc == ix_e and p.get("entry_kind") == "close"
        elif cf.get("kind") == "fvg":
            g = cf.get("fvg") or [0, 0]
            gap_here = any(
                (E[i + 1].l > E[i - 1].h and _close(g[0], E[i - 1].h) and _close(g[1], E[i + 1].l)) if long else
                (E[i + 1].h < E[i - 1].l and _close(g[0], E[i + 1].h) and _close(g[1], E[i - 1].l))
                for i in range(max(1, f), ix_e - 1))
            in_gap = ((x.l <= g[1] * (1 + tol) and x.c >= g[0] * (1 - tol)) if long else
                      (x.h >= g[0] * (1 - tol) and x.c <= g[1] * (1 + tol)))
            ok = ok and fc is not None and fc < ix_e and gap_here and in_gap and p.get("entry_kind") == "fvg" and p.get("fvg")
        else:
            ok = False
        check("7 entry candle", ok, f"{cf.get('kind')}: body {body:.0%}, close {x.c:g} vs series {level:g}, "
                                    f"{'held' if held else 'not held'}, zone {'reached' if touched else 'not reached'}")

    # stop + target: beyond the trigger extreme (0.1% buffer), 0.8%–12% from the engine entry; target ≥ 2R from the fill
    stop, fill = p["initial_stop"], p["entry"]
    dist = sg * (price - stop)
    beyond = sg * (extreme - stop) >= abs(extreme) * st.STOP_BUFFER_PCT * (1 - 1e-6)
    sized = price * st.STOP_FLOOR_PCT * (1 - 1e-6) <= dist <= price * st.STOP_MAX_PCT * (1 + 1e-6)
    target_ok = sg * (p["target"] - fill) >= 2 * p["r_price"] * (1 - 1e-6)
    check("stop + target", beyond and sized and target_ok,
          f"stop {dist / price:.2%} away, target {sg * (p['target'] - fill) / p['r_price']:.2f}R from the fill")
    check("risk per trade", 0 < p["risk_usd"] <= pb.RISK_PER_TRADE + 1e-9, f"1R = {usd(p['risk_usd'])}")
    return out


def audit_shell(state: dict, events: Sequence[dict]) -> Dict[str, List[Tuple[bool, str]]]:
    """Per entry id: listed by the latest signal scan, and the protection shell at that moment."""
    trades = {p["id"]: p for p in state["positions"] + state["closed"]}
    out: Dict[str, List[Tuple[bool, str]]] = {}
    listed: Optional[set] = None
    open_ids: Dict[str, str] = {}
    order = {e["id"]: n for n, e in enumerate(x for x in events if x["kind"] == "entry")}
    for e in events:
        if e["kind"] == "signals":
            listed = {x.split()[0] for x in e.get("signals", [])} | {x.split()[0] for x in e.get("in_zone", [])}
        elif e["kind"] == "exit":
            open_ids.pop(e["id"], None)
        elif e["kind"] == "entry":
            res: List[Tuple[bool, str]] = [(listed is not None and e["symbol"] in listed, "listed by the signal scan")]
            ts = e["ts"]
            d0, d1 = tu.day_bounds(ts)
            later = {pid for pid, n in order.items() if n >= order[e["id"]]}      # this entry and the ones after it
            realized = sum(fl["pnl"] - fl["fee"] for pid, p in trades.items()
                           for fl in p["fills"] if d0 <= fl["ts"] <= ts and not (pid in later and fl["kind"] == "entry"))
            losses = sum(1 for p in state["closed"] if d0 <= p["closed_at"] <= ts and p["net_pnl"] < 0)
            side = sum(1 for d in open_ids.values() if d == e["direction"])
            same_coin = any(trades.get(i, {}).get("symbol") == e["symbol"] for i in open_ids)
            ok = (realized > -pb.DAILY_MAX_LOSS and losses < pb.MAX_LOSSES_PER_DAY and realized < pb.DAILY_TARGET
                  and len(open_ids) < pb.MAX_OPEN_POSITIONS and side < pb.CORR_MAX_PER_SIDE and not same_coin)
            res.append((ok, f"day {realized:+.2f}, {losses} losing, {len(open_ids)} open ({side} {e['direction']})"))
            out[e["id"]] = res
            open_ids[e["id"]] = e["direction"]
    return out


def audit(md, clock, state: dict, events: Sequence[dict]) -> dict:
    """Every entry re-checked. Returns {"entries", "rules": {rule: [passed, failed]}, "failures": [...]}."""
    rules = {r: [0, 0] for r in RULES}
    failures: List[dict] = []
    shell = audit_shell(state, events)
    for p in sorted(state["positions"] + state["closed"], key=lambda x: x["opened_at"]):
        rows = audit_entry(md, clock, p)
        listed, prot = shell.get(p["id"], [(False, "no entry event"), (False, "no entry event")])
        rows = [("listed by the signal scan", listed[0], listed[1])] + rows + [("protection shell", prot[0], prot[1])]
        for rule, ok, detail in rows:
            rules[rule][0 if ok else 1] += 1
            if not ok:
                failures.append({"id": p["id"], "symbol": p["symbol"], "tag": p["tag"], "rule": rule, "detail": detail,
                                 "opened": tu.fmt(p["opened_at"])})
    n = len(state["positions"]) + len(state["closed"])
    return {"entries": n, "rules": rules, "failures": failures, "clean": not failures and n > 0}


def verdict(stats: dict) -> Tuple[bool, List[Tuple[str, bool, str]]]:
    """The framework's proof rule (the same bar as the live-trading lock)."""
    n, net, pf = stats["trades"], stats["net"], stats["profit_factor"]
    checks = [
        (f"at least {config.EDGE_MIN_TRADES} closed trades", n >= config.EDGE_MIN_TRADES, str(n)),
        ("net result above zero (after fees and funding)", net > 0, usd(net, True)),
        (f"profit factor at least {config.EDGE_MIN_PROFIT_FACTOR}", pf is not None and pf >= config.EDGE_MIN_PROFIT_FACTOR,
         f"{pf:.2f}" if pf is not None else "—"),
    ]
    return all(ok for _, ok, _ in checks), checks


def render(rep: dict, aud: dict, source: str) -> str:
    s = rep["stats"]
    proven, checks = verdict(s)
    real = source == "bitget"
    lines = [f"# Framework validation — {config.ENGINE}", "",
             f"Replay {rep['label']} · source **{'Bitget history' if real else 'synthetic market'}** · "
             f"{len(rep.get('symbols') or [])} coins", ""]
    gaps = rep.get("gaps") or []
    if real:
        if gaps:
            lines += [f"**Data coverage: {len(gaps)} coin/timeframe histories are incomplete** — results for those "
                      "coins rest on partial data:", ""]
            lines += [f"- {g['symbol']} {g['tf']}: {g['candles']} of {g['expected']} candles, "
                      f"{tu.fmt(g['from'])} → {tu.fmt(g['to'])}" for g in gaps[:30]]
            lines.append("")
        else:
            lines += ["Data coverage: every coin and timeframe covers the whole window.", ""]
    lines += ["## 1 · Does the bot follow the framework?", "",
              f"**{'Yes' if aud['clean'] else 'No' if aud['entries'] else 'Not tested (no entries)'}** — "
              f"{aud['entries']} entries re-checked against every rule from the raw candles.", "",
              "| Rule | Passed | Failed |", "|---|---|---|"]
    for rule, (ok, bad) in aud["rules"].items():
        lines.append(f"| {rule} | {ok} | {bad} |")
    if aud["failures"]:
        lines += ["", "Failures:", ""] + [f"- {f['opened']} {f['symbol']} {f['tag']} · {f['rule']}: {f['detail']}"
                                          for f in aud["failures"][:40]]
    lines += ["", "## 2 · Does it make money? (the framework's proof rule)", ""]
    if real:
        lines.append(f"**{'Valid — passes' if proven else 'Not proven — fails'}** the proof rule on this replay.")
    else:
        lines.append("**Not answerable on synthetic data.** The checks below are shown for completeness only; the "
                     "synthetic market is a random walk, so its profit or loss is not evidence either way. "
                     "Run with `--source bitget`.")
    lines += ["", "| Check | Result | Value |", "|---|---|---|"]
    lines += [f"| {name} | {'pass' if ok else 'fail'} | {val} |" for name, ok, val in checks]
    lines += ["", f"Trades {s['trades']} · win rate {pct(s['win_rate'], 1, False) if s['win_rate'] is not None else '—'} · "
                  f"expectancy {rmult(s['expectancy_r'])} · net {usd(s['net'], True)} · max drawdown "
                  f"{usd(s['max_drawdown'], True)}", "", "| Group | Value | Trades | Win rate | Expectancy | Net |",
              "|---|---|---|---|---|---|"]
    for key in ("closure", "entry_kind", "tier", "family", "trigger", "direction"):
        for name, b in (rep["by"].get(key) or {}).items():
            lines.append(f"| {key} | {name} | {b['trades']} | {pct(b['win_rate'], 1, False) if b['win_rate'] is not None else '—'} "
                         f"| {rmult(b['expectancy_r'])} | {usd(b['net'], True)} |")
    return "\n".join(lines) + "\n"


def run(days: float, source: str, home: Optional[str], seed: int, symbols: Optional[List[str]], top: int,
        end_ms: Optional[int] = None, quiet: bool = False) -> dict:
    import run_bot
    from exchange import BitgetPublic
    from sim import HistoricalMarket, SimClock, SimMarket
    from store import Paths, Store
    home_path = Path(home) if home else config.ROOT / "validate"
    rep = run_bot.replay(days, source, str(home_path), seed, symbols, top, end_ms, quiet)
    start, end = rep["window"]
    paths = Paths(home_path)
    store = Store(paths, config.ACCOUNT_SIZE)
    clock = SimClock(start)
    if source == "sim":
        md = SimMarket(clock, seed=seed, start_ms=start - 60 * tu.DAY, end_ms=end + tu.DAY)
    else:
        md = HistoricalMarket(BitgetPublic(), clock, rep["symbols"], start, end, paths.cache)   # served from the cache
    aud = audit(md, clock, store.load_state(), store.events())
    text = render(rep, aud, source)
    path = paths.reports / f"validate_{tu.fmt(start, '%Y%m%d')}_{tu.fmt(end, '%Y%m%d')}.md"
    atomic_write(path, text)
    atomic_write(path.with_suffix(".json"), json.dumps({"stats": rep["stats"], "audit": aud, "source": source,
                                                        "proven": verdict(rep["stats"])[0]}, indent=1, default=str))
    return {"report": rep, "audit": aud, "path": path, "text": text, "proven": verdict(rep["stats"])[0]}
