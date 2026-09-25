"""journal.py — journal/YYYY-MM-DD.md, one file per market day (07:00 → 07:00 MYT).

The file is re-rendered from the event log and the ledger every time something happens, so it is
always complete and idempotent. A day with zero trades still gets its journal (framework: "Journal every
day, even with zero trades").
"""
from __future__ import annotations

from pathlib import Path
from typing import List

import config
import paper_bot as pb
import timeutil as tu
from store import Store
from util import atomic_write, pct, px, rmult, usd


def _entry_block(e: dict) -> List[str]:
    lines = [f"### {tu.hm(e['ts'])} · {e['symbol']} {e['direction'].upper()} · {e['tag']} "
             f"({e.get('tier', '?')} {e.get('struct_tf', '')}→{e.get('entry_tf', '')})", "",
             f"Entry {px(e['entry'])} · stop {px(e['stop'])} ({e.get('risk_pct', 0):.2%}) · target {px(e['target'])} "
             f"({e.get('rr', 0):.2f}R) · qty {e.get('qty', 0):.6g} · 1R = {usd(e.get('risk_usd'))}", ""]
    for step in e.get("checklist") or []:
        lines.append(f"{step['step']}. {'✓' if step['ok'] else '✗'} **{step['name']}** — {step['detail']}")
    return lines + [""]


def render(store: Store, state: dict, now: int, mode: str, day_ms: int | None = None) -> tuple:
    start, end = tu.market_day(day_ms if day_ms is not None else now, config.MARKET_DAY_START)
    date = tu.date_str(start)
    events = store.events(start, end)
    closed = [t for t in state["closed"] if start <= t["closed_at"] < end]
    s = pb.trade_stats(closed)
    bal_end = pb.balance(state)
    open_bal = bal_end - pb.realized_between(state, start, now + 1)
    lines = [f"# Journal — {date} · market day 07:00 → 07:00 MYT", "",
             f"Mode **{mode.upper()}** · {config.ENGINE} · {config.EXCHANGE} · account {usd(open_bal)} → "
             f"{usd(bal_end)} (realized {usd(bal_end - open_bal, True)})  ",
             f"Updated {tu.fmt(now)} MYT", "", "## Summary", "",
             "| Trades closed | Wins | Losses | Win rate | Net | Expectancy | Fees | Funding |",
             "|---|---|---|---|---|---|---|---|",
             f"| {s['trades']} | {s['wins']} | {s['losses']} | "
             f"{pct(s['win_rate'], 1, False) if s['win_rate'] is not None else '—'} | {usd(s['net'], True)} | "
             f"{rmult(s['expectancy_r'])} | {usd(-s['fees'], True)} | {usd(s['funding'], True)} |", ""]
    g = pb.gates(state, min(now, end - 1))
    lines += [f"Risk gates (MYT day {g.day}): loss stop {'**HIT**' if g.loss_stop else 'ARMED'} "
              f"({usd(g.realized, True)} of −${pb.DAILY_MAX_LOSS:.0f}, {g.losses}/{pb.MAX_LOSSES_PER_DAY} losing) · "
              f"PROTECT {'**ON**' if g.protect else 'off'} (+${pb.DAILY_TARGET:.0f}) · open {g.open}/"
              f"{pb.MAX_OPEN_POSITIONS} (L{g.longs} S{g.shorts})", ""]

    entries = [e for e in events if e["kind"] == "entry"]
    lines += ["## Entries", ""]
    if entries:
        for e in entries:
            lines += _entry_block(e)
    else:
        lines += ["No entries.", ""]

    ladder = [e for e in events if e["kind"] in ("tp1", "lock", "exit")]
    lines += ["## Ladder & exits", ""]
    if ladder:
        lines += ["| Time | Symbol | Event | Price | R | Net |", "|---|---|---|---|---|---|"]
        for e in ladder:
            what = e["kind"] if e["kind"] != "exit" else f"exit ({e.get('reason')})"
            net = usd(e.get("net_pnl", e.get("pnl")), True) if e["kind"] != "lock" else "—"
            lines.append(f"| {tu.hm(e['ts'])} | {e['symbol']} | {what} | {px(e.get('price'))} | "
                         f"{rmult(e.get('r')) if e['kind'] != 'lock' else '—'} | {net} |")
        lines.append("")
    else:
        lines += ["No ladder events.", ""]

    if closed:
        lines += ["## Closed trades", "", "| Opened | Closed | Symbol | Setup | Exit | R | Net |", "|---|---|---|---|---|---|---|"]
        for t in closed:
            lines.append(f"| {tu.fmt(t['opened_at'], '%m-%d %H:%M')} | {tu.hm(t['closed_at'])} | {t['symbol']} "
                         f"{t['direction']} | {t['tag']} | {t['exit_reason']} | {rmult(t['r_mult'])} | "
                         f"{usd(t['net_pnl'], True)} |")
        lines.append("")

    lines += ["## Scans, signals & trade runs", ""]
    runs = [e for e in events if e["kind"] in ("scan", "signals", "trade", "gate", "report")]
    for e in runs:
        t = tu.hm(e["ts"])
        if e["kind"] == "scan":
            lines.append(f"- {t} **scan** — {e.get('universe', 0)} coins ({e.get('excluded', 0)} TradFi excluded) · "
                         f"movers/losers {e.get('movers', 0)}/{e.get('losers', 0)} · Module A {e.get('sweeps', 0)} "
                         f"sweeps ({e.get('promoted', 0)} promoted) · daily-failure {e.get('failures', 0)}")
        elif e["kind"] == "signals":
            sig = ", ".join(e.get("signals", [])) or "none"
            zone = ", ".join(e.get("in_zone", [])) or "none"
            lines.append(f"- {t} **signals** — {len(e.get('signals', []))} meet the framework: {sig} · in-zone watch: "
                         f"{zone} · armed {e.get('armed', 0)}")
        elif e["kind"] == "trade":
            if e.get("entered") or e.get("checked"):
                lines.append(f"- {t} **trade** — {e.get('checked', 0)} listed coins re-validated, entered "
                             f"{', '.join(e.get('entered', [])) or 'none'}")
        elif e["kind"] == "gate":
            lines.append(f"- {t} **gate** — {e.get('reason')}")
        elif e["kind"] == "report":
            lines.append(f"- {t} **report** — {e.get('label', '')}")
    if not runs:
        lines.append("- No scheduled runs recorded in this window yet.")
    lines.append("")

    notes = []
    if not closed and not entries:
        notes.append("No trades today — journal kept per framework.")
    errors = [e for e in events if e["kind"] == "error"]
    for e in errors[-10:]:
        notes.append(f"{tu.hm(e['ts'])} error in {e.get('job')}: {e.get('error')}")
    lines += ["## Notes", ""] + [f"- {n}" for n in notes or ["—"]] + [""]
    return date, "\n".join(lines)


def write(store: Store, state: dict, now: int, mode: str, day_ms: int | None = None) -> Path:
    date, text = render(store, state, now, mode, day_ms)
    path = store.paths.journal / f"{date}.md"
    atomic_write(path, text)
    return path


def list_days(store: Store) -> List[str]:
    return sorted((p.stem for p in Path(store.paths.journal).glob("*.md")), reverse=True)
