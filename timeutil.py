"""timeutil.py — Malaysia time (MYT = UTC+8, no DST) helpers and schedule grids.

Every timestamp inside the bot is epoch milliseconds (UTC). MYT only decides day boundaries
(risk gates reset at MYT midnight; the market/journal day starts at 07:00 MYT) and display.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Tuple

MYT = timezone(timedelta(hours=8), "MYT")
SEC = 1_000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
_OFFSET = 8 * HOUR


def now_ms() -> int:
    return int(time.time() * 1000)


def myt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, MYT)


def fmt(ms: int | None, f: str = "%Y-%m-%d %H:%M") -> str:
    return myt(ms).strftime(f) if ms else "—"


def hm(ms: int | None) -> str:
    return fmt(ms, "%H:%M")


def hhmm(s: str) -> int:
    """'07:00' → milliseconds after midnight."""
    h, m = s.strip().split(":")
    return int(h) * HOUR + int(m) * MIN


def midnight(ms: int) -> int:
    """MYT midnight at or before ms."""
    return (ms + _OFFSET) // DAY * DAY - _OFFSET


def day_bounds(ms: int) -> Tuple[int, int]:
    """MYT calendar day containing ms — the window the daily risk gates use."""
    start = midnight(ms)
    return start, start + DAY


def market_day(ms: int, start: str = "07:00") -> Tuple[int, int]:
    """Market day containing ms: it opens at `start` MYT and runs 24h (journal + report day)."""
    off = hhmm(start)
    s = midnight(ms - off) + off
    return s, s + DAY


def date_str(ms: int) -> str:
    return myt(ms).strftime("%Y-%m-%d")


def parse_date(s: str) -> int:
    """'2026-09-25' → MYT midnight epoch ms."""
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=MYT)
    return int(d.timestamp() * 1000)


def next_grid(ms: int, every_ms: int, offset_ms: int = 0) -> int:
    """Next time strictly after ms on the grid MYT-midnight + offset + k·every (every divides 24h)."""
    base = midnight(ms) - DAY + offset_ms
    return base + ((ms - base) // every_ms + 1) * every_ms


def last_grid(ms: int, every_ms: int, offset_ms: int = 0) -> int:
    """Latest grid time at or before ms."""
    base = midnight(ms) - DAY + offset_ms
    return base + ((ms - base) // every_ms) * every_ms


def next_daily(ms: int, times: Iterable[str]) -> int:
    m = midnight(ms)
    return min(c for c in (m + d * DAY + hhmm(t) for d in (0, 1) for t in times) if c > ms)


def last_daily(ms: int, times: Iterable[str]) -> int:
    m = midnight(ms)
    return max(c for c in (m + d * DAY + hhmm(t) for d in (-1, 0) for t in times) if c <= ms)


def ago(ms: int | None, now: int) -> str:
    if not ms:
        return "never"
    s = max(0, (now - ms) // SEC)
    if s < 90:
        return f"{s}s ago"
    if s < 90 * 60:
        return f"{s // 60}m ago"
    if s < 36 * 3600:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def until(ms: int | None, now: int) -> str:
    if not ms:
        return "—"
    s = max(0, (ms - now) // SEC)
    if s < 90:
        return f"in {s}s"
    if s < 90 * 60:
        return f"in {s // 60}m"
    return f"in {s / 3600:.1f}h"
