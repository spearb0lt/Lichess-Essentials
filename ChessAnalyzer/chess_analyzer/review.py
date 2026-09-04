"""The review: one pass over the game, everything else derived from it.

The expensive thing is engine time, so the design is built around spending it
once.  Every position in the game -- including the one after the last move --
is analysed exactly once, with several variations asked for at each.  That
single pass yields everything a review shows:

* the eval graph, straight off the list of scores;
* what the played move cost, because the eval *after* a move is just the eval
  of the next position, already in hand;
* what should have been played, from the first variation of the position
  before;
* whether there was a choice at all, from the second variation.

A naive implementation analyses twice per move -- once for the position, once
for "what if I had played the best move" -- and takes twice as long for the
same answer.

Positions are looked up in the shared cache first, so re-reviewing a game at
the same settings is instant, and a game whose opening you have seen before
starts part-analysed.  Cache keys include the engine settings; a 0.1-second
answer never masquerades as a 20-ply one.

Cancellation is checked between positions rather than inside the engine call,
which bounds how long "stop" takes at one position's think time.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import chess
import chess.pgn

from . import accuracy as acc
from . import classify, openings
from .engines import POOL, EngineError, EngineOptions
from .sources.common import SourceError, is_standard, parse_game, positions

#: Ready-made settings, because "movetime 0.3, multipv 3" is not a decision
#: anyone should have to make before their first review. Times are per
#: position, so multiply by roughly twice the move count for the total.
PRESETS = {
    "quick": {
        "label": "Quick",
        "movetime": 0.1,
        "multipv": 2,
        "detail": "~10s for a 40-move game. Finds blunders, misses subtleties.",
    },
    "standard": {
        "label": "Standard",
        "movetime": 0.3,
        "multipv": 3,
        "detail": "~30s for a 40-move game. The sensible default.",
    },
    "deep": {
        "label": "Deep",
        "movetime": 1.0,
        "multipv": 3,
        "detail": "~2min for a 40-move game. Worth it for a game you care about.",
    },
    "exhaustive": {
        "label": "Exhaustive",
        "depth": 22,
        "multipv": 4,
        "detail": "Fixed depth 22. Slowest, and the only one whose numbers do "
                  "not depend on how busy your machine was.",
    },
}

#: How many moves of a variation to spell out. Past this the notation is
#: longer than it is useful.
PV_MOVES = 8

#: How many turning points the summary calls out.
MOMENTS = 8

#: Rough ACPL-to-rating fit, shown with the formula attached because it is a
#: fit and not a measurement. Chess.com's "Game Rating" is not published, so
#: this is deliberately its own number with its own name.
RATING_SCALE = 3100.0
RATING_DECAY = 0.01


class ReviewCancelled(RuntimeError):
    """Raised when a caller's ``should_stop`` said to give up."""


@dataclass
class Settings:
    """Everything that changes what a review says."""

    engine_id: str | None = None
    preset: str = "standard"
    movetime: float = 0.3
    depth: int | None = None
    multipv: int = 3
    threads: int = 2
    hash_mb: int = 256
    weights: str | None = None

    @classmethod
    def from_preset(cls, name: str, **overrides) -> "Settings":
        preset = PRESETS.get(name) or PRESETS["standard"]
        values = {
            "preset": name if name in PRESETS else "standard",
            "movetime": preset.get("movetime", 0.3),
            "depth": preset.get("depth"),
            "multipv": preset.get("multipv", 3),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def options(self) -> EngineOptions:
        return EngineOptions(
            threads=self.threads, hash_mb=self.hash_mb, multipv=self.multipv,
            movetime=self.movetime, depth=self.depth, weights=self.weights)

    def json(self) -> dict:
        return {
            "engineId": self.engine_id,
            "preset": self.preset,
            "movetime": self.movetime,
            "depth": self.depth,
            "multipv": self.multipv,
            "threads": self.threads,
            "hashMb": self.hash_mb,
            "weights": self.weights,
        }


def describe_pv(board: chess.Board, ucis: list[str],
                limit: int = PV_MOVES) -> dict:
    """A principal variation as readable notation, plus its first move."""
    replay = board.copy(stack=False)
    parts: list[str] = []
    first = None

    for index, uci in enumerate(ucis[:limit]):
        try:
            move = replay.parse_uci(uci)
        except (ValueError, AssertionError):
            break
        san = replay.san(move)
        if index == 0:
            first = {"uci": uci, "san": san}
        number = replay.fullmove_number
        if replay.turn == chess.WHITE:
            parts.append(f"{number}.{san}")
        elif not parts:
            parts.append(f"{number}...{san}")
        else:
            parts.append(san)
        replay.push(move)

    return {"line": " ".join(parts), "first": first}


def eval_text(cp: int | None, mate: int | None) -> str:
    """``+1.24``, ``-0.30``, ``+M5`` -- White's point of view throughout."""
    if mate is not None:
        return ("+M" if mate > 0 else "-M") + str(abs(mate) or 0)
    if cp is None:
        return ""
    return f"{cp / 100:+.2f}"


def estimated_rating(acpl: float | None) -> int | None:
    """A rough playing strength for one game, from its centipawn loss.

    Deliberately not called an Elo: it is a single game, a fitted curve, and
    it moves 200 points on one blunder. Shown with :data:`RATING_FORMULA` next
    to it so nobody mistakes it for a measurement.
    """
    if acpl is None:
        return None
    value = RATING_SCALE * math.exp(-RATING_DECAY * max(0.0, acpl))
    return int(max(400, min(3000, round(value))))


RATING_FORMULA = "3100 x exp(-0.01 x ACPL), clamped to 400-3000"


def review(
    record,
    settings: Settings | None = None,
    *,
    cache=None,
    progress=None,
    should_stop=None,
) -> dict:
    """Analyse a whole game. Returns the payload the browser renders.

    ``progress(done, total, message)`` is called as it goes, and
    ``should_stop()`` is polled between positions so a queued review can be
    cancelled without waiting for the whole game.
    """
    settings = settings or Settings()
    game = parse_game(record.pgn)
    if not is_standard(game):
        raise SourceError("Only standard chess can be reviewed.")

    boards, moves = positions(game)
    if not moves:
        raise SourceError(
            "That game has no moves to review. Import a game with moves, or "
            "use the analysis board for a bare position.")

    engine = POOL.get(settings.engine_id, weights=settings.weights)
    options = settings.options()
    settings_key = f"{engine.name}|{options.key()}"

    named = openings.identify(boards)
    middle_ply, end_ply = acc.phase_boundaries(boards)

    # --------------------------------------------------------- the one pass

    total = len(boards)
    analyses: list[list[dict]] = []
    started = time.time()

    for index, board in enumerate(boards):
        if should_stop is not None and should_stop():
            raise ReviewCancelled("Review cancelled.")

        fen = board.fen()
        lines = cache.get(fen, settings_key) if cache is not None else None
        if lines is None:
            try:
                lines = engine.analyse(board, options)
            except EngineError:
                if index == 0:
                    raise
                # One position the engine choked on should not lose the game:
                # carry the previous eval forward and carry on.
                lines = [dict(analyses[-1][0], depth=0, pv=[])]
            if cache is not None:
                cache.put(fen, settings_key, lines)
        analyses.append(lines)

        if progress:
            progress(index + 1, total,
                     f"Position {index + 1} of {total}")

    if cache is not None:
        cache.save()

    # ------------------------------------------------------------- the rows

    rows = []
    clocks = list(_clocks(game))

    for ply, move in enumerate(moves):
        board_before = boards[ply]
        board_after = boards[ply + 1]
        before_lines = analyses[ply]
        after_lines = analyses[ply + 1]

        best = before_lines[0]
        second = before_lines[1] if len(before_lines) > 1 else None
        after = after_lines[0]

        # Book credit stops where the opening dataset stops recognising the
        # position, so a review never calls published theory a mistake.
        in_book = (ply + 1) <= named["bookPly"]

        judged = classify.classify(
            board_before=board_before,
            move=move,
            best_before=best,
            second_before=second,
            eval_after=after,
            after_board=board_after,
            after_pv=after.get("pv") or [],
            in_book=in_book,
            previous_move=moves[ply - 1] if ply else None,
        )

        described = describe_pv(board_before, best.get("pv") or [])
        alternatives = []
        for line in before_lines[:settings.multipv]:
            spelled = describe_pv(board_before, line.get("pv") or [])
            if spelled["first"] is None:
                continue
            alternatives.append({
                "rank": line.get("rank", len(alternatives) + 1),
                "uci": spelled["first"]["uci"],
                "san": spelled["first"]["san"],
                "line": spelled["line"],
                "cp": line.get("cp"),
                "mate": line.get("mate"),
                "text": eval_text(line.get("cp"), line.get("mate")),
                "depth": line.get("depth", 0),
            })

        rows.append({
            "ply": ply + 1,
            "moveNumber": board_before.fullmove_number,
            "color": "white" if board_before.turn == chess.WHITE else "black",
            "san": board_before.san(move),
            "uci": move.uci(),
            "fenBefore": board_before.fen(),
            "fen": board_after.fen(),
            "phase": acc.phase_of(ply, middle_ply, end_ply),
            "clock": clocks[ply] if ply < len(clocks) else None,
            "evalAfter": {
                "cp": after.get("cp"),
                "mate": after.get("mate"),
                "text": eval_text(after.get("cp"), after.get("mate")),
                "depth": after.get("depth", 0),
                "whiteFraction": round(
                    acc.win_percent(after.get("cp"), after.get("mate")) / 100.0, 4),
            },
            "bestSan": (described["first"] or {}).get("san"),
            "bestLine": described["line"],
            "alternatives": alternatives,
            **judged,
        })

    # ---------------------------------------------------------- the summary

    white_percents = [
        acc.win_percent(lines[0].get("cp"), lines[0].get("mate"))
        for lines in analyses
    ]

    summary = {
        "white": _side_summary(rows, chess.WHITE, white_percents),
        "black": _side_summary(rows, chess.BLACK, white_percents),
    }

    moments = sorted(
        (row for row in rows if row["winLoss"] >= acc.INACCURACY),
        key=lambda row: (-row["winLoss"], row["ply"]),
    )[:MOMENTS]

    return {
        "gameId": record.id,
        "generatedAt": time.time(),
        "elapsed": round(time.time() - started, 1),
        "engine": {
            "name": engine.name,
            "path": engine.path,
            "kind": engine.kind,
            "settingsKey": settings_key,
        },
        "settings": settings.json(),
        "opening": named,
        "phases": {"middlegame": middle_ply, "endgame": end_ply},
        "moves": rows,
        "graph": [
            {
                "ply": index,
                "cp": lines[0].get("cp"),
                "mate": lines[0].get("mate"),
                "white": round(white_percents[index], 1),
            }
            for index, lines in enumerate(analyses)
        ],
        "summary": summary,
        "moments": [row["ply"] for row in moments],
        "ratingFormula": RATING_FORMULA,
        "labelRules": classify.CHESSCOM_RULES,
        "cache": cache.stats() if cache is not None else None,
    }


def _side_summary(rows: list[dict], color: bool,
                  white_percents: list[float]) -> dict:
    """One player's numbers: counts, accuracy, ACPL, per-phase accuracy."""
    name = "white" if color == chess.WHITE else "black"
    mine = [row for row in rows if row["color"] == name]

    counts = {label: 0 for label, _ in classify.LABELS}
    for row in mine:
        counts[row["label"]] = counts.get(row["label"], 0) + 1

    judgments = {"inaccuracy": 0, "mistake": 0, "blunder": 0}
    for row in mine:
        if row["judgment"]:
            judgments[row["judgment"]] += 1

    # Book moves are excluded from ACPL for the same reason they are excluded
    # from the labels: playing 12 moves of theory is not evidence about your
    # play, and including it inflates the figure for anyone with preparation.
    scored = [row for row in mine if not row["inBook"] and row["cpLoss"] is not None]
    acpl = round(sum(row["cpLoss"] for row in scored) / len(scored), 1) \
        if scored else None

    return {
        "accuracy": acc.game_accuracy(white_percents, color),
        "acpl": acpl,
        "estimatedRating": estimated_rating(acpl),
        "moves": len(mine),
        "counts": counts,
        "judgments": judgments,
        "phases": {
            phase: acc.phase_accuracy(mine, color, phase)
            for phase in ("opening", "middlegame", "endgame")
        },
        "bestMoveShare": (round(
            100.0 * sum(1 for row in mine if row["isBest"]) / len(mine), 1)
            if mine else None),
    }


def _clocks(game: chess.pgn.Game):
    """Seconds left after each move, from the PGN's own ``[%clk]`` comments."""
    for node in game.mainline():
        yield node.clock()


__all__ = [
    "MOMENTS",
    "PRESETS",
    "RATING_FORMULA",
    "ReviewCancelled",
    "Settings",
    "describe_pv",
    "estimated_rating",
    "eval_text",
    "review",
]
