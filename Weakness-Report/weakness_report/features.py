"""What kind of position is this?

"You lose 0.4 pawns a game in the middlegame" is nearly useless -- the
middlegame is most of chess.  "You lose 0.4 pawns a game in *queenless*
middlegames" is a sentence you can act on, because it names something you can
recognise at the board and decide to study.

So every move gets a handful of labels describing the position it was played
from, and the report slices your history by them.  The rules here are all
computable from a FEN alone and are deliberately simple, because a feature
nobody can check is a feature nobody should trust.  Each one is written out in
its docstring and shown in the report's glossary.

Two things this file is careful about:

*Everything is from your point of view.*  ``material`` says whether **you**
are ahead, not whether White is.  A report that quietly switched sides halfway
would be worse than no report.

*Nothing here is a judgement.*  A closed centre is not a bad position; it is a
kind of position.  Whether you play it badly is what the aggregation works
out, and it works that out from your own results.
"""

from __future__ import annotations

import chess

#: Ordinary piece values, only ever used for "am I ahead or behind".  The
#: king is worth nothing here because both sides always have exactly one.
VALUES = {chess.PAWN: 1.0, chess.KNIGHT: 3.0, chess.BISHOP: 3.0,
          chess.ROOK: 5.0, chess.QUEEN: 9.0}

#: A material edge smaller than this is not worth calling an edge -- it is one
#: pawn's worth of noise in a position that could be about anything.
MATERIAL_EDGE = 1.0

#: Pawn pairs locked head to head.  Nought is an open position, a wall of them
#: is a closed one, and the boundary is drawn where most people would draw it.
CLOSED_LOCKS = 3
OPEN_LOCKS = 1


def queens(board: chess.Board) -> str:
    """``queens on``, ``one queen`` or ``queens off``.

    The single most useful split in a middlegame: the same player is often a
    different player with the queens off.
    """
    white = bool(board.pieces(chess.QUEEN, chess.WHITE))
    black = bool(board.pieces(chess.QUEEN, chess.BLACK))
    if white and black:
        return "queens on"
    if white or black:
        return "one queen"
    return "queens off"


def _wing(square: int | None) -> str:
    """Which side of the board a king is standing on."""
    if square is None:
        return "centre"
    file = chess.square_file(square)
    if file <= 2:
        return "queenside"
    if file >= 5:
        return "kingside"
    return "centre"


def king_side(board: chess.Board, me: bool) -> str:
    """Where **your** king is: ``kingside``, ``centre`` or ``queenside``.

    Read off the position rather than from whether you castled, because that
    is what actually matters and because a king walked to safety counts the
    same as one that castled there.
    """
    return _wing(board.king(me))


def opposite_castling(board: chess.Board, me: bool) -> bool:
    """Are the two kings on opposite wings? The sharpest kind of position."""
    mine = _wing(board.king(me))
    theirs = _wing(board.king(not me))
    return (mine in ("kingside", "queenside") and theirs in ("kingside", "queenside")
            and mine != theirs)


def locked_pawns(board: chess.Board) -> int:
    """Pawn pairs standing directly head to head, which is what jams a centre."""
    locks = 0
    for square in board.pieces(chess.PAWN, chess.WHITE):
        ahead = square + 8
        if ahead <= 63 and board.piece_at(ahead) == chess.Piece(chess.PAWN,
                                                                chess.BLACK):
            locks += 1
    return locks


def centre(board: chess.Board) -> str:
    """``open``, ``semi-open`` or ``closed``, by how many pawns are jammed."""
    locks = locked_pawns(board)
    if locks >= CLOSED_LOCKS:
        return "closed centre"
    if locks >= OPEN_LOCKS:
        return "semi-open centre"
    return "open centre"


def material(board: chess.Board, me: bool) -> str:
    """``material level``, ``material ahead`` or ``material behind``, yours."""
    total = 0.0
    for piece_type, value in VALUES.items():
        total += value * (len(board.pieces(piece_type, me))
                          - len(board.pieces(piece_type, not me)))
    if total >= MATERIAL_EDGE:
        return "material ahead"
    if total <= -MATERIAL_EDGE:
        return "material behind"
    return "material level"


def _bishop_colours(board: chess.Board, colour: bool) -> set:
    return {chess.square_rank(square) % 2 == chess.square_file(square) % 2
            for square in board.pieces(chess.BISHOP, colour)}


def ending(board: chess.Board) -> str:
    """What kind of ending this is, by which pieces are left.

    Returns ``""`` when there is too much on the board to call it an ending;
    the report only asks for this inside the endgame phase anyway, and having
    it answer honestly outside is what keeps a rook ending from being counted
    twice.
    """
    counts = {}
    for colour in (chess.WHITE, chess.BLACK):
        for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
            counts[piece_type] = counts.get(piece_type, 0) + len(
                board.pieces(piece_type, colour))

    minors = counts[chess.KNIGHT] + counts[chess.BISHOP]
    rooks, queens_left = counts[chess.ROOK], counts[chess.QUEEN]

    if queens_left:
        return "queen ending" if not rooks and not minors else ""
    if not rooks and not minors:
        return "pawn ending"
    if rooks and not minors:
        return "rook ending"
    if minors and not rooks:
        white_bishops = _bishop_colours(board, chess.WHITE)
        black_bishops = _bishop_colours(board, chess.BLACK)
        if (len(white_bishops) == 1 and len(black_bishops) == 1
                and not counts[chess.KNIGHT]
                and white_bishops != black_bishops):
            return "opposite bishops"
        return "minor piece ending"
    if rooks and minors:
        return "rook and minor ending"
    return ""


def describe(fen: str, me: bool) -> dict:
    """Every feature of one position, from your side. ``me`` is your colour."""
    try:
        board = chess.Board(fen)
    except ValueError:
        return {}
    return {
        "queens": queens(board),
        "kingSide": king_side(board, me),
        "oppositeCastling": opposite_castling(board, me),
        "centre": centre(board),
        "material": material(board, me),
        "ending": ending(board),
        "pieces": sum(len(board.pieces(piece_type, colour))
                      for piece_type in VALUES for colour in (True, False)),
    }


#: What each feature means, shown in the report so nobody has to guess.
GLOSSARY = {
    "queens on": "both queens still on the board",
    "one queen": "one side has a queen and the other does not",
    "queens off": "both queens traded",
    "kingside": "your king on the f, g or h file",
    "centre": "your king still on the d or e file",
    "queenside": "your king on the a, b or c file",
    "opposite castling": "the kings on opposite wings",
    "open centre": "no pawns standing head to head",
    "semi-open centre": "one or two pawn pairs locked",
    "closed centre": "three or more pawn pairs locked",
    "material level": "within a pawn either way",
    "material ahead": "a pawn or more up",
    "material behind": "a pawn or more down",
    "pawn ending": "kings and pawns only",
    "rook ending": "rooks and pawns, no minor pieces",
    "minor piece ending": "minor pieces and pawns, no rooks",
    "opposite bishops": "one bishop each, on opposite colours, no knights",
    "rook and minor ending": "rooks and minor pieces still on",
    "queen ending": "queens and pawns only",
}


__all__ = [
    "CLOSED_LOCKS",
    "GLOSSARY",
    "MATERIAL_EDGE",
    "centre",
    "describe",
    "ending",
    "king_side",
    "locked_pawns",
    "material",
    "opposite_castling",
    "queens",
]
