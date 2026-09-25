"""tui.py — the terminal user interface (curses, stdlib only).

  python run_bot.py tui            browse + trigger jobs by hand
  python run_bot.py tui --run      the same, with the 24/7 daemon running inside
  python run_bot.py status         one-shot colour snapshot (no curses; works on any terminal)

Tabs 1-9: Overview · Positions · Signals · Scanner · Sweeps · Daily-fail · Trades · Journal · Log
Keys: ←/→ or Tab switch · ↑/↓ PgUp/PgDn move · s scan cycle · g signals · t trade · m manage
      p report · d write dashboard · r refresh · ? help · q quit
Windows: `pip install windows-curses` for the full UI; `status` needs nothing.
"""
from __future__ import annotations

import locale
import os
import sys
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple

import dashboard
import timeutil as tu
from util import compact, pct, px, rmult, usd

try:
    import curses
except ImportError:  # Windows without windows-curses
    curses = None

TABS = ("Overview", "Positions", "Signals", "Scanner", "Sweeps", "Daily-fail", "Trades", "Journal", "Log")
JOB_KEYS = {"s": "cycle", "g": "signals", "t": "trade", "m": "manage", "p": "report"}


class Glyphs:
    def __init__(self, unicode_ok: bool):
        u = unicode_ok
        self.h, self.v = ("─", "│") if u else ("-", "|")
        self.fill, self.tick = ("━", "┼") if u else ("=", "+")
        self.dot, self.peak, self.stop = ("●", "◆", "┃") if u else ("o", "^", "|")
        self.body, self.hollow = ("█", "┃") if u else ("#", "I")
        self.ok, self.no, self.wait = ("✓", "✗", "·") if u else ("v", "x", ".")
        self.arrow_up, self.arrow_dn = ("▲", "▼") if u else ("^", "v")
        self.spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if u else "|/-\\"
        self.dash = "—" if u else "-"


def _utf8() -> bool:
    enc = (locale.getpreferredencoding(False) or "").lower()
    return "utf" in enc


# --- text helpers (shared by curses and plain status) ---------------------------------------------

def ladder_cells(p: dict, width: int, G: Glyphs) -> List[Tuple[str, str]]:
    """The R-ladder as (char, role) cells: −1R stop → BE → +1R → +2R → target."""
    width = max(12, width)
    lo, hi = -1.2, max(p["r_target"], 2.2) + 0.2

    def col(v: float) -> int:
        v = max(lo, min(hi, v))
        return int(round((v - lo) / (hi - lo) * (width - 1)))

    cells = [(" ", "dim")] * width
    for x in range(col(-1), col(p["r_target"]) + 1):
        cells[x] = (G.h, "dim")
    ticks = [-1, 0, 1] + ([2] if abs(p["r_target"] - 2) > 0.3 else []) + [p["r_target"]]
    for t in ticks:
        cells[col(t)] = (G.tick, "dim")
    a, b = sorted((col(0), col(p["r_now"])))
    role = "pos" if p["r_now"] >= 0 else "neg"
    for x in range(a, b + 1):
        cells[x] = (G.fill, role)
    if p["r_peak"] > p["r_now"] + 0.05:
        cells[col(p["r_peak"])] = (G.peak, "ink2")
    cells[col(p["r_stop"])] = (G.stop, "neg")
    cells[col(p["r_now"])] = (G.dot, role)
    return cells


def ladder_labels(p: dict, width: int) -> str:
    width = max(12, width)
    lo, hi = -1.2, max(p["r_target"], 2.2) + 0.2
    line = [" "] * (width + 8)

    def put(v: float, text: str) -> None:
        x = int(round((max(lo, min(hi, v)) - lo) / (hi - lo) * (width - 1)))
        x = max(0, min(len(line) - len(text), x - len(text) // 2))
        if all(ch == " " for ch in line[max(0, x - 1):x + len(text) + 1]):
            line[x:x + len(text)] = list(text)

    put(p["r_target"], "TP")
    put(-1, "SL")
    put(0, "BE")
    put(1, "1R")
    if abs(p["r_target"] - 2) > 0.3:
        put(2, "2R")
    return "".join(line).rstrip()


def md_plain(text: str) -> List[str]:
    """Markdown → terminal lines (tables kept as-is, emphasis markers removed)."""
    out = []
    for line in text.splitlines():
        if line.startswith("|---") or line.startswith("|:--"):
            continue
        out.append(line.replace("**", "").replace("`", ""))
    return out


# --- plain status (no curses) ------------------------------------------------------------------

class _Ansi:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


def print_status(snap: dict, out=None) -> None:
    out = out or sys.stdout
    color = _Ansi(out.isatty() and not os.environ.get("NO_COLOR"))
    G = Glyphs(_utf8())
    POS, NEG, DIM, BOLD = "32", "31", "2", "1"

    def sgn(text: str, v: float) -> str:
        return color(text, POS if v > 0 else NEG if v < 0 else "0")

    m, a, g, st, td = snap["meta"], snap["account"], snap["gates"], snap["stats"], snap["today"]
    print(color(f"AI TRADING TERMINAL · {m['engine']} · {m['exchange']} · {m['mode']}", BOLD)
          + color(f" · data {tu.fmt(m['generated_at'])} MYT", DIM), file=out)
    win = pct(st["win_rate"], 1, False) if st["win_rate"] is not None else G.dash
    pf = f"{st['profit_factor']:.2f}" if st["profit_factor"] is not None else G.dash
    print(f"Equity {color(usd(a['equity']), BOLD)} ({sgn(usd(a['net_total'], True), a['net_total'])} since start) · "
          f"today {sgn(usd(td['net'], True), td['net'])} ({rmult(td['r'])}, {td['wins']}W/{td['losses']}L) · "
          f"{st['trades']} trades · win {win} · exp {rmult(st['expectancy_r'])} · PF {pf}", file=out)
    entries = color("ALLOWED", POS) if g["entries_allowed"] else color("HALTED · " + g["reason"], NEG)
    print(f"Gates: entries {entries} · loss stop {color('HIT', NEG) if g['loss_stop'] else 'ARMED'} "
          f"{usd(g['realized'], True)}/−${g['max_loss']:.0f} · losses {g['losses']}/{g['max_losses']} · PROTECT "
          f"{color('ON', POS) if g['protect'] else 'off'} · open {g['open']}/{g['max_open']} (L{g['longs']} S{g['shorts']})",
          file=out)
    print(file=out)
    print(color("OPEN POSITIONS", BOLD), file=out)
    if not snap["positions"]:
        print(color("  flat", DIM), file=out)
    for p in snap["positions"]:
        bar = "".join(color(ch, {"pos": POS, "neg": NEG, "dim": DIM, "ink2": "0"}[role]) if ch.strip() else ch
                      for ch, role in ladder_cells(p, 34, G))
        print(f"  {p['symbol']:<12} {p['direction'].upper():<5} {p['tag']:<16} {sgn(rmult(p['r_now']), p['r_now']):>8}  "
              f"{bar}  stop {px(p['stop'])} ({p['stop_kind']}) · age {p['age_h']:.1f}h", file=out)
    print(file=out)
    sig = snap.get("signals") or {}
    listed = (sig.get("signals") or []) + (sig.get("watch") or [])
    print(color("SIGNALS", BOLD) + color(f"  scan {tu.fmt(sig.get('generated_at'))} MYT · trade enters only these" if sig else
                                          "  no scan yet", DIM), file=out)
    for s in listed[:12]:
        print(f"  {s['symbol']:<12} {s['direction'].upper():<5} {s['tag']:<18} {s['status']:<8} RR {s['rr']:>5.2f}  "
              f"entry {px(s['entry'])} · SL {px(s['stop'])} · TP {px(s['target'])}", file=out)
    if sig and not listed:
        print(color("  none right now", DIM), file=out)
    print(file=out)
    sc = snap["schedule"]
    now = m["generated_at"]
    nxt = " · ".join(f"{sc['labels'].get(j, j)} {tu.hm(sc['next'][j])} ({tu.until(sc['next'][j], now)})"
                     for j in ("manage", "trade", "cycle", "report"))
    print(color("NEXT  ", BOLD) + nxt, file=out)
    print(color("DEMO — live orders stay disabled until the signal proves positive.", DIM), file=out)


# --- curses UI ---------------------------------------------------------------------------------

class App:
    def __init__(self, ctx, run_daemon: bool, daemon_fn: Callable, job_fn: Callable):
        self.ctx, self.run_daemon, self.daemon_fn, self.job_fn = ctx, run_daemon, daemon_fn, job_fn
        self.snap: Optional[dict] = None
        self.tab = 0
        self.sel = [0] * len(TABS)
        self.scroll = [0] * len(TABS)
        self.msg = "press ? for help"
        self.busy: Optional[str] = None
        self.last_refresh = 0.0
        self.G = Glyphs(_utf8())
        self.spin_i = 0
        self.help = False

    # -- data --
    def refresh(self) -> None:
        try:
            self.snap = dashboard.snapshot(self.ctx)
        except Exception as exc:  # keep showing the last good snapshot
            self.msg = f"snapshot failed: {exc}"
        self.last_refresh = time.time()

    def run_job(self, name: str) -> None:
        if self.busy:
            self.msg = f"busy: {self.busy} is still running"
            return
        self.busy = name
        self.msg = f"running {name}…"

        def work():
            try:
                ok = self.job_fn(self.ctx, name) if name != "dashboard" else bool(dashboard.write(self.ctx))
                self.msg = f"{name}: {'done' if ok else 'FAILED — see the Log tab'}"
            except Exception as exc:
                self.msg = f"{name} failed: {exc}"
            finally:
                self.busy = None
                self.refresh()

        threading.Thread(target=work, daemon=True, name=f"job-{name}").start()

    # -- drawing primitives --
    def put(self, y: int, x: int, text: str, attr: int = 0, maxw: Optional[int] = None) -> None:
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x >= W:
            return
        width = W - x if maxw is None else min(maxw, W - x)
        if y == H - 1:
            width = min(width, W - x - 1)
        if width <= 0:
            return
        try:
            self.scr.addnstr(y, x, text, width, attr)
        except curses.error:
            pass

    def c(self, role: str) -> int:
        return self.colors.get(role, 0)

    def setup_colors(self) -> None:
        self.colors = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "rev": curses.A_REVERSE}
        if not curses.has_colors():
            self.colors.update(pos=curses.A_BOLD, neg=curses.A_BOLD, acc=curses.A_BOLD, warn=0, ink2=0, zone=curses.A_REVERSE)
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        pairs = {"pos": curses.COLOR_GREEN, "neg": curses.COLOR_RED, "acc": curses.COLOR_BLUE,
                 "warn": curses.COLOR_YELLOW, "ink2": curses.COLOR_WHITE, "cyan": curses.COLOR_CYAN}
        for i, (name, fg) in enumerate(pairs.items(), start=1):
            curses.init_pair(i, fg, bg)
            self.colors[name] = curses.color_pair(i)
        curses.init_pair(10, curses.COLOR_WHITE, curses.COLOR_BLUE)
        self.colors["zone"] = curses.color_pair(10)
        curses.init_pair(11, curses.COLOR_BLACK, curses.COLOR_CYAN)
        self.colors["tabsel"] = curses.color_pair(11) | curses.A_BOLD

    def sgn(self, v: Optional[float]) -> int:
        if v is None:
            return 0
        return self.c("pos") if v > 0 else self.c("neg") if v < 0 else 0

    # -- frame --
    def draw(self) -> None:
        self.scr.erase()
        H, W = self.scr.getmaxyx()
        if H < 16 or W < 70:
            self.put(0, 0, f"Terminal too small ({W}×{H}) — need at least 70×16.", self.c("warn"))
            self.scr.refresh()
            return
        s = self.snap
        if s is None:
            self.put(0, 0, "loading…")
            self.scr.refresh()
            return
        m, a, g, td, st = s["meta"], s["account"], s["gates"], s["today"], s["stats"]
        now_txt = tu.fmt(tu.now_ms(), "%Y-%m-%d %H:%M:%S") + " MYT"
        self.put(0, 0, " AI TRADING TERMINAL ", self.c("rev") | curses.A_BOLD)
        self.put(0, 22, f" {m['engine']} · {m['exchange']} ", self.c("dim"))
        mode = f" {m['mode']} "
        self.put(0, W - len(now_txt) - len(mode) - 3, mode, (self.c("neg") if m.get("live_orders") else self.c("acc")) | curses.A_BOLD)
        self.put(0, W - len(now_txt) - 1, now_txt, self.c("dim"))
        x = 1
        self.put(1, x, "Equity ", self.c("dim")); x += 7
        self.put(1, x, usd(a["equity"]), curses.A_BOLD); x += len(usd(a["equity"])) + 1
        txt = f"({usd(a['net_total'], True)})"
        self.put(1, x, txt, self.sgn(a["net_total"])); x += len(txt) + 3
        self.put(1, x, "Today ", self.c("dim")); x += 6
        txt = f"{usd(td['net'], True)} {rmult(td['r'])} {td['wins']}W/{td['losses']}L"
        self.put(1, x, txt, self.sgn(td["net"])); x += len(txt) + 3
        wr = pct(st["win_rate"], 1, False) if st["win_rate"] is not None else "—"
        pf = f"{st['profit_factor']:.2f}" if st["profit_factor"] is not None else "—"
        self.put(1, x, f"Win {wr} · Exp {rmult(st['expectancy_r'])} · PF {pf} · {st['trades']} trades", self.c("ink2"))
        x = 1
        ent = ("✓ ENTRIES ALLOWED" if self.G.ok == "✓" else "ENTRIES ALLOWED") if g["entries_allowed"] else "⛔ HALTED: " + g["reason"]
        self.put(2, x, ent, self.c("pos") if g["entries_allowed"] else self.c("neg") | curses.A_BOLD)
        x += len(ent) + 3
        gate = (f"Loss stop {'HIT' if g['loss_stop'] else 'ARMED'} {usd(min(0, g['realized']), True)}/−${g['max_loss']:.0f} · "
                f"losses {g['losses']}/{g['max_losses']} · PROTECT {'ON' if g['protect'] else 'off'} "
                f"{usd(max(0, g['realized']), True)}/+${g['target']:.0f} · open {g['open']}/{g['max_open']} "
                f"(L{g['longs']}/{g['corr_max']} S{g['shorts']}/{g['corr_max']})")
        self.put(2, x, gate, self.c("ink2"))
        # tabs
        x = 0
        for i, name in enumerate(TABS):
            label = f" {i + 1} {name} "
            self.put(4, x, label, self.c("tabsel") if i == self.tab else self.c("dim"))
            x += len(label) + 1
        self.put(5, 0, self.G.h * W, self.c("dim"))
        body_top, body_h = 6, H - 8
        [self.tab_overview, self.tab_positions, self.tab_signals, self.tab_scanner, self.tab_sweeps,
         self.tab_failure, self.tab_trades, self.tab_journal, self.tab_log][self.tab](body_top, body_h, W)
        # footer
        self.put(H - 2, 0, self.G.h * W, self.c("dim"))
        keys = "1-9 tabs  ↑↓ move  s cycle  g signals  t trade  m manage  p report  d dashboard  r refresh  ? help  q quit"
        if self.G.ok != "✓":
            keys = keys.replace("↑↓", "up/dn")
        status = self.msg
        if self.busy:
            self.spin_i = (self.spin_i + 1) % len(self.G.spin)
            status = f"{self.G.spin[self.spin_i]} {self.msg}"
        daemon = "  · daemon ON" if self.run_daemon else ""
        self.put(H - 1, 0, (status + daemon)[:max(0, W - len(keys) - 3)], self.c("warn") if self.busy else self.c("ink2"))
        self.put(H - 1, max(0, W - len(keys) - 1), keys, self.c("dim"))
        if self.help:
            self.draw_help(H, W)
        self.scr.refresh()

    def draw_help(self, H: int, W: int) -> None:
        lines = [
            "AI Trading Terminal — keys", "",
            "1-9 / ←→ / Tab   switch tab",
            "↑↓ j k PgUp PgDn  move selection / scroll",
            "s  scan cycle: scan + Module A + routine 2 → signals → trade",
            "g  framework-signal scan only (writes signals_live.json)",
            "t  trade: enter listed coins that re-validate right now",
            "m  manage: R-ladder + 48h time-stop",
            "p  daily report + PDF (latest 07:00/19:00 slot)",
            "d  write dashboard/index.html",
            "r  refresh   ?  close help   q  quit",
            "",
            "Mode " + self.snap["meta"]["mode"] + " · notifications: " + self.snap["meta"]["notify"],
            "DEMO: live orders stay disabled until the signal proves positive.",
        ]
        w = min(W - 4, max(len(x) for x in lines) + 4)
        h = len(lines) + 2
        y0, x0 = max(0, (H - h) // 2), max(0, (W - w) // 2)
        for i in range(h):
            self.put(y0 + i, x0, " " * w, self.c("rev"))
        for i, line in enumerate(lines):
            self.put(y0 + 1 + i, x0 + 2, line, self.c("rev") | (curses.A_BOLD if i == 0 else 0))

    # -- shared blocks --
    def table(self, y: int, x: int, h: int, w: int, headers: Sequence[Tuple[str, int, str]], rows: List[list],
              tab_i: Optional[int] = None, colors: Optional[List[List[int]]] = None) -> None:
        """headers: (title, width, align). Scrolls with self.scroll[tab_i]; highlights self.sel[tab_i]."""
        cx = x
        for title, cw, al in headers:
            self.put(y, cx, (title.rjust(cw) if al == "r" else title.ljust(cw))[:cw], self.c("dim") | curses.A_BOLD)
            cx += cw + 1
        view = h - 1
        start = 0
        if tab_i is not None and rows:
            self.sel[tab_i] = max(0, min(self.sel[tab_i], len(rows) - 1))
            if self.sel[tab_i] < self.scroll[tab_i]:
                self.scroll[tab_i] = self.sel[tab_i]
            if self.sel[tab_i] >= self.scroll[tab_i] + view:
                self.scroll[tab_i] = self.sel[tab_i] - view + 1
            start = self.scroll[tab_i]
        if not rows:
            self.put(y + 1, x, "nothing yet", self.c("dim"))
        for r, row in enumerate(rows[start:start + view]):
            idx = start + r
            cx = x
            selected = tab_i is not None and idx == self.sel[tab_i]
            if selected:
                self.put(y + 1 + r, x, " " * w, self.c("rev"))
            for ci, ((title, cw, al), val) in enumerate(zip(headers, row)):
                text = str(val)
                text = (text.rjust(cw) if al == "r" else text.ljust(cw))[:cw]
                attr = (colors[idx][ci] if colors else 0)
                self.put(y + 1 + r, cx, text, attr | (self.c("rev") if selected else 0))
                cx += cw + 1

    def section(self, y: int, x: int, title: str, w: int, note: str = "") -> None:
        self.put(y, x, title.upper(), curses.A_BOLD)
        if note:
            self.put(y, x + len(title) + 2, note[:max(0, w - len(title) - 2)], self.c("dim"))

    def ladder(self, y: int, x: int, p: dict, w: int) -> None:
        for i, (ch, role) in enumerate(ladder_cells(p, w, self.G)):
            self.put(y, x + i, ch, self.c(role))
        self.put(y + 1, x, ladder_labels(p, w), self.c("dim"))

    # -- tabs --
    def tab_overview(self, top: int, h: int, W: int) -> None:
        s = self.snap
        left_w = min(W - 2, max(60, W * 3 // 5))
        right_x = left_w + 3
        right_w = W - right_x - 1
        self.section(top, 1, "Open positions", left_w, f"{len(s['positions'])}/{s['rules']['max_open']}")
        y = top + 1
        if not s["positions"]:
            self.put(y, 1, "flat — no open positions", self.c("dim"))
            y += 1
        for p in s["positions"]:
            if y > top + h - 8:
                break
            head = f"{p['symbol']:<12} {p['direction'].upper():<5} {p['tag']:<17}"
            self.put(y, 1, head, curses.A_BOLD)
            self.put(y, 1 + len(head), f"{rmult(p['r_now']):>7} {usd(p['unreal'], True):>8}", self.sgn(p["r_now"]))
            self.ladder(y + 1, 3, p, max(20, left_w - 6))
            y += 3
        sig = s.get("signals") or {}
        listed = (sig.get("signals") or []) + (sig.get("watch") or [])
        if right_w >= 30:
            self.section(top, right_x, "Signals", right_w, f"scan {tu.hm(sig.get('generated_at'))}" if sig else "no scan yet")
            rows = [[x["symbol"], x["direction"][0].upper(), x["tag"], x["status"], f"{x['rr']:.2f}"] for x in listed[:10]]
            cols = [[curses.A_BOLD, 0, 0, self.c("acc") if x["status"] == "signal" else self.c("ink2"), 0] for x in listed[:10]]
            self.table(top + 1, right_x, 11, right_w, [("SYMBOL", 11, "l"), ("D", 1, "l"), ("SETUP", 16, "l"),
                                                         ("STATUS", 7, "l"), ("RR", 5, "r")], rows, colors=cols)
            f = sig.get("funnel") or {}
            yy = top + 13
            self.section(yy, right_x, "Funnel", right_w, f"{f.get('universe', 0)} coins")
            maxv = max(1, f.get("universe", 1))
            for i, (k, lab) in enumerate((("context", "context"), ("trigger", "raid/CISD"), ("shift", "MSS"),
                                          ("armed", "POI armed"), ("in-zone", "in-zone"), ("signal", "signal"))):
                v = f.get(k, 0)
                bw = int((right_w - 18) * v / maxv)
                self.put(yy + 1 + i, right_x, f"{lab:<10}", self.c("ink2"))
                self.put(yy + 1 + i, right_x + 11, self.G.fill * bw, self.c("acc"))
                self.put(yy + 1 + i, right_x + 12 + bw, str(v), curses.A_BOLD)
        # events at the bottom
        ey = top + h - 6
        self.section(ey, 1, "Activity", W - 2, "latest events")
        evs = [e for e in s["events"] if not (e["kind"] == "trade" and not e.get("entered"))][-5:]
        for i, e in enumerate(reversed(evs)):
            self.put(ey + 1 + i, 1, f"{tu.hm(e['ts'])}  {event_line(e)}"[:W - 2],
                     self.sgn(e.get("net_pnl", e.get("pnl"))) if e["kind"] == "exit" else self.c("neg") if e["kind"] in ("error", "gate") else 0)

    def tab_positions(self, top: int, h: int, W: int) -> None:
        s = self.snap
        ps = s["positions"]
        self.section(top, 1, "Open positions", W, "R-ladder: −1R stop → BE at +1R (50% off) → trail 1% → lock +1R at +2R → DOL target · 48h time-stop")
        if not ps:
            self.put(top + 2, 1, "flat — the trade loop enters only coins listed in signals_live.json", self.c("dim"))
            return
        self.sel[1] = max(0, min(self.sel[1], len(ps) - 1))
        y = top + 2
        for i, p in enumerate(ps):
            if y > top + h - 4:
                break
            mark = "▶ " if (i == self.sel[1] and self.G.ok == "✓") else "> " if i == self.sel[1] else "  "
            self.put(y, 0, mark, self.c("acc"))
            self.put(y, 2, f"{p['symbol']:<12} {p['direction'].upper():<5} {p['tag']:<17} {p['tier']} {p['struct_tf']}→{p['entry_tf']}", curses.A_BOLD)
            self.put(y, W - 22, f"{rmult(p['r_now']):>8} {usd(p['unreal'], True):>10}", self.sgn(p["r_now"]))
            self.ladder(y + 1, 4, p, W - 10)
            now = s["meta"]["generated_at"]
            self.put(y + 3, 4, f"entry {px(p['entry'])} · stop {px(p['stop'])} ({p['stop_kind']}) · target {px(p['target'])} · "
                               f"last {px(p['price'])} · qty {p['qty_open']:.6g} · 1R {usd(p['risk_usd'])} · age {p['age_h']:.1f}h · "
                               f"time-stop {tu.until(p['time_stop_at'], now)}", self.c("ink2"))
            y += 5
        p = ps[self.sel[1]]
        checklist = (p.get("setup") or {}).get("checklist") or []
        if checklist and y < top + h - 3:
            self.section(y, 1, f"Why {p['symbol']} was entered", W)
            for i, stp in enumerate(checklist):
                if y + 1 + i >= top + h:
                    break
                self.put(y + 1 + i, 2, f"{self.G.ok if stp['ok'] else self.G.wait} {stp['step']} {stp['name']}: {stp['detail']}",
                         self.c("pos") if stp["ok"] else self.c("dim"))

    def tab_signals(self, top: int, h: int, W: int) -> None:
        s = self.snap
        sig = s.get("signals") or {}
        items = (sig.get("signals") or []) + (sig.get("watch") or []) + (s.get("armed") or [])
        list_w = 44
        self.section(top, 1, "Setups", list_w, f"{len(sig.get('signals') or [])} signal · {len(sig.get('watch') or [])} in-zone · {len(s.get('armed') or [])} armed")
        rows = [[x["symbol"], x["direction"][0].upper(), x["tag"], x["status"]] for x in items]
        cols = [[curses.A_BOLD, 0, 0, self.c("acc") if x["status"] == "signal" else self.c("ink2") if x["status"] == "in-zone" else self.c("dim")] for x in items]
        self.table(top + 1, 1, h - 1, list_w, [("SYMBOL", 11, "l"), ("D", 1, "l"), ("SETUP", 17, "l"), ("STATUS", 8, "l")],
                   rows, tab_i=2, colors=cols)
        if not items:
            return
        x = items[max(0, min(self.sel[2], len(items) - 1))]
        dx = list_w + 3
        dw = W - dx - 1
        if dw < 30:
            return
        y = top
        self.put(y, dx, f"{x['symbol']} {x['direction'].upper()} · {x['tag']} · {x['tier']} {x['struct_tf']}→{x['entry_tf']} · {x['status']}", curses.A_BOLD)
        y += 1
        plan = (f"entry {px(x['entry'])} · SL {px(x['stop'])} ({x['risk_pct']:.2%}) · TP {px(x['target'])} · "
                f"{x['rr']:.2f}R · DOL {px(x['dol'])} ({x['dol_kind']})")
        self.put(y, dx, plan, self.c("ink2"))
        y += 2
        for stp in x.get("checklist") or []:
            self.put(y, dx, f"{self.G.ok if stp['ok'] else self.G.wait} {stp['step']} {stp['name']}", self.c("pos") if stp["ok"] else self.c("warn"))
            self.put(y + 1, dx + 4, stp["detail"][:dw - 4], self.c("ink2"))
            y += 2
        if x.get("chart") and top + h - y >= 8:
            self.candles(y + 1, dx, top + h - y - 1, dw, x)
        elif not x.get("chart"):
            self.put(y + 1, dx, "chart appears once price reaches the POI (in-zone)", self.c("dim"))

    def candles(self, y0: int, x0: int, h: int, w: int, s: dict) -> None:
        c = s["chart"]
        n = len(c["t"])
        label_w = 18
        cols = max(5, min(n, w - label_w - 1))
        start = n - cols
        levels = [s["stop"], s["target"], s["entry"], s["range"]["eq"], s["poi"]["lo"], s["poi"]["hi"]]
        lo = min(min(c["l"][start:]), *levels)
        hi = max(max(c["h"][start:]), *levels)
        span = (hi - lo) or 1.0

        def row(p: float) -> int:
            return y0 + int(round((hi - p) / span * (h - 1)))

        poi_i = next((i for i in range(start, n) if c["t"][i] >= s["poi"]["ts"]), start)
        for r in range(row(s["poi"]["hi"]), row(s["poi"]["lo"]) + 1):
            self.put(r, x0 + (poi_i - start), " " * (cols - (poi_i - start)), self.c("zone"))
        lines = [(s["target"], "TP", "pos"), (s["entry"], "entry" if s["status"] == "signal" else "plan", "ink2"),
                 (s["stop"], "SL", "neg"), (s["range"]["eq"], "EQ", "dim")]
        for p, _, role in lines:
            self.put(row(p), x0, self.G.h * cols, self.c(role))
        for k in range(cols):
            i = start + k
            up = c["c"][i] >= c["o"][i]
            top, bot = row(max(c["o"][i], c["c"][i])), row(min(c["o"][i], c["c"][i]))
            for r in range(row(c["h"][i]), row(c["l"][i]) + 1):
                ch = (self.G.hollow if up else self.G.body) if top <= r <= bot else self.G.v
                self.put(r, x0 + k, ch, self.c("pos") if up else self.c("neg"))
        placed = sorted(((row(p), f"{lab} {px(p)}", role) for p, lab, role in lines + [(s["poi"]["hi"], s["poi"]["kind"], "acc")]))
        last = -10
        for r, text, role in placed:
            r = max(r, last + 1)
            self.put(r, x0 + cols + 1, text[:label_w], self.c(role))
            last = r

    def tab_scanner(self, top: int, h: int, W: int) -> None:
        sc = self.snap.get("scan") or {}
        if not sc:
            self.put(top, 1, "no scan yet — press s", self.c("dim"))
            return
        self.section(top, 1, "Routine 1", W, f"scan {tu.fmt(sc['generated_at'])} MYT · {sc['universe']} crypto perps · "
                     f"{len(sc['excluded'])} TradFi excluded · {sc['liquid']} with 24h vol ≥ $1M")
        hdr = [("SYMBOL", 11, "l"), ("LAST", 12, "r"), ("24H", 8, "r"), ("VOL", 7, "r"), ("FUND", 9, "r"), ("PDH", 9, "l"), ("PDL", 9, "l")]
        tw = sum(x[1] + 1 for x in hdr)

        def rows(items):
            return ([[r["symbol"], px(r["last"]), pct(r["change"]), compact(r["volume"]), pct(r["funding"], 4), r["pdh_state"], r["pdl_state"]] for r in items],
                    [[curses.A_BOLD, 0, self.sgn(r["change"]), 0, 0, self.c("acc") if r["pdh_state"] != "respected" else self.c("dim"),
                      self.c("acc") if r["pdl_state"] != "respected" else self.c("dim")] for r in items])

        if W >= 2 * tw + 4:
            for i, (name, key) in enumerate((("Top movers", "movers"), ("Top losers", "losers"))):
                x = 1 + i * (tw + 3)
                self.put(top + 2, x, name, curses.A_BOLD)
                r, cl = rows(sc[key])
                self.table(top + 3, x, h - 3, tw, hdr, r, colors=cl)
        else:
            half = (h - 4) // 2
            for i, (name, key) in enumerate((("Top movers", "movers"), ("Top losers", "losers"))):
                y = top + 2 + i * (half + 1)
                self.put(y, 1, name, curses.A_BOLD)
                r, cl = rows(sc[key])
                self.table(y + 1, 1, half, tw, hdr, r, colors=cl)

    def tab_sweeps(self, top: int, h: int, W: int) -> None:
        sw = self.snap.get("sweeps") or {}
        if not sw:
            self.put(top, 1, "no scan yet — press s", self.c("dim"))
            return
        self.section(top, 1, "Module A · liquidity sweeps", W, f"{sw.get('total', len(sw['sweeps']))} sweeps · "
                     f"{len(sw['promoted'])} promoted · mode {sw['mode']}")
        self.put(top + 1, 1, "OI —  CVD —  (no historical OI/CVD on Bitget public data: OI gate off) · promoted = volume ∧ displacement ∧ time decay",
                 self.c("dim"))
        rows = [[x["symbol"], x["tf"], x["pool"], px(x["level"]), px(x["extreme"]), f"{x['vol_ratio']:.1f}", f"{x['disp_ratio']:.1f}",
                 str(x["age_bars"]), f"{x['score']:.0f}", "yes" if x["promoted"] else "—"] for x in sw["sweeps"]]
        cols = [[curses.A_BOLD, 0, 0, 0, 0, self.c("pos") if x["clauses"]["volume"] else self.c("dim"),
                 self.c("pos") if x["clauses"]["displacement"] else self.c("dim"), 0, 0,
                 self.c("acc") if x["promoted"] else self.c("dim")] for x in sw["sweeps"]]
        self.table(top + 3, 1, h - 3, W - 2, [("SYMBOL", 11, "l"), ("TF", 3, "l"), ("POOL", 9, "l"), ("LEVEL", 12, "r"),
                                              ("EXTREME", 12, "r"), ("VOL×", 5, "r"), ("DISP×", 5, "r"), ("AGE", 4, "r"),
                                              ("SCORE", 5, "r"), ("PROMOTED", 8, "l")], rows, tab_i=4, colors=cols)

    def tab_failure(self, top: int, h: int, W: int) -> None:
        df = self.snap.get("daily_failure") or {}
        if not df:
            self.put(top, 1, "no scan yet — press s", self.c("dim"))
            return
        self.section(top, 1, "Routine 2 · daily failure", W, "research watchlist only — no backtested edge, never gates entries")
        rows = [[x["symbol"], x["kind"], px(x["prior_high"]), px(x["prior_low"]), px(x["high"]), px(x["low"]), px(x["close"]), px(x["last"])]
                for x in df["items"]]
        self.table(top + 2, 1, h - 2, W - 2, [("SYMBOL", 11, "l"), ("FAILED", 8, "l"), ("PRIOR HIGH", 12, "r"), ("PRIOR LOW", 12, "r"),
                                              ("DAY HIGH", 12, "r"), ("DAY LOW", 12, "r"), ("CLOSE", 12, "r"), ("LAST", 12, "r")],
                   rows, tab_i=5)

    def tab_trades(self, top: int, h: int, W: int) -> None:
        s = self.snap
        st = s["stats"]
        wr = pct(st["win_rate"], 1, False) if st["win_rate"] is not None else "—"
        pf = f"{st['profit_factor']:.2f}" if st["profit_factor"] is not None else "—"
        self.put(top, 1, f"{st['trades']} trades · win {wr} · expectancy {rmult(st['expectancy_r'])} · PF {pf} · net {usd(st['net'], True)} · "
                         f"fees {usd(st['fees'])} · funding {usd(st['funding'], True)} · max DD {usd(st['max_drawdown'], True)}", curses.A_BOLD)
        fam = s["by"]["family"]
        line = "  ".join(f"{k}-*: {v['trades']} tr · win {pct(v['win_rate'], 0, False) if v['win_rate'] is not None else '—'} · {rmult(v['expectancy_r'])}"
                         for k, v in fam.items()) or "no closed trades yet"
        self.put(top + 1, 1, "by family  " + line, self.c("ink2"))
        trades = list(reversed(s["trades"]))
        rows = [[tu.fmt(t["closed_at"], "%m-%d %H:%M"), t["symbol"], t["direction"][0].upper(), t["tag"], t["exit_reason"],
                 f"{t['hold_h']:.1f}h", rmult(t["r_mult"]), usd(t["net_pnl"], True)] for t in trades]
        cols = [[0, curses.A_BOLD, 0, 0, 0, 0, self.sgn(t["r_mult"]), self.sgn(t["net_pnl"])] for t in trades]
        self.table(top + 3, 1, h - 3, W - 2, [("CLOSED", 11, "l"), ("SYMBOL", 11, "l"), ("D", 1, "l"), ("SETUP", 17, "l"),
                                              ("EXIT", 9, "l"), ("HOLD", 6, "r"), ("R", 7, "r"), ("NET", 9, "r")], rows, tab_i=6, colors=cols)

    def text_view(self, top: int, h: int, W: int, lines: List[str], tab_i: int, follow: bool = False) -> None:
        view = h
        maxs = max(0, len(lines) - view)
        if follow and self.scroll[tab_i] < 0:
            self.scroll[tab_i] = maxs
        self.scroll[tab_i] = max(0, min(self.scroll[tab_i], maxs))
        for i, line in enumerate(lines[self.scroll[tab_i]:self.scroll[tab_i] + view]):
            attr = curses.A_BOLD if line.startswith("#") else self.c("neg") if " ERROR " in line else self.c("warn") if " WARNING " in line else 0
            self.put(top + i, 1, line[:W - 2], attr)

    def tab_journal(self, top: int, h: int, W: int) -> None:
        j = self.snap.get("journal") or {}
        days = [d for d in j.get("days", []) if d in j.get("entries", {})]
        if not days:
            self.put(top, 1, "no journal yet", self.c("dim"))
            return
        self.text_view(top, h, W, md_plain(j["entries"][days[0]]), 7)

    def tab_log(self, top: int, h: int, W: int) -> None:
        path = self.ctx.store.paths.state / "bot.log"
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()[-2000:]
        except OSError:
            lines = ["no log yet"]
        self.text_view(top, h, W, lines, 8, follow=True)

    # -- input --
    def key(self, ch: int) -> bool:
        if ch in (ord("q"), ord("Q")):
            return False
        if self.help:
            self.help = False
            return True
        if ch == ord("?"):
            self.help = True
        elif ord("1") <= ch <= ord("9"):
            self.tab = ch - ord("1")
        elif ch in (curses.KEY_RIGHT, 9, ord("l")):
            self.tab = (self.tab + 1) % len(TABS)
        elif ch in (curses.KEY_LEFT, curses.KEY_BTAB, ord("h")):
            self.tab = (self.tab - 1) % len(TABS)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif ch in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif ch == curses.KEY_NPAGE:
            self.move(10)
        elif ch == curses.KEY_PPAGE:
            self.move(-10)
        elif ch == curses.KEY_HOME:
            self.move(-10 ** 6)
        elif ch == curses.KEY_END:
            self.move(10 ** 6)
        elif ch == ord("r"):
            self.refresh()
            self.msg = "refreshed"
        elif ch == ord("d"):
            self.run_job("dashboard")
        elif 0 <= ch < 256 and chr(ch) in JOB_KEYS:
            self.run_job(JOB_KEYS[chr(ch)])
        elif ch == curses.KEY_RESIZE:
            pass
        return True

    def move(self, d: int) -> None:
        if self.tab in (7, 8):
            self.scroll[self.tab] = max(0, self.scroll[self.tab] + d)
        else:
            self.sel[self.tab] = max(0, self.sel[self.tab] + d)

    def main(self, stdscr) -> int:
        self.scr = stdscr
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.setup_colors()
        stdscr.keypad(True)
        stdscr.timeout(250)
        self.scroll[8] = -1
        self.refresh()
        if self.run_daemon:
            threading.Thread(target=self.daemon_fn, args=(self.ctx,), daemon=True, name="daemon").start()
            self.msg = "daemon started — jobs run on the MYT schedule"
        while True:
            if time.time() - self.last_refresh > 5:
                self.refresh()
            self.draw()
            ch = stdscr.getch()
            if ch != -1 and not self.key(ch):
                break
        self.ctx.stop.set()
        return 0


def event_line(e: dict) -> str:
    k = e["kind"]
    if k == "entry":
        return f"ENTRY {e['symbol']} {e['direction']} {e['tag']} @ {px(e['entry'])} · SL {px(e['stop'])} · TP {px(e['target'])}"
    if k == "exit":
        return f"EXIT {e['symbol']} {e.get('reason')} @ {px(e.get('price'))} · {rmult(e.get('r_trade', e.get('r')))} · {usd(e.get('net_pnl', e.get('pnl')), True)}"
    if k == "tp1":
        return f"TP1 {e['symbol']} 50% off @ {px(e.get('price'))} · stop → breakeven"
    if k == "lock":
        return f"LOCK {e['symbol']} +2R printed · stop ≥ +1R"
    if k == "scan":
        return f"scan · {e.get('universe')} coins · {e.get('sweeps')} sweeps ({e.get('promoted')} promoted) · {e.get('failures')} daily-failure"
    if k == "signals":
        return f"signals · {len(e.get('signals', []))} meet the framework · {len(e.get('in_zone', []))} in-zone · {e.get('armed')} armed"
    if k == "trade":
        return f"trade run · {e.get('checked')} re-validated · entered {', '.join(e.get('entered', [])) or 'none'}"
    if k == "report":
        return f"report · {e.get('label')}"
    if k == "gate":
        return f"entries halted · {e.get('reason')}"
    if k == "error":
        return f"ERROR in {e.get('job')}: {e.get('error')}"
    return k


def main(ctx, run_daemon: bool = False, daemon_fn: Optional[Callable] = None, job_fn: Optional[Callable] = None) -> int:
    locale.setlocale(locale.LC_ALL, "")
    if curses is None:
        print("curses is not available (on Windows: pip install windows-curses). Showing a one-shot status instead:\n")
        print_status(dashboard.snapshot(ctx))
        return 1
    if not sys.stdout.isatty():
        print_status(dashboard.snapshot(ctx))
        return 0
    app = App(ctx, run_daemon, daemon_fn, job_fn)
    return curses.wrapper(app.main)
