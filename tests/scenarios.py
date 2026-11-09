"""Hand-built candle scenarios shared by the engine and bot tests."""
from exchange import Candle
import timeutil as tu

H4 = 4 * tu.HOUR
M15 = 15 * tu.MIN
T0 = tu.parse_date("2026-09-01")          # MYT midnight; 4H buckets align with UTC 4H buckets


def bars(rows, t0, step):
    return [Candle(t0 + i * step, o, h, l, c, 1_000_000.0) for i, (o, h, l, c) in enumerate(rows)]


def filler(n=25, base=99.0):
    """Mild rising zigzag: monotonic swing highs/lows, so it adds no intermediate-term structure."""
    rows, p = [], base
    for i in range(n):
        up = (i // 3) % 2 == 0
        o = p
        c = p + (0.9 if up else -0.6)
        rows.append((o, max(o, c) + 0.2, min(o, c) - 0.2, c))
        p = c
    return rows


# Long setup after the filler (F = offset): pool 95.0 swept to 94.0 and reclaimed, structure shift
# through the 102.0 lower-high, bullish FVG 97.4–98.6 left by the displacement, price back in discount.
# Step 5 is a C3 closure: the raid candle (C2) closed 96.0, below C1's low 96.8; C3 closed 98.8.
LONG_TAIL = [
    (100.0, 100.5, 98.5, 99.0), (99.0, 99.3, 96.5, 97.0), (97.0, 97.5, 95.5, 96.0),
    (96.0, 96.6, 95.0, 96.4),                              # F+3  swing low 95.0 (the pool)
    (96.4, 98.0, 96.0, 97.8), (97.8, 100.5, 97.5, 100.2), (100.2, 102.5, 99.8, 102.0),
    (102.0, 103.5, 101.2, 103.0), (103.0, 104.0, 102.4, 102.8),   # F+8 swing high 104
    (102.8, 103.2, 100.5, 100.8), (100.8, 101.0, 99.0, 99.4),     # F+10 swing low 99.0
    (99.4, 101.0, 99.2, 100.8), (100.8, 102.0, 100.5, 101.6),     # F+12 lower-high 102 (up-close)
    (101.6, 101.8, 99.5, 99.8), (99.8, 100.0, 96.8, 97.2),
    (97.2, 97.4, 94.0, 96.0),                              # F+15 raid: pierce 95.0 → 94.0, close 96.0
    (96.0, 99.0, 95.8, 98.8),
    (98.8, 103.5, 98.6, 103.2),                            # F+17 structure shift (close > 102)
    (103.2, 104.5, 102.5, 103.0),                          # F+18 post-shift extreme 104.5
    (103.0, 103.2, 100.0, 100.4), (100.4, 100.6, 98.2, 98.4),     # retrace into the FVG
]


def long_scenario():
    rows = filler() + LONG_TAIL
    S = bars(rows, T0, H4)
    now = S[-1].ts + H4 + 20 * tu.MIN                       # 20 min into the next 4H candle
    e0 = now - 5 * tu.MIN - 4 * M15                         # last 15m candle closed 5 min ago
    E = bars([(98.6, 98.7, 98.2, 98.3), (98.3, 98.4, 97.9, 98.0), (98.1, 98.2, 97.8, 97.9),
              (97.95, 98.75, 97.9, 98.7)], e0, M15)          # 3 down-closes into the FVG (series top 98.6),
    daily = bars([(100.0, 108.0, 92.0, 101.0), (101.0, 110.0, 93.0, 100.0)],   # then a solid bull body
                 tu.midnight(now) - 2 * tu.DAY, tu.DAY)                       # closes above it
    return S, E, daily, now, 98.3


def mirror(candles, pivot=200.0):
    """Reflect prices through `pivot`: a long setup becomes the equivalent short setup."""
    return [Candle(x.ts, pivot - x.o, pivot - x.l, pivot - x.h, pivot - x.c, x.v) for x in candles]


def getter(struct, entry, daily, struct_tf="4H", entry_tf="15m"):
    def get(tf, limit):
        data = {struct_tf: struct, entry_tf: entry, "1D": daily}.get(tf, [])
        return data[-limit:]
    return get


def staircase(n=40):
    """Clean uptrend whose pools all sit far below the later pullback (so nothing gets raided)."""
    rows, p = [], 80.0
    for i in range(n):
        if i < 34 and i % 6 in (4, 5):
            o, c = p, p - 0.6
            rows.append((o, o + 0.2, c - 0.2, c))
        else:
            o, c = p, p + 1.0
            rows.append((o, c + 0.2, o - 0.2, c))
        p = c
    return rows


# Pullback that takes no pool: down-close leg 103.1 → swing low 99.7, closed back above 103.1 (CISD),
# shift through the 105.0 short-term high, FVG 101.5–102.2 in discount, price 101.9 inside it.
CISD_TAIL = [(104.0, 104.7, 103.5, 104.5), (104.5, 105.0, 103.9, 104.1), (104.1, 104.3, 102.5, 102.7),
             (102.7, 103.4, 102.3, 103.1), (103.1, 103.2, 101.0, 101.3), (101.3, 101.5, 99.7, 100.0),
             (100.0, 102.5, 99.9, 102.4), (102.4, 103.9, 102.2, 103.7), (103.7, 105.9, 103.5, 105.7),
             (105.7, 106.5, 105.1, 105.5), (105.5, 105.6, 103.7, 103.9), (103.9, 104.0, 102.0, 102.1)]


def cisd_scenario():
    S = bars(staircase() + CISD_TAIL, T0, H4)
    now = S[-1].ts + H4 + 20 * tu.MIN
    e0 = now - 5 * tu.MIN - 2 * M15
    E = bars([(102.0, 102.1, 101.6, 101.7), (101.7, 102.15, 101.6, 102.1)], e0, M15)   # series 102.0 → close 102.1
    daily = bars([(100.0, 108.0, 92.0, 101.0), (101.0, 110.0, 93.0, 100.0)], tu.midnight(now) - 2 * tu.DAY, tu.DAY)
    return S, E, daily, now, 101.9
