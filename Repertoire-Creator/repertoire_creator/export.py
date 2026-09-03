"""PDF export, borrowed wholesale from the sibling app.

Lichess-Study-to-PDF already turns a study into a typeset chess book, a
contact sheet of diagrams or a step-through slideshow, and it takes its input
as parsed study PGN.  A repertoire *is* study PGN.  So the whole export here
is: render the repertoire to PGN, hand it over, collect the file.

The eval bars in the exported PDF come from the ``[%eval]`` annotations
already baked into the moves, which is why baking them is worth doing -- no
second analysis pass at export time, and the numbers in the PDF are the same
ones you saw on the board.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from .bridge import FeatureUnavailable, require
from .storage import Repertoire

#: What the sibling app calls its layouts, in the order worth offering.
MODES = {
    "grid": "Contact sheet - twelve diagrams a page",
    "slideshow": "One big board a page, step through with the arrow keys",
    "book": "Typeset chess book (needs a LaTeX install)",
    "acrobat": "Layered single page (Adobe Reader only)",
}


def build(
    repertoire: Repertoire,
    *,
    mode: str = "grid",
    chapter_ids=None,
    include_notation: bool = True,
    include_steps: bool = True,
    show_evals: bool = True,
    board_size: float = 424.0,
    landscape_pages: bool = True,
    max_depth: int | None = None,
    diagrams: str | None = None,
    out_path: Path | None = None,
) -> Path:
    """Write a PDF of the repertoire and return the path to it."""
    if mode not in MODES:
        raise ValueError(f"Unknown PDF mode {mode!r}. Try one of: {', '.join(MODES)}")

    parse = require("parse", "PDF export")
    pdf = require("pdf", "PDF export")
    from reportlab.lib.pagesizes import A4, landscape, portrait

    pgn_text = repertoire.study_pgn(chapter_ids)
    study = parse.parse_study(pgn_text, repertoire.meta.lichess_url or "")

    evals = _collect_evals(repertoire, chapter_ids) if show_evals else {}

    options = pdf.PdfOptions(
        mode=mode,
        include_notation=include_notation,
        include_steps=include_steps,
        show_evals=bool(show_evals and evals),
        board_size=board_size,
        page_size=landscape(A4) if landscape_pages else portrait(A4),
        diagrams=diagrams,
        max_depth=max_depth,
    )

    if out_path is None:
        safe = "".join(
            ch for ch in repertoire.meta.name if ch.isalnum() or ch in " -_"
        ).strip()
        out_path = Path(tempfile.gettempdir()) / (
            f"{(safe or 'repertoire')[:60]}-{uuid.uuid4().hex[:6]}.pdf"
        )

    pdf.build_pdf(study, out_path, evals=evals, options=options)
    return Path(out_path)


def _collect_evals(repertoire: Repertoire, chapter_ids=None) -> dict:
    """``{fen: Eval}`` for every move that carries a baked evaluation."""
    evals_module = require("evals", "PDF eval bars")
    from .model import iter_nodes

    wanted = list(chapter_ids) if chapter_ids else [
        c.id for c in repertoire.meta.chapters
    ]
    out = {}
    for chapter_id in wanted:
        game = repertoire.game(chapter_id)
        for node, board_before in iter_nodes(game):
            score = node.eval()
            if score is None:
                continue
            after = board_before.copy(stack=False)
            after.push(node.move)
            white = score.white()
            out[after.fen()] = evals_module.Eval(
                cp=white.score(),
                mate=white.mate(),
                depth=node.eval_depth() or 0,
                source="cache",
            )
    return out


__all__ = ["FeatureUnavailable", "MODES", "build"]
