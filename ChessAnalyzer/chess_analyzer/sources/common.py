"""The one shape every game source produces.

Lichess, Chess.com and a pasted PGN arrive in three unrelated formats -- ndjson
with a PGN inside it, JSON with a TCN move string, and a text file.  Everything
downstream of here works on :class:`GameRecord` instead, so the review, the
library and the browser never learn which site a game came from.

``id`` is deliberately stable and derived from the source: re-importing the
same Lichess game finds the cached review instead of re-running the engine,
and the same pasted PGN hashes to the same id whatever whitespace it arrived
with.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field

import chess
import chess.pgn

USER_AGENT = ("chess-analyzer/0.1 (local game review tool; "
              "https://github.com/lichess-essentials)")


class SourceError(RuntimeError):
    """A game could not be fetched or understood, with a message to show."""


@dataclass
class GameRecord:
    """One game, however it arrived."""

    id: str
    source: str                      # "lichess" | "chesscom" | "pgn"
    pgn: str
    url: str = ""
    white: str = "?"
    black: str = "?"
    white_elo: str = ""
    black_elo: str = ""
    result: str = "*"
    date: str = ""
    event: str = ""
    time_control: str = ""
    speed: str = ""
    variant: str = "standard"
    finished: bool = True
    turn: str = "white"
    clocks: dict = field(default_factory=dict)
    opening: dict = field(default_factory=dict)
    ply_count: int = 0

    def json(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "white": self.white,
            "black": self.black,
            "whiteElo": self.white_elo,
            "blackElo": self.black_elo,
            "result": self.result,
            "date": self.date,
            "event": self.event,
            "timeControl": self.time_control,
            "speed": self.speed,
            "variant": self.variant,
            "finished": self.finished,
            "turn": self.turn,
            "clocks": self.clocks,
            "opening": self.opening,
            "plyCount": self.ply_count,
        }


def parse_game(pgn: str) -> chess.pgn.Game:
    """PGN text -> a python-chess game, or a message worth reading."""
    if not pgn or not pgn.strip():
        raise SourceError("That PGN is empty.")
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
    except Exception as exc:                      # noqa: BLE001
        raise SourceError(f"That PGN would not parse: {exc}") from exc
    if game is None:
        raise SourceError("That PGN contains no game.")
    if game.errors:
        # python-chess collects illegal-move errors rather than raising, which
        # would otherwise leave a game silently truncated at the bad move.
        first = game.errors[0]
        raise SourceError(f"That PGN has an illegal or unreadable move: {first}")
    return game


def is_standard(game: chess.pgn.Game) -> bool:
    """Only standard chess is reviewed; variants get an honest refusal."""
    variant = (game.headers.get("Variant", "") or "").strip().lower()
    return variant in ("", "standard", "chess", "from position")


def positions(game: chess.pgn.Game) -> tuple[list[chess.Board], list[chess.Move]]:
    """Every position in the mainline, and the moves between them.

    Returns ``len(moves) + 1`` boards: one per position including the start,
    which is the shape the review and the phase divider both want.
    """
    board = game.board()
    boards = [board.copy(stack=False)]
    moves = []
    for move in game.mainline_moves():
        if move not in board.legal_moves:
            break
        moves.append(move)
        board.push(move)
        boards.append(board.copy(stack=False))
    return boards, moves


def digest(text: str) -> str:
    """A stable short id for a pasted PGN, insensitive to whitespace."""
    normalised = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]


def build_pgn(headers: dict, ucis: list[str], *,
              start_fen: str | None = None) -> str:
    """A move list plus tags -> PGN text.

    Both live sources hand over moves rather than PGN, and everything after
    this point speaks PGN, so this is where they meet.
    """
    game = chess.pgn.Game()
    for key, value in headers.items():
        if value not in (None, ""):
            game.headers[key] = str(value)

    if start_fen and start_fen != chess.STARTING_FEN:
        game.headers["FEN"] = start_fen
        game.headers["SetUp"] = "1"

    board = chess.Board(start_fen) if start_fen else chess.Board()
    node = game
    for uci in ucis:
        try:
            move = board.parse_uci(uci)
        except (ValueError, AssertionError):
            break
        node = node.add_variation(move)
        board.push(move)

    exporter = chess.pgn.StringExporter(headers=True, variations=False,
                                        comments=False)
    return game.accept(exporter)


def record_from_pgn(pgn: str, *, source: str, game_id: str | None = None,
                    url: str = "", finished: bool = True,
                    speed: str = "", clocks: dict | None = None,
                    opening: dict | None = None) -> GameRecord:
    """Fill in a record's fields from the PGN's own tags."""
    game = parse_game(pgn)
    headers = game.headers
    boards, moves = positions(game)
    board = boards[-1]

    named = opening or {}
    if not named and headers.get("ECO"):
        named = {"eco": headers.get("ECO", ""),
                 "name": headers.get("Opening", "") or headers.get("ECO", "")}

    return GameRecord(
        id=game_id or f"{source}-{digest(pgn)}",
        source=source,
        pgn=pgn,
        url=url or headers.get("Site", ""),
        white=headers.get("White", "?"),
        black=headers.get("Black", "?"),
        white_elo=headers.get("WhiteElo", ""),
        black_elo=headers.get("BlackElo", ""),
        result=headers.get("Result", "*"),
        date=headers.get("UTCDate") or headers.get("Date", ""),
        event=headers.get("Event", ""),
        time_control=headers.get("TimeControl", ""),
        speed=speed,
        variant=headers.get("Variant", "standard"),
        finished=finished,
        turn="white" if board.turn == chess.WHITE else "black",
        clocks=clocks or {},
        opening=named,
        ply_count=len(moves),
    )


__all__ = [
    "USER_AGENT",
    "GameRecord",
    "SourceError",
    "build_pgn",
    "digest",
    "is_standard",
    "parse_game",
    "positions",
    "record_from_pgn",
]
