"""Tests that pin down the behaviour that is easy to get wrong."""

import textwrap
from pathlib import Path

import chess
import pytest
from fastapi import HTTPException

from lichess_study_pdf.fetch import StudyFetchError, parse_study_url
from lichess_study_pdf.notation import child_map, continuation, notation_blocks
from lichess_study_pdf.parse import nag_text, parse_study, split_comment
from lichess_study_pdf.pdf import PdfOptions, build_pdf
from lichess_study_pdf.server import (
    SaveStudyRequest,
    list_studies,
    save_study,
)
from lichess_study_pdf.sidelines import (
    PALETTE,
    PALETTE_SIZE,
    color_for,
    slot_for,
    tag_for,
)
from lichess_study_pdf.studies import (
    SAVED_HEADING,
    add_study,
    load_studies,
    parse_studies,
)

FIXTURE = Path(__file__).parent / "fixtures" / "knight-bishop-mate.pgn"


@pytest.fixture(scope="module")
def study():
    return parse_study(FIXTURE.read_text(encoding="utf-8"),
                       "https://lichess.org/study/ByhlXnmM")


# ------------------------------------------------------------------- urls

@pytest.mark.parametrize("value,expected", [
    ("https://lichess.org/study/i7hMEq7h", ("i7hMEq7h", None)),
    ("https://lichess.org/study/i7hMEq7h/9rd7XwOw", ("i7hMEq7h", "9rd7XwOw")),
    ("lichess.org/study/i7hMEq7h?page=2", ("i7hMEq7h", None)),
    ("https://lichess.org/study/i7hMEq7h.pgn", ("i7hMEq7h", None)),
    ("i7hMEq7h", ("i7hMEq7h", None)),
])
def test_parse_study_url(value, expected):
    ref = parse_study_url(value)
    assert (ref.study_id, ref.chapter_id) == expected


def test_parse_study_url_rejects_rubbish():
    with pytest.raises(StudyFetchError):
        parse_study_url("https://example.com/not-a-study")


# --------------------------------------------------------------- comments

def test_split_comment_extracts_shapes_and_keeps_prose():
    raw = "{ Good plan. [%csl Ga8,Rh1][%cal Ga2g8,Bb1h7][%clk 0:03:00] }"
    text, circles, arrows = split_comment(raw)
    assert "Good plan." in text
    assert "%csl" not in text and "%clk" not in text
    assert circles == [("green", "a8"), ("red", "h1")]
    assert arrows == [("green", "a2", "g8"), ("blue", "b1", "h7")]


def test_nag_symbols():
    assert nag_text({3}) == "!!"
    assert nag_text({7}) == "□"
    assert nag_text({999}) == ""


# ---------------------------------------------------------------- parsing

def test_study_has_all_chapters(study):
    assert len(study.chapters) == 12
    assert "Knight and Bishop" in study.name


def test_sidelines_are_present(study):
    assert sum(c.variation_count for c in study.chapters) > 0
    deeper = [s for c in study.chapters for s in c.steps if s.depth > 0]
    assert deeper, "no sideline moves were emitted"


def test_comments_survive(study):
    commented = [s for c in study.chapters for s in c.steps if s.comment]
    assert len(commented) > 50


def test_every_step_line_replays_to_its_own_fen(study):
    """The single most important invariant: line paths must be legal."""
    for chapter in study.chapters:
        for step in chapter.steps:
            if not step.san:
                continue
            board = chess.Board(chapter.initial_fen)
            for index in step.line[1:]:
                board.push_uci(chapter.steps[index].uci)
            assert board.fen() == step.fen, f"{chapter.name} @ {step.move_label()}"


def test_mainline_move_precedes_its_sidelines(study):
    """PGN reading order: the move, then the alternatives to it."""
    for chapter in study.chapters:
        for step in chapter.steps:
            if not step.starts_variation:
                continue
            parent = step.line[-2]
            siblings = [i for i in child_map(chapter).get(parent, [])]
            assert siblings[0] < step.index


def test_continuation_follows_the_current_line(study):
    chapter = max(study.chapters, key=lambda c: c.variation_count)
    kids = child_map(chapter)
    variation_start = next(s for s in chapter.steps if s.starts_variation)
    ahead = continuation(chapter, variation_start, kids, 3)
    assert ahead, "a sideline should have its own continuation"
    assert all(s.depth == variation_start.depth for s in ahead)


# --------------------------------------------------------------- notation

def test_notation_blocks_cover_every_move(study):
    chapter = max(study.chapters, key=lambda c: c.variation_count)
    covered = {i for b in notation_blocks(chapter) for i in b.step_indices}
    expected = {s.index for s in chapter.steps if s.san}
    assert covered == expected


# -------------------------------------------------------------------- pdf

def test_slideshow_pdf_has_a_page_per_position(study, tmp_path):
    pypdfium2 = pytest.importorskip("pypdfium2")
    target = tmp_path / "slideshow.pdf"
    options = PdfOptions(mode="slideshow", chapter_filter=(2,),
                         include_notation=False)
    build_pdf(study, target, evals={}, options=options)

    chapter = study.chapters[2]
    document = pypdfium2.PdfDocument(target)
    # title page + contents + one page per position
    assert len(document) == 2 + len(chapter.steps)


def test_acrobat_pdf_layers_one_position_at_a_time(study, tmp_path):
    pikepdf = pytest.importorskip("pikepdf")
    target = tmp_path / "acrobat.pdf"
    options = PdfOptions(mode="acrobat", chapter_filter=(2,),
                         include_notation=False)
    build_pdf(study, target, evals={}, options=options)

    chapter = study.chapters[2]
    with pikepdf.open(target) as pdf:
        # title, the viewer warning, contents, then one layered chapter page
        assert len(pdf.pages) == 4
        properties = pdf.Root.OCProperties
        assert len(properties.OCGs) == len(chapter.steps)
        assert len(properties.D.ON) == 1
        assert len(properties.D.OFF) == len(chapter.steps) - 1
        assert "/JavaScript" in pdf.Root.Names.keys()


# ---------------------------------------------------- private-study fallback

def test_chapter_url_is_parsed_as_study_plus_chapter():
    ref = parse_study_url("https://lichess.org/study/i7hMEq7h/0KOpBPyc")
    assert ref.study_id == "i7hMEq7h"
    assert ref.chapter_id == "0KOpBPyc"


def test_chapter_list_is_extracted_from_embedded_json():
    from lichess_study_pdf.fetch import _extract_json_array

    html = (
        'window.x = {"name":"S","chapters":'
        '[{"id":"AAAAAAAA","name":"One [x]"},{"id":"BBBBBBBB","name":"Two"}],'
        '"other":1};'
    )
    raw = _extract_json_array(html, "chapters")
    assert raw is not None
    import json
    entries = json.loads(raw)
    assert [e["id"] for e in entries] == ["AAAAAAAA", "BBBBBBBB"]


def test_extract_json_array_respects_brackets_inside_strings():
    from lichess_study_pdf.fetch import _extract_json_array

    html = '{"chapters":[{"id":"AAAAAAAA","name":"a ] } tricky \\" name"}],"z":0}'
    import json
    entries = json.loads(_extract_json_array(html, "chapters"))
    assert entries[0]["id"] == "AAAAAAAA"


# ------------------------------------------------------------- latex book

def test_figurine_notation():
    from lichess_study_pdf.pdf_latex import figurine

    assert figurine("e4") == "e4"
    assert figurine("Nf3").startswith("\symknight")
    assert "\symqueen" in figurine("e8=Q")
    assert figurine("O-O") == "O-O"
    assert figurine("Qxf7#").endswith("\#")


def test_latex_escape_drops_emoji_but_keeps_prose():
    from lichess_study_pdf.pdf_latex import latex_escape

    out = latex_escape("Fritz \U0001F632 Variation & 100% fun_time")
    assert "\&" in out and "\%" in out and "\_" in out
    assert "\U0001F632" not in out
    assert "Fritz" in out and "Variation" in out


def test_latex_escape_handles_chess_symbols():
    from lichess_study_pdf.pdf_latex import latex_escape

    assert "\pm" in latex_escape("±")
    assert "Box" in latex_escape("□")


@pytest.mark.skipif(
    __import__("lichess_study_pdf.pdf_latex", fromlist=["find_latex"]).find_latex()
    is None,
    reason="no pdflatex installed",
)
def test_book_mode_compiles(study, tmp_path):
    pypdfium2 = pytest.importorskip("pypdfium2")
    target = tmp_path / "book.pdf"
    build_pdf(study, target, evals={},
              options=PdfOptions(mode="book", chapter_filter=(2, 4)))
    assert target.is_file()
    document = pypdfium2.PdfDocument(target)
    assert len(document) >= 2


# ------------------------------------------------- eval provider behaviour

def test_cloud_rate_limit_does_not_block(monkeypatch):
    """A 429 must never sleep: it would freeze the live eval bar."""
    import time as time_module

    from lichess_study_pdf import evals as evals_module

    class FakeResponse:
        status_code = 429
        headers = {"Retry-After": "60"}

    monkeypatch.setattr(evals_module.requests, "get",
                        lambda *a, **k: FakeResponse())
    slept = []
    monkeypatch.setattr(evals_module.time, "sleep", lambda s: slept.append(s))

    provider = evals_module.EvalProvider(None, cloud_interval=0.0)
    started = time_module.perf_counter()
    result = provider._from_cloud(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    elapsed = time_module.perf_counter() - started

    assert result is None
    assert provider.cloud_rate_limited is True
    assert not [s for s in slept if s >= 1.0], "back-off must not sleep"
    assert elapsed < 1.0

    # And it stops asking rather than hammering Lichess.
    assert provider._cloud_blocked_until > 0


def test_cloud_is_not_asked_about_deep_positions():
    from lichess_study_pdf.evals import EvalProvider

    provider = EvalProvider(None, cloud_max_fullmove=20)
    assert provider._cloud_worth_asking(
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert not provider._cloud_worth_asking(
        "6k1/5ppp/8/3N4/2B5/8/5PPP/6K1 w - - 0 62")


def test_cloud_and_engine_results_use_separate_cache_keys():
    """A cloud answer must be reusable whatever the engine settings are."""
    from lichess_study_pdf.evals import Eval, EvalProvider

    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    provider = EvalProvider(None, movetime=0.25)
    provider._store(fen, Eval(cp=19, depth=40, source="cloud"))

    other = EvalProvider(None, movetime=0.05)
    other._cache = provider._cache
    assert other._lookup(fen) is not None, "cloud entry should survive a settings change"


def test_console_helper_survives_emoji():
    from lichess_study_pdf.cli import _make_console_forgiving

    _make_console_forgiving()   # must not raise on any platform


# -------------------------------------------------------------- grid mode

def test_grid_mode_packs_twelve_positions_per_page(study, tmp_path):
    pypdfium2 = pytest.importorskip("pypdfium2")
    chapter = max(study.chapters, key=lambda c: len(c.steps))
    target = tmp_path / "grid.pdf"
    build_pdf(study, target, evals={},
              options=PdfOptions(mode="grid", chapter_filter=(chapter.index,),
                                 include_notation=False))

    import math
    expected = 2 + math.ceil(len(chapter.steps) / 12)   # title + contents
    document = pypdfium2.PdfDocument(target)
    assert len(document) == expected
    document.close()


def test_grid_is_far_shorter_than_slideshow(study, tmp_path):
    pypdfium2 = pytest.importorskip("pypdfium2")
    chapter = max(study.chapters, key=lambda c: len(c.steps))
    common = dict(chapter_filter=(chapter.index,), include_notation=False)

    grid = tmp_path / "g.pdf"
    slides = tmp_path / "s.pdf"
    build_pdf(study, grid, evals={}, options=PdfOptions(mode="grid", **common))
    build_pdf(study, slides, evals={},
              options=PdfOptions(mode="slideshow", **common))

    g = pypdfium2.PdfDocument(grid)
    s = pypdfium2.PdfDocument(slides)
    assert len(g) * 6 < len(s), "grid should be many times shorter"
    g.close(); s.close()


def test_every_chapter_starts_on_a_new_page(study, tmp_path):
    """No page may carry diagrams from two different chapters."""
    pypdfium2 = pytest.importorskip("pypdfium2")
    target = tmp_path / "chapters.pdf"
    build_pdf(study, target, evals={},
              options=PdfOptions(mode="grid", chapter_filter=(2, 3, 4),
                                 include_notation=False))

    document = pypdfium2.PdfDocument(target)
    # Match the numbered heading, not the bare name: one chapter here is
    # called "Exercise: <name of another chapter>", so bare names overlap.
    headings = [f"{i + 1}. {study.chapters[i].name}" for i in (2, 3, 4)]
    import re

    # "positions 1-12 of 31" appears only in a grid page header; the contents
    # page lists every chapter by design and every page has a Contents link.
    grid_marker = re.compile(r"positions \d+-\d+ of \d+")

    checked = 0
    for page in range(len(document)):
        flat = " ".join(document[page].get_textpage().get_text_range().split())
        if not grid_marker.search(flat):
            continue
        hits = [h for h in headings if " ".join(h.split()) in flat]
        checked += 1
        assert len(hits) == 1, f"page {page} mixes chapters: {hits}"
    assert checked >= 3, "expected at least one diagram page per chapter"
    document.close()


def test_emoji_are_stripped_not_turned_into_question_marks():
    from lichess_study_pdf.fonts import safe_text

    assert safe_text("\U0001F632Fritz Variation\U0001F632") == "Fritz Variation"
    assert "?" not in safe_text("\U0001F3AFRepertoire\U0001F3AF")


# ------------------------------------------- notation diagrams in grid mode

def test_grid_mode_keeps_diagrams_out_of_the_notation_section():
    """Grid already shows every position; duplicating them wrecks the flow."""
    assert PdfOptions(mode="grid").effective_diagrams() == "none"
    assert PdfOptions(mode="book").effective_diagrams() == "every:6"
    assert PdfOptions(mode="slideshow").effective_diagrams() == "every:6"
    # An explicit choice always wins.
    assert PdfOptions(mode="grid", diagrams="all").effective_diagrams() == "all"


def test_grid_export_has_no_sparse_diagram_pages(study, tmp_path):
    """Every diagram page must be a full grid page, not a stray one-board page."""
    pypdfium2 = pytest.importorskip("pypdfium2")
    import re

    target = tmp_path / "grid-full.pdf"
    build_pdf(study, target, evals={}, options=PdfOptions(mode="grid"))

    grid_marker = re.compile(r"positions \d+-\d+ of \d+")
    # The notation section captions its diagrams "after 12.Nf3 - Black to play";
    # matching that exactly avoids tripping over the same words in prose.
    caption = re.compile(r"after \S+ - (?:White|Black) to play")

    document = pypdfium2.PdfDocument(target)
    for page in range(len(document)):
        flat = " ".join(document[page].get_textpage().get_text_range().split())
        if grid_marker.search(flat):
            continue
        assert not caption.search(flat), (
            f"page {page} carries a diagram outside the grid")
    document.close()


def test_invisible_emoji_plumbing_is_stripped():
    """ZWJ and variation selectors are in Arial's cmap but draw as boxes."""
    from lichess_study_pdf.fonts import safe_text

    assert "‍" not in safe_text("Intro‍text")
    assert "️" not in safe_text("Flag️ here")
    # Real punctuation and chess symbols must survive.
    assert "’" in safe_text("it’s")
    assert "±" in safe_text("±")


# ------------------------------------------------------- sideline colours

BRANCHY = Path(__file__).parent / "fixtures" / "many-sidelines.pgn"


@pytest.fixture(scope="module")
def branchy():
    """A chapter with five alternatives to one move, and nesting inside them."""
    return parse_study(BRANCHY.read_text(encoding="utf-8")).chapters[0]


def test_every_sideline_gets_its_own_number(branchy):
    numbers = {s.branch for s in branchy.steps if s.depth}
    assert 0 not in numbers
    assert len(numbers) == branchy.variation_count == 10
    assert all(s.branch == 0 for s in branchy.steps if not s.depth)


def test_one_sideline_keeps_one_number_for_all_of_its_moves(branchy):
    # 2...Nf6 3.Nxe5 d6 ... 4.Nf3 Nxe4 is one sideline, even though a nested
    # one interrupts it in the middle.
    labels = [s.move_label() for s in branchy.steps if s.branch == 1]
    assert labels == ["2...Nf6", "3.Nxe5", "3...d6", "4.Nf3", "4...Nxe4"]


def test_siblings_at_one_branch_point_never_share_a_colour(branchy):
    kids = child_map(branchy)
    for parent in kids:
        siblings = [branchy.steps[i] for i in kids[parent]
                    if branchy.steps[i].starts_variation]
        slots = [slot_for(s.branch) for s in siblings]
        assert len(set(slots)) == len(slots), f"colour clash under {parent}"


def test_a_nested_sideline_differs_from_the_one_it_sits_in(branchy):
    for step in branchy.steps:
        if not step.starts_variation or step.depth < 2:
            continue
        # The parent of a nested sideline is a move of the enclosing one.
        enclosing = branchy.steps[step.line[-2]]
        assert enclosing.branch and enclosing.branch != step.branch
        assert slot_for(enclosing.branch) != slot_for(step.branch)


def test_notation_blocks_never_mix_two_sidelines(branchy):
    for block in notation_blocks(branchy):
        branches = {branchy.steps[i].branch for i in block.step_indices}
        assert len(branches) <= 1
        assert branches <= {block.branch}


def test_main_line_has_no_sideline_colour():
    assert color_for(0) is None
    assert tag_for(0) == "main"
    assert tag_for(3) == "s3"


def test_colours_repeat_only_after_the_whole_palette():
    slots = {slot_for(branch) for branch in range(1, PALETTE_SIZE + 1)}
    assert len(slots) == PALETTE_SIZE
    assert slot_for(PALETTE_SIZE + 1) == slot_for(1)


def _luminance(hex_color):
    from lichess_study_pdf.sidelines import _relative_luminance

    value = hex_color.lstrip("#")
    return _relative_luminance([int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)])


@pytest.mark.parametrize("color", PALETTE, ids=lambda c: f"s{c.slot + 1}")
def test_every_sideline_colour_is_readable(color):
    """Move text has to clear 4.5:1 on the page *and* on its own wash."""
    on_white = 1.05 / (_luminance(color.ink) + 0.05)
    on_tint = (_luminance(color.tint) + 0.05) / (_luminance(color.ink) + 0.05)
    assert on_white >= 4.5, f"{color.ink} on white is {on_white:.2f}:1"
    assert on_tint >= 4.5, f"{color.ink} on {color.tint} is {on_tint:.2f}:1"
    # And the wash itself must stay a wash, not a highlight.
    assert _luminance(color.tint) >= 0.72


def test_grid_and_book_modes_render_a_branchy_chapter(tmp_path):
    """The colouring code paths run for real, on every writer that has one."""
    pypdfium2 = pytest.importorskip("pypdfium2")
    study = parse_study(BRANCHY.read_text(encoding="utf-8"))
    for mode in ("grid", "slideshow"):
        target = tmp_path / f"{mode}.pdf"
        build_pdf(study, target, evals={}, options=PdfOptions(mode=mode))
        assert len(pypdfium2.PdfDocument(target)) > 2


# ---------------------------------------------------- the home page's list

STUDIES_FILE = Path(__file__).resolve().parent.parent / "studies.txt"


def test_the_shipped_studies_file_is_readable():
    """The list the home page opens on has to parse, names and all."""
    listing = load_studies(STUDIES_FILE)
    assert listing.problems == []
    assert len(listing.entries) >= 5
    for entry in listing.entries:
        assert entry.name and entry.name.strip() == entry.name
        assert entry.study_id
        assert entry.section        # every seeded study sits under a heading


def test_the_private_study_is_listed_by_chapter_url():
    """A chapter URL is how a private study loads without a token, so the
    list must keep the chapter id rather than the tidier study URL."""
    entries = {e.study_id: e for e in load_studies(STUDIES_FILE).entries}
    fried_liver = entries["i7hMEq7h"]
    assert fried_liver.chapter_id == "T5rBUcOn"
    assert fried_liver.private_hint


def test_the_parser_forgives_a_hand_edited_file():
    text = """
    # a comment
    ## Openings
    Named study | https://lichess.org/study/abcd1234

    https://lichess.org/study/efgh5678
    not a study at all
    Duplicate | https://lichess.org/study/abcd1234/ch000001
    """
    entries, problems = parse_studies(textwrap.dedent(text))

    assert [e.study_id for e in entries] == ["abcd1234", "efgh5678"]
    assert entries[0].name == "Named study"
    assert entries[0].section == "Openings"
    # A bare URL keeps working; the button just shows the id.
    assert entries[1].name == "efgh5678"
    # The junk line is reported with its line number, not raised.
    assert [text for _, text in problems] == ["not a study at all"]


def test_saving_a_study_appends_it_once(tmp_path):
    target = tmp_path / "studies.txt"
    target.write_text("## Openings\nFirst | https://lichess.org/study/abcd1234\n",
                      encoding="utf-8")

    entry, added = add_study("https://lichess.org/study/zzzz9999",
                             "A new study", path=target)
    assert added and entry.name == "A new study"
    assert SAVED_HEADING in target.read_text(encoding="utf-8")

    # Saving the same study again changes nothing and reports the first entry.
    again, added_again = add_study("https://lichess.org/study/zzzz9999",
                                   "Different name", path=target)
    assert not added_again
    assert again.name == "A new study"

    listing = load_studies(target)
    assert [e.study_id for e in listing.entries] == ["abcd1234", "zzzz9999"]


def test_saving_keeps_the_chapter_url_and_cleans_the_name(tmp_path):
    target = tmp_path / "studies.txt"
    entry, added = add_study("https://lichess.org/study/abcd1234/ch000001",
                             "Pipes | and\nnewlines  squashed", path=target)
    assert added
    assert entry.chapter_id == "ch000001"

    reloaded = load_studies(target).entries[0]
    assert reloaded.url.endswith("/ch000001")
    assert reloaded.name == "Pipes / and newlines squashed"


def test_the_studies_api_reads_and_writes_the_list(tmp_path, monkeypatch):
    target = tmp_path / "studies.txt"
    target.write_text("## Mine\nOne | https://lichess.org/study/abcd1234\n",
                      encoding="utf-8")
    monkeypatch.setenv("LICHESS_STUDIES_FILE", str(target))

    listing = list_studies()
    assert listing["count"] == 1
    assert listing["sections"][0]["heading"] == "Mine"
    assert listing["sections"][0]["studies"][0]["name"] == "One"

    saved = save_study(SaveStudyRequest(url="https://lichess.org/study/zzzz9999",
                                       name="Two"))
    assert saved["added"] is True
    assert list_studies()["count"] == 2

    with pytest.raises(HTTPException):
        save_study(SaveStudyRequest(url="https://example.com/nope", name="No"))
