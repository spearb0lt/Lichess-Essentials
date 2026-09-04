"""The prep sheet: a report you can take away from the screen.

A scouting report is read once, an hour before a game, often on paper or a
phone.  So the export is not a dump of the JSON -- it is the four things
worth having in front of you, as a chess document:

* what they open with as White, and as Black, with how often and how they score
* the moves that cost them the most points, which is where to steer
* every position their games reach that your book has no answer for

All of it as **one PGN with four chapters**, which is then handed to the
sibling exporter's layout engine.  A repertoire is study PGN and so is this,
which is why the whole export here is "build the PGN, hand it over, collect
the file" rather than a second implementation of page layout.

Lines are merged into a tree rather than written out one per chapter.  Twenty
gaps as twenty chapters is twenty nearly-empty pages, because every chapter
starts a fresh one; twenty gaps merged into one tree is two pages of diagrams
that share their opening moves, which is also how you actually think about
them.
"""

from __future__ import annotations

import io
import tempfile
import uuid
from pathlib import Path

import chess
import chess.pgn

from .bridge import FeatureUnavailable, require

#: What the sibling app calls its layouts, in the order worth offering here.
#: Grid first: a prep sheet is a contact sheet, not a book.
MODES = {
    "grid": "Contact sheet - twelve diagrams a page",
    "book": "Typeset chess book (needs a LaTeX install)",
    "slideshow": "One big board a page, step through with the arrow keys",
    "acrobat": "Layered single page (Adobe Reader only)",
}

#: Lines longer than this stop being preparation and start being a game.
MAX_LINE_PLIES = 24


def _add_line(game: chess.pgn.Game, line_uci, comment: str = "") -> bool:
    """Graft one line onto a game tree, reusing the moves already there.

    Returns False for a line that will not play, which is how a malformed
    cached game stays out of the document instead of raising during an export.
    """
    node = game
    board = game.board()
    for uci in (line_uci or [])[:MAX_LINE_PLIES]:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return False
        if move not in board.legal_moves:
            return False
        existing = None
        for child in node.variations:
            if child.move == move:
                existing = child
                break
        node = existing if existing is not None else node.add_variation(move)
        board.push(move)

    if comment:
        node.comment = (node.comment + " " + comment).strip() if node.comment \
            else comment
    return True


def _chapter(study_name: str, chapter_name: str, intro: str = "") -> chess.pgn.Game:
    game = chess.pgn.Game()
    game.headers["Event"] = f"{study_name}: {chapter_name}"
    game.headers["Site"] = "Player Prepper"
    game.headers["Result"] = "*"
    game.headers["StudyName"] = study_name
    game.headers["ChapterName"] = chapter_name
    if intro:
        game.comment = intro
    return game


def _percent(value) -> str:
    return f"{round((value or 0) * 100)}%"


def _openings_chapter(study_name: str, section: dict, username: str) -> chess.pgn.Game | None:
    """Their choices in one colour, merged into a tree of their real lines."""
    their_color = section.get("theyPlay", "white")
    rows = section.get("topMoves") or []
    if not rows:
        return None

    tally = section.get("tally") or {}
    intro = (f"{username} as {their_color}: {tally.get('games', 0)} games, "
             f"scoring {_percent(tally.get('score'))}. "
             "Each line below carries how often they chose it and how they "
             "did with it, from their point of view.")

    chapter = _chapter(study_name, f"What they play as {their_color.title()}",
                       intro)
    for row in rows:
        line = list(row.get("lineUci") or []) + [row.get("uci", "")]
        note = (f"{row.get('san', '')}: {row.get('games', 0)} games "
                f"({_percent(row.get('share'))} of the {row.get('reached', 0)} "
                f"they reached here), scoring {_percent(row.get('score'))} "
                f"[+{row.get('w', 0)} ={row.get('d', 0)} -{row.get('l', 0)}]")
        named = row.get("opening") or {}
        if named.get("known"):
            note += f". {named.get('eco', '')} {named.get('name', '')}".rstrip()
        _add_line(chapter, line, note)

    return chapter if chapter.variations else None


def _weak_chapter(study_name: str, sections, username: str) -> chess.pgn.Game | None:
    """Where their own results say to steer, both colours in one chapter."""
    rows = []
    for section in sections:
        for row in section.get("weakSpots") or []:
            rows.append((section.get("theyPlay", ""), row))
    if not rows:
        return None
    rows.sort(key=lambda pair: -pair[1].get("leak", 0))

    intro = (f"Moves {username} has actually lost points with, worst first. "
             "'Leak' is games x (0.5 - their score): the points they have "
             "dropped below even in this line. It is a fact about their "
             "results, not a verdict on the move -- the raw record is shown "
             "so you can judge the sample yourself.")
    chapter = _chapter(study_name, "Where they leak points", intro)

    for their_color, row in rows:
        line = list(row.get("lineUci") or []) + [row.get("uci", "")]
        note = (f"As {their_color}, {row.get('san', '')}: {row.get('games', 0)} "
                f"games, scoring {_percent(row.get('score'))} "
                f"[+{row.get('w', 0)} ={row.get('d', 0)} -{row.get('l', 0)}], "
                f"leaking {row.get('leak', 0)} points")
        _add_line(chapter, line, note)

    return chapter if chapter.variations else None


def _gaps_chapter(study_name: str, section: dict) -> chess.pgn.Game | None:
    """Positions their games reach that your book has nothing for."""
    coverage = section.get("coverage") or {}
    gaps = coverage.get("gaps") or []
    if not gaps:
        return None

    my_color = coverage.get("youPlay", "white")
    intro = (f"You have {my_color}. Of their {coverage.get('inScope', 0)} games "
             f"that reach your repertoire, {coverage.get('covered', 0)} stay "
             f"inside it ({coverage.get('percent', 0)}%). The rest arrive at "
             f"one of these {coverage.get('gapPositions', 0)} positions, where "
             "it is your move and you have written nothing. The count is how "
             "many of their games put you there.")
    chapter = _chapter(study_name, f"Your gaps when you have {my_color.title()}",
                       intro)

    for gap in gaps:
        note = (f"{gap.get('games', 0)} of their games reach this; "
                f"they score {_percent(gap.get('theirScore'))} from here. "
                "You have no move written down.")
        named = gap.get("opening") or {}
        if named.get("known"):
            note += f" ({named.get('eco', '')} {named.get('name', '')})".replace(
                "( ", "(")
        engine = gap.get("engine") or {}
        best = (engine.get("lines") or [{}])[0]
        if best.get("line"):
            note += (f" Engine: {best.get('text', '')} {best['line']}"
                     f" (depth {best.get('depth', 0)}, {engine.get('source', '')}).")
        _add_line(chapter, gap.get("lineUci") or [], note)

    return chapter if chapter.variations else None


def build_pgn(report: dict) -> str:
    """The whole prep sheet as one multi-chapter study PGN."""
    username = report.get("username", "opponent")
    site = "Lichess" if report.get("site") == "lichess" else "Chess.com"
    study_name = f"Prep: {username} ({site})"

    sections = [report.get("colors", {}).get(colour) or {}
                for colour in ("white", "black")]
    sections = [section for section in sections if section]

    chapters = []
    for section in sections:
        chapter = _openings_chapter(study_name, section, username)
        if chapter is not None:
            chapters.append(chapter)

    weak = _weak_chapter(study_name, sections, username)
    if weak is not None:
        chapters.append(weak)

    for section in sections:
        chapter = _gaps_chapter(study_name, section)
        if chapter is not None:
            chapters.append(chapter)

    if not chapters:
        raise ValueError(
            "There is nothing to export yet -- scout a player first.")

    # A fresh exporter per chapter: StringExporter accumulates, so reusing one
    # makes every chapter carry the text of the ones before it.
    def render(chapter):
        return chapter.accept(chess.pgn.StringExporter(
            headers=True, variations=True, comments=True))

    return "\n\n".join(render(chapter) for chapter in chapters) + "\n"


def build(report: dict, *, mode: str = "grid", out_path: Path | None = None,
          include_notation: bool = True, include_steps: bool = True,
          landscape_pages: bool = True, board_size: float = 424.0,
          diagrams: str | None = None) -> Path:
    """Write a PDF prep sheet and return the path to it.

    ``include_steps`` is the sibling exporter's name for the section that
    draws the positions, and it is what produces the twelve-to-a-page contact
    sheet in grid mode -- so it is on by default.  Turning it off leaves a
    text-only document: every line and every number, no diagrams.  In
    *slideshow* mode the same section is one page per position, which for a
    twenty-gap report is a hundred pages, so turn it off there unless that is
    what you want.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown PDF mode {mode!r}. Try one of: {', '.join(MODES)}")

    parse = require("parse", "PDF export")
    pdf = require("pdf", "PDF export")
    from reportlab.lib.pagesizes import A4, landscape, portrait

    study = parse.parse_study(build_pgn(report), "")

    options = pdf.PdfOptions(
        mode=mode,
        include_notation=include_notation,
        include_steps=include_steps,
        show_evals=False,               # nothing here is an evaluation
        board_size=board_size,
        page_size=landscape(A4) if landscape_pages else portrait(A4),
        diagrams=diagrams,
    )

    if out_path is None:
        safe = "".join(ch for ch in report.get("username", "prep")
                       if ch.isalnum() or ch in " -_").strip()
        out_path = Path(tempfile.gettempdir()) / (
            f"prep-{(safe or 'opponent')[:40]}-{uuid.uuid4().hex[:6]}.pdf")

    pdf.build_pdf(study, out_path, evals={}, options=options)
    return Path(out_path)


def chapter_count(report: dict) -> int:
    """How many chapters an export would have, for the dialog to say so."""
    try:
        text = build_pgn(report)
    except ValueError:
        return 0
    handle = io.StringIO(text)
    count = 0
    while chess.pgn.read_game(handle) is not None:
        count += 1
    return count


__all__ = ["MODES", "FeatureUnavailable", "build", "build_pgn", "chapter_count"]
