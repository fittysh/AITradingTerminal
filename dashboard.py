"""dashboard.py — the UX dashboard (dashboard/index.html) and its read-only live server.

snapshot(ctx)  one JSON document with everything the UIs show (web dashboard, TUI, `status`)
write(ctx)     dashboard/index.html = template + embedded snapshot → opens straight from disk (file://)
serve(ctx)     http://127.0.0.1:8787 — the same page, refreshed from /api/state every 20 s.
               Read-only by design: no endpoint can place, change or cancel anything.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List

import config
import journal
import paper_bot as pb
import schedule
import timeutil as tu
from util import atomic_write, dumps

TEMPLATE = Path(__file__).resolve().parent / "dashboard" / "template.html"
log = logging.getLogger("bot")


def _downsample(points: List, n: int) -> List:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]


def position_view(p: dict, now: int) -> dict:
    d, r = p["dir"], p["r_price"]
    price = p.get("mark_price") or p["last_price"]

    def R(x):
        return d * (x - p["entry"]) / r

    return {k: p.get(k) for k in ("id", "symbol", "direction", "tag", "tier", "family", "trigger", "closure",
                                  "entry_kind", "fvg", "struct_tf",
                                  "entry_tf", "entry", "stop", "initial_stop", "stop_kind", "target", "tp1",
                                  "lock_trigger", "lock_level", "peak", "qty", "qty_open", "risk_usd", "tp1_done",
                                  "trail_armed", "lock_done", "opened_at", "mode")} | {
        "price": price, "r_now": R(price), "r_peak": R(p["peak"]), "r_stop": R(p["stop"]), "r_target": R(p["target"]),
        "unreal": d * (price - p["entry"]) * p["qty_open"],
        "realized": sum(f["pnl"] - f["fee"] for f in p["fills"]),
        "age_h": (now - p["opened_at"]) / tu.HOUR, "time_stop_at": p["opened_at"] + pb.MAX_TRADE_AGE_HOURS * tu.HOUR,
        "setup": {k: (p.get("setup") or {}).get(k) for k in ("checklist", "poi", "range", "raid", "rr")}}


def trade_row(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "symbol", "direction", "tag", "tier", "family", "trigger", "closure",
                                  "entry_kind", "fvg", "opened_at",
                                  "closed_at", "entry", "exit_price", "exit_reason", "qty", "risk_usd", "gross_pnl",
                                  "fees", "funding", "net_pnl", "r_mult", "mfe_r", "mae_r", "hold_h", "mode")}


def _daily(state: dict, now: int, days: int = 60) -> List[list]:
    """[market-day date, net realized, trades closed] for the last `days` market days."""
    net, count = {}, {}
    for p in state["positions"] + state["closed"]:
        for f in p["fills"]:
            day = tu.date_str(tu.market_day(f["ts"], config.MARKET_DAY_START)[0])
            net[day] = net.get(day, 0.0) + f["pnl"] - f["fee"]
    for t in state["closed"]:
        day = tu.date_str(tu.market_day(t["closed_at"], config.MARKET_DAY_START)[0])
        count[day] = count.get(day, 0) + 1
    if not net:
        return []
    first = tu.parse_date(min(net)) + tu.hhmm(config.MARKET_DAY_START)     # market days open at 07:00 MYT
    cur = tu.market_day(now, config.MARKET_DAY_START)[0]
    out, t = [], max(first, cur - (days - 1) * tu.DAY)
    while t <= cur:
        day = tu.date_str(t)
        out.append([day, round(net.get(day, 0.0), 4), count.get(day, 0)])
        t += tu.DAY
    return out


def snapshot(ctx) -> dict:
    now = ctx.now()
    store = ctx.store
    st = store.load_state()
    g = pb.gates(st, now)
    closed = st["closed"]
    bal, unreal = pb.balance(st), pb.unrealized(st)
    d0, d1 = tu.day_bounds(now)
    today = [t for t in closed if d0 <= t["closed_at"] < d1]
    sig_live = store.read("signals") or {}
    armed = store.read("armed") or {}
    sweeps = store.read("sweeps") or {}
    if sweeps.get("sweeps"):
        sweeps = dict(sweeps, sweeps=sweeps["sweeps"][:80], total=len(sweeps["sweeps"]))
    dfail = store.read("daily_failure") or {}
    if dfail.get("items"):
        dfail = dict(dfail, items=dfail["items"][:80], total=len(dfail["items"]))
    events = []
    for e in store.events(limit=160):
        e = {k: v for k, v in e.items() if k not in ("checklist", "funnel", "skipped")}
        events.append(e)
    days = journal.list_days(store)[:10]
    entries, views = {}, {}
    for day in days[:7]:
        try:
            entries[day] = (store.paths.journal / f"{day}.md").read_text(encoding="utf-8")
        except OSError:
            continue
        views[day] = journal.day_view(store, st, now, ctx.label, day_ms=tu.parse_date(day) + 12 * tu.HOUR)
    outbox = sorted((p.name for p in store.paths.outbox.glob("*.html")), reverse=True)[:12]
    curve = _downsample(pb.equity_curve(st), 600)
    return {
        "meta": {"generated_at": now, "mode": ctx.label, "trading_mode": ctx.mode, "sim": ctx.sim,
                 "engine": config.ENGINE, "exchange": config.EXCHANGE, "tz": "MYT (UTC+8)",
                 "market_day_start": config.MARKET_DAY_START, "notify": ctx.notifier.channels(),
                 "home": str(store.paths.home), "live_orders": ctx.mode == "live"},
        "account": {"start": st["account"]["start_balance"], "balance": bal, "unrealized": unreal,
                    "equity": bal + unreal, "net_total": bal - st["account"]["start_balance"]},
        "gates": g.to_dict(),
        "today": {"net": g.realized, "trades": len(today), "wins": g.wins, "losses": g.losses,
                  "r": g.realized / pb.RISK_PER_TRADE},
        "rules": {"risk": pb.RISK_PER_TRADE, "max_open": pb.MAX_OPEN_POSITIONS, "corr": pb.CORR_MAX_PER_SIDE,
                  "max_loss": pb.DAILY_MAX_LOSS, "max_losses": pb.MAX_LOSSES_PER_DAY, "target": pb.DAILY_TARGET,
                  "max_age_h": pb.MAX_TRADE_AGE_HOURS, "trail_pct": pb.TRAIL_PCT},
        "stats": pb.trade_stats(closed),
        "by": {k: pb.breakdown(closed, k) for k in ("family", "tier", "trigger", "closure", "entry_kind", "direction")},
        "positions": [position_view(p, now) for p in st["positions"]],
        "signals": sig_live, "armed": armed.get("items", [])[:60],
        "scan": store.read("scan") or {}, "sweeps": sweeps, "daily_failure": dfail,
        "trades": [trade_row(t) for t in sorted(closed, key=lambda t: t["closed_at"])[-400:]],
        "equity": [[ts, round(v, 4)] for ts, v in curve],
        "daily": _daily(st, now),
        "schedule": {"next": schedule.upcoming(now), "last": store.read("schedule", {}) or {},
                     "labels": schedule.LABELS},
        "events": events,
        "journal": {"days": days, "entries": entries, "views": views},
        "outbox": outbox,
    }


def render_html(snap: dict) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = dumps(snap).replace("</", "<\\/")
    return html.replace("/*__STATE__*/null", payload, 1)


def write(ctx) -> Path:
    path = ctx.store.paths.dashboard / "index.html"
    atomic_write(path, render_html(snapshot(ctx)))
    return path


def serve(ctx, host: str = config.DASHBOARD_HOST, port: int = config.DASHBOARD_PORT) -> None:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AITradingTerminal/4"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            route = self.path.split("?", 1)[0]
            try:
                if route in ("/", "/index.html"):
                    with lock:
                        body = render_html(snapshot(ctx)).encode("utf-8")
                    self._send(200, body, "text/html; charset=utf-8")
                elif route == "/api/state":
                    with lock:
                        body = dumps(snapshot(ctx)).encode("utf-8")
                    self._send(200, body, "application/json")
                elif route == "/healthz":
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as exc:  # never kill the server on one bad request
                log.error("dashboard request failed: %s", exc)
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

        def log_message(self, fmt, *args):
            log.debug("dashboard %s", fmt % args)

    srv = ThreadingHTTPServer((host, port), Handler)
    log.info("dashboard on http://%s:%d (read-only) · Ctrl+C to stop", host, port)
    print(f"Dashboard: http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

