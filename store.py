"""store.py — persistence for the bot: atomic JSON files, an append-only event log, and a
cross-process lock so the daemon, the TUI and one-off CLI runs never interleave their writes.

Layout under a home directory (the repo by default, `sim/` for the synthetic market):
  state/state.json          paper/demo account, open positions, closed trades
  state/signals_live.json   framework signals + in-zone watch (the trade loop's only input)
  state/sweeps.json         Module A liquidity-sweep screen
  state/scan.json           Routine 1 movers / losers
  state/daily_failure.json  Routine 2 research watchlist
  state/schedule.json       last run of every job (daemon catch-up, UI "next run")
  state/events.jsonl        everything that happened, one JSON object per line
  journal/YYYY-MM-DD.md     reports/*.md|pdf      dashboard/index.html
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional

import paper_bot
from util import dumps, read_json, write_json

try:
    import fcntl
except ImportError:          # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

FILES = {"state": "state.json", "signals": "signals_live.json", "sweeps": "sweeps.json", "scan": "scan.json",
         "daily_failure": "daily_failure.json", "schedule": "schedule.json", "armed": "armed.json"}


class Paths:
    def __init__(self, home: Path | str):
        self.home = Path(home).resolve()
        self.state = self.home / "state"
        self.journal = self.home / "journal"
        self.reports = self.home / "reports"
        self.dashboard = self.home / "dashboard"
        self.cache = self.home / "cache"
        self.outbox = self.state / "outbox"

    def ensure(self) -> "Paths":
        for p in (self.state, self.journal, self.reports, self.dashboard, self.outbox):
            p.mkdir(parents=True, exist_ok=True)
        return self


class Store:
    def __init__(self, paths: Paths, account_size: Optional[float] = None):
        self.paths = paths.ensure()
        self.account_size = account_size
        self._rlock = threading.RLock()
        self._depth = 0
        self._fh = None
        self.events_path = self.paths.state / "events.jsonl"

    # -- files --
    def path(self, key: str) -> Path:
        return self.paths.state / FILES[key]

    def read(self, key: str, default=None):
        return read_json(self.path(key), default)

    def write(self, key: str, obj) -> None:
        write_json(self.path(key), obj)

    def load_state(self) -> dict:
        st = self.read("state")
        if not st:
            st = paper_bot.new_state(self.account_size) if self.account_size else paper_bot.new_state()
        st.setdefault("positions", [])
        st.setdefault("closed", [])
        st.setdefault("traded_setups", {})
        return st

    def save_state(self, st: dict) -> None:
        cutoff = max((t["closed_at"] for t in st["closed"]), default=0) - 7 * 86_400_000
        st["traded_setups"] = {k: v for k, v in st["traded_setups"].items() if v >= cutoff}
        self.write("state", st)

    # -- events --
    def event(self, kind: str, ts: int, **fields) -> dict:
        rec = {"ts": int(ts), "kind": kind, **fields}
        line = dumps(rec) + "\n"
        with self._rlock:
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return rec

    def events(self, start: Optional[int] = None, end: Optional[int] = None,
               kinds: Optional[Iterable[str]] = None, limit: Optional[int] = None) -> List[dict]:
        kinds = set(kinds) if kinds else None
        out = []
        try:
            with open(self.events_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if start is not None and e["ts"] < start or end is not None and e["ts"] >= end:
                        continue
                    if kinds and e["kind"] not in kinds:
                        continue
                    out.append(e)
        except FileNotFoundError:
            return []
        out.sort(key=lambda e: e["ts"])
        return out[-limit:] if limit else out

    # -- schedule bookkeeping --
    def mark_run(self, job: str, ts: int, ok: bool = True, note: str = "") -> None:
        with self._rlock:
            sched = self.read("schedule", {}) or {}
            sched[job] = {"ts": int(ts), "ok": ok, "note": note[:300]}
            self.write("schedule", sched)

    def last_run(self, job: str) -> int:
        return int(((self.read("schedule", {}) or {}).get(job) or {}).get("ts", 0))

    # -- locking --
    @contextmanager
    def lock(self, timeout: float = 600.0):
        """Exclusive across threads (re-entrant) and across processes (flock / msvcrt)."""
        with self._rlock:
            if self._depth == 0:
                self._fh = open(self.paths.state / ".lock", "a+")
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        if fcntl:
                            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        elif msvcrt:
                            self._fh.seek(0)
                            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            self._fh.close()
                            self._fh = None
                            raise TimeoutError("another bot process holds the state lock")
                        time.sleep(0.25)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fh is not None:
                    try:
                        if fcntl:
                            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                        elif msvcrt:
                            self._fh.seek(0)
                            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    finally:
                        self._fh.close()
                        self._fh = None


def prune_outbox(paths: Paths, keep: int = 60) -> None:
    files = sorted(paths.outbox.glob("*"), key=lambda p: p.stat().st_mtime)
    for p in files[:-keep]:
        try:
            os.unlink(p)
        except OSError:
            pass
