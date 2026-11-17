"""The Bitget-history replay path, offline: a fake Bitget serves the synthetic market through the real API shape
(200 candles per page, paged backwards by endTime). Replaying that "history" must give the same trades as
replaying the synthetic market directly — so the real-data test (validate --source bitget) runs the same bot."""
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_bot  # noqa: E402
import timeutil as tu  # noqa: E402
from exchange import BitgetPublic, bucket, closed_only  # noqa: E402
from sim import SimClock, SimMarket  # noqa: E402
from store import Paths, Store  # noqa: E402

END = tu.parse_date("2026-09-24") + 21 * tu.HOUR + 7 * tu.MIN
DAYS, SEED = 2.0, 11


class FakeBitget:
    """Transport for BitgetPublic: /contracts and /history-candles answered from a SimMarket. Like Bitget,
    history-candles returns at most 200 bars, newest last, that open before the bar containing endTime."""

    def __init__(self, seed: int, start: int, end: int):
        self.clock = SimClock(start)
        self.sim = SimMarket(self.clock, seed=seed, start_ms=start - 60 * tu.DAY, end_ms=end + tu.DAY)
        self.calls = []

    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((u.path, q))
        if u.path.endswith("/contracts"):
            data = [{"symbol": c.symbol, "baseCoin": c.base, "quoteCoin": c.quote, "pricePlace": str(c.price_place),
                     "priceEndStep": str(c.price_end_step), "volumePlace": str(c.volume_place),
                     "sizeMultiplier": repr(c.size_mult), "minTradeNum": repr(c.min_qty), "minTradeUSDT": repr(c.min_usdt),
                     "symbolStatus": c.status, "symbolType": c.kind, "isRwa": "YES" if c.is_rwa else "NO",
                     "maxLever": str(c.max_lever)} for c in self.sim.contracts().values()]
        elif u.path.endswith("/history-candles"):
            limit, tf, end = int(q["limit"]), q["granularity"], int(q["endTime"])
            assert limit <= 200, "Bitget serves at most 200 history candles per request"
            cut = bucket(end, tf)
            self.clock.set(cut)
            rows = closed_only(self.sim.candles(q["symbol"], tf, limit + 1), tf, cut)[-limit:]
            data = [[str(c.ts), repr(c.o), repr(c.h), repr(c.l), repr(c.c), "0", repr(c.v)] for c in rows]
        else:
            data = []
        return {"code": "00000", "msg": "success", "data": data}


def trades(home: Path):
    st = Store(Paths(home)).load_state()
    return sorted((t["symbol"], t["opened_at"], t["direction"], t["tag"], round(t["entry"], 9), round(t["initial_stop"], 9),
                   t.get("exit_reason"), round(t.get("closed_at") or 0)) for t in st["closed"] + st["positions"])


class HistoryReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        end = END // (5 * tu.MIN) * (5 * tu.MIN)
        start = end - int(DAYS * tu.DAY)
        with mock.patch.object(SimMarket, "funding", lambda self, sym, now: 0.0):   # Bitget replay has no funding
            run_bot.replay(DAYS, "sim", str(root / "sim"), seed=SEED, end_ms=END, quiet=True)
        cls.fake = FakeBitget(SEED, start, end)
        cls.symbols = cls.fake.sim.symbols()
        with mock.patch.object(run_bot, "BitgetPublic", lambda: BitgetPublic(transport=cls.fake, rps=1e9)):
            cls.rep = run_bot.replay(DAYS, "bitget", str(root / "hist"), symbols=cls.symbols, end_ms=END, quiet=True)
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_same_trades_as_the_synthetic_replay(self):
        a, b = trades(self.root / "sim"), trades(self.root / "hist")
        self.assertGreater(len(a), 0)
        self.assertEqual(a, b)

    def test_history_is_paged_200_at_a_time_and_cached(self):
        pages = [q for path, q in self.fake.calls if path.endswith("/history-candles")]
        self.assertTrue(all(int(q["limit"]) <= 200 for q in pages))
        five = [q for q in pages if q["granularity"] == "5m" and q["symbol"] == self.symbols[0]]
        self.assertGreater(len(five), 1, "3 days of 5m candles need more than one page")
        cached = list((self.root / "hist" / "cache").glob("*.json")) or list((self.root / "hist").rglob("*_5m_*.json"))
        self.assertTrue(cached)

    def test_full_coverage_reports_no_gaps(self):
        self.assertEqual(self.rep["gaps"], [])

    def test_one_missing_candle_is_reported(self):
        from sim import HistoricalMarket
        end = END // (5 * tu.MIN) * (5 * tu.MIN)
        start = end - int(DAYS * tu.DAY)
        pub = BitgetPublic(transport=FakeBitget(SEED, start, end), rps=1e9)
        real_history = pub.history

        def holed(sym, tf, s, e):
            rows = real_history(sym, tf, s, e)
            return rows[:100] + rows[101:]                      # one bar missing, 0.1% of the history
        pub.history = holed
        with tempfile.TemporaryDirectory() as d:
            md = HistoricalMarket(pub, SimClock(start), self.symbols[:1], start, end, Path(d))
            md._fetch(self.symbols[0], "5m")
            self.assertEqual(len(md.gaps), 1)
            self.assertEqual(md.gaps[0]["expected"] - md.gaps[0]["candles"], 1)

    def test_short_history_is_reported(self):
        from sim import HistoricalMarket
        end = END // (5 * tu.MIN) * (5 * tu.MIN)
        start = end - int(DAYS * tu.DAY)
        fake = FakeBitget(SEED, start, end)
        pub = BitgetPublic(transport=fake, rps=1e9)
        real_history = pub.history
        pub.history = lambda sym, tf, s, e: real_history(sym, tf, s + tu.DAY, e)      # Bitget stops a day short
        with tempfile.TemporaryDirectory() as d:
            md = HistoricalMarket(pub, SimClock(start), self.symbols[:1], start, end, Path(d))
            md._fetch(self.symbols[0], "5m")
            self.assertEqual(len(md.gaps), 1)
            self.assertEqual((md.gaps[0]["symbol"], md.gaps[0]["tf"]), (self.symbols[0], "5m"))


if __name__ == "__main__":
    unittest.main()
