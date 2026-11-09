"""Protection shell + R-ladder tests (fees/slippage off unless a test is about them)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_bot as pb  # noqa: E402
import timeutil as tu  # noqa: E402
from exchange import Candle, Contract  # noqa: E402

T = tu.parse_date("2026-09-25") + 10 * tu.HOUR          # 10:00 MYT
M1 = tu.MIN


def setup(direction="long", stop=98.0, dol=None, symbol="TESTUSDT", sid="s1"):
    return {"symbol": symbol, "direction": direction, "stop": stop, "dol": dol, "tier": "T1",
            "tag": "sweep-T1-raid", "family": "sweep", "trigger": "raid", "setup_id": sid,
            "struct_tf": "4H", "entry_tf": "15m", "entry": 100.0, "target": None}


def pos(direction="long", entry=100.0, stop=98.0, dol=None, **kw):
    qty = pb.RISK_PER_TRADE / abs(entry - stop)
    return pb.open_position(setup(direction, stop, dol, **kw), qty, entry, 0.0, T, "paper")


def path(*prices, t0=T, step=M1):
    """Candles from a list of (o, h, l, c)."""
    return [Candle(t0 + i * step, o, h, l, c, 0.0) for i, (o, h, l, c) in enumerate(prices)]


def run(p, candles, now=None, fee=0.0, slip=0.0):
    now = now or candles[-1].ts + 10 * M1
    return pb.mark(p, candles, now, M1, fee, slip)


class Ladder(unittest.TestCase):
    def test_hard_stop_is_minus_one_r(self):
        p = pos()
        ev = run(p, path((100, 100.2, 99.0, 99.2), (99.2, 99.3, 97.5, 97.8)))
        self.assertEqual((ev[-1]["reason"], p["exit_price"]), ("stop", 98.0))
        t = pb.finalize(p)
        self.assertAlmostEqual(t["net_pnl"], -2.5)
        self.assertAlmostEqual(t["r_mult"], -1.0)

    def test_tp1_half_off_then_breakeven(self):
        p = pos(stop=99.5)                                   # R = 0.5 → trail (1%) sits below BE at +1R
        ev = run(p, path((100, 100.6, 99.9, 100.5), (100.5, 100.55, 99.7, 99.8)))
        self.assertEqual([e["kind"] for e in ev], ["tp1", "exit"])
        self.assertEqual(ev[-1]["reason"], "breakeven")
        self.assertAlmostEqual(pb.finalize(p)["net_pnl"], 1.25)           # half at +1R, half at 0

    def test_lock_plus_one_r_once_two_r_prints(self):
        p = pos(stop=99.5, dol=110.0)
        ev = run(p, path((100, 101.05, 99.9, 101.0), (101.0, 101.0, 100.2, 100.3)))
        self.assertEqual([e["kind"] for e in ev], ["tp1", "lock", "exit"])
        self.assertEqual(ev[-1]["reason"], "lock")
        self.assertAlmostEqual(pb.finalize(p)["r_mult"], 1.0)             # +1R banked + +1R locked

    def test_runner_trails_one_percent_behind_peak(self):
        p = pos(stop=99.5, dol=110.0)
        run(p, path((100, 104.0, 99.9, 103.9), (103.9, 103.95, 102.0, 102.1)))
        self.assertEqual(p["exit_reason"], "trail")
        self.assertAlmostEqual(p["exit_price"], 104.0 * 0.99)
        self.assertAlmostEqual(pb.finalize(p)["net_pnl"], 1.25 + 2.5 * (102.96 - 100))

    def test_target_is_dol_with_two_r_floor(self):
        p = pos(stop=98.0, dol=103.0)                        # DOL at 1.5R → floored to 2R (104)
        self.assertEqual(p["target"], 104.0)
        run(p, path((100, 104.5, 99.8, 104.2)))
        self.assertEqual((p["exit_reason"], p["exit_price"]), ("target", 104.0))
        self.assertAlmostEqual(pb.finalize(p)["r_mult"], 1.5)             # half at +1R, half at +2R
        self.assertEqual(pos(stop=98.0, dol=107.0)["target"], 107.0)      # DOL beyond 2R is kept

    def test_time_stop_after_48h(self):
        p = pos()
        candles = path(*[(100, 100.3, 99.7, 100.1)] * 3, t0=T + 47 * tu.HOUR + 58 * M1)
        ev = run(p, candles)
        self.assertEqual(ev[-1]["reason"], "time")
        self.assertEqual(p["closed_at"], T + 48 * tu.HOUR)

    def test_intrabar_path_is_conservative(self):
        p = pos(stop=99.5)                                   # up-close candle: O→L→H→C → stop first
        run(p, path((100, 101.2, 99.4, 101.0)))
        self.assertEqual(p["exit_reason"], "stop")
        p = pos(stop=99.5)                                   # down-close candle: O→H→L→C → TP1, then BE
        run(p, path((100, 100.7, 99.6, 99.7)))
        self.assertEqual([f["kind"] for f in p["fills"]], ["entry", "tp1", "exit"])
        self.assertEqual(p["exit_reason"], "breakeven")

    def test_gap_through_stop_fills_at_open(self):
        p = pos()
        run(p, path((100, 100.1, 99.9, 100.0), (97.0, 97.2, 96.5, 96.8)))
        self.assertEqual(p["exit_price"], 97.0)

    def test_short_mirror(self):
        p = pos(direction="short", entry=100.0, stop=100.5, dol=90.0)
        ev = run(p, path((100, 100.1, 98.95, 99.0), (99.0, 99.8, 98.98, 99.7)))
        self.assertEqual([e["kind"] for e in ev], ["tp1", "lock", "exit"])
        self.assertAlmostEqual(pb.finalize(p)["r_mult"], 1.0)

    def test_fees_and_slippage(self):
        p = pb.PaperBroker(fee_rate=0.0006, slippage=0.0002).open(setup(), 1.25, 100.0, T)
        self.assertAlmostEqual(p["entry"], 100.02)
        run(p, path((100, 100.1, 97.0, 97.5)), fee=0.0006, slip=0.0002)
        t = pb.finalize(p)
        self.assertAlmostEqual(p["exit_price"], 98.0 * 0.9998)
        self.assertAlmostEqual(t["fees"], 100.02 * 1.25 * 0.0006 + 98.0 * 0.9998 * 1.25 * 0.0006)
        self.assertLess(t["net_pnl"], -2.5)

    def test_forming_candle_skipped_and_replay_idempotent(self):
        p = pos()
        c = path((100, 100.4, 99.8, 100.2), (100.2, 100.3, 97.0, 97.5))
        pb.mark(p, c, c[1].ts + 30 * 1000, M1)               # second candle still forming
        self.assertEqual(p["qty_open"], p["qty"])
        self.assertEqual(p["last_mark_ts"], c[1].ts)
        pb.mark(p, c, c[1].ts + 30 * 1000, M1)
        self.assertEqual(len(p["fills"]), 1)
        pb.mark(p, c, c[1].ts + M1, M1)
        self.assertEqual(p["exit_reason"], "stop")

    def test_funding_long_pays_positive_rate(self):
        p = pos()
        ev = pb.apply_funding(p, 0.0001, T + 16 * tu.HOUR, 100.0)   # 02:00 → 18:00 UTC
        self.assertEqual(len(ev), 2)                          # settles at 08:00 and 16:00 UTC
        self.assertAlmostEqual(p["funding"], -2 * 1.25 * 100 * 0.0001)


class Shell(unittest.TestCase):
    def closed_trade(self, net, ts, sym="X", sid=None):
        p = pos(symbol=sym, sid=sid or f"{sym}{ts}")
        p["fills"].append({"ts": ts, "kind": "exit", "qty": p["qty"], "price": 100 + net / p["qty"],
                           "pnl": net, "fee": 0.0})
        p.update(qty_open=0.0, closed_at=ts, exit_reason="stop", exit_price=100.0)
        return pb.finalize(p)

    def test_daily_loss_stop_by_dollars_and_by_count(self):
        st = pb.new_state()
        st["closed"] = [self.closed_trade(-7.4, T + i * tu.HOUR) for i in range(2)]
        self.assertFalse(pb.gates(st, T + 3 * tu.HOUR).loss_stop)      # −14.80
        st["closed"].append(self.closed_trade(-0.2, T + 3 * tu.HOUR))     # −15.00 → hit
        g = pb.gates(st, T + 4 * tu.HOUR)
        self.assertTrue(g.loss_stop)
        self.assertFalse(g.entries_allowed)
        st = pb.new_state()
        st["closed"] = [self.closed_trade(-0.2, T + i * M1) for i in range(6)]
        g = pb.gates(st, T + tu.HOUR)
        self.assertTrue(g.loss_stop)
        self.assertIn("6 losing trades", g.reason)

    def test_protect_mode_at_daily_target(self):
        st = pb.new_state()
        st["closed"] = [self.closed_trade(15.0, T)]
        g = pb.gates(st, T + tu.HOUR)
        self.assertTrue(g.protect)
        self.assertFalse(pb.check_entry(st, g, "long", "NEWUSDT")[0])

    def test_partial_profit_counts_as_realized(self):
        st = pb.new_state()
        p = pos(stop=99.5)
        run(p, path((100, 100.6, 99.9, 100.5)))
        st["positions"] = [p]
        self.assertAlmostEqual(pb.gates(st, T + tu.HOUR).realized, 1.25)

    def test_gates_reset_at_myt_midnight(self):
        st = pb.new_state()
        yesterday = tu.midnight(T) - tu.HOUR                  # 23:00 MYT the day before
        st["closed"] = [self.closed_trade(-20.0, yesterday)]
        self.assertTrue(pb.gates(st, yesterday + 30 * M1).loss_stop)
        self.assertFalse(pb.gates(st, tu.midnight(T) + M1).loss_stop)

    def test_max_positions_correlation_and_duplicates(self):
        st = pb.new_state()
        st["positions"] = [pos(symbol=f"L{i}USDT", sid=f"l{i}") for i in range(3)]
        g = pb.gates(st, T)
        self.assertIn("correlation cap", pb.check_entry(st, g, "long", "NEWUSDT")[1])
        self.assertTrue(pb.check_entry(st, g, "short", "NEWUSDT")[0])
        self.assertIn("already", pb.check_entry(st, g, "short", "L0USDT")[1])
        st["positions"] += [pos(direction="short", stop=102, symbol=f"S{i}USDT", sid=f"s{i}") for i in range(2)]
        self.assertIn("max open", pb.check_entry(st, pb.gates(st, T), "short", "NEWUSDT")[1])
        st = pb.new_state()
        st["traded_setups"]["abc"] = T
        self.assertIn("already traded", pb.check_entry(st, pb.gates(st, T), "long", "Z", "abc")[1])

    def test_position_sizing(self):
        qty, risk, why = pb.size_position(100.0, 98.0)
        self.assertEqual((qty, risk, why), (1.25, 2.5, ""))
        c = Contract("BTCUSDT", "BTC", volume_place=3, size_mult=0.001, min_qty=0.001, min_usdt=5.0)
        qty, risk, _ = pb.size_position(64000.0, 63488.0, c)  # 0.8% stop
        self.assertEqual(qty, 0.004)
        self.assertLessEqual(risk, 2.5)
        c = Contract("XUSDT", "X", volume_place=0, size_mult=1.0, min_qty=1.0, min_usdt=5.0)
        self.assertIn("minimum", pb.size_position(1000.0, 880.0, c)[2])

    def test_edge_gate_keeps_live_off_until_proven(self):
        st = pb.new_state()
        self.assertFalse(pb.edge_gate(st)[0])
        st["closed"] = [self.closed_trade(2.0 if i % 2 else -1.0, T + i * tu.HOUR) for i in range(40)]
        ok, why = pb.edge_gate(st)
        self.assertTrue(ok, why)
        st["closed"] = [self.closed_trade(-1.0 if i % 2 else 0.9, T + i * tu.HOUR) for i in range(40)]
        self.assertFalse(pb.edge_gate(st)[0])

    def test_balance_and_equity_curve(self):
        st = pb.new_state(600.0)
        st["closed"] = [self.closed_trade(3.0, T), self.closed_trade(-2.5, T + tu.HOUR)]
        self.assertAlmostEqual(pb.balance(st), 600.5)
        self.assertEqual([round(e, 2) for _, e in pb.equity_curve(st)][-1], 600.5)
        s = pb.trade_stats(st["closed"])
        self.assertEqual((s["trades"], s["wins"], s["losses"]), (2, 1, 1))
        self.assertAlmostEqual(s["profit_factor"], 1.2)
        self.assertAlmostEqual(s["max_drawdown"], -2.5)


if __name__ == "__main__":
    unittest.main()
