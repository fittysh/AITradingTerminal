"""bitget_report.py — the authoritative net-of-fees daily report (07:00 & 19:00 MYT).

Source of truth
  paper mode   the local ledger: every fill carries its taker fee and slippage, funding settles every 8h
  demo / live  Bitget's own position history (pnl, netProfit, fees, totalFunding), joined to the
               journal by symbol + open time to recover the setup tag / tier / 1R
Windows
  07:00 report  the market day that just ended   [yesterday 07:00, today 07:00) MYT
  19:00 report  the market day so far             [today 07:00, today 19:00) MYT
Renderings: markdown (journal + reports/), Telegram HTML, Gmail HTML, PDF attachment.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import config
import paper_bot as pb
import timeutil as tu
from notify import esc
from pdfgen import PDF
from util import pct, px, rmult, usd

FAMILY_ORDER = ("sweep", "unicorn", "OB")


def window_for(slot_ms: int) -> tuple:
    """(start, end, label) for a report slot time."""
    hhmm = tu.hm(slot_ms)
    day_start, _ = tu.market_day(slot_ms - 1, config.MARKET_DAY_START)
    if hhmm == config.MARKET_DAY_START:
        return day_start, slot_ms, f"market day {tu.date_str(day_start)} (07:00→07:00 MYT)"
    return day_start, slot_ms, f"market day {tu.date_str(day_start)} so far (07:00→{hhmm} MYT)"


def _row(t: dict) -> dict:
    return {"symbol": t["symbol"], "direction": t["direction"], "tag": t.get("tag", "?"), "tier": t.get("tier", "?"),
            "family": t.get("family", "?"), "trigger": t.get("trigger", "?"),
            "opened_at": t["opened_at"], "closed_at": t["closed_at"], "entry": t["entry"],
            "exit": t.get("exit_price"), "qty": t.get("qty"), "gross": t["gross_pnl"], "fees": t["fees"],
            "funding": t["funding"], "net": t["net_pnl"], "r": t["r_mult"], "reason": t.get("exit_reason", "?"),
            "hold_h": t.get("hold_h")}


def exchange_trades(client, state: dict, start: int, end: int) -> List[dict]:
    """Closed positions from Bitget's position history (authoritative net), tagged from the local journal."""
    local = state["closed"] + state["positions"]
    out = []
    for r in client.position_history(start, end):
        closed_at = int(r.get("utime") or r.get("uTime") or 0)
        if not start <= closed_at < end:
            continue
        opened_at = int(r.get("ctime") or r.get("cTime") or closed_at)
        sym = r.get("symbol", "")
        gross = float(r.get("pnl") or 0)
        funding = float(r.get("totalFunding") or 0)
        net = float(r.get("netProfit") or 0)
        match = min((t for t in local if t["symbol"] == sym and abs(t["opened_at"] - opened_at) < 15 * tu.MIN),
                    key=lambda t: abs(t["opened_at"] - opened_at), default=None)
        risk = (match or {}).get("risk_usd") or pb.RISK_PER_TRADE
        out.append({"symbol": sym, "direction": r.get("holdSide", "?"), "tag": (match or {}).get("tag", "unmatched"),
                    "tier": (match or {}).get("tier", "?"), "family": (match or {}).get("family", "?"),
                    "closure": (match or {}).get("closure", "?"), "entry_kind": (match or {}).get("entry_kind", "?"),
                    "trigger": (match or {}).get("trigger", "?"), "opened_at": opened_at, "closed_at": closed_at,
                    "entry": float(r.get("openAvgPrice") or 0), "exit_price": float(r.get("closeAvgPrice") or 0),
                    "qty": float(r.get("closeTotalPos") or 0), "gross_pnl": gross, "funding": funding,
                    "fees": gross + funding - net, "net_pnl": net, "r_mult": net / risk,
                    "exit_reason": (match or {}).get("exit_reason", "exchange"),
                    "hold_h": (closed_at - opened_at) / tu.HOUR})
    return out


def build(state: dict, start: int, end: int, now: int, mode: str, label: str,
          trades: Optional[List[dict]] = None, source: Optional[str] = None) -> dict:
    if trades is None:
        trades = [t for t in state["closed"] if start <= t["closed_at"] < end]
        source = source or "paper ledger (fees + slippage + funding simulated)"
    trades = sorted(trades, key=lambda t: t["closed_at"])
    bal = pb.balance(state)
    unreal = pb.unrealized(state)
    curve = pb.equity_curve(state)
    return {
        "title": "Daily report", "label": label, "window": [start, end], "generated_at": now, "mode": mode,
        "source": source or "Bitget position history", "engine": config.ENGINE, "exchange": config.EXCHANGE,
        "account": {"start": state["account"]["start_balance"], "balance": bal, "unrealized": unreal,
                    "equity": bal + unreal},
        "realized_window": pb.realized_between(state, start, end),
        "stats": pb.trade_stats(trades),
        "by": {k: pb.breakdown(trades, k) for k in ("family", "tier", "trigger", "closure", "entry_kind", "direction")},
        "trades": [_row(t) for t in trades],
        "open": [{"symbol": p["symbol"], "direction": p["direction"], "tag": p["tag"], "entry": p["entry"],
                  "stop": p["stop"], "stop_kind": p["stop_kind"], "target": p["target"], "price": p.get("last_price"),
                  "r": pb.r_now(p), "tp1_done": p["tp1_done"], "age_h": (now - p["opened_at"]) / tu.HOUR}
                 for p in state["positions"]],
        "gates": pb.gates(state, now).to_dict(),
        "equity": curve[-400:],
    }


# --- renderers -----------------------------------------------------------------------------------

def _kpis(rep: dict) -> List[tuple]:
    s = rep["stats"]
    return [("Net P&L", usd(s["net"], signed=True)), ("Trades", f"{s['trades']} ({s['wins']}W / {s['losses']}L)"),
            ("Win rate", pct(s["win_rate"], 1, signed=False) if s["win_rate"] is not None else "—"),
            ("Expectancy", rmult(s["expectancy_r"])),
            ("Profit factor", f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "—"),
            ("Fees + funding", usd(-s["fees"] + s["funding"], signed=True))]


def _gate_line(g: dict) -> str:
    loss = "HIT" if g["loss_stop"] else "ARMED"
    prot = "ON" if g["protect"] else "off"
    return (f"Loss stop {loss} ({usd(g['realized'], signed=True)} / −${g['max_loss']:.0f}; losses {g['losses']}/"
            f"{g['max_losses']}) · PROTECT {prot} (target +${g['target']:.0f}) · open {g['open']}/{g['max_open']} "
            f"(L{g['longs']}/S{g['shorts']} of {g['corr_max']})")


def to_markdown(rep: dict) -> str:
    a = rep["account"]
    lines = [f"# {rep['title']} — {rep['label']}", "",
             f"Mode **{rep['mode'].upper()}** · engine {rep['engine']} · {rep['exchange']} · source: {rep['source']}  ",
             f"Generated {tu.fmt(rep['generated_at'])} MYT · window {tu.fmt(rep['window'][0])} → {tu.fmt(rep['window'][1])}",
             "", "| " + " | ".join(k for k, _ in _kpis(rep)) + " |", "|" + "---|" * len(_kpis(rep)),
             "| " + " | ".join(v for _, v in _kpis(rep)) + " |", "",
             f"Account: balance {usd(a['balance'])} · equity {usd(a['equity'])} (unrealized "
             f"{usd(a['unrealized'], signed=True)}) · start {usd(a['start'])}  ",
             f"Gates (MYT day): {_gate_line(rep['gates'])}", ""]
    lines += ["## Closed trades", ""]
    if rep["trades"]:
        lines += ["| Closed | Symbol | Side | Setup | Exit | Hold | R | Gross | Fees | Funding | Net |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for t in rep["trades"]:
            lines.append(f"| {tu.fmt(t['closed_at'], '%m-%d %H:%M')} | {t['symbol']} | {t['direction']} | {t['tag']} | "
                         f"{t['reason']} | {t['hold_h']:.1f}h | {rmult(t['r'])} | {usd(t['gross'], True)} | "
                         f"{usd(-t['fees'], True)} | {usd(t['funding'], True)} | **{usd(t['net'], True)}** |")
    else:
        lines.append("No trades closed in this window.")
    lines += ["", "## By setup family (sweep-* / unicorn-* / OB-*)", "",
              "| Family | Trades | Win rate | Expectancy | Net |", "|---|---|---|---|---|"]
    fams = rep["by"]["family"]
    for fam in [f for f in FAMILY_ORDER if f in fams] + [f for f in fams if f not in FAMILY_ORDER]:
        b = fams[fam]
        lines.append(f"| {fam} | {b['trades']} | {pct(b['win_rate'], 1, False) if b['win_rate'] is not None else '—'} "
                     f"| {rmult(b['expectancy_r'])} | {usd(b['net'], True)} |")
    if not fams:
        lines.append("| — | 0 | — | — | — |")
    lines += ["", "## Open positions", ""]
    if rep["open"]:
        lines += ["| Symbol | Side | Setup | Entry | Stop | Target | Last | R now | Age |", "|---|---|---|---|---|---|---|---|---|"]
        for p in rep["open"]:
            lines.append(f"| {p['symbol']} | {p['direction']} | {p['tag']} | {px(p['entry'])} | {px(p['stop'])} "
                         f"({p['stop_kind']}) | {px(p['target'])} | {px(p['price'])} | {rmult(p['r'])} | {p['age_h']:.1f}h |")
    else:
        lines.append("Flat.")
    lines += ["", "_Net of fees and funding. DEMO — live orders stay disabled until the signal proves positive._", ""]
    return "\n".join(lines)


def to_telegram(rep: dict) -> str:
    head = (f"📒 <b>DAILY REPORT</b> · {esc(rep['label'])}\n"
            f"Mode {rep['mode'].upper()} · {esc(rep['engine'])} · net of fees\n")
    kp = "\n".join(f"{k:<15}{v}" for k, v in _kpis(rep))
    body = f"<pre>{esc(kp)}</pre>\n"
    if rep["trades"]:
        rows = [f"{t['symbol'][:10]:<10} {t['direction'][0].upper()} {t['reason'][:5]:<5} {t['r']:+5.2f}R "
                f"{t['net']:+7.2f}" for t in rep["trades"][-15:]]
        body += "<pre>" + esc("\n".join(rows)) + "</pre>\n"
    else:
        body += "No trades closed in this window.\n"
    a = rep["account"]
    body += (f"Balance {usd(a['balance'])} · equity {usd(a['equity'])}\n"
             f"{esc(_gate_line(rep['gates']))}\n"
             f"Open: {len(rep['open'])}" + ("" if not rep["open"] else " — " + ", ".join(
                 f"{p['symbol']} {rmult(p['r'], 1)}" for p in rep["open"])))
    return head + body + "\n(PDF attached)"


def to_html(rep: dict) -> str:
    """Email body — inline styles only (Gmail strips <style>)."""
    k = "".join(f"<td style='padding:10px 14px;border:1px solid #e3e6ea'><div style='font-size:11px;color:#667'>{esc(n)}"
                f"</div><div style='font-size:17px;font-weight:600'>{esc(v)}</div></td>" for n, v in _kpis(rep))
    th = "style='text-align:left;padding:6px 8px;border-bottom:2px solid #d0d5db;font-size:12px;color:#445'"
    td = "style='padding:5px 8px;border-bottom:1px solid #eceef1;font-size:12px'"

    def color(v):
        return "#1a7f37" if v > 0 else "#c62828" if v < 0 else "#444"

    trades = "".join(
        f"<tr><td {td}>{tu.fmt(t['closed_at'], '%m-%d %H:%M')}</td><td {td}><b>{esc(t['symbol'])}</b></td>"
        f"<td {td}>{t['direction']}</td><td {td}>{esc(t['tag'])}</td><td {td}>{t['reason']}</td>"
        f"<td {td} align=right>{t['r']:+.2f}R</td><td {td} align=right style='color:{color(t['net'])}'>"
        f"{usd(t['net'], True)}</td></tr>" for t in rep["trades"]) or \
        f"<tr><td {td} colspan=7>No trades closed in this window.</td></tr>"
    fam = "".join(f"<tr><td {td}>{esc(f)}</td><td {td}>{b['trades']}</td><td {td}>"
                  f"{pct(b['win_rate'], 1, False) if b['win_rate'] is not None else '—'}</td><td {td}>"
                  f"{rmult(b['expectancy_r'])}</td><td {td} style='color:{color(b['net'])}'>{usd(b['net'], True)}</td></tr>"
                  for f, b in rep["by"]["family"].items()) or f"<tr><td {td} colspan=5>—</td></tr>"
    a = rep["account"]
    return (f"<div style='font-family:Segoe UI,Arial,sans-serif;color:#1d232b;max-width:760px'>"
            f"<div style='background:#10151d;color:#fff;padding:14px 18px'><div style='font-size:12px;opacity:.7'>"
            f"AI TRADING TERMINAL · {esc(rep['engine'])} · {rep['mode'].upper()}</div>"
            f"<div style='font-size:20px;font-weight:600'>Daily report — {esc(rep['label'])}</div></div>"
            f"<table style='border-collapse:collapse;margin:14px 0'><tr>{k}</tr></table>"
            f"<p style='font-size:13px'>Balance <b>{usd(a['balance'])}</b> · equity {usd(a['equity'])} · "
            f"unrealized {usd(a['unrealized'], True)}<br><span style='color:#556'>{esc(_gate_line(rep['gates']))}</span></p>"
            f"<h3 style='font-size:15px'>Closed trades</h3><table style='border-collapse:collapse;width:100%'>"
            f"<tr><th {th}>Closed</th><th {th}>Symbol</th><th {th}>Side</th><th {th}>Setup</th><th {th}>Exit</th>"
            f"<th {th}>R</th><th {th}>Net</th></tr>{trades}</table>"
            f"<h3 style='font-size:15px'>By setup family</h3><table style='border-collapse:collapse'>"
            f"<tr><th {th}>Family</th><th {th}>Trades</th><th {th}>Win rate</th><th {th}>Expectancy</th><th {th}>Net</th>"
            f"</tr>{fam}</table><p style='font-size:11px;color:#778'>Net of fees and funding · source: "
            f"{esc(rep['source'])} · DEMO — no live orders until the signal proves positive.</p></div>")


def to_pdf(rep: dict, path: Path) -> Path:
    ink, mute, rule = (0.11, 0.14, 0.18), (0.42, 0.46, 0.52), (0.85, 0.87, 0.90)
    pos_c, neg_c, accent = (0.10, 0.50, 0.22), (0.78, 0.16, 0.16), (0.16, 0.42, 0.85)
    doc = PDF(f"Daily report {rep['label']}")
    M, W = 36, PDF.W - 72
    state = {"y": 0.0, "page": 0}

    def new_page():
        doc.add_page()
        state["page"] += 1
        doc.rect(0, PDF.H - 64, PDF.W, 64, fill=(0.06, 0.08, 0.11))
        doc.text(M, PDF.H - 28, "AI TRADING TERMINAL", 9, "F2", (0.55, 0.78, 1.0))
        doc.text(M, PDF.H - 48, f"Daily report - {rep['label']}", 15, "F2", (1, 1, 1))
        doc.text(PDF.W - M, PDF.H - 28, f"{rep['mode'].upper()} | {rep['engine']}", 8, "F1", (0.8, 0.85, 0.9), "right")
        doc.text(PDF.W - M, PDF.H - 48, f"generated {tu.fmt(rep['generated_at'])} MYT", 8, "F1", (0.8, 0.85, 0.9), "right")
        doc.text(M, 22, "Net of fees and funding. DEMO - live orders disabled until the signal proves positive.", 7, "F1", mute)
        doc.text(PDF.W - M, 22, f"page {state['page']}", 7, "F1", mute, "right")
        state["y"] = PDF.H - 90

    def need(h):
        if state["y"] - h < 40:
            new_page()

    def heading(t):
        need(34)
        state["y"] -= 8
        doc.text(M, state["y"], t, 11, "F2", ink)
        state["y"] -= 6
        doc.line(M, state["y"], M + W, state["y"], rule, 0.6)
        state["y"] -= 14

    new_page()
    kp = _kpis(rep)
    tw = W / len(kp)
    for i, (k, v) in enumerate(kp):
        x = M + i * tw
        doc.rect(x + 2, state["y"] - 40, tw - 4, 44, fill=(0.96, 0.97, 0.98), stroke=rule)
        doc.text(x + 9, state["y"] - 8, k.upper(), 6.5, "F2", mute)
        doc.text(x + 9, state["y"] - 28, v, 10.5, "F2", ink)
    state["y"] -= 58
    a = rep["account"]
    doc.text(M, state["y"], f"Balance {usd(a['balance'])}   equity {usd(a['equity'])}   unrealized "
                            f"{usd(a['unrealized'], True)}   start {usd(a['start'])}", 8.5, "F1", ink)
    state["y"] -= 13
    doc.text(M, state["y"], _gate_line(rep["gates"]), 7.5, "F1", mute)
    state["y"] -= 10

    heading("Equity (net, all fills)")
    pts = rep["equity"]
    h = 120
    need(h + 20)
    top, bot = state["y"], state["y"] - h
    doc.rect(M, bot, W, h, stroke=rule)
    if len(pts) >= 2:
        vals = [v for _, v in pts]
        lo_v, hi_v = min(vals + [a["start"]]), max(vals + [a["start"]])
        span = (hi_v - lo_v) or 1.0
        t0, t1 = pts[0][0], pts[-1][0]
        tspan = (t1 - t0) or 1

        def xy(t, v):
            return M + 6 + (W - 12) * (t - t0) / tspan, bot + 8 + (h - 16) * (v - lo_v) / span

        base = xy(t0, a["start"])[1]
        doc.line(M, base, M + W, base, mute, 0.4, dash=(2, 2))
        doc.polyline([xy(t, v) for t, v in pts], accent, 1.4)
        doc.text(M + 4, top - 10, usd(hi_v), 7, "F1", mute)
        doc.text(M + 4, bot + 3, usd(lo_v), 7, "F1", mute)
        doc.text(M + W - 4, bot + 3, tu.fmt(t1, "%m-%d %H:%M"), 7, "F1", mute, "right")
    else:
        doc.text(M + W / 2, bot + h / 2, "no fills yet", 9, "F1", mute, "center")
    state["y"] = bot - 14

    heading("Closed trades")
    cols = [("Closed", 62, "l"), ("Symbol", 70, "l"), ("Side", 34, "l"), ("Setup", 92, "l"), ("Exit", 58, "l"),
            ("R", 44, "r"), ("Fees", 50, "r"), ("Funding", 50, "r"), ("Net", 60, "r")]

    def row(vals, font="F3", color=ink, size=7.5):
        need(12)
        x = M
        for (name, w, al), v in zip(cols, vals):
            c = color(v) if callable(color) else color
            doc.text(x + (w - 2 if al == "r" else 0), state["y"], str(v), size, font, c, "right" if al == "r" else "left")
            x += w
        state["y"] -= 11

    row([c[0] for c in cols], "F4", mute, 7)
    if rep["trades"]:
        for t in rep["trades"]:
            netc = pos_c if t["net"] > 0 else neg_c if t["net"] < 0 else ink
            need(12)
            vals = [tu.fmt(t["closed_at"], "%m-%d %H:%M"), t["symbol"][:11], t["direction"], t["tag"][:18], t["reason"],
                    f"{t['r']:+.2f}", f"{-t['fees']:+.2f}", f"{t['funding']:+.2f}", f"{t['net']:+.2f}"]
            x = M
            for i, ((name, w, al), v) in enumerate(zip(cols, vals)):
                doc.text(x + (w - 2 if al == "r" else 0), state["y"], v, 7.5, "F3", netc if i in (5, 8) else ink,
                         "right" if al == "r" else "left")
                x += w
            state["y"] -= 11
    else:
        doc.text(M, state["y"], "No trades closed in this window.", 8.5, "F1", mute)
        state["y"] -= 12

    heading("By setup family / tier / trigger / closure / entry")
    for key in ("family", "tier", "trigger", "closure", "entry_kind"):
        for name, b in rep["by"][key].items():
            need(12)
            wr = pct(b["win_rate"], 1, False) if b["win_rate"] is not None else "-"
            doc.text(M, state["y"], f"{key:<8} {name:<9} trades {b['trades']:>3}   win {wr:>6}   exp "
                                    f"{rmult(b['expectancy_r']):>7}   net {usd(b['net'], True):>9}", 7.5, "F3", ink)
            state["y"] -= 11
    if not rep["by"]["family"]:
        doc.text(M, state["y"], "No closed trades yet.", 8.5, "F1", mute)
        state["y"] -= 12

    heading("Open positions")
    if rep["open"]:
        for p in rep["open"]:
            need(12)
            doc.text(M, state["y"], f"{p['symbol']:<12} {p['direction']:<5} {p['tag']:<18} entry {px(p['entry']):>11}  stop "
                                    f"{px(p['stop']):>11} ({p['stop_kind']:<9}) R {p['r']:+.2f}  age {p['age_h']:.1f}h",
                     7.5, "F3", ink)
            state["y"] -= 11
    else:
        doc.text(M, state["y"], "Flat.", 8.5, "F1", mute)
    return doc.save(path)


def render_all(rep: dict, reports_dir: Path, stamp: str) -> Dict[str, Path]:
    md = reports_dir / f"{stamp}.md"
    md.write_text(to_markdown(rep), encoding="utf-8")
    pdf = to_pdf(rep, reports_dir / f"{stamp}.pdf")
    return {"md": md, "pdf": pdf}
