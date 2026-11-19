"""Rules of the converter, on lines built by hand rather than read from a PDF."""

from markitdown_pdf_headings import _plugin as P

BODY = "ABCDEF+URWPalladioL-Roma"
BOLD = "ABCDEF+URWPalladioL-Bold"
CODE = "ABCDEF+SFTT1000"
CELL = 5.25  # one glyph of CODE at 10 pt


def line(text, *, page=1, x=100.0, baseline=100.0, font=BODY, size=10.0, bold=False, mono=frozenset({CODE})):
    """A line of glyphs, monospace ones CELL wide and others 5 pt; a space is a gap."""
    glyphs, pos = [], x
    for ch in text:
        width = CELL if font in mono else 5.0
        if ch != " ":
            glyphs.append(P._Glyph(pos, pos + width, ch, font, size))
        pos += width
    ln = P._Line(
        page=page,
        glyphs=glyphs,
        x0=x,
        x1=pos,
        top=baseline - size,
        baseline=baseline,
        size=size,
        max_size=size,
        bold=bold,
    )
    ln.runs = P._runs(ln, set(mono))
    ln.text = "".join(t for t, _ in ln.runs)
    return ln


def joined(*parts):
    """One line from (text, font) parts set side by side."""
    lines, x = [], 100.0
    for text, font in parts:
        lines.append(line(text, x=x, font=font))
        x = lines[-1].x1 + 2.5  # a space between parts
    first = lines[0]
    first.glyphs = [g for ln in lines for g in ln.glyphs]
    first.x1 = lines[-1].x1
    first.runs = P._runs(first, {CODE})
    first.text = "".join(t for t, _ in first.runs)
    return first


def layout(**overrides):
    values = dict(body_size=10.0, pitch=12.0, left={0: 100.0, 1: 100.0}, right={0: 500.0, 1: 500.0})
    values.update(overrides)
    return P._Layout(**values)


# Monospace fonts


def glyph_line(font, widths):
    glyphs = [P._Glyph(i * 10.0, i * 10.0 + w, ch, font, 10.0) for i, (ch, w) in enumerate(widths.items())]
    return P._Line(1, glyphs, 0, 100, 0, 10, 10, 10, False)


def test_a_font_whose_glyphs_share_one_width_is_monospace():
    body = glyph_line(BODY, {"i": 2.8, "m": 8.3, "e": 4.4, "a": 5.0, "n": 5.6, "t": 3.3} | dict.fromkeys("bcdfgh", 5.0))
    code = glyph_line(CODE, dict.fromkeys("im.e@", 5.25))

    assert P._monospace_fonts([body, body, code]) == {CODE}


def test_a_font_too_rare_to_measure_is_judged_by_its_name():
    body = glyph_line(BODY, {"i": 2.8, "m": 8.3, "e": 4.4})
    tex = glyph_line("ABCDEF+CMTT10", {"x": 5.25})
    windows = glyph_line("MKPKAE+MSTT31c4cb00", {"x": 5.0})  # any TrueType font a printer driver embedded

    assert P._monospace_fonts([body, body, tex, windows]) == {"ABCDEF+CMTT10"}


def test_a_typewritten_document_has_no_code():
    code = glyph_line(CODE, dict.fromkeys("im.e@", 5.25))
    other = glyph_line("ABCDEF+Courier", dict.fromkeys("im", 6.0))

    assert P._monospace_fonts([code, code, other]) == set()


# Code


def test_code_keeps_its_lines_indentation_and_blank_lines():
    lines = [
        line("# inside class Time:", baseline=100, font=CODE),
        line("def increment(self, seconds):", x=100 + 4 * CELL, baseline=124.4, font=CODE),
        line("seconds += self.time_to_int()", x=100 + 8 * CELL, baseline=136.6, font=CODE),
    ]
    block = P._Block("code", lines)

    assert P._code(block, layout(code_pitch={10.0: 12.2})) == (
        "```\n"
        "# inside class Time:\n"
        "\n"
        "    def increment(self, seconds):\n"
        "        seconds += self.time_to_int()\n"
        "```"
    )


def test_code_rebuilds_its_spaces_from_glyph_positions():
    block = P._Block("code", [line("x = 1     # comment", font=CODE)])

    assert "x = 1     # comment" in P._code(block, layout())


def test_code_continues_on_the_next_page_with_its_indentation():
    lines = [
        line("def f(x):", page=1, x=130, font=CODE),
        line("return x", page=2, x=86 + 4 * CELL, baseline=60, font=CODE),
    ]
    block = P._Block("code", lines)

    assert "def f(x):\n    return x" in P._code(block, layout(left={1: 130.0, 0: 86.0}))


def test_monospace_words_in_prose_go_in_backticks():
    ln = joined(("the", BODY), ("__init__", CODE), ("method", BODY))

    assert P._markdown(ln.runs) == "the `__init__` method"


def test_code_holding_a_backtick_gets_a_longer_fence():
    assert P._markdown([("a`b", True)]) == "``a`b``"


def test_a_url_that_wraps_stays_whole():
    hyphens = P._Hyphens([])
    url = P._Block("para", [line("https://example.org/", font=CODE), line("page.html", font=CODE)])
    code = P._Block("para", [line("n =", font=CODE), line("circumference", font=CODE)])

    assert P._render(url, hyphens) == "`https://example.org/page.html`"
    assert P._render(code, hyphens) == "`n =` `circumference`"


def test_a_paragraph_starting_with_a_hash_is_not_a_heading():
    assert P._escape("# not a heading") == "\\# not a heading"
    assert P._escape("plain") == "plain"


# Hyphens


def hyphens_of(*texts):
    return P._Hyphens([P._Block("para", [line(text)]) for text in texts])


def test_a_hyphen_breaking_a_word_the_document_spells_whole_goes():
    hyphens = hyphens_of("For example, this.")

    assert hyphens.joins("an exam-", "ple of it")
    assert hyphens.joins("An Exam-", "ple")


def test_a_hyphen_the_document_writes_inside_lines_stays():
    hyphens = hyphens_of("a built-in function", "builtin")

    assert not hyphens.joins("a built-", "in function")


def test_a_hyphen_between_numbers_or_unknown_words_stays():
    hyphens = hyphens_of("A Grã-Bretanha cresceu de 1756 a 1763.", "reuse", "re-use")

    assert not hyphens.joins("entre 1756-", "1763")
    assert not hyphens.joins("tornava-", "se")
    assert not hyphens.joins("re-", "use")


def test_the_halves_of_a_broken_word_are_not_words():
    hyphens = P._Hyphens([P._Block("para", [line("an exam-"), line("ple here")])])

    assert "exam" not in hyphens.words and "ple" not in hyphens.words


# Running heads


def page_of(number, head, *, body_lines=3):
    lines = [line(head, page=number, baseline=68.4, bold=True)] if head else []
    lines += [line(f"text {number} {i}", page=number, baseline=96 + 12 * i) for i in range(body_lines)]
    return lines


def test_running_heads_whose_number_follows_the_page_go():
    lines = [ln for n in range(30, 40) for ln in page_of(n, f"{n - 22} Chapter 2. Variables" if n % 2 else f"2.{n}. Section {n - 22}")]

    kept = P._drop_running_heads(lines, layout())

    assert [ln.text for ln in kept if ln.bold] == []
    assert len(kept) == 30


def test_running_heads_that_repeat_go():
    lines = [ln for n in range(1, 11) for ln in page_of(n, "Economics in One Lesson")]

    assert all(not ln.bold for ln in P._drop_running_heads(lines, layout()))


def test_a_book_without_running_heads_loses_nothing():
    # História econômica global: an advert's line repeats at the foot of its
    # text, but at a different height on each page.
    lines = [ln for n in range(1, 11) for ln in page_of(n, None)]
    lines += [line("Compre agora e leia", page=n, baseline=y) for n, y in ((3, 180), (6, 640), (9, 370))]

    assert P._drop_running_heads(lines, layout()) == lines


def test_a_heading_larger_than_the_text_is_never_a_running_head():
    lines = []
    for n in range(1, 11):
        heading = line("1 Introduction", page=n, baseline=68.4, size=12.0)
        heading.max_size = 12.0
        lines += [heading, *page_of(n, None)]

    assert P._drop_running_heads(lines, layout()) == lines


def test_page_numbers_and_running_heads_are_found_in_roman_numerals():
    assert P._edge_numbers("xvi Contents") == {("roman", 16)}
    assert P._edge_numbers("2.8. Debugging 13") == {("arabic", 13)}
    assert P._running_key("Contents xvii") == P._running_key("Contents xv") == "contents #"


# Run-in headings


def test_a_short_bold_line_opening_a_paragraph_titles_it():
    heading = P._Block("heading", [line("A.2.2 My program hangs.", bold=True)], level=4)
    run_in = P._Block("bold", [line("Infinite Loop", bold=True)])
    caption = P._Block("bold", [line("Figure 2.1: State diagram", bold=True)])
    blocks = [heading, run_in, P._Block("para", [line("If you think...")]), caption, P._Block("para", [line("Text.")])]

    P.PdfHeadingsConverter()._run_in_headings(blocks, layout())

    assert (run_in.kind, run_in.level) == ("heading", 5)
    assert caption.kind == "bold"
