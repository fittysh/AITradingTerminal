"""Infrastructure tests: synthetic market, Bitget adapters (fake transport), universe filter, schedule,
notifications splitting, PDF writer."""
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule  # noqa: E402
import timeutil as tu  # noqa: E402
from exchange import (BitgetPrivate, BitgetPublic, Contract, closed_only, is_tradfi, sign,  # noqa: E402
                      universe, MarketData)
from notify import split_telegram  # noqa: E402
from pdfgen import PDF  # noqa: E402
from sim import SimClock, SimMarket  # noqa: E402

NOW = tu.parse_date("2026-09-20") + 9 * tu.HOUR + 3 * tu.MIN


class Sim(unittest.TestCase):
    def test_windows_agree_and_no_lookahead(self):
        a = SimMarket(SimClock(NOW), seed=3, days=20)
        b = SimMarket(SimClock(NOW), seed=3, start_ms=NOW - 12 * tu.DAY, end_ms=NOW + tu.DAY)
        for tf in ("5m", "1H", "4H", "1D"):
            ca, cb = a.candles("SOLUSDT", tf, 50), b.candles("SOLUSDT", tf, 50)
            k = min(len(ca), len(cb))
            self.assertGreater(k, 5)
            self.assertEqual(ca[-k:], cb[-k:], tf)                    # overlapping history is identical
        c5 = a.candles("SOLUSDT", "5m", 5)
        self.assertLessEqual(c5[-1].ts + 5 * tu.MIN, NOW)             # only closed 5m bars are visible
        h4 = a.candles("SOLUSDT", "4H", 5)
        self.assertEqual(h4[-1].ts, NOW // (4 * tu.HOUR) * (4 * tu.HOUR))   # forming 4H rebuilt from closed bars
        self.assertEqual(h4[-1].c, c5[-1].c)
        self.assertEqual(len(closed_only(h4, "4H", NOW)), 4)
        self.assertEqual(a.candles("SOLUSDT", "1D", 1)[0].ts, tu.midnight(NOW))   # Bitget 1D = MYT day

    def test_universe_excludes_tradfi(self):
        md = SimMarket(SimClock(NOW), seed=3, days=5)
        keep, excluded = universe(md)
        self.assertEqual(excluded, ["NVDAUSDT", "TSLAUSDT", "XAUUSDT"])
        self.assertIn("TONUSDT", [t.symbol for t in keep])           # TON ends in "ON" but is crypto

    def test_tradfi_rules(self):
        c = lambda base, rwa=False: Contract(base + "USDT", base, is_rwa=rwa)  # noqa: E731
        self.assertTrue(is_tradfi(c("XAU")))
        self.assertTrue(is_tradfi(c("TSLAX")))
        self.assertTrue(is_tradfi(c("AAPLON")))
        self.assertTrue(is_tradfi(c("FOO", rwa=True)))
        self.assertFalse(is_tradfi(c("TON")))
        self.assertFalse(is_tradfi(c("APEX")))
        self.assertFalse(is_tradfi(c("BTC")))


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        path = url.split("?")[0].split(".com", 1)[1]
        data = self.routes.get(path)
        if callable(data):
            data = data(url, body)
        return {"code": "00000", "msg": "success", "data": data}


class Bitget(unittest.TestCase):
    def test_public_parsing(self):
        http = FakeHttp({
            "/api/v2/mix/market/tickers": [{"symbol": "BTCUSDT", "lastPr": "65000.5", "change24h": "0.0123",
                                            "usdtVolume": "123456789", "fundingRate": "0.0001", "high24h": "66000",
                                            "low24h": "64000", "markPrice": "65001"},
                                           {"symbol": "DEADUSDT", "lastPr": "0"}],
            "/api/v2/mix/market/candles": [["1700000060000", "2", "3", "1", "2.5", "10", "25"],
                                           ["1700000000000", "1", "2", "0.5", "2", "10", "20"]],
            "/api/v2/mix/market/contracts": [{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "pricePlace": "1",
                                              "priceEndStep": "1", "volumePlace": "3", "sizeMultiplier": "0.001",
                                              "minTradeNum": "0.001", "minTradeUSDT": "5", "symbolStatus": "normal",
                                              "symbolType": "perpetual", "maxLever": "125"}],
        })
        pub = BitgetPublic(transport=http, rps=1000)
        t = pub.tickers()
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0].last, t[0].change24h, t[0].volume_usd), (65000.5, 0.0123, 123456789.0))
        c = pub.candles("BTCUSDT", "4H", 2)
        self.assertEqual([x.ts for x in c], [1700000000000, 1700000060000])     # sorted ascending
        self.assertEqual(c[0].v, 20.0)                                            # quote volume
        self.assertIn("granularity=4H", http.calls[1][1])
        k = pub.contracts()["BTCUSDT"]
        self.assertEqual((k.tick, k.round_qty(0.0049), k.round_price(65000.26, "down")), (0.1, 0.004, 65000.2))

    def test_private_signing_and_demo_header(self):
        http = FakeHttp({"/api/v2/mix/order/place-order": {"orderId": "1"}})
        cli = BitgetPrivate("k", "s3cret", "pp", demo=True, transport=http, rps=1000)
        cli.place_market("BTCUSDT", "buy", "0.004", stop_loss="63000.0", take_profit="70000.0", client_oid="x1")
        method, url, headers, body = http.calls[0]
        self.assertEqual(headers["paptrading"], "1")
        payload = json.loads(body)
        self.assertEqual((payload["side"], payload["orderType"], payload["presetStopLossPrice"]), ("buy", "market", "63000.0"))
        self.assertNotIn("tradeSide", payload)                                    # one-way mode
        prehash = headers["ACCESS-TIMESTAMP"] + "POST" + "/api/v2/mix/order/place-order" + body.decode()
        expect = base64.b64encode(hmac.new(b"s3cret", prehash.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(headers["ACCESS-SIGN"], expect)
        self.assertEqual(sign("s", "1", "get", "/p", "a=1", ""), base64.b64encode(
            hmac.new(b"s", b"1GET/p?a=1", hashlib.sha256).digest()).decode())

    def test_legacy_demo_symbol_mapping(self):
        cli = BitgetPrivate("k", "s", "p", demo=True, legacy_demo=True, transport=FakeHttp({}))
        self.assertEqual((cli.xsym("BTCUSDT"), cli.usym("SBTCSUSDT"), cli.product), ("SBTCSUSDT", "BTCUSDT", "SUSDT-FUTURES"))

    def test_broker_places_exchange_held_ladder(self):
        import logging
        from bitget_broker import BitgetBroker
        state = {"total": "0"}

        def positions(url, body):
            return [{"symbol": "BTCUSDT", "holdSide": "long", "total": state["total"], "openPriceAvg": "65010"}]

        def place(url, body):
            state["total"] = json.loads(body)["size"]
            return {"orderId": "E1"}

        http = FakeHttp({"/api/v2/mix/account/account": {"posMode": "one_way_mode"},
                         "/api/v2/mix/account/set-margin-mode": {}, "/api/v2/mix/account/set-leverage": {},
                         "/api/v2/mix/order/place-order": place, "/api/v2/mix/position/all-position": positions,
                         "/api/v2/mix/order/place-tpsl-order": {"orderId": "P"}})
        cli = BitgetPrivate("k", "s", "p", demo=True, transport=http, rps=1000)
        md = MarketData()
        broker = BitgetBroker(cli, md, "demo", logging.getLogger("t"))
        contract = Contract("BTCUSDT", "BTC", price_place=1, volume_place=3, size_mult=0.001, min_qty=0.001)
        setup = {"symbol": "BTCUSDT", "direction": "long", "stop": 64490.0, "target": 66100.0, "dol": 66100.0,
                 "tier": "T1", "tag": "sweep-T1-raid", "family": "sweep", "trigger": "raid", "setup_id": "x",
                 "struct_tf": "4H", "entry_tf": "15m"}
        import bitget_broker
        bitget_broker.time.sleep = lambda s: None
        pos = broker.open(setup, 0.004, 65000.0, NOW, contract)
        plans = [json.loads(c[3]) for c in http.calls if c[1].endswith("place-tpsl-order")]
        self.assertEqual([p["planType"] for p in plans], ["profit_plan", "moving_plan"])
        self.assertEqual([p["size"] for p in plans], ["0.002", "0.002"])
        self.assertEqual(plans[1]["rangeRate"], "1")
        self.assertEqual(plans[0]["holdSide"], "buy")                             # one-way mode hold side
        self.assertEqual((pos["entry"], pos["qty"], pos["mode"]), (65010.0, 0.004, "demo"))


class Schedule(unittest.TestCase):
    def test_grid(self):
        t = tu.parse_date("2026-09-25") + 7 * tu.HOUR + 1 * tu.MIN            # 07:01 MYT
        self.assertEqual(tu.hm(schedule.next_due("cycle", t)), "08:02")
        self.assertEqual(tu.fmt(schedule.next_due("trade", t), "%H:%M:%S"), "07:30:30")
        self.assertEqual(tu.fmt(schedule.next_due("manage", t), "%H:%M:%S"), "07:05:10")
        due = schedule.next_due("report", t)
        self.assertEqual((tu.fmt(due, "%H:%M:%S"), tu.hm(schedule.report_slot(due))), ("19:00:45", "19:00"))
        self.assertEqual(tu.hm(schedule.last_due("cycle", t)), "04:02")

    def test_market_day_and_gate_day(self):
        t = tu.parse_date("2026-09-25") + 3 * tu.HOUR                           # 03:00 MYT
        start, end = tu.market_day(t)
        self.assertEqual((tu.fmt(start), tu.fmt(end)), ("2026-09-24 07:00", "2026-09-25 07:00"))
        self.assertEqual(tu.fmt(tu.day_bounds(t)[0]), "2026-09-25 00:00")


class Delivery(unittest.TestCase):
    def test_telegram_split_keeps_pre_balanced(self):
        text = "head\n<pre>" + "\n".join(f"row {i:04d} " + "x" * 60 for i in range(200)) + "</pre>\ntail"
        chunks = split_telegram(text, limit=1000)
        self.assertGreater(len(chunks), 5)
        for c in chunks:
            self.assertLessEqual(len(c), 1000)
            self.assertEqual(c.count("<pre>"), c.count("</pre>"))
        self.assertEqual("".join(chunks).count("row "), 200)

    def test_pdf_structure(self):
        with tempfile.TemporaryDirectory() as d:
            p = PDF("Test — ✓")
            p.add_page()
            p.text(40, 800, "Hello (world) \\ ≥ 1:2", 12, "F2")
            p.polyline([(40, 400), (100, 420)])
            path = p.save(Path(d) / "t.pdf")
            data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        xref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
        self.assertTrue(data[xref:].startswith(b"xref"))
        body = zlib.decompress(data.split(b"stream\n", 1)[1].split(b"\nendstream", 1)[0])
        self.assertIn(b"(Hello \\(world\\) \\\\ >= 1:2) Tj", body)


if __name__ == "__main__":
    unittest.main()
