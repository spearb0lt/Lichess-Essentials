"""Training: can you actually find your own moves?

A repertoire you cannot recall is a document, not a repertoire.  Drill mode
turns the tree into questions -- here is a position, you are to move, play
what you wrote -- and schedules them with a plain SM-2 spaced-repetition
curve so the lines you keep missing come back tomorrow and the ones you know
cold come back in a month.

**What counts as a question.**  Every node where it is your turn and you have
an answer.  The answer is the first child: in a repertoire, the main line is
the move you have decided to play, and any siblings are alternatives you have
kept notes on.  Getting a sibling right is not wrong, so it scores as a
partial hit rather than a failure.

**What state is stored.**  One entry per question, keyed by the position
rather than by its path, so restructuring a chapter does not reset your
progress on lines that survived the edit.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import chess
import chess.pgn

from .analysis import line_label, position_key
from .model import format_path, is_my_turn, path_of

#: SM-2 constants, with the usual floor on the ease factor.
MIN_EASE = 1.3
START_EASE = 2.5
#: A fresh card and a lapsed card both start here, in days.
FIRST_INTERVAL = 1.0
SECOND_INTERVAL = 3.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def card_key(board: chess.Board) -> str:
    """Identify a question by its position, not by where it sits in the tree."""
    return position_key(board)


def collect_cards(chapters: list, color: str) -> list:
    """Every drillable position across the repertoire.

    ``chapters`` is a list of ``(chapter_meta, game)``.
    """
    cards = []
    seen = set()

    def visit(meta, node, board):
        if is_my_turn(board, color) and node.variations:
            key = card_key(board)
            # The same position reached twice is one question, not two.
            if key not in seen:
                seen.add(key)
                answer = node.variations[0]
                alternatives = [
                    board.san(child.move) for child in node.variations[1:]
                ]
                cards.append({
                    "key": key,
                    "chapterId": meta.id,
                    "chapterName": meta.name,
                    "path": format_path(path_of(node)),
                    "fen": board.fen(),
                    "ply": len(path_of(node)),
                    "answerSan": board.san(answer.move),
                    "answerUci": answer.move.uci(),
                    "alternatives": alternatives,
                    "alternativeUcis": [
                        child.move.uci() for child in node.variations[1:]
                    ],
                    "line": line_label(node, board),
                    "comment": (answer.comment or "").strip(),
                    "orientation": meta.orientation,
                })
        for child in node.variations:
            after = board.copy(stack=False)
            after.push(child.move)
            visit(meta, child, after)

    for meta, game in chapters:
        visit(meta, game, game.board())
    return cards


# ---------------------------------------------------------------- scheduling


def due_state(state: dict, key: str) -> dict:
    return state.get(key) or {
        "ease": START_EASE, "interval": 0.0, "reps": 0, "lapses": 0,
        "due": None, "last": None,
    }


def is_due(entry: dict, at: datetime | None = None) -> bool:
    when = _parse(entry.get("due"))
    if when is None:
        return True                      # never seen: always eligible
    return when <= (at or _now())


def build_session(cards: list, state: dict, *, limit: int = 20,
                  new_limit: int = 8, chapter_id: str | None = None,
                  shuffle: bool = True) -> list:
    """Pick the questions for one sitting: overdue first, then new."""
    pool = [c for c in cards if chapter_id is None or c["chapterId"] == chapter_id]

    overdue, fresh = [], []
    for card in pool:
        entry = state.get(card["key"])
        if entry is None:
            fresh.append(card)
        elif is_due(entry):
            overdue.append(card)

    # Most overdue first, so nothing rots at the bottom of the pile.
    overdue.sort(key=lambda c: _parse(state[c["key"]].get("due")) or _now())
    # New material shallowest first: an early move is reached far more often
    # than a move fifteen plies down a sideline.
    fresh.sort(key=lambda c: c["ply"])

    session = fresh[:min(new_limit, limit)]
    session += overdue[:max(0, limit - len(session))]
    if shuffle:
        random.shuffle(session)
    return session[:limit]


def grade(state: dict, key: str, quality: int) -> dict:
    """Apply one answer. ``quality`` is 0 (wrong) to 5 (instant and right).

    The standard SM-2 rules: anything below 3 resets the interval, anything
    above it grows the interval by the ease factor, and the ease factor
    drifts with how hard the card is proving.
    """
    entry = dict(due_state(state, key))
    quality = max(0, min(5, int(quality)))

    if quality < 3:
        entry["reps"] = 0
        entry["lapses"] = int(entry.get("lapses", 0)) + 1
        entry["interval"] = FIRST_INTERVAL
    else:
        entry["reps"] = int(entry.get("reps", 0)) + 1
        if entry["reps"] == 1:
            entry["interval"] = FIRST_INTERVAL
        elif entry["reps"] == 2:
            entry["interval"] = SECOND_INTERVAL
        else:
            entry["interval"] = float(entry["interval"]) * float(entry["ease"])

    ease = float(entry.get("ease", START_EASE))
    ease += 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
    entry["ease"] = round(max(MIN_EASE, ease), 3)
    entry["interval"] = round(float(entry["interval"]), 3)

    now = _now()
    entry["last"] = _iso(now)
    entry["due"] = _iso(now + timedelta(days=entry["interval"]))
    state[key] = entry
    return entry


def quality_for(correct: bool, *, alternative: bool = False,
                hinted: bool = False) -> int:
    """Turn what happened at the board into an SM-2 quality score."""
    if not correct:
        return 0
    if alternative:
        return 3            # a move you keep notes on, but not your choice
    if hinted:
        return 3
    return 5


def summarise(cards: list, state: dict) -> dict:
    now = _now()
    known = due = new = 0
    for card in cards:
        entry = state.get(card["key"])
        if entry is None:
            new += 1
        elif is_due(entry, now):
            due += 1
        else:
            known += 1
    return {"total": len(cards), "new": new, "due": due, "scheduled": known}
