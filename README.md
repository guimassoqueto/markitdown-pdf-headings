# markitdown-pdf-headings

MarkItDown plugin that converts text-based PDFs (books, reports) to Markdown
**with headings**. The built-in PdfConverter only emits plain text, so chapters
and sections never become `#` headings.

How it finds structure:

- **PDF bookmarks (outline)** name and level the headings they match
  (e.g. `# Capítulo 1: A grande divergência`).
- **Bold lines larger than the body font** become headings; other heading
  styles are ranked by size below the bookmark levels (e.g. `## Salários reais`).
- **Bold body-size lines** (figure captions) become `**bold**` paragraphs.
- Paragraphs are rebuilt from first-line indentation; line-final hyphens are kept
  (`Grã-Bretanha`, `tornava-se`, `1756-1763`).

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

- Single-column, text-based PDFs. Scanned PDFs fall back to the built-in converter.
- Headings must be **bold** and larger than body text. For books that use
  non-bold headings, tune `BOLD_RATIO` / `HEADING_SIZE_RATIO` in `_plugin.py`.
- Figures and tables stored as images are not extracted (same as the built-in).

## Checking a new book

Count headings and compare word coverage against the built-in converter:

```bash
grep -c '^# ' book.md; grep -c '^## ' book.md
markitdown book.pdf -o /tmp/builtin.md   # without the plugin
python - /tmp/builtin.md book.md <<'EOF'
import sys, re, collections
tok = lambda p: collections.Counter(re.findall(r"\w+", open(p).read().lower()))
a, b = tok(sys.argv[1]), tok(sys.argv[2])
print("missing:", sum((a - b).values()), "extra:", sum((b - a).values()))
EOF
```
