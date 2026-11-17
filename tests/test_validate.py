"""validate.py: the independent audit passes a real replay and catches every kind of broken entry."""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import timeutil as tu  # noqa: E402
from exchange import tf_ms  # noqa: E402
import validate  # noqa: E402
from sim import SimClock, SimMarket  # noqa: E402
from store import Paths, Store  # noqa: E402

END = tu.parse_date("2026-09-24") + 21 * tu.HOUR + 7 * tu.MIN


class Audit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home = Path(cls.tmp.name) / "val"
        cls.res = validate.run(2.0, "sim", str(home), 11, None, 40, end_ms=END, quiet=True)
        store = Store(Paths(home))
        cls.state, cls.events = store.load_state(), store.events()
        start, end = cls.res["report"]["window"]
        cls.clock = SimClock(start)
        cls.md = SimMarket(cls.clock, seed=11, start_ms=start - 60 * tu.DAY, end_ms=end + tu.DAY)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def failed_rules(self, state, events=None):
        aud = validate.audit(self.md, self.clock, state, events if events is not None else self.events)
        return {f["rule"] for f in aud["failures"]}

    def mutate(self, fn, rule):
        """Break one entry with fn(position) and expect exactly that rule to be flagged."""
        st = copy.deepcopy(self.state)
        p = (st["closed"] + st["positions"])[0]
        fn(p)
        self.assertIn(rule, self.failed_rules(st))

    def test_the_replay_follows_every_rule(self):
        aud = self.res["audit"]
        self.assertGreater(aud["entries"], 0)
        self.assertEqual(aud["failures"], [])
        self.assertTrue(all(bad == 0 for _, bad in aud["rules"].values()))
        self.assertIn("Not answerable on synthetic data", self.res["text"])
        self.assertTrue(self.res["path"].is_file())

    def test_catches_a_wrong_closure(self):
        def flip(p):
            k = "C3" if p["closure"] == "C2" else "C2"
            p["closure"], p["tag"], p["setup"]["candle"]["kind"] = k, p["tag"][:-2] + k, k
        self.mutate(flip, "5 C2 / C3 closure")

    def test_catches_an_entry_candle_that_is_not_the_series_close(self):
        def older(p):
            p["setup"]["confirm"]["ts"] -= tu.HOUR
        self.mutate(older, "7 entry candle")

    def test_catches_a_wrong_series_level(self):
        def level(p):
            p["setup"]["series"]["level"] *= 1.01
        self.mutate(level, "7 entry candle")

    def test_catches_an_entry_outside_discount_or_premium(self):
        def chase(p):
            rg = p["setup"]["range"]
            p["setup"]["entry"] = rg["hi"] if p["direction"] == "long" else rg["lo"]
        self.mutate(chase, "4 discount / premium")

    def test_catches_a_zone_in_the_wrong_half(self):
        def zone(p):                        # move the zone into the other half of the dealing range
            rg, z = p["setup"]["range"], p["setup"]["poi"]
            z["lo"], z["hi"] = (rg["eq"], rg["hi"]) if p["direction"] == "long" else (rg["lo"], rg["eq"])
        self.mutate(zone, "6 zone")

    def test_catches_a_stop_inside_the_raid(self):
        def stop(p):
            p["initial_stop"] = p["setup"]["raid"]["extreme"]
        self.mutate(stop, "stop + target")

    def test_catches_oversized_risk(self):
        self.mutate(lambda p: p.update(risk_usd=3.0), "risk per trade")

    def test_catches_a_raid_that_was_not_reclaimed_in_time(self):
        def late(p):
            r = p["setup"]["raid"]
            r["confirm_ts"] = r["ts"] + 10 * tf_ms(p["struct_tf"])
        self.mutate(late, "2 trigger")

    def test_catches_an_unlisted_entry(self):
        events = [e for e in self.events if e["kind"] != "signals"]
        self.assertIn("listed by the signal scan", self.failed_rules(self.state, events))

    def test_catches_entries_past_the_daily_loss_stop(self):
        st = copy.deepcopy(self.state)
        first = min(st["closed"] + st["positions"], key=lambda p: p["opened_at"])
        later = [p for p in st["closed"] + st["positions"] if p["opened_at"] > first["opened_at"]
                 and tu.day_bounds(p["opened_at"])[0] == tu.day_bounds(first["opened_at"])[0]]
        if not later:
            self.skipTest("only one entry on the first day")
        first["fills"].append({"ts": first["opened_at"], "kind": "exit", "qty": 0, "price": 0, "pnl": -20.0, "fee": 0.0})
        self.assertIn("protection shell", self.failed_rules(st))

    def test_verdict_rule(self):
        ok, checks = validate.verdict({"trades": 40, "net": 12.0, "profit_factor": 1.35})
        self.assertTrue(ok)
        ok, checks = validate.verdict({"trades": 40, "net": 12.0, "profit_factor": 1.1})
        self.assertFalse(ok)
        self.assertEqual([c[1] for c in checks], [True, True, False])
        self.assertFalse(validate.verdict({"trades": 12, "net": 30.0, "profit_factor": 3.0})[0])


if __name__ == "__main__":
    unittest.main()
