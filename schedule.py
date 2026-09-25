"""schedule.py — the MYT job grid (FRAMEWORK.md → Core Responsibilities).

  cycle   every 4h: scan (routine 1 + Module A + routine 2) → framework signals → trade pass.
          Anchored 2 min after each Bitget 4H close (00:02, 04:02, … MYT) so T1 structure is fresh.
  trade   every 30 min (:00:30 / :30:30) — the latest 15m/5m entry candle has just closed
  manage  every 5 min (+10 s)
  report  07:00 and 19:00 MYT (+45 s, after that minute's manage run)
"""
from __future__ import annotations

from typing import Dict

import config
import timeutil as tu

REPORT_DELAY = 45 * tu.SEC
JOBS = ("manage", "report", "cycle", "trade")          # also the priority order when due together
LABELS = {"manage": "manage · R-ladder", "report": "daily report", "cycle": "scan + signals", "trade": "trade"}


def next_due(job: str, after: int) -> int:
    """First run strictly after `after`."""
    if job == "manage":
        return tu.next_grid(after, config.MANAGE_EVERY_MIN * tu.MIN, config.MANAGE_OFFSET_S * tu.SEC)
    if job == "trade":
        return tu.next_grid(after, config.TRADE_EVERY_MIN * tu.MIN, config.TRADE_OFFSET_S * tu.SEC)
    if job == "cycle":
        return tu.next_grid(after, config.SCAN_EVERY_MIN * tu.MIN, tu.hhmm(config.SCAN_OFFSET))
    if job == "report":
        return tu.next_daily(after - REPORT_DELAY, config.REPORT_TIMES) + REPORT_DELAY
    raise ValueError(job)


def last_due(job: str, at: int) -> int:
    """Latest scheduled run at or before `at`."""
    if job == "manage":
        return tu.last_grid(at, config.MANAGE_EVERY_MIN * tu.MIN, config.MANAGE_OFFSET_S * tu.SEC)
    if job == "trade":
        return tu.last_grid(at, config.TRADE_EVERY_MIN * tu.MIN, config.TRADE_OFFSET_S * tu.SEC)
    if job == "cycle":
        return tu.last_grid(at, config.SCAN_EVERY_MIN * tu.MIN, tu.hhmm(config.SCAN_OFFSET))
    if job == "report":
        return tu.last_daily(at - REPORT_DELAY, config.REPORT_TIMES) + REPORT_DELAY
    raise ValueError(job)


def report_slot(due: int) -> int:
    """The 07:00/19:00 slot a report run belongs to."""
    return due - REPORT_DELAY


def upcoming(now: int) -> Dict[str, int]:
    return {job: next_due(job, now) for job in JOBS}
