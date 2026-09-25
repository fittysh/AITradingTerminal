"""pdfgen.py — a minimal PDF writer (stdlib only) for the daily report.

Supports A4 pages, the PDF core fonts (Helvetica, Helvetica-Bold, Courier, Courier-Bold) with
WinAnsi encoding, filled/stroked rectangles, lines and polylines. Content streams are Flate-compressed.
Text is folded to Latin-1 first (see util.ascii_text), so arbitrary strings are safe.
"""
from __future__ import annotations

import zlib
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from util import ascii_text

Color = Tuple[float, float, float]
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier", "F4": "Courier-Bold"}

# Helvetica advance widths (1/1000 em) for ASCII 32..126 — used for right alignment
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
         556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,
         722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
         278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _rgb(c: Color) -> str:
    return f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f}"


class PDF:
    W, H = 595.28, 841.89

    def __init__(self, title: str = "", author: str = "AITradingTerminal"):
        self.title, self.author = title, author
        self.pages: List[List[str]] = []
        self.ops: List[str] = []

    def add_page(self) -> None:
        self.ops = []
        self.pages.append(self.ops)

    @staticmethod
    def width(s: str, size: float, font: str = "F1") -> float:
        s = ascii_text(s)
        if font in ("F3", "F4"):
            return 0.6 * size * len(s)
        scale = 1.05 if font == "F2" else 1.0
        return sum(_HELV[ord(ch) - 32] if 32 <= ord(ch) <= 126 else 556 for ch in s) * size / 1000 * scale

    def text(self, x: float, y: float, s: str, size: float = 10, font: str = "F1",
             color: Color = (0, 0, 0), align: str = "left") -> None:
        s = ascii_text(str(s))
        if align != "left":
            w = self.width(s, size, font)
            x -= w if align == "right" else w / 2
        self.ops.append(f"BT /{font} {size:.2f} Tf {_rgb(color)} rg {x:.2f} {y:.2f} Td ({_esc(s)}) Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, fill: Optional[Color] = None,
             stroke: Optional[Color] = None, lw: float = 0.5) -> None:
        ops = [f"{lw:.2f} w"]
        if fill:
            ops.append(f"{_rgb(fill)} rg")
        if stroke:
            ops.append(f"{_rgb(stroke)} RG")
        ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {'B' if fill and stroke else 'f' if fill else 'S'}")
        self.ops.append(" ".join(ops))

    def line(self, x1: float, y1: float, x2: float, y2: float, color: Color = (0, 0, 0), lw: float = 0.5,
             dash: Optional[Tuple[float, float]] = None) -> None:
        d = f"[{dash[0]:.1f} {dash[1]:.1f}] 0 d " if dash else "[] 0 d "
        self.ops.append(f"{d}{lw:.2f} w {_rgb(color)} RG {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S [] 0 d")

    def polyline(self, pts: Sequence[Tuple[float, float]], color: Color = (0, 0, 0), lw: float = 1.0) -> None:
        if len(pts) < 2:
            return
        path = " ".join(f"{x:.2f} {y:.2f} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(pts))
        self.ops.append(f"1 j {lw:.2f} w {_rgb(color)} RG {path} S")

    def save(self, path: Path | str) -> Path:
        objs: List[bytes] = []

        def add(body: bytes) -> int:
            objs.append(body)
            return len(objs)

        catalog = add(b"")                                 # placeholders, filled once ids are known
        pages_id = add(b"")
        font_ids = {k: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{v} /Encoding /WinAnsiEncoding >>".encode())
                    for k, v in FONTS.items()}
        info = add(f"<< /Title ({_esc(ascii_text(self.title))}) /Author ({_esc(self.author)}) "
                   f"/Producer (AITradingTerminal pdfgen) >>".encode("latin-1"))
        fonts = " ".join(f"/{k} {i} 0 R" for k, i in font_ids.items())
        kids = []
        for ops in self.pages or [[]]:
            data = zlib.compress("\n".join(ops).encode("latin-1"))
            content = add(f"<< /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode() + data + b"\nendstream")
            kids.append(add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {self.W} {self.H}] "
                            f"/Resources << /Font << {fonts} >> >> /Contents {content} 0 R >>".encode()))
        objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        objs[pages_id - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                              f"/Count {len(kids)} >>").encode()
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(out))
        return path
