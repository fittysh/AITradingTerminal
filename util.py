"""util.py — small shared helpers: number formatting and atomic JSON/text IO."""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


def px(x: float | None) -> str:
    """Price with precision that suits its magnitude (BTC 65,012.50 · SOL 142.3100 · PEPE 0.00000851)."""
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.2f}"
    if a >= 1:
        return f"{x:.4f}"
    if a == 0:
        return "0"
    d = min(10, max(4, -int(math.floor(math.log10(a))) + 3))
    return f"{x:.{d}f}"


def pct(x: float | None, dp: int = 2, signed: bool = True) -> str:
    """Fraction → percent string (0.0123 → +1.23%)."""
    if x is None:
        return "—"
    return f"{x * 100:+.{dp}f}%" if signed else f"{x * 100:.{dp}f}%"


def usd(x: float | None, signed: bool = False, dp: int = 2) -> str:
    if x is None:
        return "—"
    if signed:
        sign = "+" if x > 0 else ("−" if x < 0 else "")
        return f"{sign}${abs(x):,.{dp}f}"
    return f"-${abs(x):,.{dp}f}" if x < 0 else f"${x:,.{dp}f}"


def rmult(x: float | None, dp: int = 2) -> str:
    return "—" if x is None else f"{x:+.{dp}f}R"


def compact(x: float | None) -> str:
    """$45.2M style volume."""
    if x is None:
        return "—"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.1f}{unit}"
    return f"${x:.0f}"


def sig(x: float | None, n: int = 6) -> float | None:
    """Round to n significant digits (keeps JSON payloads compact)."""
    if x is None or x == 0 or not math.isfinite(x):
        return x
    return round(x, n - 1 - int(math.floor(math.log10(abs(x)))))


def ascii_text(s: str) -> str:
    """Fold the few typographic symbols we use into ASCII (PDF core fonts / plain terminals)."""
    table = {"−": "-", "–": "-", "—": "-", "→": "->", "←": "<-", "≥": ">=", "≤": "<=", "×": "x",
             "·": "|", "…": "...", "✓": "OK", "✗": "x", "•": "*", "±": "+/-", "’": "'", "“": '"', "”": '"'}
    out = "".join(table.get(ch, ch) for ch in s)
    return out.encode("latin-1", "replace").decode("latin-1")


def atomic_write(path: Path | str, text: str) -> None:
    """Write via temp file + rename so readers (TUI, dashboard server) never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path | str, obj: Any, indent: int | None = 1) -> None:
    atomic_write(path, dumps(obj, indent=indent))


def dumps(obj: Any, indent: int | None = None) -> str:
    """Strict JSON: NaN/inf become null so the files stay readable by browsers and other tools."""
    return json.dumps(_finite(obj), indent=indent, default=_json_default, allow_nan=False)


def _finite(o: Any) -> Any:
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    if hasattr(o, "to_dict"):
        return _finite(o.to_dict())
    return o


def read_json(path: Path | str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def _json_default(o: Any) -> Any:
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, float) and not math.isfinite(o):
        return None
    raise TypeError(f"not JSON serializable: {type(o).__name__}")
