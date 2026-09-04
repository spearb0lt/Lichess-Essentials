"""Turning centipawns into the numbers a review actually shows.

A centipawn score is a terrible unit for judging a move, because its meaning
depends on the position: throwing away 200 centipawns from +0.1 loses the
game, and throwing away 200 from +9 loses nothing.  Every published accuracy
metric therefore converts to **winning chances** first and measures the drop
in *those*.

The conversion here is Lichess's own logistic curve, and the accuracy formula
is the one Lichess documents and uses -- chosen over guessing at Chess.com's,
which is not published.  Chess.com's own numbers come out a few points
different on the same game; that is expected, and the review labels which
scale it is showing rather than implying the two agree.

Every score entering this module is **White's point of view**, matching the
sibling app's ``Eval`` and ``PovScore.white()``.  Functions that judge a move
take the mover's colour and flip internally, because a 30-point drop in
White's winning chances is a *gain* for Black.
"""

from __future__ import annotations

import math
from statistics import pstdev

import chess

#: Lichess's centipawn -> winning-chances constant. Same value the sibling
#: app uses for its eval bar, so bar and review never disagree.
WIN_K = -0.00368208

#: The accuracy curve: ``103.1668 * exp(-0.04354 * loss) - 3.1669``, fitted by
#: Lichess so that a perfect game lands on 100 and a typical one lands where
#: players expect. Kept as named constants because they are a fit, not maths.
ACCURACY_SCALE = 103.1668
ACCURACY_DECAY = -0.04354
ACCURACY_SHIFT = -3.1669

#: Win-chance points lost, above which a move earns each judgment. These are
#: Lichess's thresholds; :mod:`chess_analyzer.classify` layers the
#: Chess.com-style ladder on top of the same numbers.
INACCURACY = 10.0
MISTAKE = 20.0
BLUNDER = 30.0


def win_percent(cp: int | None, mate: int | None = None) -> float:
    """White's winning chances in ``[0, 100]``.

    A forced mate is 100 or 0 rather than a huge centipawn number, because
    the difference between mate in 3 and mate in 9 does not change how good
    the position is, and letting it scale would make every move in a won
    endgame look like a blunder.
    """
    if mate is not None:
        if mate > 0:
            return 100.0
        if mate < 0:
            return 0.0
        # mate == 0 means mate is already on the board: the side to move has
        # been mated, so the *other* side won.
        return 0.0
    if cp is None:
        return 50.0
    capped = max(-1500, min(1500, int(cp)))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(WIN_K * capped)) - 1.0)


def pov_win_percent(cp, mate, color: bool) -> float:
    """The same figure from ``color``'s point of view."""
    white = win_percent(cp, mate)
    return white if color == chess.WHITE else 100.0 - white


def move_accuracy(before: float, after: float) -> float:
    """How accurate one move was, in ``[0, 100]``.

    Both arguments are the mover's winning chances -- before the move and
    after it.  A move that improves the position is 100, not more.
    """
    loss = max(0.0, before - after)
    value = ACCURACY_SCALE * math.exp(ACCURACY_DECAY * loss) + ACCURACY_SHIFT
    return max(0.0, min(100.0, value))


def harmonic_mean(values: list[float]) -> float:
    """Harmonic mean, which punishes one terrible move properly.

    An arithmetic mean lets 40 good moves hide a game-losing one; the
    harmonic mean does not, which is why Lichess averages the two rather
    than picking a side.
    """
    usable = [max(v, 0.5) for v in values]      # a zero would make it zero
    if not usable:
        return 0.0
    return len(usable) / sum(1.0 / v for v in usable)


def volatility_weights(pov_percents: list[float], moves: int) -> list[float]:
    """How much each move counts, from how sharp the position was.

    A quiet position where anything holds should not count as much as a
    knife-edge one, so each move is weighted by the standard deviation of
    the winning chances in a window around it.  Window size grows with game
    length exactly as Lichess's does.
    """
    if len(pov_percents) < 2:
        return [1.0] * max(0, len(pov_percents) - 1)

    window = max(2, min(8, moves // 10))
    weights = []
    for index in range(len(pov_percents) - 1):
        start = max(0, index - window + 1)
        chunk = pov_percents[start:index + 2]
        spread = pstdev(chunk) if len(chunk) > 1 else 0.0
        weights.append(max(0.5, min(12.0, spread)))
    return weights


def game_accuracy(win_percents: list[float], color: bool) -> float:
    """One side's accuracy over a whole game, in ``[0, 100]``.

    ``win_percents`` is White's winning chances at every position in the
    game including the start, in order.  Only the transitions where
    ``color`` was the one to move are judged.
    """
    if len(win_percents) < 2:
        return 100.0

    pov = [w if color == chess.WHITE else 100.0 - w for w in win_percents]
    weights = volatility_weights(pov, len(win_percents) - 1)

    accuracies, used = [], []
    for index in range(len(pov) - 1):
        # Position `index` has `index` moves played, so White moved out of
        # the even-numbered ones.
        mover = chess.WHITE if index % 2 == 0 else chess.BLACK
        if mover != color:
            continue
        accuracies.append(move_accuracy(pov[index], pov[index + 1]))
        used.append(weights[index])

    if not accuracies:
        return 100.0

    total = sum(used) or 1.0
    weighted = sum(a * w for a, w in zip(accuracies, used)) / total
    return round((weighted + harmonic_mean(accuracies)) / 2.0, 1)


def phase_accuracy(rows: list[dict], color: bool, phase: str) -> float | None:
    """Accuracy over one phase only, or None if the side never moved in it.

    Chess.com breaks its review down this way and it is the most useful part
    of the report: "72% opening" tells you where to spend a week far better
    than one number for the whole game does.
    """
    values = [
        row["accuracy"] for row in rows
        if row["color"] == ("white" if color == chess.WHITE else "black")
        and row["phase"] == phase
    ]
    if not values:
        return None
    return round((sum(values) / len(values) + harmonic_mean(values)) / 2.0, 1)


# --------------------------------------------------------------- game phase

#: Pieces that are neither pawns nor kings, which is what "how far into the
#: game are we" actually tracks.
MAJORS_AND_MINORS = (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)


def piece_count(board: chess.Board) -> int:
    return sum(len(board.pieces(kind, color))
               for kind in MAJORS_AND_MINORS
               for color in (chess.WHITE, chess.BLACK))


def backrank_sparse(board: chess.Board) -> bool:
    """True once either home rank has emptied out.

    Development, not material, is what ends an opening: a board still full of
    pieces that have all left the back rank is a middlegame.
    """
    for color, rank in ((chess.WHITE, 0), (chess.BLACK, 7)):
        occupied = 0
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            if piece is not None and piece.color == color:
                occupied += 1
        if occupied < 4:
            return True
    return False


def phase_boundaries(boards: list[chess.Board]) -> tuple[int, int]:
    """Where the middlegame and the endgame start, as ply indexes.

    Follows the shape of Lichess's own divider -- piece count, plus a
    developed back rank for the opening -- rather than a fixed move number,
    which gets a 40-move theoretical line badly wrong.  Returns
    ``(middlegame_ply, endgame_ply)``; either can be ``len(boards)`` when the
    game never got there.
    """
    total = len(boards)
    middle = end = total

    for ply, board in enumerate(boards):
        pieces = piece_count(board)
        if middle == total and (pieces <= 10 or backrank_sparse(board)):
            middle = ply
        if end == total and pieces <= 6:
            end = ply
            break

    # A game cannot reach its endgame before its middlegame.
    return min(middle, end), end


def phase_of(ply: int, middle: int, end: int) -> str:
    if ply >= end:
        return "endgame"
    if ply >= middle:
        return "middlegame"
    return "opening"


__all__ = [
    "BLUNDER",
    "INACCURACY",
    "MISTAKE",
    "WIN_K",
    "backrank_sparse",
    "game_accuracy",
    "harmonic_mean",
    "move_accuracy",
    "phase_accuracy",
    "phase_boundaries",
    "phase_of",
    "piece_count",
    "pov_win_percent",
    "win_percent",
]
