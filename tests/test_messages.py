"""The signal message tells the reader what to check on the chart for every signal."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notify  # noqa: E402
import run_bot  # noqa: E402
import strategy as st  # noqa: E402
from scenarios import getter, long_scenario  # noqa: E402
from store import Paths, Store  # noqa: E402


class SignalMessage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        S, E, D, now, price = long_scenario()
        self.setup = st.evaluate("TESTUSDT", getter(S, E, D), price, now).setup.to_dict()
        self.assertEqual(self.setup["status"], "signal")

        class Ctx:
            pass
        self.ctx = Ctx()
        self.ctx.store = Store(Paths(Path(self.tmp.name)))
        self.ctx.label = "PAPER"
        self.payload = {"generated_at": now, "funnel": {"universe": 1, "signal": 1}, "signals": [self.setup],
                        "watch": [dict(self.setup, status="in-zone")]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_telegram_explains_each_signal_with_candle_times(self):
        subject, tg, _ = run_bot.fmt_signals(self.ctx, self.payload, 3)
        text = re.sub(r"<[^>]+>", "", tg)
        self.assertIn("1 signal(s)", subject)
        self.assertIn("TESTUSDT LONG · T1 4H→15m · sweep-T1-raid-C3 · series close", text)
        self.assertIn("5 4H C3 closure", text)
        self.assertIn("level 98.6", text)
        self.assertIn("entry candle", text)
        self.assertIn("waiting for a ≥50% body close beyond the series", text)
        self.assertTrue(all(len(c) <= 4096 for c in notify.split_telegram(tg)))

    def test_email_lists_all_seven_steps_and_the_candle_times(self):
        _, _, html = run_bot.fmt_signals(self.ctx, self.payload, 3)
        for name in ("Context", "Trigger", "Structure shift", "Discount / premium", "C2 / C3 closure", "Zone",
                     "Entry candle"):
            self.assertIn(f"<b>{name}</b>", html)
        self.assertIn("Candle times (open, MYT): 4H C1", html)
        self.assertIn("C3 closed back inside", html)


if __name__ == "__main__":
    unittest.main()
