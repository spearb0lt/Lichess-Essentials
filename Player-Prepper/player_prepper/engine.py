"""The engine, used for exactly one thing: filling a gap.

Scouting is arithmetic on somebody's results and needs no engine at all --
which is why every number in a report is computed without one, and why this
module is optional.  What the engine adds is the sentence after the finding:
a gap says *"they play 3...Bb4 here forty times and you have written nothing"*,
and it is a great deal more useful if it also says *"the engine likes 4.e5"*.

So this is deliberately small.  There is no evaluation of their moves, no
accuracy, no judgement of anything they played.  Given a position you have no
answer for, it returns the engine's best few moves and stops.

The ladder itself -- warm local Stockfish, Lichess cloud when there is none,
disk cache in front of both -- belongs to the sibling exporter and is used
as-is rather than reimplemented.  Reaching into ``EvalProvider`` for its
engine handle and lock is the same coupling Repertoire-Creator has, for the
same reason: a UCI engine is one conversation at a time, and a second
``analyse()`` while one is in flight corrupts both.
"""

from __future__ import annotations

import threading
from pathlib import Path

import chess
import chess.engine
import requests

from .bridge import FeatureUnavailable, optional, require

CACHE_FILE = Path.home() / ".cache" / "player-prepper" / "evals.json"

#: More than a handful of candidate moves for a gap is a menu, not advice.
MAX_LINES = 5

#: How much of the principal variation to show. Enough to see the idea.
PV_MOVES = 8

_PROVIDER = None
_PROVIDER_LOCK = threading.Lock()


def available() -> bool:
    return optional("evals") is not None


def get_provider():
    """One warm engine for the whole process."""
    global _PROVIDER
    evals = require("evals", "Engine suggestions")
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = evals.EvalProvider(
                CACHE_FILE,
                use_cloud=True,
                stockfish_path=evals.find_stockfish(),
                movetime=0.3,
            )
        return _PROVIDER


def close_provider() -> None:
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is not None:
            try:
                _PROVIDER.close()
            except Exception:                                # noqa: BLE001
                pass
            _PROVIDER = None


def _score_json(pov: chess.engine.PovScore) -> dict:
    """A score from White's point of view, matching both sibling apps."""
    white = pov.white()
    cp, mate = white.score(), white.mate()
    if mate is not None:
        text = ("+M" if mate > 0 else "-M") + str(abs(mate))
    else:
        text = f"{(cp or 0) / 100:+.2f}"
    return {"cp": cp, "mate": mate, "text": text}


def _describe(board: chess.Board, moves) -> dict:
    """A principal variation as readable notation, plus its first move."""
    replay = board.copy(stack=False)
    parts, first = [], None
    for index, move in enumerate(moves[:PV_MOVES]):
        if move not in replay.legal_moves:
            break
        san = replay.san(move)
        if index == 0:
            first = {"uci": move.uci(), "san": san}
        number = replay.fullmove_number
        if replay.turn == chess.WHITE:
            parts.append(f"{number}.{san}")
        elif not parts:
            parts.append(f"{number}...{san}")
        else:
            parts.append(san)
        replay.push(move)
    return {"line": " ".join(parts), "first": first}


def _cloud_lines(fen: str, count: int) -> list:
    """Ask the Lichess cloud for several variations at once.

    Only answers for positions already in its database, which in practice
    means openings -- which is exactly where a scouting gap lives, so this is
    a better fallback here than it would be anywhere else.
    """
    try:
        response = requests.get(
            "https://lichess.org/api/cloud-eval",
            params={"fen": fen, "multiPv": count},
            headers={"User-Agent": "player-prepper/0.1"}, timeout=8)
    except requests.RequestException:
        return []
    if not response.ok:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    try:
        board = chess.Board(fen)
    except ValueError:
        return []

    lines = []
    for rank, pv in enumerate(data.get("pvs") or [], start=1):
        moves = []
        for uci in (pv.get("moves") or "").split()[:PV_MOVES]:
            try:
                moves.append(chess.Move.from_uci(uci))
            except ValueError:
                break
        described = _describe(board, moves)
        if described["first"] is None:
            continue
        # The cloud reports from the mover's point of view; everything in this
        # repository is White's, so flip when Black is to move.
        cp, mate = pv.get("cp"), pv.get("mate")
        if board.turn == chess.BLACK:
            cp = -cp if cp is not None else None
            mate = -mate if mate is not None else None
        if mate is not None:
            text = ("+M" if mate > 0 else "-M") + str(abs(mate))
        else:
            text = f"{(cp or 0) / 100:+.2f}"
        lines.append({"rank": rank, "depth": int(data.get("depth", 0) or 0),
                      "cp": cp, "mate": mate, "text": text, **described})
    return lines


def top_lines(fen: str, *, count: int = 3, movetime: float = 0.4,
              depth: int | None = None) -> dict:
    """The engine's best ``count`` moves here, as ranked variations."""
    count = max(1, min(MAX_LINES, int(count)))
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise ValueError(f"Bad FEN: {exc}") from exc

    if board.is_game_over():
        return {"lines": [], "source": "over", "gameOver": True}

    provider = get_provider()
    handle = provider._ensure_engine()          # noqa: SLF001 - see docstring

    if handle is not None:
        limit = (chess.engine.Limit(depth=depth) if depth
                 else chess.engine.Limit(time=movetime))
        infos = None
        try:
            with provider._engine_lock:         # noqa: SLF001
                infos = handle.analyse(board, limit, multipv=count)
        except Exception:                                    # noqa: BLE001
            provider._reset_engine()            # noqa: SLF001
        if infos:
            lines = []
            for rank, info in enumerate(infos, start=1):
                score = info.get("score")
                if score is None:
                    continue
                described = _describe(board, info.get("pv") or [])
                if described["first"] is None:
                    continue
                lines.append({"rank": rank,
                              "depth": int(info.get("depth", 0) or 0),
                              **_score_json(score), **described})
            if lines:
                return {"lines": lines, "source": "local", "gameOver": False}

    cloud = _cloud_lines(fen, count)
    if cloud:
        return {"lines": cloud, "source": "cloud", "gameOver": False}
    return {"lines": [], "source": "none", "gameOver": False}


def suggest(fen: str, *, count: int = 3, movetime: float = 0.4) -> dict | None:
    """One gap's worth of advice, or None when there is no engine.

    Never raises for a missing engine: a report without suggestions is still
    a report, and the caller has enough to say so.
    """
    try:
        result = top_lines(fen, count=count, movetime=movetime)
    except (FeatureUnavailable, ValueError):
        return None
    return result if result.get("lines") else None


def fill_suggestions(gaps, *, count: int = 3, movetime: float = 0.4,
                     limit: int = 12, progress=None, should_stop=None) -> int:
    """Attach an engine suggestion to the first ``limit`` gaps, in place."""
    if not available():
        return 0
    filled = 0
    targets = list(gaps)[:limit]
    for index, gap in enumerate(targets):
        if should_stop and should_stop():
            break
        if progress:
            progress(index, len(targets))
        result = suggest(gap.get("fen", ""), count=count, movetime=movetime)
        if result:
            gap["engine"] = result
            filled += 1
    return filled


def fill_exploit(rows, *, count: int = 2, movetime: float = 0.6,
                 progress=None, should_stop=None) -> int:
    """Analyse every exploit candidate in place. Same shape as the gap filler.

    A row whose engine call comes back empty keeps ``engine: None`` rather
    than a blank result, so the ranking can tell "no edge known" from "no
    edge", and the tab can say which rows are still missing.
    """
    if not available():
        return 0
    filled = 0
    rows = list(rows)
    for index, row in enumerate(rows):
        if should_stop and should_stop():
            break
        if progress:
            progress(index, len(rows))
        result = suggest(row.get("fen", ""), count=count, movetime=movetime)
        if result:
            row["engine"] = result
            filled += 1
    if progress:
        progress(len(rows), len(rows))
    return filled


# ------------------------------------------------------------------ eval bar


def evaluate_one(fen: str) -> dict | None:
    """One position's evaluation, for the bar beside the board.

    Goes through the sibling's whole ladder -- disk cache, then the Lichess
    cloud, then local Stockfish -- which is what makes dragging a piece around
    feel instant for opening positions: they are nearly all cached or in the
    cloud already.
    """
    try:
        provider = get_provider()
    except FeatureUnavailable:
        return None
    try:
        chess.Board(fen)
    except ValueError:
        return None

    value = provider.evaluate(fen)
    if value is None or not value.known:
        return None
    return {
        "cp": value.cp,
        "mate": value.mate,
        "depth": value.depth,
        "source": value.source,
        "text": value.text(),
        "whiteFraction": round(value.white_fraction(), 4),
        "bestMove": value.best_move,
    }


__all__ = [
    "CACHE_FILE",
    "MAX_LINES",
    "available",
    "close_provider",
    "evaluate_one",
    "fill_exploit",
    "fill_suggestions",
    "get_provider",
    "suggest",
    "top_lines",
]
