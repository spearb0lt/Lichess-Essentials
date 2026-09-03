"""Evaluation: the live bar, and baking evals into the saved PGN.

The evaluation ladder itself (Lichess cloud, then local Stockfish, then a
disk cache) belongs to the sibling app and is used as-is.  What is this app's
own business is *persisting* the result: every move you save carries an
``[%eval]`` in its PGN comment, which means the eval survives into the file on
disk, into the Lichess study when you push, and into the PDF export -- rather
than living only in the browser tab you happened to have open.

The written form is plain ``[%eval 0.31]`` / ``[%eval #-3]``, white point of
view, exactly what Lichess itself writes.  python-chess can also append the
search depth (``[%eval 0.31,22]``), and we deliberately do not: Lichess never
emits that form and there is no upside to betting a push on it parsing.
"""

from __future__ import annotations

import threading
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from .bridge import FeatureUnavailable, optional, require
from .model import iter_nodes

CACHE_FILE = Path.home() / ".cache" / "repertoire-creator" / "evals.json"

_PROVIDER = None
_PROVIDER_LOCK = threading.Lock()


def get_provider():
    """One warm engine for the whole process.

    Spawning Stockfish costs about a second; keeping it alive is what makes
    the eval bar respond while you click around the board.
    """
    global _PROVIDER
    evals = require("evals", "Position evaluation")
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = evals.EvalProvider(
                CACHE_FILE,
                use_cloud=True,
                stockfish_path=evals.find_stockfish(),
                movetime=0.2,
            )
        return _PROVIDER


def close_provider() -> None:
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is not None:
            _PROVIDER.close()
            _PROVIDER = None


def available() -> bool:
    return optional("evals") is not None


def eval_json(value) -> dict | None:
    """Serialise a sibling-app ``Eval`` for the browser."""
    if value is None or not value.known:
        return None
    return {
        "cp": value.cp,
        "mate": value.mate,
        "depth": value.depth,
        "source": value.source,
        "bestMove": value.best_move,
        "text": value.text(),
        "whiteFraction": value.white_fraction(),
    }


def evaluate_one(fen: str, *, movetime: float = 0.2, depth: int | None = None,
                 use_cloud: bool = True) -> dict | None:
    """One position, for the live eval bar."""
    provider = get_provider()
    cached = provider._lookup(fen)
    if cached is not None:
        return eval_json(cached)

    value = provider._from_cloud(fen) if use_cloud else None
    if value is None:
        value = provider._from_engine(fen, movetime=movetime, depth=depth)
    if value is None or not value.known:
        return None
    provider._store(fen, value)
    provider.save_cache()
    return eval_json(value)


# ----------------------------------------------------------- best lines


#: How many moves of each variation to spell out. Past this the notation is
#: longer than it is useful.
PV_MOVES = 8
MAX_LINES = 5


def _score_json(pov: chess.engine.PovScore) -> dict:
    white = pov.white()
    cp, mate = white.score(), white.mate()
    if mate is not None:
        text = ("+M" if mate > 0 else "-M") + str(abs(mate))
    else:
        text = f"{(cp or 0) / 100:+.2f}"
    return {"cp": cp, "mate": mate, "text": text}


def _describe(board: chess.Board, moves) -> dict:
    """Turn a principal variation into readable notation."""
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


def top_lines(fen: str, *, count: int = 2, movetime: float = 0.35,
              depth: int | None = None) -> dict:
    """The engine's best ``count`` moves here, as ranked variations.

    Runs on the same warm engine as the eval bar, under the same lock -- a
    UCI engine is one conversation at a time, and a second analyse() while
    one is in flight corrupts both.

    Falls back to the Lichess cloud when there is no local engine: the cloud
    serves up to five variations, but only for positions already in its
    database, which in practice means openings.
    """
    count = max(1, min(MAX_LINES, int(count)))
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise ValueError(f"Bad FEN: {exc}") from exc

    if board.is_game_over():
        return {"lines": [], "source": "over", "gameOver": True}

    provider = get_provider()
    engine_handle = provider._ensure_engine()

    if engine_handle is not None:
        limit = (chess.engine.Limit(depth=depth) if depth
                 else chess.engine.Limit(time=movetime))
        try:
            with provider._engine_lock:
                infos = engine_handle.analyse(board, limit, multipv=count)
        except Exception:
            provider._reset_engine()
            infos = None
        if infos:
            lines = []
            for rank, info in enumerate(infos, start=1):
                score = info.get("score")
                if score is None:
                    continue
                described = _describe(board, info.get("pv") or [])
                if described["first"] is None:
                    continue
                lines.append({
                    "rank": rank,
                    "depth": int(info.get("depth", 0) or 0),
                    **_score_json(score),
                    **described,
                })
            if lines:
                return {"lines": lines, "source": "local", "gameOver": False}

    cloud = _cloud_lines(fen, count)
    if cloud:
        return {"lines": cloud, "source": "cloud", "gameOver": False}
    return {"lines": [], "source": "none", "gameOver": False}


def _cloud_lines(fen: str, count: int) -> list:
    """Ask the Lichess cloud for several variations at once."""
    import requests

    try:
        response = requests.get(
            "https://lichess.org/api/cloud-eval",
            params={"fen": fen, "multiPv": count},
            timeout=8,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
        board = chess.Board(fen)
    except (ValueError, KeyError):
        return []

    lines = []
    for rank, pv in enumerate(data.get("pvs") or [], start=1):
        moves = []
        for token in (pv.get("moves") or "").split():
            try:
                moves.append(chess.Move.from_uci(token))
            except ValueError:
                break
        described = _describe(board, moves)
        if described["first"] is None:
            continue
        mate, cp = pv.get("mate"), pv.get("cp")
        if mate is not None:
            text = ("+M" if mate > 0 else "-M") + str(abs(mate))
        else:
            text = f"{(cp or 0) / 100:+.2f}"
        lines.append({
            "rank": rank, "depth": int(data.get("depth", 0) or 0),
            "cp": cp, "mate": mate, "text": text, **described,
        })
    return lines


# ------------------------------------------------------------------ baking


def _to_pov(value) -> chess.engine.PovScore | None:
    """Sibling ``Eval`` (white point of view) -> python-chess score."""
    if value is None or not value.known:
        return None
    if value.mate is not None:
        score = chess.engine.Mate(value.mate)
    else:
        score = chess.engine.Cp(int(value.cp))
    return chess.engine.PovScore(score, chess.WHITE)


def bake_chapter(
    game: chess.pgn.Game,
    *,
    movetime: float = 0.15,
    depth: int | None = None,
    only_missing: bool = True,
    progress=None,
) -> dict:
    """Write an ``[%eval]`` onto every move in a chapter.

    ``only_missing`` skips moves that already carry one, so re-running after
    adding a few lines costs seconds rather than reanalysing the tree.
    """
    provider = get_provider()

    todo = []
    for node, board_before in iter_nodes(game):
        if only_missing and node.eval() is not None:
            continue
        after = board_before.copy(stack=False)
        after.push(node.move)
        todo.append((node, after.fen()))

    if not todo:
        return {"evaluated": 0, "skipped": 0, "missing": 0}

    saved_movetime, saved_depth = provider.movetime, provider.depth
    provider.movetime, provider.depth = movetime, depth
    try:
        results = provider.evaluate_many(
            [fen for _, fen in todo],
            progress=progress,
        )
    finally:
        provider.movetime, provider.depth = saved_movetime, saved_depth

    written = missing = 0
    for node, fen in todo:
        score = _to_pov(results.get(fen))
        if score is None:
            missing += 1
            continue
        # No depth argument: keep the annotation in the exact shape Lichess
        # writes and reads. See the module docstring.
        node.set_eval(score)
        written += 1

    return {
        "evaluated": written,
        "skipped": sum(1 for _ in iter_nodes(game)) - len(todo),
        "missing": missing,
    }


def strip_evals(game: chess.pgn.Game) -> int:
    """Remove every ``[%eval]`` from a chapter. Returns how many went."""
    removed = 0
    for node, _ in iter_nodes(game):
        if node.eval() is not None:
            node.set_eval(None)
            removed += 1
    return removed


__all__ = [
    "MAX_LINES",
    "FeatureUnavailable",
    "available",
    "bake_chapter",
    "close_provider",
    "eval_json",
    "evaluate_one",
    "get_provider",
    "strip_evals",
    "top_lines",
]
