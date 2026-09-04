"""Board diagrams for the browser, drawn server-side as SVG.

A worst moment is only understandable as a picture, and this app shows a dozen
of them.  python-chess draws the board itself, so this needs nothing beyond
the core dependency.

Unlike the sibling apps there is nothing to play here: a weakness report is a
document about games already finished, so the board is a diagram and stays
one.  The palette matches the rest of the repository so a position looks the
same on screen as it does in the printed report.
"""

from __future__ import annotations

import chess
import chess.svg

BOARD_COLORS = {
    "square light": "#f0d9b5",
    "square dark": "#b58863",
    "square light lastmove": "#cdd26a",
    "square dark lastmove": "#aaa23a",
    "margin": "#f7f2e8",
    "coord": "#5c4a33",
    "arrow green": "#15781baa",
    "arrow red": "#882020aa",
    "arrow yellow": "#e68f00aa",
    "arrow blue": "#003088aa",
}


def _arrow(uci: str, colour: str):
    if not uci or len(uci) < 4:
        return None
    try:
        move = chess.Move.from_uci(uci[:5])
    except ValueError:
        return None
    return chess.svg.Arrow(move.from_square, move.to_square, color=colour)


def board_svg(fen: str, *, size: int = 320, flipped: bool = False,
              arrows: str = "", coordinates: bool = True) -> str:
    """One position.

    ``arrows`` is the compact query form ``red:e2e4,green:g1f3``. Throughout
    this app **red is the move you played** and **green is the move the engine
    wanted**, which is the only pair of colours a reader needs to learn.
    """
    board = chess.Board(fen)

    shapes = []
    for item in filter(None, (arrows or "").split(",")):
        colour, _, uci = item.partition(":")
        arrow = _arrow(uci, colour or "green")
        if arrow is not None:
            shapes.append(arrow)

    return chess.svg.board(
        board, size=size,
        orientation=chess.BLACK if flipped else chess.WHITE,
        arrows=shapes, coordinates=coordinates,
        check=board.king(board.turn) if board.is_check() else None,
        colors=BOARD_COLORS)


def uci_of(fen: str, san: str) -> str:
    """The UCI for a SAN move in a position, or ``""``. Used to draw arrows."""
    if not san:
        return ""
    try:
        board = chess.Board(fen)
        return board.parse_san(san).uci()
    except (ValueError, AssertionError):
        return ""


__all__ = ["BOARD_COLORS", "board_svg", "uci_of"]
