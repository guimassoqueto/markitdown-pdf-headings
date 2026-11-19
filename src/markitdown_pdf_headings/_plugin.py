"""
MarkItDown plugin: PDF -> Markdown with headings recovered from font metadata.

The built-in PdfConverter emits plain text (pdfminer / pdfplumber
``extract_text``), which throws away font size and weight, so chapters and
sections never become ``#`` headings. This converter:

1. Groups glyphs into visual lines, merging small-caps / drop-cap glyphs that
   share a baseline (e.g. "C" at 21pt + "APÍTULO" at 15pt -> "CAPÍTULO").
2. Finds the body font size (most common size, weighted by character count).
3. Drops running heads and page numbers: lines set apart at the top or bottom
   of pages whose text repeats, or whose number follows the page number.
4. Treats lines set entirely in a monospace font as code, kept line by line
   with their indentation in fenced blocks; monospace words in prose go in
   backticks.
5. Treats bold lines clearly larger than body text as headings; bold lines at
   body size (e.g. figure captions) become bold paragraphs, except short titles
   that open a paragraph, which become headings one level down.
6. Uses the PDF outline (bookmarks), when present, to name and level the
   headings it matches; remaining heading styles are ranked by size below them.
7. Rebuilds paragraphs from first-line indentation, short lines and gaps, also
   across page breaks, and joins words a line-final hyphen broke when the
   document spells them whole elsewhere.

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
from statistics import median
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
RUNNING_REPEATS = 3  # page-edge text seen this often, numbers aside, is a running head
RUNNING_SHARE = 0.2  # running heads sit at one height on at least this share of pages
RUN_IN_MAX_CHARS = 60  # longest bold line that can be a run-in heading
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.IGNORECASE)
# Monospace by name (Courier, DejaVuSansMono, TeX's CMTT10 and SFTT1000), for
# fonts whose glyphs in the document are too few to measure. Not "MSTT31c4cb00":
# that is any TrueType font a Windows printer driver embedded.
_MONO_FONT = re.compile(
    r"mono|courier|consol|typewriter|cursor|menlo|monaco|code|(?-i:(?:CM|SF|EC|LM)[A-Z]*TT\d)",
    re.IGNORECASE,
)
# Glyphs that a proportional font draws narrower than _WIDE ones.
_NARROW = frozenset("il.,:;!|'")
_WIDE = frozenset("mwMW@")
_URL_BREAKS = frozenset("/.:_-?=&#@~%")
_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)\S*$", re.IGNORECASE)
_CAPTION = re.compile(r"\w+\.?\s+[A-Z]?[\d.–-]*\d\b")  # "Figure 2.1:", "Tabela 3 –"
_BULLETS = frozenset("•◦▪▫‣∙●○■□")
_LIST_ITEM = re.compile(r"(?:[•◦▪▫‣∙●○■□–—*-]|\(?(?:\d{1,3}|[a-z])[.)])\s+", re.IGNORECASE)
_ROMAN = re.compile(r"[ivxlcdm]+", re.IGNORECASE)
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_WORD = re.compile(r"\w+(?:-\w+)*")


def register_converters(markitdown: MarkItDown, **kwargs: Any) -> None:
    # Lower priority values are tried first; the built-in PdfConverter is 0.0.
    markitdown.register_converter(PdfHeadingsConverter(), priority=-1.0)


@dataclass(slots=True)
class _Glyph:
    x0: float
    x1: float
    text: str
    font: str
    size: float


Run = tuple[str, bool]  # text, and whether it is set in a monospace font


@dataclass
class _Line:
    page: int
    glyphs: list[_Glyph]  # left to right, spaces included
    x0: float
    x1: float
    top: float
    baseline: float
    size: float  # dominant glyph size
    max_size: float
    bold: bool
    runs: list[Run] = field(default_factory=list)  # set once monospace fonts are known
    text: str = ""
    code: bool = False

    @property
    def upper(self) -> bool:
        letters = [c for c in self.text if c.isalpha()]
        return bool(letters) and all(c.isupper() for c in letters)


@dataclass
class _Block:
    kind: str  # "heading" | "bold" | "para" | "code"
    lines: list[_Line] = field(default_factory=list)
    level: int = 0
    title: str | None = None  # bookmark title, when matched

    @property
    def style(self) -> tuple[int, bool]:
        return (round(self.lines[0].max_size), self.lines[0].upper)


@dataclass
class _Layout:
    body_size: float
    pitch: float  # distance between consecutive body lines
    left: dict[int, float]  # where lines start, by page parity: books mirror margins
    right: dict[int, float]  # where full lines end, by page parity
    code_pitch: dict[float, float] = field(default_factory=dict)  # by font size

    def code_line_pitch(self, size: float) -> float:
        return self.code_pitch.get(size) or self.pitch * size / (self.body_size or size or 1)


def _make_line(chars: list[dict], page_no: int) -> _Line | None:
    ink = [c for c in chars if c["text"].strip()]
    if not ink:
        return None
    bold_share = sum(bool(_BOLD_FONT.search(c["fontname"])) for c in ink) / len(ink)
    return _Line(
        page=page_no,
        glyphs=[
            _Glyph(c["x0"], c["x1"], c["text"], c["fontname"], c["size"])
            for c in sorted(chars, key=lambda c: c["x0"])
        ],
        x0=min(c["x0"] for c in ink),
        x1=max(c["x1"] for c in ink),
        top=min(c["top"] for c in ink),
        baseline=Counter(round(_baseline(c), 1) for c in ink).most_common(1)[0][0],
        size=Counter(round(c["size"], 1) for c in ink).most_common(1)[0][0],
        max_size=max(c["size"] for c in ink),
        bold=bold_share >= BOLD_RATIO,
    )


def _baseline(char: dict) -> float:
    """Distance from the top of the page to the glyph's baseline. Unlike ``top``,
    it doesn't depend on the font, so it measures line spacing across fonts."""
    matrix = char.get("matrix")
    return char["bottom"] - (matrix[5] - char["y0"]) if matrix else char["bottom"]


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


def _monospace_fonts(lines: list[_Line]) -> set[str]:
    """Fonts whose glyphs all have one width. A font is measured once it has drawn
    both narrow and wide glyphs; until then its name decides. The body font is
    never one: a typewritten document has no code to set apart."""
    count: Counter[str] = Counter()
    widths: dict[str, dict[str, float]] = {}
    for ln in lines:
        for g in ln.glyphs:
            if g.text.strip():
                count[g.font] += 1
                if len(g.text) == 1 and g.size > 0:
                    widths.setdefault(g.font, {}).setdefault(g.text, (g.x1 - g.x0) / g.size)
    mono = set()
    for font in count:
        seen = widths.get(font, {})
        if seen.keys() & _NARROW and seen.keys() & _WIDE:
            width = median(seen.values())
            alike = sum(abs(w - width) <= 0.03 * width for w in seen.values())
            fixed = 0.35 <= width <= 0.75 and alike >= 0.9 * len(seen)
        else:
            fixed = bool(_MONO_FONT.search(font.split("+", 1)[-1]))
        if fixed:
            mono.add(font)
    return set() if not count or count.most_common(1)[0][0] in mono else mono


def _runs(ln: _Line, mono: set[str]) -> list[Run]:
    runs: list[Run] = []
    prev: _Glyph | None = None
    space: _Glyph | None = None  # a space glyph since the previous glyph
    for g in ln.glyphs:
        if not g.text.strip():
            space = space or g
            continue
        is_mono = g.font in mono
        if prev is not None:
            both = is_mono and prev.font in mono
            gap, cell = g.x0 - prev.x1, prev.x1 - prev.x0
            # A URL stretches after its break points ("https: // example. org"),
            # by less than the whole glyph width a monospace space takes.
            if both and prev.text in _URL_BREAKS:
                limit = 0.5 * cell
            else:
                limit = 0.15 * min(g.size, prev.size)
            if space is not None or gap > limit:
                # A space set in the monospace font, or as wide as its glyphs,
                # is code's own; any other one is prose, outside the backticks.
                if space is not None:
                    inside = both and space.font in mono
                else:
                    cells = gap / cell if cell > 0 else 0
                    inside = both and round(cells) >= 1 and abs(cells - round(cells)) < 0.15
                runs.append((" ", inside))
        runs.append((g.text, is_mono))
        prev = g
        space = None
    return runs


def _markdown(runs: list[Run]) -> str:
    """Join runs, putting monospace ones in backticks."""
    merged: list[list[Any]] = []
    for text, mono in runs:
        if merged and merged[-1][1] == mono:
            merged[-1][0] += text
        else:
            merged.append([text, mono])
    out = []
    for text, mono in merged:
        if mono:
            ticks = "`" * (max(map(len, re.findall("`+", text)), default=0) + 1)
            pad = " " if text.startswith("`") or text.endswith("`") else ""
            text = f"{ticks}{pad}{text}{pad}{ticks}"
        out.append(text)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _measure(lines: list[_Line]) -> _Layout:
    sizes: Counter[float] = Counter()
    for ln in lines:
        sizes[ln.size] += len(ln.text)
    body_size = sizes.most_common(1)[0][0]

    body = [ln for ln in lines if not ln.bold and abs(ln.size - body_size) < 0.5]
    body = body or lines
    gaps = Counter(
        round(b.top - a.top)
        for a, b in zip(body, body[1:])
        if a.page == b.page and 0 < b.top - a.top < 3 * body_size
    )
    pitch = gaps.most_common(1)[0][0] if gaps else body_size * 1.4
    left, right = {}, {}
    for parity in (0, 1):
        side = [ln for ln in body if ln.page % 2 == parity] or body
        left[parity] = Counter(round(ln.x0) for ln in side).most_common(1)[0][0]
        right[parity] = Counter(round(ln.x1) for ln in side).most_common(1)[0][0]
    return _Layout(body_size, pitch, left, right)


def _mark_code(lines: list[_Line], mono: set[str], layout: _Layout) -> None:
    """Flag lines set entirely in a monospace font as code. One that carries on
    a prose line, at the usual spacing, is a long name or URL that wrapped."""
    if not mono:
        return
    prev = None
    for ln in lines:
        if all(g.font in mono for g in ln.glyphs if g.text.strip()) and (
            ln.max_size < layout.body_size * HEADING_SIZE_RATIO
        ):
            ln.code = not (
                prev is not None
                and prev.page == ln.page
                and not prev.code
                and not prev.bold
                and 0 < ln.baseline - prev.baseline <= 1.1 * layout.pitch
                and prev.text[-1:] not in ".:;!?"
            )
        prev = ln
    # Line spacing of code in each size, to tell blank lines inside code apart.
    gaps: dict[float, Counter[float]] = {}
    for a, b in zip(lines, lines[1:]):
        if a.code and b.code and a.page == b.page and a.size == b.size and b.baseline > a.baseline:
            gaps.setdefault(a.size, Counter())[round(b.baseline - a.baseline, 1)] += 1
    layout.code_pitch = {size: c.most_common(1)[0][0] for size, c in gaps.items()}


def _roman(numeral: str) -> int:
    values = [_ROMAN_VALUES[c] for c in numeral.lower()]
    return sum(-v if v < nxt else v for v, nxt in zip(values, [*values[1:], 0]))


def _edge_numbers(text: str) -> set[tuple[str, int]]:
    """The numbers at either end of a line: a running head's page number."""
    words = text.split()
    found = set()
    for word in {words[0], words[-1]} if words else set():
        word = word.strip(".,;:()[]|—–-")
        if word.isdecimal():
            found.add(("arabic", int(word)))
        elif _ROMAN.fullmatch(word):
            found.add(("roman", _roman(word)))
    return found


def _running_key(text: str) -> str:
    """A running head's text without its page number, so that it repeats."""
    words = text.casefold().split()
    for i in {0, len(words) - 1} if words else set():
        if _edge_numbers(words[i]):
            words[i] = "#"
    return re.sub(r"\d+", "#", " ".join(words))


def _drop_running_heads(lines: list[_Line], layout: _Layout) -> list[_Line]:
    """Drop running heads, running feet and page numbers.

    Candidates are the first and last lines of pages, set apart from the text
    and at the height where such lines sit on many pages. One is running when
    its text repeats on other pages, numbers aside, or when a number at either
    end follows the page number at a fixed distance: "2.8. Debugging 13" never
    repeats, but it sits on page 35, as "2.9. Glossary 15" sits on page 37.
    Lines larger than body text are never candidates: a "Glossary" heading at
    the top of a page is set apart like a running head, and recurs like one.
    """
    pages: dict[int, list[_Line]] = {}
    for ln in lines:
        pages.setdefault(ln.page, []).append(ln)

    def apart(ln: _Line, neighbour: _Line | None) -> bool:
        if ln.code or ln.max_size >= layout.body_size * HEADING_SIZE_RATIO:
            return False
        return neighbour is None or abs(ln.baseline - neighbour.baseline) > 1.3 * layout.pitch

    tops, feet = [], []
    for pl in pages.values():
        if apart(pl[0], pl[1] if len(pl) > 1 else None):
            tops.append(pl[0])
        if apart(pl[-1], pl[-2] if len(pl) > 1 else None):
            feet.append(pl[-1])
    candidates: list[_Line] = []
    for edge in (tops, feet):
        if edge:
            height, count = Counter(round(ln.baseline) for ln in edge).most_common(1)[0]
            if count >= max(RUNNING_REPEATS, RUNNING_SHARE * len(pages)):
                candidates += [ln for ln in edge if abs(ln.baseline - height) <= 2]

    repeats = Counter(_running_key(ln.text) for ln in candidates)
    votes = Counter(
        (kind, ln.page - n) for ln in candidates for kind, n in _edge_numbers(ln.text)
    )
    offsets = {offset for offset, n in votes.items() if n >= RUNNING_REPEATS}
    running = {
        id(ln)
        for ln in candidates
        if repeats[_running_key(ln.text)] >= RUNNING_REPEATS
        or any((kind, ln.page - n) in offsets for kind, n in _edge_numbers(ln.text))
    }
    # A document of nothing but such lines (one repeated line per page) keeps them.
    return [ln for ln in lines if id(ln) not in running] or lines


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


def _block_text(block: _Block) -> str:
    text = ""
    for ln in block.lines:
        if not text:
            text = ln.text
        else:
            text = text + ln.text if text.endswith("-") else f"{text} {ln.text}"
    return re.sub(r"\s+", " ", text).strip()


class _Hyphens:
    """Decides, at a line-final hyphen, whether it breaks a word in two.

    The document's own spelling decides: "exam-|ple" joins when "example"
    appears elsewhere, and "built-|in" keeps its hyphen when "built-in" appears
    inside a line. A number on either side keeps it ("1756-|1763"), and so does
    a word the document never spells either way ("tornava-|se").
    """

    def __init__(self, blocks: list[_Block]) -> None:
        self.words: set[str] = set()
        self.compounds: set[str] = set()
        for b in blocks:
            if b.kind == "code":
                continue
            prev = None
            for ln in b.lines:
                words = _WORD.findall(ln.text)
                if re.search(r"\w-$", ln.text):
                    words = words[:-1]  # the broken word's first half
                if prev is not None and re.search(r"\w-$", prev.text) and ln.text[:1].isalnum():
                    words = words[1:]  # and its second half
                for word in map(str.casefold, words):
                    self.words.add(word)
                    if "-" in word:
                        self.compounds.add(word)
                prev = ln

    def joins(self, before: str, after: str) -> bool:
        left = re.search(r"(\w+(?:-\w+)*)-$", before)
        right = _WORD.match(after)
        if not left or not right:
            return False
        left_word, right_word = left.group(1), right.group(0)
        if left_word[-1].isdigit() or right_word[0].isdigit():
            return False
        if f"{left_word}-{right_word}".casefold() in self.compounds:
            return False
        return f"{left_word}{right_word}".casefold() in self.words


def _code_run(runs: list[Run], end: int) -> str:
    """The monospace text at one end of a list of runs (end: 0 or -1)."""
    found = []
    for text, mono in runs if end == 0 else reversed(runs):
        if not mono:
            break
        found.append(text)
    return "".join(found if end == 0 else reversed(found))


def _render(block: _Block, hyphens: _Hyphens) -> str:
    runs: list[Run] = []
    prev = None
    for ln in block.lines:
        if prev is not None:
            last, last_mono = runs[-1]
            if last_mono and last[-1:] in _URL_BREAKS and _URL.search(
                _code_run(runs, -1) + _code_run(ln.runs, 0)
            ):
                pass  # a URL that wrapped: "https://example.org/" + "page.html"
            elif not prev.text.endswith("-"):
                runs.append((" ", False))
            elif not last_mono and hyphens.joins(prev.text, ln.text):
                runs[-1] = (last[:-1], last_mono)
        runs.extend(ln.runs)
        prev = ln
    return _markdown(runs)


def _code(block: _Block, layout: _Layout) -> str:
    """A fenced code block. Indentation comes from each line's start, in glyph
    widths from the block's leftmost line; blank lines from the gaps."""
    cell = median(g.x1 - g.x0 for ln in block.lines for g in ln.glyphs if g.text.strip())
    cell = cell if cell > 0 else 0.5 * layout.body_size  # a font without widths
    indent = min(ln.x0 - layout.left[ln.page % 2] for ln in block.lines)
    rows: list[str] = []
    prev = None
    for ln in block.lines:
        if prev is not None and prev.page == ln.page:
            gap = (ln.baseline - prev.baseline) / layout.code_line_pitch(ln.size)
            rows += [""] * max(0, round(gap) - 1)
        start = layout.left[ln.page % 2] + indent
        row, column = "", 0
        for g in ln.glyphs:
            if g.text.strip():
                at = max(column, round((g.x0 - start) / cell))
                row += " " * (at - column) + g.text
                column = at + max(1, round((g.x1 - g.x0) / cell))
        rows.append(row.rstrip())
        prev = ln
    body = "\n".join(rows)
    fence = "`" * max(3, max(map(len, re.findall("`+", body)), default=0) + 1)
    return f"{fence}\n{body}\n{fence}"


def _item_text(ln: _Line) -> float | None:
    """Where a list item's text starts, after its bullet or number."""
    marker = _LIST_ITEM.match(ln.text)
    if not marker:
        return None
    ink = [g for g in ln.glyphs if g.text.strip()]
    skip = sum(not c.isspace() for c in marker.group(0))
    return ink[skip].x0 if len(ink) > skip else None


def _escape(text: str) -> str:
    """Keep a paragraph from turning into a heading or a quote: "# comment"."""
    return "\\" + text if text.startswith(("#", ">")) else text


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

        mono = _monospace_fonts(lines)
        for ln in lines:
            ln.runs = _runs(ln, mono)
            ln.text = "".join(text for text, _ in ln.runs)
        layout = _measure(lines)
        _mark_code(lines, mono, layout)
        lines = _drop_running_heads(lines, layout)

        blocks = self._blocks(lines, layout)
        self._assign_levels(blocks, outline)
        self._run_in_headings(blocks, layout)
        hyphens = _Hyphens(blocks)

        out: list[str] = []
        for b in blocks:
            if b.kind == "code":
                out.append(_code(b, layout))
            elif b.kind == "heading":
                out.append(f"{'#' * b.level} {b.title or _render(b, hyphens)}")
            elif b.kind == "bold":
                out.append(f"**{_render(b, hyphens)}**")
            else:
                out.append(_escape(_render(b, hyphens)))
        return DocumentConverterResult(
            markdown="\n\n".join(out) + "\n",
            title=title if isinstance(title, str) else None,
        )

    def _blocks(self, lines: list[_Line], layout: _Layout) -> list[_Block]:
        body_size, pitch = layout.body_size, layout.pitch
        # Where text normally starts on a page; a block only carries over a page
        # break when the next page's text starts there (not below a figure).
        page_top: dict[int, float] = {}
        for ln in lines:
            page_top[ln.page] = min(page_top.get(ln.page, ln.top), ln.top)
        top_margin = Counter(round(t) for t in page_top.values()).most_common(1)[0][0]

        def kind(ln: _Line) -> str:
            if ln.code:
                return "code"
            if ln.bold and ln.max_size >= body_size * HEADING_SIZE_RATIO:
                return "heading"
            return "bold" if ln.bold else "para"

        def inline_bold(block: _Block, ln: _Line) -> bool:
            # A bold term that fills a line of its own, mid-sentence: the line
            # before it runs to the right margin without ending a sentence, at
            # the usual spacing. A bold title follows a sentence's end.
            prev = block.lines[-1]
            return (
                prev.page == ln.page
                and 0 < ln.baseline - prev.baseline <= 1.2 * pitch * prev.size / (body_size or 1)
                and (prev.x1 >= layout.right[prev.page % 2] - body_size or prev.text.endswith("-"))
                and prev.text.rstrip("”\"'’»)]")[-1:] not in ".!?:"
            )

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
            if not same_page and ln.top > top_margin + 0.5 * pitch:
                return False
            if block.kind == "code":
                # Blank lines inside code widen the gap; a figure or a new block doesn't.
                return not same_page or 0 < ln.baseline - prev.baseline <= 3.5 * pitch
            # Text cut by a page break runs along the margin, unlike a title
            # page's centered lines.
            if not same_page and prev.x0 > layout.left[prev.page % 2] + 6 * body_size:
                return False
            if abs(ln.size - prev.size) > 1:
                return False
            if block.kind == "bold":
                return not same_page or 0 < gap <= 1.5 * pitch
            # Paragraph: break on first-line indent, short previous line ending
            # a sentence, or a large vertical gap. A list item's next lines are
            # indented to its text, after the bullet or number; a bullet starts
            # a new item.
            if ln.text[:1] in _BULLETS:
                return False
            indent = ln.x0 - layout.left[ln.page % 2]
            first = block.lines[0]
            item = _item_text(first)
            if item is not None:
                first_indent = first.x0 - layout.left[first.page % 2]
                if _LIST_ITEM.match(ln.text) and abs(indent - first_indent) <= 1:
                    return False  # the next item: "2." where "1." was
            # Justification stretches the space after the number, not the indent.
            in_item = (
                item is not None
                and abs(indent - (item - layout.left[first.page % 2])) <= 0.5 * body_size
            )
            if not in_item and 0.5 * body_size <= indent <= 6 * body_size and abs(ln.x0 - prev.x0) > 1:
                return False
            short = prev.x1 < layout.right[prev.page % 2] - 3 * body_size
            if short and prev.text[-1:] in ".!?:”\"'»)":
                return False
            return not (same_page and gap > 1.5 * pitch)

        blocks: list[_Block] = []
        for ln in lines:
            k = kind(ln)
            if k == "bold" and blocks and blocks[-1].kind == "para" and inline_bold(blocks[-1], ln):
                k = "para"
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

    def _run_in_headings(self, blocks: list[_Block], layout: _Layout) -> None:
        """A bold body-size line that stands alone flush left, short and without
        final punctuation, and opens an ordinary paragraph, titles it: a heading
        one level below the current one. Captions ("Figure 2.1: ...") stay bold."""
        level = 0
        for b, after in zip(blocks, blocks[1:]):
            if b.kind == "heading":
                level = b.level
                continue
            if b.kind != "bold" or len(b.lines) != 1 or after.kind != "para":
                continue
            ln = b.lines[0]
            if (
                len(ln.text) <= RUN_IN_MAX_CHARS
                and ln.text[:1].isupper()
                and ln.text[-1:].isalpha()
                and not _CAPTION.match(ln.text)
                and abs(ln.x0 - layout.left[ln.page % 2]) <= 1
            ):
                b.kind, b.level = "heading", min(level + 1, 6)
