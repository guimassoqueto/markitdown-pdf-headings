# markitdown-pdf-headings

MarkItDown plugin that converts text-based PDFs (books, reports) to Markdown
**with headings**. The built-in PdfConverter only emits plain text, so chapters
and sections never become `#` headings.

How it finds structure:

- **PDF bookmarks (outline)** name and level the headings they match
  (e.g. `# Capítulo 1: A grande divergência`).
- **Bold lines larger than the body font** become headings; other heading
  styles are ranked by size below the bookmark levels (e.g. `## Salários reais`).
- **Bold body-size lines** (figure captions) become `**bold**` paragraphs. A
  short one that stands alone flush left, without final punctuation, and opens
  a paragraph is a run-in title instead: a heading one level below the current
  one (`Infinite Loop` in *Think Python*'s Appendix A). Captions
  (`Figure 2.1: …`) stay bold.
- **Code**: lines set entirely in a monospace font become fenced code blocks,
  one line per line, with their indentation rebuilt from the glyphs' positions.
  A font is monospace when its glyphs share one width (or, for a font with too
  few glyphs to tell, when its name says so: `Courier`, `…Mono`, TeX's `SFTT1000`).
  Monospace words inside prose go in backticks: `` `__init__` ``.
- **Running heads and page numbers** are dropped: lines at the top or bottom of
  pages, set apart from the text at the height where such lines sit on many
  pages, whose text repeats or whose number follows the page number
  (`2.8. Debugging 13` sits on page 35, `2.9. Glossary 15` on page 37).
- Paragraphs are rebuilt from first-line indentation, also across page breaks;
  a list item's lines stay together.
- **Line-final hyphens** are joined when the book spells the word whole
  elsewhere (`exam-ple` → `example`), and kept when it writes the hyphenated
  form inside a line (`built-in`), when there is a number on either side
  (`1756-1763`), or when there is no evidence either way (`tornava-se`).

## Install

Install it into the **same Python environment as markitdown**. Use the line
that matches how markitdown is installed:

| markitdown installed with | Install the plugin with |
|---|---|
| `pip` (inside a venv) | `pip install git+https://github.com/guimassoqueto/markitdown-pdf-headings` |
| `pipx` | `pipx inject markitdown git+https://github.com/guimassoqueto/markitdown-pdf-headings` |
| `uv tool` | `uv tool install 'markitdown[pdf]' --with git+https://github.com/guimassoqueto/markitdown-pdf-headings --force` |

Not sure which environment that is? `head -1 "$(which markitdown)"` shows the
Python interpreter markitdown runs on.

Check that it's picked up:

```bash
markitdown --list-plugins   # should show: pdf_headings
```

## Use

```bash
markitdown --use-plugins book.pdf -o book.md

# every PDF in a folder
for pdf in books/*.pdf; do markitdown --use-plugins "$pdf" -o "${pdf%.pdf}.md"; done
```

Leave out `--use-plugins` for forms, invoices and tables; the built-in
converter handles those better.

## Limits

- Single-column, text-based PDFs. Scanned PDFs fall back to the built-in
  converter. A two-column page, such as an index, comes out with its columns'
  lines interleaved.
- Headings must be **bold** and larger than body text. For books that use
  non-bold headings, tune `BOLD_RATIO` / `HEADING_SIZE_RATIO` in `_plugin.py`.
- Figures and tables stored as images are not extracted (same as the built-in).
- A word broken across lines that the book spells nowhere else keeps its
  hyphen (`lexi-con`), since the book gives no evidence that it isn't a
  compound.

## Checking a new book

Count headings, outside code blocks, where a Python comment is a line like
`# inside class Time:`, and compare word coverage against the built-in
converter. Running heads, page numbers and the halves of rejoined words count
as missing:

```bash
awk '/^```/{code=!code; next} !code' book.md | grep -c '^# '
markitdown book.pdf -o /tmp/builtin.md   # without the plugin
python - /tmp/builtin.md book.md <<'EOF'
import sys, re, collections
tok = lambda p: collections.Counter(re.findall(r"\w+", open(p).read().lower()))
a, b = tok(sys.argv[1]), tok(sys.argv[2])
print("missing:", sum((a - b).values()), "extra:", sum((b - a).values()))
EOF
```

## Development

The tests build lines by hand, so they need no PDF. Run them in the
environment that has markitdown:

```bash
pip install pytest
pytest
```
