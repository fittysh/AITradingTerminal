"""End-to-end: fast-forward the whole bot on the synthetic market and check the framework's invariants."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import journal  # noqa: E402
import paper_bot as pb  # noqa: E402
import run_bot  # noqa: E402
import timeutil as tu  # noqa: E402
from store import Paths, Store  # noqa: E402

END = tu.parse_date("2026-09-24") + 21 * tu.HOUR + 7 * tu.MIN


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name) / "e2e"
        cls.report = run_bot.replay(2.0, "sim", str(cls.home), seed=11, end_ms=END)
        cls.store = Store(Paths(cls.home))
        cls.state = cls.store.load_state()
        cls.events = cls.store.events()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outputs_exist(self):
        st = self.home / "state"
        for name in ("state.json", "signals_live.json", "sweeps.json", "scan.json", "daily_failure.json",
                     "events.jsonl", "schedule.json"):
            self.assertTrue((st / name).is_file(), name)
        self.assertTrue(list((self.home / "reports").glob("*_0700.pdf")))
        self.assertTrue(list((self.home / "reports").glob("*_1900.md")))
        html = (self.home / "dashboard" / "index.html").read_text()
        blob = re.search(r'<script id="state" type="application/json">(.*?)</script>', html, re.S).group(1)
        snap = json.loads(blob.replace("<\\/", "</"))
        self.assertEqual(snap["meta"]["mode"], "REPLAY·PAPER")

    def test_journal_every_market_day(self):
        start = END - 2 * tu.DAY
        days = {tu.date_str(tu.market_day(t)[0]) for t in range(start, END, tu.HOUR)}
        have = {p.stem for p in (self.home / "journal").glob("*.md")}
        self.assertTrue(days <= have, days - have)

    def test_journal_follows_the_design(self):
        seen_rows = 0
        for md in (self.home / "journal").glob("*.md"):
            page = md.with_suffix(".html").read_text()
            for part in ("Crypto Trading Journal", "At a glance", "Total P&amp;L ($)", "Entries", "rows filled in",
                         "<th>Date</th><th>Ticker</th><th>Side</th><th>Size</th>"):
                if part.startswith("<th>") and "No trades closed" in page:
                    continue
                self.assertIn(part, page, md.stem)
            v = journal.day_view(self.store, self.state, END, "paper", tu.parse_date(md.stem) + 12 * tu.HOUR)
            start, end = tu.market_day(tu.parse_date(md.stem) + 12 * tu.HOUR)
            closed = [t for t in self.state["closed"] if start <= t["closed_at"] < end]
            self.assertEqual(v["entries"], len(closed))
            self.assertAlmostEqual(v["total_pnl"], sum(t["net_pnl"] for t in closed), places=9)
            self.assertEqual([r["ticker"] for r in v["rows"]],
                             [t["symbol"] for t in sorted(closed, key=lambda t: (t["opened_at"], t["closed_at"]), reverse=True)])
            for r, t in zip(v["rows"], sorted(closed, key=lambda t: (t["opened_at"], t["closed_at"]), reverse=True)):
                gross = sum(f["pnl"] for f in t["fills"] if f["kind"] in ("tp1", "exit"))
                self.assertAlmostEqual(t["dir"] * (r["exit"] - r["entry"]) * r["size"], gross, places=6,
                                       msg="exit is the size-weighted average of the exit fills")
            seen_rows += len(v["rows"])
        self.assertGreater(seen_rows, 0)

    def test_every_entry_was_listed_by_the_signal_scan(self):
        listed = None
        for e in self.events:
            if e["kind"] == "signals":
                listed = {s.split()[0] for s in e["signals"]} | {s.split()[0] for s in e["in_zone"]}
            elif e["kind"] == "entry":
                self.assertIsNotNone(listed)
                self.assertIn(e["symbol"], listed, "entry outside signals_live.json")

    def test_shell_limits_never_breached(self):
        open_pos = {}
        for e in self.events:
            if e["kind"] == "entry":
                open_pos[e["id"]] = e["direction"]
                self.assertLessEqual(len(open_pos), pb.MAX_OPEN_POSITIONS)
                self.assertLessEqual(sum(1 for d in open_pos.values() if d == e["direction"]), pb.CORR_MAX_PER_SIDE)
            elif e["kind"] == "exit":
                open_pos.pop(e["id"], None)
        self.assertEqual(set(open_pos), {p["id"] for p in self.state["positions"]})

    def test_accounting_identities(self):
        st = self.state
        self.assertGreater(len(st["closed"]), 0, "the replay should trade at least once")
        fills = sum(f["pnl"] - f["fee"] for p in st["positions"] + st["closed"] for f in p["fills"])
        self.assertAlmostEqual(pb.balance(st), st["account"]["start_balance"] + fills, places=9)
        for t in st["closed"]:
            self.assertAlmostEqual(t["gross_pnl"] - t["fees"] + t["funding"], t["net_pnl"], places=9)
            self.assertAlmostEqual(t["r_mult"], t["net_pnl"] / t["risk_usd"], places=9)
            self.assertLessEqual(t["risk_usd"], pb.RISK_PER_TRADE + 1e-9)
            self.assertIn(t["exit_reason"], ("stop", "breakeven", "trail", "lock", "target", "time"))
            self.assertLessEqual(t["hold_h"], pb.MAX_TRADE_AGE_HOURS + 0.25)
            self.assertGreaterEqual(t["r_mult"], -1.6, "a full stop costs 1R plus fees/slippage, never more")
        rep = self.report["stats"]
        self.assertEqual(rep["trades"], len(st["closed"]))

    def test_tags_follow_the_framework_families(self):
        for t in self.state["closed"] + self.state["positions"]:
            self.assertRegex(t["tag"], r"^(sweep|OB|unicorn)-T[12]-(raid|cisd)-(C2|C3)$")
            self.assertEqual(t["closure"], t["tag"][-2:])
            self.assertIn(t["entry_kind"], ("close", "fvg"))
            steps = t["setup"]["checklist"]
            self.assertEqual([x["step"] for x in steps], [1, 2, 3, 4, 5, 6, 7])
            self.assertTrue(all(x["ok"] for x in steps), "every entry passed all seven steps")


if __name__ == "__main__":
    unittest.main()
