"""
MarkItDown plugin: PDF -> Markdown with headings recovered from font metadata.

The built-in PdfConverter emits plain text (pdfminer / pdfplumber
``extract_text``), which throws away font size and weight, so chapters and
sections never become ``#`` headings. This converter:

1. Groups glyphs into visual lines, merging small-caps / drop-cap glyphs that
   share a baseline (e.g. "C" at 21pt + "APÍTULO" at 15pt -> "CAPÍTULO").
2. Finds the body font size (most common size, weighted by character count).
3. Treats bold lines clearly larger than body text as headings; bold lines at
   body size (e.g. figure captions) become bold paragraphs.
4. Uses the PDF outline (bookmarks), when present, to name and level the
   headings it matches; remaining heading styles are ranked by size below them.
5. Rebuilds paragraphs from first-line indentation, short lines and gaps.

Scope: single-column, text-based PDFs (books, reports). Scanned PDFs fall back
to the built-in converter.
"""

import io
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, BinaryIO

from markitdown import (
    DocumentConverter,
    DocumentConverterResult,
    MarkItDown,
    StreamInfo,
)
from markitdown.converters import PdfConverter

_dependency_exc_info = None
try:
    import pdfplumber
    from pdfminer.utils import decode_text
except ImportError:
    _dependency_exc_info = sys.exc_info()

__plugin_interface_version__ = 1

ACCEPTED_MIME_TYPE_PREFIXES = ["application/pdf", "application/x-pdf"]
ACCEPTED_FILE_EXTENSIONS = [".pdf"]

BOLD_RATIO = 0.7  # share of bold glyphs for a line to count as bold
HEADING_SIZE_RATIO = 1.15  # headings are at least this much larger than body
OUTLINE_MATCH = 0.85  # min similarity between heading text and bookmark title
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.IGNORECASE)


def register_converters(markitdown: MarkItDown, **kwargs: Any) -> None:
    # Lower priority values are tried first; the built-in PdfConverter is 0.0.
    markitdown.register_converter(PdfHeadingsConverter(), priority=-1.0)


@dataclass
class _Line:
    page: int
    text: str
    x0: float
    x1: float
    top: float
    size: float  # dominant glyph size
    max_size: float
    bold: bool

    @property
    def upper(self) -> bool:
        letters = [c for c in self.text if c.isalpha()]
        return bool(letters) and all(c.isupper() for c in letters)


@dataclass
class _Block:
    kind: str  # "heading" | "bold" | "para"
    lines: list[_Line] = field(default_factory=list)
    level: int = 0
    title: str | None = None  # bookmark title, when matched

    @property
    def style(self) -> tuple[int, bool]:
        return (round(self.lines[0].max_size), self.lines[0].upper)


def _make_line(chars: list[dict], page_no: int) -> _Line | None:
    ink = [c for c in chars if c["text"].strip()]
    if not ink:
        return None
    parts: list[str] = []
    prev = None
    pending_space = False
    for c in sorted(chars, key=lambda c: c["x0"]):
        if not c["text"].strip():
            pending_space = True
            continue
        if prev is not None and (
            pending_space
            or c["x0"] - prev["x1"] > 0.15 * min(c["size"], prev["size"])
        ):
            parts.append(" ")
        parts.append(c["text"])
        prev = c
        pending_space = False
    bold_share = sum(bool(_BOLD_FONT.search(c["fontname"])) for c in ink) / len(ink)
    return _Line(
        page=page_no,
        text="".join(parts).strip(),
        x0=min(c["x0"] for c in ink),
        x1=max(c["x1"] for c in ink),
        top=min(c["top"] for c in ink),
        size=Counter(round(c["size"], 1) for c in ink).most_common(1)[0][0],
        max_size=max(c["size"] for c in ink),
        bold=bold_share >= BOLD_RATIO,
    )


def _page_lines(page: Any, page_no: int) -> list[_Line]:
    """Group glyphs into lines. A glyph joins the current line when its vertical
    midpoint falls inside the line's extent, which keeps drop caps and small
    caps (different sizes, same baseline) on one line."""
    chars = [c for c in page.chars if c.get("upright", True)]
    chars.sort(key=lambda c: ((c["top"] + c["bottom"]) / 2, c["x0"]))
    groups: list[list[dict]] = []
    top = bottom = 0.0
    for c in chars:
        mid = (c["top"] + c["bottom"]) / 2
        if groups and top <= mid <= bottom:
            groups[-1].append(c)
            top, bottom = min(top, c["top"]), max(bottom, c["bottom"])
        else:
            groups.append([c])
            top, bottom = c["top"], c["bottom"]
    return [ln for g in groups if (ln := _make_line(g, page_no)) is not None]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]+", " ", s.lower()).strip()


def _outline(pdf: Any) -> list[tuple[int, str]]:
    try:
        entries = []
        for level, title, *_ in pdf.doc.get_outlines():
            if isinstance(title, bytes):
                title = decode_text(title)
            if title and title.strip():
                entries.append((level, re.sub(r"\s+", " ", title).strip()))
        return entries
    except Exception:  # PDFNoOutlines, malformed outlines, ...
        return []


def _join(a: str, b: str) -> str:
    # Keep line-final hyphens and join without a space: correct for compounds,
    # enclitics and ranges ("Grã-|Bretanha", "tornava-|se", "1756-|1763").
    return a + b if a.endswith("-") else f"{a} {b}"


def _block_text(block: _Block) -> str:
    text = ""
    for ln in block.lines:
        text = _join(text, ln.text) if text else ln.text
    return re.sub(r"\s+", " ", text).strip()


class PdfHeadingsConverter(DocumentConverter):
    def accepts(
        self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()
        return extension in ACCEPTED_FILE_EXTENSIONS or any(
            mimetype.startswith(p) for p in ACCEPTED_MIME_TYPE_PREFIXES
        )

    def convert(
        self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any
    ) -> DocumentConverterResult:
        if _dependency_exc_info is not None:
            raise _dependency_exc_info[1].with_traceback(_dependency_exc_info[2])  # type: ignore[union-attr]

        pdf_bytes = io.BytesIO(file_stream.read())
        lines: list[_Line] = []
        with pdfplumber.open(pdf_bytes) as pdf:
            outline = _outline(pdf)
            title = (pdf.metadata or {}).get("Title")
            for i, page in enumerate(pdf.pages):
                lines.extend(_page_lines(page, i + 1))
                page.close()

        if not lines:  # scanned / image-only PDF: let the built-in handle it
            pdf_bytes.seek(0)
            return PdfConverter().convert(pdf_bytes, stream_info, **kwargs)

        blocks = self._blocks(lines)
        self._assign_levels(blocks, outline)

        out: list[str] = []
        for b in blocks:
            text = _block_text(b)
            if b.kind == "heading":
                out.append(f"{'#' * b.level} {b.title or text}")
            elif b.kind == "bold":
                out.append(f"**{text}**")
            else:
                out.append(text)
        return DocumentConverterResult(
            markdown="\n\n".join(out) + "\n",
            title=title if isinstance(title, str) else None,
        )

    def _blocks(self, lines: list[_Line]) -> list[_Block]:
        sizes: Counter[float] = Counter()
        for ln in lines:
            sizes[ln.size] += len(ln.text)
        body_size = sizes.most_common(1)[0][0]

        body = [ln for ln in lines if not ln.bold and abs(ln.size - body_size) < 0.5]
        body = body or lines
        margin = Counter(round(ln.x0) for ln in body).most_common(1)[0][0]
        right = Counter(round(ln.x1) for ln in body).most_common(1)[0][0]
        gaps = Counter(
            round(b.top - a.top)
            for a, b in zip(body, body[1:])
            if a.page == b.page and 0 < b.top - a.top < 3 * body_size
        )
        pitch = gaps.most_common(1)[0][0] if gaps else body_size * 1.4
        # Where text normally starts on a page; a block only carries over a page
        # break when the next page's text starts there (not below a figure).
        page_top: dict[int, float] = {}
        for ln in lines:
            page_top[ln.page] = min(page_top.get(ln.page, ln.top), ln.top)
        top_margin = Counter(round(t) for t in page_top.values()).most_common(1)[0][0]

        def kind(ln: _Line) -> str:
            if ln.bold and ln.max_size >= body_size * HEADING_SIZE_RATIO:
                return "heading"
            return "bold" if ln.bold else "para"

        def continues(block: _Block, ln: _Line) -> bool:
            prev = block.lines[-1]
            same_page = prev.page == ln.page
            gap = ln.top - prev.top
            if block.kind == "heading":
                return (
                    same_page
                    and (round(ln.max_size), ln.upper) == block.style
                    and 0 < gap <= 2 * ln.max_size
                )
            if abs(ln.size - prev.size) > 1:
                return False
            if not same_page and ln.top > top_margin + 0.5 * pitch:
                return False
            if block.kind == "bold":
                return not same_page or 0 < gap <= 1.5 * pitch
            # Paragraph: break on first-line indent, short previous line ending
            # a sentence, or a large vertical gap.
            indent = ln.x0 - margin
            if 0.5 * body_size <= indent <= 6 * body_size and abs(ln.x0 - prev.x0) > 1:
                return False
            if prev.x1 < right - 3 * body_size and prev.text[-1:] in ".!?:”\"'»)":
                return False
            return not (same_page and gap > 1.5 * pitch)

        blocks: list[_Block] = []
        for ln in lines:
            k = kind(ln)
            if blocks and blocks[-1].kind == k and continues(blocks[-1], ln):
                blocks[-1].lines.append(ln)
            else:
                blocks.append(_Block(kind=k, lines=[ln]))
        return blocks

    def _assign_levels(self, blocks: list[_Block], outline: list[tuple[int, str]]) -> None:
        headings = [b for b in blocks if b.kind == "heading"]

        # 1) Match headings to bookmarks, in document order.
        oi = 0
        for b in headings:
            key = _norm(_block_text(b))
            for j in range(oi, len(outline)):
                level, title = outline[j]
                if SequenceMatcher(None, key, _norm(title)).ratio() >= OUTLINE_MATCH:
                    b.level, b.title, oi = level, title, j + 1
                    break

        # 2) Unmatched headings: reuse the level of a matched heading with the
        #    same visual style; rank any other style by size (recurring styles
        #    first, all-caps above mixed case at equal size).
        style_levels: dict[tuple[int, bool], Counter[int]] = {}
        for b in headings:
            if b.level:
                style_levels.setdefault(b.style, Counter())[b.level] += 1
        known = {s: c.most_common(1)[0][0] for s, c in style_levels.items()}
        base = max(known.values(), default=0)

        counts = Counter(b.style for b in headings if not b.level and b.style not in known)
        ranked = sorted(counts, key=lambda s: (counts[s] < 2, -s[0], not s[1]))
        new_levels = {s: min(base + i + 1, 6) for i, s in enumerate(ranked)}

        for b in headings:
            if not b.level:
                b.level = known.get(b.style) or new_levels[b.style]
