"""Naming moves: the Lichess judgment, and the Chess.com-style ladder.

Two scales, computed from the same numbers and reported side by side, because
they answer different questions and disagree on purpose.

**The Lichess scale** is three labels -- inaccuracy, mistake, blunder -- at 10,
20 and 30 winning-chance points lost.  It is published, so this is not an
approximation of it: it *is* it.

**The Chess.com-style ladder** is the ten-label one with brilliant, great and
miss in it.  Chess.com has never published its criteria, so this cannot be a
reimplementation and does not pretend to be.  What it is: the same idea built
from stated rules, listed in ``CHESSCOM_RULES`` and shown in the UI, so that
when a label surprises you you can see why it fired instead of guessing.  On
the sample games it lands close to Chess.com's own labels for best/excellent/
good/inaccuracy/mistake/blunder, and less closely for brilliant and great,
which is where the unpublished judgment mostly lives.

The interesting part is the sacrifice test behind *brilliant*.  Rather than a
hand-rolled static exchange evaluation, it reads material off the engine's own
principal variation for the resulting position: if the engine's best play
leaves you a piece down four plies later and still says you are fine, you gave
up material and it worked.  That is exactly what a sound sacrifice is, and the
engine is a better judge of it than any swap-off heuristic.
"""

from __future__ import annotations

import chess

from .accuracy import (
    BLUNDER,
    INACCURACY,
    MISTAKE,
    move_accuracy,
    pov_win_percent,
)

#: Material, in centipawns, for the sacrifice test only. Kings are excluded:
#: they cannot be captured, and giving one a value would swamp the arithmetic.
PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
}

#: Chess.com-style thresholds, in winning-chance points lost. Tighter than the
#: Lichess ones at the good end -- which is why the same game shows more
#: "mistakes" on this scale and fewer on that one.
CC_EXCELLENT = 2.0
CC_GOOD = 5.0
CC_INACCURACY = 10.0
CC_MISTAKE = 20.0

#: Most centipawns a single move may contribute to average centipawn loss.
#: Ten pawns is already "you lost the game here"; letting a move count 4,000
#: because the eval was a forced mate would make ACPL a blunder detector
#: rather than an average.
CP_LOSS_CAP = 1000

#: How much material the mover must still be down, four plies into the
#: engine's line, for the move to count as a sacrifice rather than a trade.
SACRIFICE_CP = 180

#: How far the second-best move must trail for the best one to be "great":
#: the point of the label is that there was only one move, so the gap has to
#: be big enough that the alternatives genuinely fail.
ONLY_MOVE_GAP = 15.0

#: Neither "great" nor "brilliant" is awarded once the game is effectively
#: over. Finding the only move at 96% is not a feat, it is arithmetic, and a
#: review that hands out medals in a won endgame stops meaning anything.
DECIDED = 92.0

#: Winning chances the mover must keep for a sacrifice to be brilliant rather
#: than merely desperate. Throwing a piece at a lost position is not brilliant.
BRILLIANT_FLOOR = 45.0

#: How good the position already was for a thrown-away advantage to read as a
#: missed opportunity rather than an ordinary error.
MISS_FLOOR = 65.0
MISS_DROP = 15.0

#: What each label means, verbatim, for the UI's own tooltip. If you change a
#: rule above, change its sentence here -- this is the only documentation a
#: user of the app ever sees.
CHESSCOM_RULES = {
    "brilliant": (
        "Engine's top move, gives up at least 1.8 pawns of material that the "
        "engine's own line never wins back, and still keeps you at 45% winning "
        "chances or better. Never a recapture, and never once the game is "
        "already decided (past 92%)."
    ),
    "great": (
        "Engine's top move, and the second-best move is at least 15 winning-"
        "chance points worse -- there was only one move and you found it. "
        "Never a recapture, since taking back the piece that was just taken "
        "beats the alternatives for reasons that are not to your credit, and "
        "never once the game is already decided."
    ),
    "best": "The engine's first choice.",
    "book": "Still inside published theory, per Lichess's openings dataset. Book moves are excluded from accuracy and centipawn loss, so preparation is not scored as skill.",
    "excellent": "Not the top move, but costs under 2 winning-chance points.",
    "good": "Costs between 2 and 5 winning-chance points.",
    "inaccuracy": "Costs between 5 and 10 winning-chance points.",
    "miss": (
        "You had a forced mate, or a position at 65% or better, and let it go "
        "-- the opportunity mattered more than the size of the error."
    ),
    "mistake": "Costs between 10 and 20 winning-chance points.",
    "blunder": "Costs 20 winning-chance points or more.",
    "forced": "The only legal move.",
}

#: Display order, worst last, and the glyph each label wears on the board.
LABELS = [
    ("brilliant", "!!"),
    ("great", "!"),
    ("best", "*"),
    ("book", "B"),
    ("excellent", "+"),
    ("good", "o"),
    ("forced", "="),
    ("inaccuracy", "?!"),
    ("mistake", "?"),
    ("miss", "x"),
    ("blunder", "??"),
]

LABEL_GLYPHS = dict(LABELS)


def material(board: chess.Board, color: bool) -> int:
    """Centipawn material for one side, pawns and pieces, no king."""
    return sum(
        len(board.pieces(kind, color)) * value
        for kind, value in PIECE_VALUES.items()
    )


def material_delta(board: chess.Board, color: bool) -> int:
    """How far ``color`` is ahead on material, in centipawns."""
    return material(board, color) - material(board, not color)


def sacrifice_depth(after: chess.Board, pv: list[str], color: bool,
                    plies: int = 4) -> int:
    """The worst material deficit ``color`` runs, walking the engine's line.

    ``after`` is the position the played move produced and ``pv`` the
    engine's best continuation from it, as UCI strings.  Returns the minimum
    material delta seen over the position itself and the next ``plies`` moves,
    which is negative when the mover is down.

    Walking the line rather than the move itself is what separates a real
    sacrifice from a trade: an exchange dips and comes straight back, and this
    reports the recovery.  A sound sacrifice never recovers, which is the
    whole idea.
    """
    worst = material_delta(after, color)
    board = after.copy(stack=False)
    for uci in pv[:plies]:
        try:
            board.push_uci(uci)
        except (ValueError, AssertionError):
            break
        worst = min(worst, material_delta(board, color))
    return worst


def _lichess_judgment(loss: float) -> str | None:
    if loss >= BLUNDER:
        return "blunder"
    if loss >= MISTAKE:
        return "mistake"
    if loss >= INACCURACY:
        return "inaccuracy"
    return None


def _chesscom_label(
    *,
    loss: float,
    before: float,
    after: float,
    is_best: bool,
    forced: bool,
    in_book: bool,
    second_best_loss: float | None,
    sacrificed: int | None,
    missed_mate: bool,
    recapture: bool,
) -> str:
    """The Chess.com-style label. See ``CHESSCOM_RULES`` for every rule."""
    if forced:
        return "forced"
    if in_book:
        return "book"

    # Missing a forced mate is the one error worth naming ahead of its size:
    # "blunder" tells you the eval moved, "miss" tells you what you missed.
    if missed_mate:
        return "miss"

    if is_best:
        # Taking back the piece that was just taken is the move everyone
        # finds, and it scores enormously against the alternatives precisely
        # because the alternatives hang a piece. Without this, half the
        # recaptures in a game come out "great".
        remarkable = not recapture and before < DECIDED and before > (100 - DECIDED)
        if (remarkable and sacrificed is not None
                and sacrificed <= -SACRIFICE_CP and after >= BRILLIANT_FLOOR):
            return "brilliant"
        if (remarkable and second_best_loss is not None
                and second_best_loss >= ONLY_MOVE_GAP):
            return "great"
        return "best"

    if loss < CC_EXCELLENT:
        return "excellent"
    if loss < CC_GOOD:
        return "good"
    if loss < CC_INACCURACY:
        return "inaccuracy"

    # A thrown-away winning position reads better as a missed chance than as
    # a mistake of a particular size.
    if before >= MISS_FLOOR and (before - after) >= MISS_DROP:
        return "miss"

    if loss < CC_MISTAKE:
        return "mistake"
    return "blunder"


def classify(
    *,
    board_before: chess.Board,
    move: chess.Move,
    best_before,
    second_before,
    eval_after,
    after_board: chess.Board | None = None,
    after_pv: list[str] | None = None,
    in_book: bool = False,
    previous_move: chess.Move | None = None,
) -> dict:
    """Judge one played move.

    ``best_before`` and ``second_before`` are the engine's first and second
    choices for ``board_before``, as ``{cp, mate, pv}`` dictionaries in White's
    point of view; ``second_before`` may be None when only one line was asked
    for or only one move is legal.  ``eval_after`` is the same shape for the
    position the move produced.

    Everything returned is already from the mover's point of view, so the UI
    never has to think about signs.
    """
    color = board_before.turn
    legal = board_before.legal_moves.count()
    forced = legal <= 1

    before_pov = pov_win_percent(
        best_before.get("cp"), best_before.get("mate"), color)
    after_pov = pov_win_percent(
        eval_after.get("cp"), eval_after.get("mate"), color)
    # A move cannot be better than the best move, and search noise between two
    # separate analyses can say otherwise; clamp so no move scores above 100%.
    after_pov = min(after_pov, before_pov)
    loss = max(0.0, before_pov - after_pov)

    best_uci = (best_before.get("pv") or [None])[0]
    is_best = best_uci == move.uci()

    second_best_loss = None
    if second_before is not None:
        second_pov = pov_win_percent(
            second_before.get("cp"), second_before.get("mate"), color)
        second_best_loss = max(0.0, before_pov - second_pov)

    best_mate = best_before.get("mate")
    played_mate = eval_after.get("mate")
    # Mate counts are White-POV, so "a mate for the mover" is a positive
    # number for White and a negative one for Black.
    had_mate = best_mate is not None and (
        best_mate > 0 if color == chess.WHITE else best_mate < 0)
    kept_mate = played_mate is not None and (
        played_mate > 0 if color == chess.WHITE else played_mate < 0)
    missed_mate = had_mate and not kept_mate

    sacrificed = None
    if after_board is not None:
        sacrificed = sacrifice_depth(after_board, after_pv or [], color)

    recapture = bool(
        previous_move is not None
        and board_before.is_capture(move)
        and move.to_square == previous_move.to_square
    )

    label = _chesscom_label(
        loss=loss,
        before=before_pov,
        after=after_pov,
        is_best=is_best,
        forced=forced,
        in_book=in_book,
        second_best_loss=second_best_loss,
        sacrificed=sacrificed,
        missed_mate=missed_mate,
        recapture=recapture,
    )

    cp_before, cp_after = best_before.get("cp"), eval_after.get("cp")
    cp_loss = None
    if cp_before is not None and cp_after is not None:
        signed = cp_before - cp_after
        # Capped, because average centipawn loss is an *average*: one move
        # that drops 40 pawns of eval in an already-decided position would
        # otherwise set the figure for the whole game on its own.
        cp_loss = min(CP_LOSS_CAP, max(0, signed if color == chess.WHITE
                                       else -signed))

    return {
        "label": label,
        "glyph": LABEL_GLYPHS.get(label, ""),
        "judgment": _lichess_judgment(loss),
        "isBest": is_best,
        "forced": forced,
        "inBook": in_book,
        "bestMove": best_uci,
        "winBefore": round(before_pov, 1),
        "winAfter": round(after_pov, 1),
        "winLoss": round(loss, 1),
        "cpLoss": cp_loss,
        "accuracy": round(move_accuracy(before_pov, after_pov), 1),
        "onlyMoveGap": (None if second_best_loss is None
                        else round(second_best_loss, 1)),
        "sacrifice": sacrificed,
        "missedMate": missed_mate,
    }


__all__ = [
    "CHESSCOM_RULES",
    "LABELS",
    "LABEL_GLYPHS",
    "PIECE_VALUES",
    "classify",
    "material",
    "material_delta",
    "sacrifice_depth",
]
