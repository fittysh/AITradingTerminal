"""journal.py — the daily trading journal, one per market day (07:00 → 07:00 MYT).

Layout follows the journal design: a title, a one-line note on how P&L is worked out, the currency,
"At a glance" (total P&L + entries), then the trade log — Date · Ticker · Side · Size · Entry · Exit · P&L,
newest first (by the trade's open time, which the Date column shows). Below it, smaller: the framework notes per trade (setup, C2/C3 closure, entry type, exit
reason, R) and the day's notes (risk gates, open positions, errors).

Rows are the trades CLOSED in the market day, so every row has an exit and a realized P&L. P&L is net:
side × (exit − entry) × size, minus fees, plus funding. Exit is the size-weighted average of the exit
fills when the trade closed in parts (TP1 at +1R, then the rest).

Written as journal/YYYY-MM-DD.md (TUI, dashboard fallback) and journal/YYYY-MM-DD.html (the designed page,
light and dark). Re-rendered from the ledger and the event log after every job, so it is idempotent; a day
with zero trades still gets its journal.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import List, Optional

import config
import paper_bot as pb
import timeutil as tu
from store import Store
from util import atomic_write, px, rmult, usd

TITLE = "Crypto Trading Journal"
DATE_FMT = "%m/%d/%Y"          # the design's date format (month/day/year)
SUBTITLE = ("Trade log with entry, exit and size. P&L is net: worked out per trade from the side, the size "
            "and the two prices, minus fees, plus funding.")
ENTRY_KIND = {"close": "series close", "fvg": "FVG retest"}


def money(x: float) -> str:
    """6395 → 6,395.00 · −4.1 → −4.10 (the design's plain number style, true minus sign)."""
    s = f"{abs(x):,.2f}"
    return ("−" + s) if x < -0.004999 else s


def size(q: float) -> str:
    return f"{q:,.6g}"


def avg_exit(t: dict) -> Optional[float]:
    fills = [f for f in t.get("fills", []) if f["kind"] in ("tp1", "exit") and f["qty"] > 0]
    q = sum(f["qty"] for f in fills)
    return sum(f["qty"] * f["price"] for f in fills) / q if q else t.get("exit_price")


def day_view(store: Store, state: dict, now: int, mode: str, day_ms: int | None = None) -> dict:
    """Everything the journal shows for one market day, as plain data (the dashboard renders it too)."""
    start, end = tu.market_day(day_ms if day_ms is not None else now, config.MARKET_DAY_START)
    closed = sorted((t for t in state["closed"] if start <= t["closed_at"] < end),
                    key=lambda t: (t["opened_at"], t["closed_at"]), reverse=True)
    rows = []
    for t in closed:
        rows.append({
            "date": tu.fmt(t["opened_at"], DATE_FMT), "time": tu.hm(t["opened_at"]),
            "closed": tu.fmt(t["closed_at"], DATE_FMT + " %H:%M"),
            "ticker": t["symbol"], "side": t["direction"].capitalize(), "size": t["qty"],
            "entry": t["entry"], "exit": avg_exit(t), "pnl": t["net_pnl"],
            "tag": t.get("tag", ""), "closure": t.get("closure", ""),
            "entry_kind": ENTRY_KIND.get(t.get("entry_kind", ""), "—"), "fvg": bool(t.get("fvg")),
            "reason": t.get("exit_reason", ""), "r": t.get("r_mult"),
        })
    events = store.events(start, end)
    g = pb.gates(state, min(now, end - 1))
    notes: List[str] = []
    opened_today = [p for p in state["positions"] if start <= p["opened_at"] < end]
    if state["positions"]:
        notes.append(f"{len(state['positions'])} position(s) still open ({len(opened_today)} opened this market day); "
                     f"they join the log when they close.")
    if not rows:
        notes.append("No trades closed this market day — journal kept per framework.")
    if g.loss_stop:
        notes.append(f"Daily loss stop hit ({usd(g.realized, True)}, {g.losses} losing) — no new entries until 00:00 MYT.")
    if g.protect:
        notes.append(f"PROTECT on (+${pb.DAILY_TARGET:.0f} banked) — no new entries until 00:00 MYT.")
    signals = [e for e in events if e["kind"] == "signals"]
    if signals:
        n_sig = sum(len(e.get("signals", [])) for e in signals)
        notes.append(f"{len(signals)} signal scan(s), {n_sig} framework signal(s) listed.")
    for e in [e for e in events if e["kind"] == "error"][-5:]:
        notes.append(f"{tu.hm(e['ts'])} error in {e.get('job')}: {e.get('error')}")
    return {
        "date": tu.date_str(start), "date_label": tu.fmt(start, DATE_FMT), "title": TITLE, "subtitle": SUBTITLE,
        "currency": "$", "total_pnl": sum(r["pnl"] for r in rows), "entries": len(rows), "rows": rows,
        "notes": notes, "mode": mode.upper(), "engine": config.ENGINE, "updated": tu.fmt(now, DATE_FMT + " %H:%M"),
        "window": f"market day {config.MARKET_DAY_START} → {config.MARKET_DAY_START} MYT",
    }


# --- markdown ----------------------------------------------------------------------------------------

def render_md(v: dict) -> str:
    lines = [f"# {v['title']} — {v['date_label']}", "", f"_{v['subtitle']}_", "",
             f"Currency **{v['currency']}** · {v['window']} · {v['mode']} · {v['engine']} · updated {v['updated']} MYT",
             "", "## At a glance", "", f"| Total P&L ({v['currency']}) | Entries |", "|---|---|",
             f"| **{money(v['total_pnl'])}** | **{v['entries']}** rows filled in |", "",
             f"## {v['title']}", ""]
    if v["rows"]:
        lines += [f"| Date | Ticker | Side | Size | Entry ({v['currency']}) | Exit ({v['currency']}) | P&L ({v['currency']}) |",
                  "|---|---|---|---|---|---|---|"]
        for r in v["rows"]:
            lines.append(f"| {r['date']} {r['time']} | {r['ticker']} | {r['side']} | {size(r['size'])} | {px(r['entry'])} | "
                         f"{px(r['exit'])} | {money(r['pnl'])} |")
        lines += ["", "## Trade notes", "", "| Ticker | Setup | Closure | Entry | Exit | R |", "|---|---|---|---|---|---|"]
        for r in v["rows"]:
            lines.append(f"| {r['ticker']} | {r['tag']} | {r['closure'] or '—'} | {r['entry_kind']}{' +FVG' if r['fvg'] else ''} "
                         f"| {r['reason']} | {rmult(r['r'])} |")
    else:
        lines.append("No trades closed this market day.")
    lines += ["", "## Day notes", ""] + [f"- {n}" for n in v["notes"] or ["—"]] + [""]
    return "\n".join(lines)


# --- html (the designed page) ------------------------------------------------------------------------

CSS = """
:root { color-scheme: light; --page: #ffffff; --ink: #141c33; --ink-2: #4a5163; --muted: #9a9fab;
  --rule: #141c33; --cell: #f1f3f5; --cell-2: #ffffff; --line: #e3e6ea; --head: #172242; --head-ink: #ffffff;
  --box: #f1f3f5; --box-line: #cdd1d7; --neg: #c0392b; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --page: #0f121a;
  --ink: #eef0f5; --ink-2: #b5bac6; --muted: #858b99; --rule: #8d97b5; --cell: #1a1f2b; --cell-2: #141823;
  --line: #262c3a; --head: #2a3a66; --head-ink: #ffffff; --box: #1a1f2b; --box-line: #3a4152; --neg: #ff7b6b; } }
:root[data-theme="dark"] { color-scheme: dark; --page: #0f121a; --ink: #eef0f5; --ink-2: #b5bac6; --muted: #858b99;
  --rule: #8d97b5; --cell: #1a1f2b; --cell-2: #141823; --line: #262c3a; --head: #2a3a66; --head-ink: #ffffff;
  --box: #1a1f2b; --box-line: #3a4152; --neg: #ff7b6b; }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--page); color: var(--ink); }
body { font: 15px/1.45 "Helvetica Neue", Arial, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
.page { max-width: 1040px; margin: 0 auto; padding: 36px 24px 48px; }
h1 { font: 900 clamp(30px, 6vw, 56px)/1.05 "Arial Black", "Helvetica Neue", Arial, sans-serif; letter-spacing: -0.01em; margin: 0 0 18px; }
.top { display: flex; gap: 24px; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; }
.sub { font: italic 17px/1.45 Georgia, "Times New Roman", serif; color: var(--ink-2); margin: 0; max-width: 620px; }
.boxes { display: grid; gap: 8px; }
.box { display: grid; grid-template-columns: auto 150px; align-items: center; gap: 14px; color: var(--muted);
  font-size: 13px; letter-spacing: .06em; text-transform: uppercase; }
.box b { display: block; background: var(--box); border: 2px solid var(--box-line); padding: 10px 14px; text-align: right;
  color: var(--ink); font-weight: 500; letter-spacing: 0; text-transform: none; font-size: 17px; }
h2 { font: 900 22px/1.2 "Arial Black", "Helvetica Neue", Arial, sans-serif; letter-spacing: .02em; text-transform: uppercase;
  margin: 46px 0 14px; }
h2.small { font-size: 15px; margin-top: 36px; color: var(--ink-2); }
.glance { display: grid; grid-template-columns: 1fr 1fr; border-top: 4px solid var(--rule); }
.glance > div { border: 1px solid var(--line); border-top: 0; padding: 22px 18px 20px; text-align: center; }
.glance > div + div { border-left: 0; }
.glance .k { color: var(--muted); font-weight: 800; letter-spacing: .08em; font-size: 15px; text-transform: uppercase; }
.glance .v { font: 900 clamp(34px, 6vw, 58px)/1.1 "Arial Black", "Helvetica Neue", Arial, sans-serif; margin: 18px 0 8px;
  font-variant-numeric: tabular-nums; }
.glance .c { color: var(--muted); font-size: 14px; }
.tw { overflow-x: auto; }
table { width: 100%; border-collapse: separate; border-spacing: 0; font-variant-numeric: tabular-nums; }
th { background: var(--head); color: var(--head-ink); font-weight: 800; font-size: 15px; padding: 15px 12px; white-space: nowrap; }
td { background: var(--cell); padding: 13px 12px; border-bottom: 2px solid var(--page); white-space: nowrap; font-size: 15px; }
td.pnl { background: var(--cell-2); border-bottom-color: var(--line); }
.c { text-align: center; } .r { text-align: right; }
td small { display: block; color: var(--muted); font-size: 12px; }
.neg { color: var(--neg); }
.notes th { background: var(--cell); color: var(--ink-2); font-size: 12.5px; padding: 9px 10px; }
.notes td { background: transparent; border-bottom: 1px solid var(--line); font-size: 13px; padding: 8px 10px; color: var(--ink-2); }
ul { margin: 0; padding-left: 20px; color: var(--ink-2); font-size: 14px; }
.empty { background: var(--cell); padding: 22px; text-align: center; color: var(--muted); }
footer { margin-top: 30px; color: var(--muted); font-size: 12.5px; }
@media (max-width: 640px) { .page { padding: 24px 16px 36px; } .box { grid-template-columns: 1fr 120px; }
  .glance { grid-template-columns: 1fr; } .glance > div + div { border-left: 1px solid var(--line); } }
"""


def render_html(v: dict) -> str:
    e = html.escape
    cur = e(v["currency"])

    def pnl_cell(x: float, cls: str = "") -> str:
        return f'<td class="r pnl{(" " + cls) if cls else ""}"><span class="{"neg" if x < -0.004999 else ""}">{money(x)}</span></td>'

    if v["rows"]:
        body = "".join(
            f'<tr><td class="c">{e(r["date"])}<small>{e(r["time"])} MYT</small></td><td class="c">{e(r["ticker"])}</td>'
            f'<td class="c">{e(r["side"])}</td><td class="r">{size(r["size"])}</td><td class="r">{px(r["entry"])}</td>'
            f'<td class="r">{px(r["exit"])}</td>{pnl_cell(r["pnl"])}</tr>' for r in v["rows"])
        log = (f'<div class="tw"><table><thead><tr><th>Date</th><th>Ticker</th><th>Side</th><th>Size</th>'
               f'<th>Entry ({cur})</th><th>Exit ({cur})</th><th>P&amp;L ({cur})</th></tr></thead><tbody>{body}</tbody></table></div>')
        nrows = "".join(
            f'<tr><td>{e(r["ticker"])}</td><td>{e(r["tag"])}</td><td>{e(r["closure"] or "—")}</td>'
            f'<td>{e(r["entry_kind"])}{" +FVG" if r["fvg"] else ""}</td><td>{e(r["reason"])}</td>'
            f'<td class="r">{e(rmult(r["r"]))}</td><td>{e(r["closed"])}</td></tr>' for r in v["rows"])
        trade_notes = (f'<h2 class="small">Trade notes</h2><div class="tw"><table class="notes"><thead><tr><th>Ticker</th>'
                       f'<th>Setup</th><th>Closure</th><th>Entry</th><th>Exit</th><th class="r">R</th><th>Closed (MYT)</th>'
                       f'</tr></thead><tbody>{nrows}</tbody></table></div>')
    else:
        log = '<div class="empty">No trades closed this market day.</div>'
        trade_notes = ""
    notes = "".join(f"<li>{e(n)}</li>" for n in v["notes"]) or "<li>—</li>"
    total = v["total_pnl"]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(v["title"])} · {e(v["date_label"])}</title>
<style>{CSS}</style>
</head>
<body>
<main class="page">
  <h1>{e(v["title"])}</h1>
  <div class="top">
    <p class="sub">{e(v["subtitle"])}</p>
    <div class="boxes">
      <div class="box">Market day <b>{e(v["date_label"])}</b></div>
      <div class="box">Currency <b>{cur}</b></div>
    </div>
  </div>
  <h2>At a glance</h2>
  <div class="glance">
    <div><div class="k">Total P&amp;L ({cur})</div><div class="v{" neg" if total < -0.004999 else ""}">{money(total)}</div></div>
    <div><div class="k">Entries</div><div class="v">{v["entries"]}</div><div class="c">rows filled in</div></div>
  </div>
  <h2>{e(v["title"])}</h2>
  {log}
  {trade_notes}
  <h2 class="small">Day notes</h2>
  <ul>{notes}</ul>
  <footer>{e(v["window"])} · {e(v["mode"])} · {e(v["engine"])} · updated {e(v["updated"])} MYT</footer>
</main>
</body>
</html>
"""


# --- files -------------------------------------------------------------------------------------------

def render(store: Store, state: dict, now: int, mode: str, day_ms: int | None = None) -> tuple:
    v = day_view(store, state, now, mode, day_ms)
    return v["date"], render_md(v)


def write(store: Store, state: dict, now: int, mode: str, day_ms: int | None = None) -> Path:
    v = day_view(store, state, now, mode, day_ms)
    path = store.paths.journal / f"{v['date']}.md"
    atomic_write(path, render_md(v))
    atomic_write(path.with_suffix(".html"), render_html(v))
    return path


def list_days(store: Store) -> List[str]:
    return sorted((p.stem for p in Path(store.paths.journal).glob("*.md")), reverse=True)
