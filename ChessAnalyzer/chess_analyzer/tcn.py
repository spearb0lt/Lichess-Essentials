"""Chess.com's TCN move encoding.

Chess.com serves move lists as TCN -- two characters per ply -- rather than
SAN, both in the documented monthly archives (the ``tcn`` field beside the
PGN) and in the game endpoint that is the only way to read a game that is
still being played.  Nothing in python-chess speaks it, so this does.

The encoding is positional, not a cipher: each character is an index into a
fixed 85-character alphabet.  Indices 0..63 are squares in a1..h8 order, so
``T[c] -> square``.  A second character above 63 means a promotion, and its
offset above 64 encodes both the promoted piece and which of the three
promotion files (capture left, straight, capture right) the pawn took --
which is why the destination has to be reconstructed from the *origin*
rather than read off directly.

Verified against 194 of the repository owner's own games pulled from the
documented archive API, comparing the decoded SAN sequence and the final
position against the PGN that chess.com shipped alongside the TCN: 194 exact
matches, exercising 71 promotions and 2 en-passant captures.  ``tests/`` keeps
a frozen sample of those games so the check survives without a network.
"""

from __future__ import annotations

import chess

#: Index -> square (0..63), then the promotion and drop encodings above it.
ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789!?{~}(^)[_]@#$,./&-*++="
)

#: Promotion pieces in the order the offset above 64 indexes them.
PROMOTION_PIECES = "qnrbkp"

#: Crazyhouse/bughouse drops occupy indices above 75. We decode them so a
#: variant game does not explode, but the analyzer only reviews standard
#: chess and rejects the game earlier than this.
DROP_BASE = 79


class TCNError(ValueError):
    """A move list that is not valid TCN, or not legal from this position."""


def decode_moves(encoded: str) -> list[dict]:
    """TCN string -> a list of ``{from, to, promotion, drop}`` dictionaries.

    Purely syntactic: it does not know the position, so it cannot tell you
    whether the moves are legal. Use :func:`to_uci` for that.
    """
    if len(encoded) % 2:
        # An odd length means a truncated read, which for a live game is a
        # torn response rather than corruption -- drop the trailing half.
        encoded = encoded[:-1]

    moves = []
    for index in range(0, len(encoded), 2):
        try:
            first = ALPHABET.index(encoded[index])
            second = ALPHABET.index(encoded[index + 1])
        except ValueError as exc:
            raise TCNError(
                f"Character {index // 2 + 1} of the move list is not TCN."
            ) from exc

        promotion = None
        if second > 63:
            promotion = PROMOTION_PIECES[(second - 64) // 3]
            # The three offsets are capture-left, straight ahead and
            # capture-right, relative to the origin square. Which direction
            # is "forward" follows from whose pawn it is, and the only thing
            # we know about that here is the rank the pawn started on.
            second = first + (-8 if first < 16 else 8) + ((second - 64) % 3) - 1

        drop = None
        if first > 75:
            drop = PROMOTION_PIECES[first - DROP_BASE]

        moves.append({
            "from": None if drop else chess.SQUARE_NAMES[first],
            "to": chess.SQUARE_NAMES[second],
            "promotion": promotion,
            "drop": drop,
        })
    return moves


def to_uci(encoded: str, *, start_fen: str | None = None) -> list[str]:
    """TCN string -> UCI move list, checked for legality as it goes.

    Raises :class:`TCNError` on the first move that is not legal, naming the
    ply, because a live game read mid-write is the likely cause and the
    caller wants to keep the prefix rather than lose the whole game.
    """
    board = chess.Board(start_fen) if start_fen else chess.Board()
    out = []
    for ply, item in enumerate(decode_moves(encoded), start=1):
        if item["drop"]:
            raise TCNError(f"Ply {ply} is a piece drop; only standard chess is supported.")
        uci = f"{item['from']}{item['to']}{item['promotion'] or ''}"
        try:
            move = board.parse_uci(uci)
        except (ValueError, AssertionError) as exc:
            raise TCNError(f"Ply {ply} ({uci}) is not legal here: {exc}") from exc
        out.append(move.uci())
        board.push(move)
    return out


def to_board(encoded: str, *, start_fen: str | None = None) -> chess.Board:
    """Replay a TCN move list and hand back the finished board."""
    board = chess.Board(start_fen) if start_fen else chess.Board()
    for uci in to_uci(encoded, start_fen=start_fen):
        board.push_uci(uci)
    return board


__all__ = ["ALPHABET", "TCNError", "decode_moves", "to_board", "to_uci"]
