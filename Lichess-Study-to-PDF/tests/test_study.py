"""Tests that pin down the behaviour that is easy to get wrong."""

from pathlib import Path

import chess
import pytest

from lichess_study_pdf.fetch import StudyFetchError, parse_study_url
from lichess_study_pdf.notation import child_map, continuation, notation_blocks
from lichess_study_pdf.parse import nag_text, parse_study, split_comment
from lichess_study_pdf.pdf import PdfOptions, build_pdf

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
