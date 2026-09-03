"""Board images, rendered server-side as SVG.

python-chess draws the board itself, so this needs nothing beyond the core
dependency -- which matters, because the board is the app.  Evaluation and
PDF export may degrade to "install the sibling app"; being unable to see the
position may not.

The palette matches the sibling PDF exporter so a position looks the same on
screen as it does in the printed book.
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

_SQUARES = {name: index for index, name in enumerate(chess.SQUARE_NAMES)}

_MARGIN_CACHE: dict[bool, float] = {}


def margin_fraction(coordinates: bool = True) -> float:
    """How much of the image is the coordinate margin, as a fraction of a side.

    With coordinates switched on, python-chess draws the rank and file labels
    *inside* the SVG: the image is 390 units across but the 8x8 board only
    occupies 15..375 of it.  A click overlay stretched across the whole image
    is therefore offset by that margin on every square -- which is a bug you
    see rather than read about, as misplaced legal-move dots.

    The value is measured from a real SVG rather than taken from
    ``chess.svg.MARGIN``, because that constant does not match what the
    library actually emits, and a future version could change either one.
    """
    if coordinates in _MARGIN_CACHE:
        return _MARGIN_CACHE[coordinates]

    svg = chess.svg.board(chess.Board(), size=400, coordinates=coordinates)
    fraction = 0.0
    view_box = re.search(r'viewBox="[\d.]+ [\d.]+ ([\d.]+) ', svg)
    # Must be a *square* rect: the first rect in the document is the board
    # background, which spans the margin too and would measure zero.
    square = re.search(
        r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*class="square', svg
    )
    if view_box and square:
        total = float(view_box.group(1))
        board_size = float(square.group(2)) * 8
        if total > 0 and 0 < board_size <= total:
            fraction = (total - board_size) / 2.0 / total

    _MARGIN_CACHE[coordinates] = fraction
    return fraction


def parse_shapes(circles: str, arrows: str):
    """Parse the compact query form: ``green:e4`` and ``red:g1:f3``."""
    parsed_circles = []
    for item in filter(None, (circles or "").split(",")):
        parts = item.split(":")
        if len(parts) == 2:
            parsed_circles.append((parts[0], parts[1]))

    parsed_arrows = []
    for item in filter(None, (arrows or "").split(",")):
        parts = item.split(":")
        if len(parts) == 3:
            parsed_arrows.append((parts[0], parts[1], parts[2]))
    return parsed_circles, parsed_arrows


def board_svg(
    fen: str,
    *,
    size: int = 400,
    flipped: bool = False,
    last_move: str = "",
    circles=(),
    arrows=(),
    coordinates: bool = True,
    check: bool = True,
) -> str:
    board = chess.Board(fen)

    move = None
    if last_move and len(last_move) >= 4:
        try:
            parsed = chess.Move.from_uci(last_move[:5])
            move = parsed
        except ValueError:
            move = None

    shapes = []
    # An arrow whose tail equals its head renders as a circle, which is how
    # Lichess draws its [%csl] square markers.
    for color, square in circles:
        index = _SQUARES.get(square)
        if index is not None:
            shapes.append(chess.svg.Arrow(index, index, color=color))
    for color, src, dst in arrows:
        a, b = _SQUARES.get(src), _SQUARES.get(dst)
        if a is not None and b is not None:
            shapes.append(chess.svg.Arrow(a, b, color=color))

    return chess.svg.board(
        board,
        size=size,
        orientation=chess.BLACK if flipped else chess.WHITE,
        lastmove=move,
        arrows=shapes,
        coordinates=coordinates,
        check=board.king(board.turn) if check and board.is_check() else None,
        colors=BOARD_COLORS,
    )


def legal_moves(fen: str, square: str = "") -> dict:
    """Legal destinations per origin square, for click-to-move."""
    board = chess.Board(fen)
    out: dict[str, list] = {}
    for move in board.legal_moves:
        out.setdefault(chess.square_name(move.from_square), []).append(
            chess.square_name(move.to_square)
        )
    if square:
        out = {square: sorted(set(out.get(square, [])))}
    else:
        out = {key: sorted(set(value)) for key, value in out.items()}

    return {
        "moves": out,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "check": board.is_check(),
        "gameOver": board.is_game_over(),
    }
