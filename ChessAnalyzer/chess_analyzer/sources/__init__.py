"""Where a game comes from, and one box that accepts all of it.

The import box in the UI takes whatever you have: a Lichess URL, a Chess.com
URL, a bare game id, a FEN, or a PGN pasted straight out of anything.
:func:`resolve` works out which it is, so nobody has to pick "source: lichess"
from a dropdown before pasting a link that says lichess.org in it.

Order matters.  PGN is checked first and by structure -- a ``[Event "..."]``
tag or something that parses as moves -- because a PGN can easily contain the
word ``lichess.org`` in its ``Site`` tag and must not be mistaken for a URL to
go and fetch.
"""

from __future__ import annotations

import re

import chess

from . import chesscom, lichess
from .common import (
    USER_AGENT,
    GameRecord,
    SourceError,
    build_pgn,
    digest,
    is_standard,
    parse_game,
    positions,
    record_from_pgn,
)

#: A PGN is anything with a tag pair, or a line that starts like a move list.
PGN_HINT = re.compile(r"^\s*(\[[A-Za-z0-9_]+\s+\"|1\.\s*[A-Za-z]|1\s+[A-Za-z])")

#: Six space-separated fields, the middle ones from a small alphabet.
FEN_HINT = re.compile(
    r"^\s*([rnbqkpRNBQKP1-8]+/){7}[rnbqkpRNBQKP1-8]+\s+[wb]\s+\S+\s+\S+")


def looks_like_pgn(text: str) -> bool:
    return bool(PGN_HINT.match(text or ""))


def looks_like_fen(text: str) -> bool:
    return bool(FEN_HINT.match(text or ""))


def from_pgn(text: str, *, name: str = "") -> GameRecord:
    """A pasted or uploaded PGN, checked before it is accepted."""
    game = parse_game(text)
    if not is_standard(game):
        raise SourceError(
            f"That is a {game.headers.get('Variant')} game. This reviews "
            "standard chess only -- the evaluation and every accuracy figure "
            "would be meaningless for a variant.")

    finished = game.headers.get("Result", "*") != "*"
    record = record_from_pgn(text, source="pgn", finished=finished)
    if name:
        record.event = name
    return record


def from_fen(fen: str) -> GameRecord:
    """A bare position, wrapped as a zero-move game so everything else works."""
    try:
        board = chess.Board(fen.strip())
    except ValueError as exc:
        raise SourceError(f"That is not a legal position: {exc}") from exc

    pgn = build_pgn(
        {"Event": "Position", "White": "?", "Black": "?", "Result": "*"},
        [], start_fen=board.fen())
    record = record_from_pgn(pgn, source="pgn", finished=False)
    record.event = "Position"
    return record


def resolve(text: str, *, token: str | None = None) -> GameRecord:
    """Whatever you pasted, as a game. Raises with a readable message.

    Structure before hostname: a PGN whose ``Site`` tag says lichess.org is a
    PGN, not a link to fetch.
    """
    candidate = (text or "").strip()
    if not candidate:
        raise SourceError("Paste a game URL, an id, a PGN or a FEN.")

    if looks_like_pgn(candidate):
        return from_pgn(candidate)
    if looks_like_fen(candidate):
        return from_fen(candidate)

    chesscom_reference = chesscom.parse_reference(candidate)
    if chesscom_reference and "chess.com" in candidate.lower():
        kind, game_id = chesscom_reference
        return chesscom.game(game_id, kind=kind)

    if "lichess.org" in candidate.lower():
        game_id = lichess.parse_reference(candidate)
        if game_id:
            return lichess.game(game_id, token=token)
        raise SourceError("That looks like a Lichess link but has no game id in it.")

    # A bare id with no host: 8 alphanumerics is Lichess, a long number is
    # Chess.com. The whole string has to be the id -- searching inside it
    # would make every eight-letter word a Lichess game reference, and turn a
    # typo into "Lichess has no such game" instead of "what is that?".
    if not any(character.isspace() for character in candidate):
        game_id = lichess.parse_reference(candidate)
        if game_id == candidate:
            return lichess.game(game_id, token=token)
        if chesscom_reference:
            kind, chesscom_id = chesscom_reference
            return chesscom.game(chesscom_id, kind=kind)

    raise SourceError(
        "Could not tell what that is. Paste a Lichess or Chess.com game URL, a "
        "game id, a PGN, or a FEN.")


__all__ = [
    "USER_AGENT",
    "GameRecord",
    "SourceError",
    "build_pgn",
    "chesscom",
    "digest",
    "from_fen",
    "from_pgn",
    "is_standard",
    "lichess",
    "looks_like_fen",
    "looks_like_pgn",
    "parse_game",
    "positions",
    "record_from_pgn",
    "resolve",
]
