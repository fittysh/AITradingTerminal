"""Engine tests: every primitive of the v5 sequence plus full evaluate() runs on hand-built scenarios."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy as st  # noqa: E402
from exchange import Candle  # noqa: E402
from scenarios import (H4, M15, T0, bars, cisd_scenario, getter, long_scenario, mirror,  # noqa: E402
                       LONG_TAIL, filler)
import timeutil as tu  # noqa: E402


def zigzag(points, t0=T0, step=H4):
    """Candles walking linearly between turning points (one candle per unit step)."""
    rows = []
    for a, b in zip(points, points[1:]):
        n = max(1, int(abs(b - a)))
        for k in range(n):
            o = a + (b - a) * k / n
            c = a + (b - a) * (k + 1) / n
            rows.append((o, max(o, c) + 0.1, min(o, c) - 0.1, c))
    return bars(rows, t0, step)


class Primitives(unittest.TestCase):
    def test_swing_seq_finds_fractals(self):
        c = bars([(1, 2, 0.5, 1.5), (1.5, 3, 1, 2.5), (2.5, 5, 2, 4), (4, 4.5, 3, 3.2), (3.2, 3.5, 1.5, 2)], T0, H4)
        highs, lows = st._swing_seq(c, 1)
        self.assertEqual(highs, [(2, 5)])
        self.assertEqual(lows, [])

    def test_market_structure(self):
        # legs with minor counter-swings, so leg extremes become intermediate-term highs/lows
        legs = [100, 106, 104, 110, 106, 107, 103, 109, 107, 116, 112, 113, 108, 114, 112, 122, 117, 118,
                113, 120, 118, 128]
        up = zigzag(legs)
        down = zigzag([200 - p for p in legs])
        self.assertEqual(st.market_structure(up), "up")
        self.assertEqual(st.market_structure(down), "down")
        self.assertEqual(st.market_structure(zigzag([100, 110, 104, 116, 109, 122])), "range",
                         "a clean staircase has no intermediate-term swings yet")
        S, *_ = long_scenario()
        self.assertEqual(st.market_structure(S), "range")

    def test_daily_bias_uses_close_beyond_prior_range_only(self):
        d = lambda rows: bars(rows, T0, tu.DAY)  # noqa: E731
        self.assertEqual(st.daily_bias(d([(10, 12, 9, 11), (11, 13, 10, 12.5)])), "bullish")
        self.assertEqual(st.daily_bias(d([(10, 12, 9, 11), (11, 11.5, 8, 8.5)])), "bearish")
        self.assertEqual(st.daily_bias(d([(10, 12, 9, 11), (11, 12.5, 9.5, 11.8)])), "neutral",
                         "a failed break above PDH must not set the bias (daily failure is research only)")
        self.assertEqual(st.daily_bias(d([(10, 12, 9, 11)])), "neutral")

    def test_daily_failure(self):
        d = lambda rows: bars(rows, T0, tu.DAY)  # noqa: E731
        self.assertEqual(st.daily_failure(d([(10, 12, 9, 11), (11, 12.5, 9.5, 11.8)]))["kind"], "bearish")
        self.assertEqual(st.daily_failure(d([(10, 12, 9, 11), (11, 11.5, 8.5, 9.4)]))["kind"], "bullish")
        self.assertEqual(st.daily_failure(d([(10, 12, 9, 11), (11, 12.5, 8.5, 10)]))["kind"], "both")
        self.assertIsNone(st.daily_failure(d([(10, 12, 9, 11), (11, 12.5, 9.5, 12.2)])))  # closed beyond
        self.assertIsNone(st.daily_failure(d([(10, 12, 9, 11), (11, 11.5, 9.5, 11)])))    # inside day

    def test_liquidity_sweep_raid(self):
        S, *_ = long_scenario()
        t = st.liquidity_sweep(S, st.LONG)
        self.assertEqual((t.kind, t.level, t.extreme), ("raid", 95.0, 94.0))
        self.assertEqual(t.idx, 25 + 15)

    def test_sweep_needs_reclaim_and_intact_extreme(self):
        rows = filler() + LONG_TAIL[:15] + [(97.2, 97.4, 94.0, 94.5), (94.5, 94.8, 93.5, 94.0),
                                           (94.0, 94.6, 93.8, 94.3), (94.3, 94.9, 94.0, 94.8)]
        self.assertIsNone(st.liquidity_sweep(bars(rows, T0, H4), st.LONG), "no close back above 95 → no raid")
        rows = filler() + LONG_TAIL[:17] + [(98.8, 99.0, 93.0, 94.0)]
        self.assertIsNone(st.liquidity_sweep(bars(rows, T0, H4), st.LONG), "raid extreme undercut → failed raid")

    def test_cisd_uses_last_delivery_leg(self):
        S, *_ = cisd_scenario()
        self.assertIsNone(st.liquidity_sweep(S, st.LONG))
        t = st._cisd(S, st.LONG)
        self.assertEqual((t.kind, t.level, t.extreme), ("cisd", 103.1, 99.7))

    def test_structure_shift(self):
        S, *_ = long_scenario()
        sh = st.structure_shift(S, st.LONG, st.liquidity_sweep(S, st.LONG))
        self.assertEqual((sh.level, sh.idx), (102.0, 25 + 17))

    def test_fvg_freshness(self):
        c = bars([(10, 10.5, 9.8, 10.4), (10.4, 11.6, 10.3, 11.5), (11.5, 12.0, 11.0, 11.9),
                  (11.9, 12.1, 11.2, 11.3)], T0, H4)
        fvgs = st.find_fvgs(c, st.LONG)
        self.assertEqual([(z.lo, z.hi, z.fresh) for z in fvgs], [(10.5, 11.0, True)])
        filled = c + bars([(11.3, 11.4, 10.4, 10.6)], c[-1].ts + H4, H4)
        self.assertFalse(st.find_fvgs(filled, st.LONG)[0].fresh)

    def test_order_block_and_unicorn(self):
        S, *_ = long_scenario()
        t = st.liquidity_sweep(S, st.LONG)
        ob = st.order_block(S, st.LONG, t.ext_idx)
        self.assertEqual((ob.lo, ob.hi), (94.0, 97.4))
        bb = st.breaker_block(S, st.LONG, 25 + 12)
        self.assertEqual((bb.lo, bb.hi), (100.5, 102.0))
        self.assertEqual(st.breaker_overlap(S, st.LONG, 25 + 12, st.Zone("FVG", 99.0, 101.0, 0)), (100.5, 101.0))
        self.assertIsNone(st.breaker_overlap(S, st.LONG, 25 + 12, st.Zone("FVG", 97.4, 98.6, 0)))

    def test_stop_floor_and_cap(self):
        stop, dist, _ = st.stop_for(st.LONG, 100.0, 99.9)          # raid extreme 0.2% away → floored
        self.assertAlmostEqual(dist, 0.8)
        self.assertAlmostEqual(stop, 99.2)
        stop, dist, why = st.stop_for(st.LONG, 100.0, 85.0)         # 15% away → rejected
        self.assertIsNone(stop)
        self.assertIn("12%", why)
        stop, dist, _ = st.stop_for(st.SHORT, 100.0, 104.0)
        self.assertAlmostEqual(stop, 104.104)


def r_stage(S, E, D, now, price=101.0):
    return st.evaluate("TESTUSDT", getter(S, E, D), price, now).stage


class Closure(unittest.TestCase):
    """Step 5 on hand-built candle pairs (C1 = index 0)."""

    def c(self, *rows):
        return bars(rows, T0, H4)

    def test_c2_closure(self):
        c = self.c((10, 10.5, 9.0, 9.5), (9.5, 9.8, 8.5, 9.2))          # takes 9.0, closes 9.2
        cl, _ = st.candle_closure(c, st.LONG, 1)
        self.assertEqual((cl["kind"], cl["level"], cl["close"]), ("C2", 9.0, 1))

    def test_c3_closure(self):
        c = self.c((10, 10.5, 9.0, 9.5), (9.5, 9.6, 8.5, 8.8), (8.8, 9.6, 8.7, 9.4))
        cl, _ = st.candle_closure(c, st.LONG, 1)
        self.assertEqual((cl["kind"], cl["close"]), ("C3", 2))

    def test_waits_for_c3(self):
        cl, why = st.candle_closure(self.c((10, 10.5, 9.0, 9.5), (9.5, 9.6, 8.5, 8.8)), st.LONG, 1)
        self.assertIsNone(cl)
        self.assertIn("waiting for C3", why)

    def test_continuation_is_rejected(self):
        c = self.c((10, 10.5, 9.0, 9.5), (9.5, 9.6, 8.5, 8.8), (8.8, 8.95, 8.6, 8.7))
        cl, why = st.candle_closure(c, st.LONG, 1)
        self.assertIsNone(cl)
        self.assertIn("continuation", why)

    def test_must_trade_through_c1(self):
        cl, why = st.candle_closure(self.c((10, 10.5, 9.0, 9.5), (9.5, 9.8, 9.1, 9.7)), st.LONG, 1)
        self.assertIsNone(cl)

    def test_short_mirror(self):
        c = mirror(self.c((10, 10.5, 9.0, 9.5), (9.5, 9.8, 8.5, 9.2)), 20.0)
        cl, _ = st.candle_closure(c, st.SHORT, 1)
        self.assertEqual((cl["kind"], cl["level"]), ("C2", 11.0))


class Evaluate(unittest.TestCase):
    def test_long_signal(self):
        S, E, D, now, price = long_scenario()
        r = st.evaluate("TESTUSDT", getter(S, E, D), price, now)
        s = r.setup
        self.assertEqual((r.stage, s.status, s.direction, s.tier, s.tag),
                         (st.STAGE["signal"], "signal", "long", "T1", "sweep-T1-raid-C3"))
        self.assertEqual((s.closure, s.entry_kind, s.confirm["level"]), ("C3", "close", 98.6))
        self.assertEqual([c["step"] for c in s.checklist], [1, 2, 3, 4, 5, 6, 7])
        self.assertAlmostEqual(s.stop, 94.0 * 0.999)
        self.assertAlmostEqual(s.target, 98.3 + 2 * (98.3 - 93.906), places=6)   # DOL 104.5 < 2R → 1:2 floor
        self.assertEqual(s.rr, 2.0)
        self.assertEqual((s.poi["lo"], s.poi["hi"]), (97.4, 98.6))
        self.assertTrue(all(step["ok"] for step in s.checklist))
        self.assertEqual(len(s.chart["t"]), len(S))

    def test_short_signal_is_the_mirror(self):
        S, E, D, now, price = long_scenario()
        r = st.evaluate("TESTUSDT", getter(mirror(S), mirror(E), mirror(D)), 200 - price, now)
        s = r.setup
        self.assertEqual((s.status, s.direction, s.tag), ("signal", "short", "sweep-T1-raid-C3"))
        self.assertAlmostEqual(s.stop, 106.0 * 1.001)
        self.assertAlmostEqual((s.poi["lo"], s.poi["hi"])[0], 200 - 98.6)
        self.assertGreaterEqual(s.rr, 2.0)

    def test_cisd_signal(self):
        S, E, D, now, price = cisd_scenario()
        s = st.evaluate("CISDUSDT", getter(S, E, D), price, now).setup
        self.assertEqual((s.status, s.tag, s.raid["kind"]), ("signal", "sweep-T1-cisd-C3", "cisd"))
        self.assertEqual(s.target, 106.5)                           # the DOL, already ≥ 2R
        self.assertEqual(s.dol_kind, "swing high")

    def test_in_zone_waits_for_the_series_close(self):
        S, E, D, now, price = long_scenario()
        s = st.evaluate("TESTUSDT", getter(S, E[:-1], D), price, now - M15).setup   # before the last candle
        self.assertEqual(s.status, "in-zone")
        self.assertFalse(s.checklist[6]["ok"])
        self.assertIn("close above the series at 98.6", s.checklist[6]["detail"])

    def test_close_below_the_series_is_not_an_entry(self):
        S, E, D, now, price = long_scenario()
        solid = E[:-1] + [Candle(E[-1].ts, 97.95, 98.5, 97.9, 98.45, 1.0)]   # 83% body, but under 98.6
        self.assertEqual(st.evaluate("T", getter(S, solid, D), price, now).setup.status, "in-zone")

    def test_wick_candle_through_the_series_is_not_an_entry(self):
        S, E, D, now, price = long_scenario()
        wick = E[:-1] + [Candle(E[-1].ts, 98.5, 98.9, 97.5, 98.65, 1.0)]     # closes above, body 11%
        s = st.evaluate("T", getter(S, wick, D), price, now).setup
        self.assertEqual(s.status, "in-zone")
        self.assertIn("spent (body 11% < 50%)", s.checklist[6]["detail"])

    def test_series_close_counts_for_30_minutes(self):
        S, E, D, now, price = long_scenario()
        later = E + [Candle(E[-1].ts + M15, 98.7, 98.8, 98.65, 98.75, 1.0)]  # +15m, still above 98.6
        self.assertEqual(st.evaluate("T", getter(S, later, D), price, now + M15).setup.status, "signal")
        s = st.evaluate("T", getter(S, E, D), price, now + 40 * tu.MIN).setup   # 45 min old → spent
        self.assertEqual(s.status, "in-zone")

    def test_shallow_pullback_after_the_close_keeps_the_entry(self):
        S, E, D, now, price = long_scenario()
        red = E + [Candle(E[-1].ts + M15, 98.7, 98.72, 98.55, 98.62, 1.0)]   # red, dips into the FVG top
        s = st.evaluate("T", getter(S, red, D), price, now + M15).setup
        self.assertEqual((s.status, s.confirm["c"]), ("signal", 98.7))

    def test_close_back_through_the_series_cancels(self):
        S, E, D, now, price = long_scenario()
        back = E + [Candle(E[-1].ts + M15, 98.7, 98.75, 98.2, 98.3, 1.0)]    # closes back under 98.6
        self.assertEqual(st.evaluate("T", getter(S, back, D), price, now + M15).setup.status, "in-zone")

    def test_series_close_after_a_c2_candle_like_the_chart(self):
        """The annotated chart: 2 down-closes into the zone, a C2 candle sweeps lower and closes back up,
        inner candles stay under the series top, then a body closes above it → entry."""
        S, _, D, now, price = long_scenario()
        rows = [(98.9, 99.0, 98.85, 98.95),                                   # last up-close before the series
                (98.55, 98.6, 98.2, 98.25), (98.25, 98.3, 97.9, 97.95),        # series: top of bodies 98.55
                (97.95, 98.2, 97.6, 98.15),                                   # C2: new low 97.6, closes up
                (98.15, 98.35, 98.1, 98.3), (98.3, 98.45, 98.25, 98.4),       # inner candles under 98.55
                (98.4, 98.95, 98.35, 98.9)]                                   # close above the series → entry
        E = bars(rows, now - 5 * tu.MIN - len(rows) * M15, M15)
        s = st.evaluate("T", getter(S, E, D), price, now).setup
        self.assertEqual((s.status, s.entry_kind, s.confirm["level"], s.series["bars"]), ("signal", "close", 98.55, 2))

    def test_fvg_retest_is_a_second_entry(self):
        """Series close 45 min ago left a 15m FVG; a solid candle retests it now → +FVG entry."""
        S, _, D, now, price = long_scenario()
        rows = [(98.6, 98.65, 98.2, 98.25), (98.25, 98.3, 97.8, 97.85),       # series into the zone, top 98.6
                (97.85, 98.9, 97.8, 98.85),                                   # close above it (45 min ago)
                (98.85, 99.6, 98.8, 99.5),                                    # displacement: FVG 98.9–99.2
                (99.5, 99.7, 99.2, 99.3),
                (99.1, 99.45, 99.05, 99.4)]                                   # retest of the FVG, solid close
        E = bars(rows, now - 5 * tu.MIN - len(rows) * M15, M15)
        s = st.evaluate("T", getter(S, E, D), price, now).setup
        self.assertEqual((s.status, s.entry_kind, s.fvg), ("signal", "fvg", True))
        self.assertEqual(s.confirm["fvg"], [98.9, 99.2])
        self.assertIn("+FVG", s.checklist[6]["detail"])

    def test_c2_closure(self):
        tail = list(LONG_TAIL)
        tail[15] = (97.2, 97.4, 94.0, 97.0)                     # raid candle closes back above C1's low 96.8
        S = bars(filler() + tail, T0, H4)
        _, E, D, now, price = long_scenario()
        s = st.evaluate("C2USDT", getter(S, E, D), price, now).setup
        self.assertEqual((s.status, s.closure, s.tag), ("signal", "C2", "sweep-T1-raid-C2"))
        self.assertIn("C2 closure", s.checklist[4]["detail"])

    def test_no_c2_or_c3_closure_means_no_long(self):
        tail = list(LONG_TAIL)
        tail[16] = (96.0, 96.7, 95.8, 96.5)                     # C3 also closes under C1's low 96.8
        S = bars(filler() + tail, T0, H4)
        _, E, D, now, price = long_scenario()
        r = st.evaluate("NOUSDT", getter(S, E, D), price, now)
        self.assertTrue(r.setup is None or r.setup.direction == "short")
        self.assertTrue(any("no candle closure" in n and "continuation" in n for n in r.notes), r.notes)

    def test_armed_when_price_still_in_premium(self):
        S, E, D, now, _ = long_scenario()
        s = st.evaluate("TESTUSDT", getter(S, E, D), 101.0, now).setup
        self.assertEqual(s.status, "armed")
        self.assertEqual(s.entry, 98.6)                              # first touch of the FVG
        self.assertFalse(s.checklist[3]["ok"])
        self.assertEqual(r_stage(S, E, D, now), st.STAGE["armed"])

    def test_order_block_entry_targets_the_dol(self):
        S, E, D, now, _ = long_scenario()
        e = bars([(96.6, 96.7, 96.0, 96.1), (96.1, 96.2, 95.2, 95.3),         # series through the FVG into the OB
                  (95.3, 96.8, 95.2, 96.7)], now - 5 * tu.MIN - 3 * M15, M15)   # closes above the series top 96.6
        s = st.evaluate("TESTUSDT", getter(S, e, D), 95.5, now).setup
        self.assertEqual((s.status, s.family, s.tag), ("signal", "OB", "OB-T1-raid-C3"))
        self.assertEqual(s.target, 104.5)                            # DOL beyond 2R → target is the DOL
        self.assertGreater(s.rr, 5)

    def test_unicorn(self):
        tail = LONG_TAIL[:18] + [(103.2, 112.0, 102.8, 111.0), (111.0, 111.5, 106.0, 106.5),
                                 (106.5, 107.0, 101.0, 101.4)]
        S = bars(filler() + tail, T0, H4)
        now = S[-1].ts + H4 + 20 * tu.MIN
        E = bars([(101.0, 101.1, 100.8, 100.9), (100.9, 101.5, 100.85, 101.4)], now - 5 * tu.MIN - 2 * M15, M15)
        _, _, D, _, _ = long_scenario()
        s = st.evaluate("UNIUSDT", getter(S, E, D), 101.2, now).setup
        self.assertEqual((s.status, s.family, s.tag), ("signal", "unicorn", "unicorn-T1-raid-C3"))
        self.assertEqual((s.poi["lo"], s.poi["hi"]), (100.5, 102.0))

    def test_context_blocks_longs_in_downtrend(self):
        S, E, D, now, price = long_scenario()
        bearish_daily = bars([(100, 108, 92, 101), (101, 102, 90, 91)], D[0].ts, tu.DAY)
        r = st.evaluate("TESTUSDT", getter(S, E, bearish_daily), price, now)
        self.assertTrue(r.setup is None or r.setup.direction == "short")
        self.assertTrue(any("context blocks" in n for n in r.notes))

    def test_first_signal_wins_across_tiers(self):
        S, E, D, now, price = long_scenario()
        E5 = bars([(x.o, x.h, x.l, x.c) for x in E], now - 20 * tu.MIN, 5 * tu.MIN)
        only_t2 = getter(S, E5, D, struct_tf="1H", entry_tf="5m")
        r = st.evaluate("TESTUSDT", only_t2, price, now)
        self.assertEqual((r.setup.tier, r.setup.status), ("T2", "signal"))
        self.assertTrue(any(n.startswith("T1: only 0") for n in r.notes))

    def test_t3_disabled(self):
        self.assertEqual([t.name for t in st.TIERS if t.enabled], ["T1", "T2"])
        self.assertEqual(next(t for t in st.TIERS if t.name == "T3").entry_tf, "1m")


if __name__ == "__main__":
    unittest.main()
