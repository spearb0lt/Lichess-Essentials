"""Vector board rendering for the PDF layer.

Boards go out as real vector graphics rather than rasterised images, so a
500-position export stays small and stays sharp at any zoom.  The path is
python-chess SVG -> svglib -> ReportLab drawing.
"""

from __future__ import annotations

import io
from functools import lru_cache

import chess
import chess.svg
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors as rl_colors
from svglib.svglib import svg2rlg

#: Board palette close to Lichess's default brown.
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

_SQUARE_NAMES = {name: idx for idx, name in enumerate(chess.SQUARE_NAMES)}


def _square(name: str):
    return _SQUARE_NAMES.get(name)


def build_board_svg(
    fen: str,
    *,
    size: int = 400,
    flipped: bool = False,
    last_move: tuple | None = None,
    circles=(),
    arrows=(),
    coordinates: bool = True,
) -> str:
    """Produce the SVG for one position, including Lichess shape annotations."""
    board = chess.Board(fen)

    move = None
    if last_move:
        try:
            move = chess.Move(last_move[0], last_move[1])
        except (TypeError, ValueError):
            move = None

    shapes = []
    # python-chess renders an Arrow whose tail == head as a circle, which is
    # exactly how Lichess draws [%csl] square markers.
    for color, square_name in circles:
        sq = _square(square_name)
        if sq is not None:
            shapes.append(chess.svg.Arrow(sq, sq, color=color))
    for color, from_name, to_name in arrows:
        src, dst = _square(from_name), _square(to_name)
        if src is not None and dst is not None:
            shapes.append(chess.svg.Arrow(src, dst, color=color))

    return chess.svg.board(
        board,
        size=size,
        orientation=chess.BLACK if flipped else chess.WHITE,
        lastmove=move,
        arrows=shapes,
        coordinates=coordinates,
        colors=BOARD_COLORS,
    )


def svg_to_drawing(svg_text: str, target_size: float) -> Drawing | None:
    """Convert SVG markup into a ReportLab drawing scaled to ``target_size`` pt."""
    try:
        drawing = svg2rlg(io.BytesIO(svg_text.encode("utf-8")))
    except Exception:
        return None
    if drawing is None or not drawing.width or not drawing.height:
        return None

    scale = target_size / float(drawing.width)
    drawing.scale(scale, scale)
    drawing.width = float(drawing.width) * scale
    drawing.height = float(drawing.height) * scale
    return drawing


@lru_cache(maxsize=512)
def _cached_drawing(spec: tuple, target_size: float) -> Drawing | None:
    fen, flipped, last_move, circles, arrows, coordinates = spec
    svg_text = build_board_svg(
        fen,
        size=400,
        flipped=flipped,
        last_move=last_move,
        circles=circles,
        arrows=arrows,
        coordinates=coordinates,
    )
    return svg_to_drawing(svg_text, target_size)


def board_drawing(
    fen: str,
    target_size: float,
    *,
    flipped: bool = False,
    last_move: tuple | None = None,
    circles=(),
    arrows=(),
    coordinates: bool = True,
) -> Drawing | None:
    """Cached entry point used by the PDF writers."""
    spec = (
        fen,
        bool(flipped),
        tuple(last_move) if last_move else None,
        tuple(tuple(c) for c in circles),
        tuple(tuple(a) for a in arrows),
        bool(coordinates),
    )
    drawing = _cached_drawing(spec, float(target_size))
    return drawing


# --------------------------------------------------------------- eval bar

EVAL_WHITE = rl_colors.HexColor("#f2f2f2")
EVAL_BLACK = rl_colors.HexColor("#3a3a3a")
EVAL_BORDER = rl_colors.HexColor("#9a9a9a")


def draw_eval_bar(canvas, x, y, width, height, ev, *, flipped: bool = False,
                  label: bool = True) -> None:
    """Draw a vertical Lichess-style eval bar.

    White's share grows from the bottom (or the top when the board is flipped,
    so the bar always agrees with the side facing the reader).
    """
    canvas.saveState()

    fraction = ev.white_fraction() if ev is not None and ev.known else 0.5
    canvas.setFillColor(EVAL_BLACK)
    canvas.rect(x, y, width, height, stroke=0, fill=1)

    white_height = height * fraction
    canvas.setFillColor(EVAL_WHITE)
    if flipped:
        canvas.rect(x, y + height - white_height, width, white_height, stroke=0, fill=1)
    else:
        canvas.rect(x, y, width, white_height, stroke=0, fill=1)

    canvas.setStrokeColor(EVAL_BORDER)
    canvas.setLineWidth(0.5)
    canvas.rect(x, y, width, height, stroke=1, fill=0)

    # Midpoint tick makes small advantages readable at a glance.
    canvas.setStrokeColor(rl_colors.HexColor("#8a8a8a"))
    canvas.setLineWidth(0.4)
    canvas.line(x, y + height / 2.0, x + width, y + height / 2.0)

    if label and ev is not None and ev.known:
        text = ev.text()
        canvas.setFont("Helvetica-Bold", 7)
        # Pick the ink colour from whatever actually fills the top of the bar.
        strip = 13.0
        if flipped:
            top_is_white = height * fraction >= strip
        else:
            top_is_white = height * fraction >= height - strip
        canvas.setFillColor(
            rl_colors.HexColor("#222222") if top_is_white
            else rl_colors.HexColor("#f0f0f0")
        )
        canvas.drawCentredString(x + width / 2.0, y + height - strip + 1.0, text)

    canvas.restoreState()
