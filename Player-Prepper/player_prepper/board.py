"""Board images, rendered server-side as SVG.

python-chess draws the board itself, so a diagram needs nothing beyond the
core dependency.  That matters here because a gap is only understandable as a
picture: "you have no answer after 1.e4 c5 2.Nf3 d6 3.d4 cxd4 4.Nxd4 Nf6
5.Nc3 a6" is a sentence nobody can hold in their head, and the same thing as
a diagram is obvious.

The palette matches the sibling apps so a position looks the same here, in the
repertoire editor and in the printed book.

The board is also playable: you can carry on from any position the report puts
in front of you.  Legality is decided here rather than in the browser, which
keeps the page free of a chess library -- the same choice both sibling apps
made.  That costs a request per move and buys one source of truth about what
is legal.
"""

from __future__ import annotations

import re

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


def _arrow(uci: str, color: str):
    """One ``e2e4`` into a python-chess arrow, or None if it is nonsense."""
    if not uci or len(uci) < 4:
        return None
    try:
        move = chess.Move.from_uci(uci[:5])
    except ValueError:
        return None
    return chess.svg.Arrow(move.from_square, move.to_square, color=color)


def board_svg(
    fen: str,
    *,
    size: int = 360,
    flipped: bool = False,
    last_move: str = "",
    arrows: str = "",
    coordinates: bool = True,
) -> str:
    """A diagram of one position.

    ``arrows`` is the compact query form the browser sends:
    ``blue:e2e4,yellow:g1f3``.  The colours mean one thing each, everywhere:
    **blue** is what the engine suggests, **yellow** is the next move of the
    line you are looking at, and **green** is a move of your own.
    """
    board = chess.Board(fen)

    shapes = []
    for item in filter(None, (arrows or "").split(",")):
        color, _, uci = item.partition(":")
        arrow = _arrow(uci, color or "green")
        if arrow is not None:
            shapes.append(arrow)

    move = None
    if last_move and len(last_move) >= 4:
        try:
            move = chess.Move.from_uci(last_move[:5])
        except ValueError:
            move = None

    return chess.svg.board(
        board,
        size=size,
        orientation=chess.BLACK if flipped else chess.WHITE,
        lastmove=move,
        arrows=shapes,
        coordinates=coordinates,
        check=board.king(board.turn) if board.is_check() else None,
        colors=BOARD_COLORS,
    )


_MARGIN_CACHE: dict = {}


def margin_fraction(coordinates: bool = True) -> float:
    """How much of the image is coordinate margin, as a fraction of one side.

    With coordinates on, python-chess draws the rank and file labels *inside*
    the SVG: the image is 390 units across but the 8x8 board only occupies
    15..375 of it.  A click overlay stretched across the whole image is
    therefore off by that margin on every square -- a bug you see rather than
    read about, as a piece that picks up the wrong square near the edge.

    Measured from a real SVG rather than taken from ``chess.svg.MARGIN``,
    because that constant does not match what the library actually emits and
    either one could change in a future version.  Same approach, and the same
    reason, as Repertoire-Creator's copy.
    """
    if coordinates in _MARGIN_CACHE:
        return _MARGIN_CACHE[coordinates]

    svg = chess.svg.board(chess.Board(), size=400, coordinates=coordinates)
    fraction = 0.0
    view_box = re.search(r'viewBox="[\d.]+ [\d.]+ ([\d.]+) ', svg)
    # Must be a *square* rect: the first rect in the document is the board
    # background, which spans the margin too and would measure zero.
    square = re.search(
        r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*class="square', svg)
    if view_box and square:
        total = float(view_box.group(1))
        board_size = float(square.group(2)) * 8
        if total > 0 < board_size <= total:
            fraction = (total - board_size) / 2.0 / total

    _MARGIN_CACHE[coordinates] = fraction
    return fraction


def legal_moves(fen: str) -> dict:
    """Legal destinations per origin square, for click-to-move.

    The browser has no chess library -- deliberately, as in both sibling apps
    -- so legality is decided here and the page only ever draws what it is
    told.
    """
    board = chess.Board(fen)
    out: dict = {}
    for move in board.legal_moves:
        out.setdefault(chess.square_name(move.from_square), set()).add(
            chess.square_name(move.to_square))

    return {
        "moves": {key: sorted(value) for key, value in out.items()},
        "turn": "white" if board.turn == chess.WHITE else "black",
        "check": board.is_check(),
        "gameOver": board.is_game_over(),
    }


def play_move(fen: str, uci: str) -> dict:
    """Play one move and report the position it reaches.

    Promotions are filled in as a queen when the browser sends a bare
    ``e7e8``: offering an under-promotion picker in a scouting tool is UI
    nobody would use, and refusing the move outright would look broken.
    """
    board = chess.Board(fen)
    try:
        move = chess.Move.from_uci(uci)
    except (ValueError, AssertionError) as exc:
        raise ValueError(f"{uci} is not a move.") from exc

    if move not in board.legal_moves and move.promotion is None:
        promoted = chess.Move(move.from_square, move.to_square,
                              promotion=chess.QUEEN)
        if promoted in board.legal_moves:
            move = promoted

    if move not in board.legal_moves:
        raise ValueError(f"{uci} is not legal in that position.")

    san = board.san(move)
    board.push(move)
    return {
        "fen": board.fen(),
        "uci": move.uci(),
        "san": san,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "check": board.is_check(),
        "gameOver": board.is_game_over(),
    }


def line_positions(ucis) -> dict:
    """Every position along a line, so the board can be stepped through.

    One request per line rather than one per ply: the wheel has to feel
    instant, and a round trip for each notch would not.  Stops at the first
    move that will not play and says how far it got, which keeps a stale
    cached line usable instead of erroring.
    """
    board = chess.Board()
    fens = [board.fen()]
    sans = []
    played = []

    for uci in ucis or []:
        try:
            move = chess.Move.from_uci(uci)
        except (ValueError, AssertionError):
            break
        if move not in board.legal_moves:
            break
        sans.append(board.san(move))
        board.push(move)
        fens.append(board.fen())
        played.append(uci)

    return {"fens": fens, "sans": sans, "uci": played,
            "complete": len(played) == len(list(ucis or []))}


__all__ = [
    "BOARD_COLORS",
    "board_svg",
    "legal_moves",
    "line_positions",
    "margin_fraction",
    "play_move",
]
