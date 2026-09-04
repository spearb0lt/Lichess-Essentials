"""The arithmetic: your moves, sliced, counted, and compared against yourself.

Every number here is defined to match ChessAnalyzer's, because the point of
this app is to aggregate that app's reviews rather than to have a second
opinion about them:

* **ACPL** is the mean centipawn loss over your moves, **excluding book moves**
  and moves the engine could not score -- exactly as ``_side_summary`` does it.
  Including opening theory would flatter anyone with preparation and tell them
  nothing about their play.
* **Accuracy** is the mean of the arithmetic and harmonic means of your move
  accuracies in the bucket, which is what ``phase_accuracy`` does. The
  harmonic half is what stops one brilliant move from hiding three terrible
  ones. The function itself is imported rather than copied.

The one number this app adds is **excess loss**, and it is the number the
whole report is built on:

    excess = moves_in_bucket x (bucket ACPL - your overall ACPL)

which is "how many centipawns this kind of position has cost you *beyond what
you cost yourself anyway*".  Divided by the number of games, it becomes the
sentence you actually want: *queenless middlegames cost you 0.4 pawns a game
more than your average*.

That formula is chosen over the two obvious alternatives on purpose. Ranking
by bucket ACPL alone crowns whichever bucket has eleven moves in it; ranking
by total centipawns lost crowns the middlegame every time, because that is
where most of your moves are. Excess loss is the one that answers "what should
I work on", because it multiplies how bad you are at something by how often it
happens to you.
"""

from __future__ import annotations

from collections import defaultdict

from . import features as feat
from .bridge import analyzer
from .buckets import DIMENSIONS

#: A bucket smaller than this is not evidence.  Both have to be met: forty
#: moves inside one game is one game's worth of noise.
MIN_MOVES = 40
MIN_GAMES = 5


def _harmonic_mean(values: list) -> float:
    """ChessAnalyzer's, when it can be found; the same formula when it cannot.

    This is the only place in the app with a fallback, and it is four lines of
    arithmetic with no judgement in it, so the two cannot drift in any way
    that matters.
    """
    accuracy_module = analyzer("accuracy")
    if accuracy_module is not None:
        return accuracy_module.harmonic_mean(values)
    positive = [max(value, 0.01) for value in values]
    if not positive:
        return 0.0
    return len(positive) / sum(1.0 / value for value in positive)


def bucket_accuracy(accuracies: list) -> float | None:
    """The accuracy of one bucket, by ChessAnalyzer's per-phase definition."""
    if not accuracies:
        return None
    mean = sum(accuracies) / len(accuracies)
    return round((mean + _harmonic_mean(accuracies)) / 2.0, 1)


def move_rows(games: list, reviews: dict) -> list:
    """Flatten reviews into one list of *your* moves, ready to slice.

    ``games`` are the records, ``reviews`` maps a game id to its review.  A
    game with no review is skipped silently; the caller counts those.
    """
    rows = []
    for game in games:
        review = reviews.get(game.get("id"))
        if not review:
            continue
        you = game.get("you")
        if you not in ("white", "black"):
            continue

        opening = review.get("opening") or {}
        context = {
            "id": game.get("id"),
            "url": game.get("url", ""),
            "you": you,
            "them": game.get("them", ""),
            "youElo": game.get("youElo", ""),
            "themElo": game.get("themElo", ""),
            "speed": game.get("speed", ""),
            "result": game.get("result", "*"),
            "date": game.get("date", ""),
            "opening": opening,
        }
        me = you == "white"

        for row in review.get("moves") or []:
            if row.get("color") != you:
                continue
            rows.append({
                "ply": row.get("ply"),
                "moveNumber": row.get("moveNumber"),
                "san": row.get("san", ""),
                "phase": row.get("phase", ""),
                "clock": row.get("clock"),
                "cpLoss": row.get("cpLoss"),
                "winLoss": row.get("winLoss"),
                "accuracy": row.get("accuracy"),
                "label": row.get("label", ""),
                "judgment": row.get("judgment"),
                "inBook": bool(row.get("inBook")),
                "forced": bool(row.get("forced")),
                "isBest": bool(row.get("isBest")),
                "bestSan": row.get("bestSan"),
                "fenBefore": row.get("fenBefore", ""),
                "features": feat.describe(row.get("fenBefore", ""), me),
                "game": context,
            })
    return rows


def _scored(move: dict) -> bool:
    """Does this move count towards ACPL? ChessAnalyzer's rule, unchanged."""
    return not move["inBook"] and move.get("cpLoss") is not None


class Tally:
    """One bucket's running totals."""

    __slots__ = ("moves", "scored", "cp", "accuracies", "games", "labels",
                 "judgments", "best", "forced")

    def __init__(self):
        self.moves = 0
        self.scored = 0
        self.cp = 0
        self.accuracies = []
        self.games = set()
        self.labels = defaultdict(int)
        self.judgments = defaultdict(int)
        self.best = 0
        self.forced = 0

    def add(self, move: dict) -> None:
        self.moves += 1
        self.games.add((move.get("game") or {}).get("id"))
        if move.get("label"):
            self.labels[move["label"]] += 1
        if move.get("judgment"):
            self.judgments[move["judgment"]] += 1
        if move.get("isBest"):
            self.best += 1
        if move.get("forced"):
            self.forced += 1
        if move.get("accuracy") is not None:
            self.accuracies.append(move["accuracy"])
        if _scored(move):
            self.scored += 1
            self.cp += move["cpLoss"]

    @property
    def acpl(self):
        return self.cp / self.scored if self.scored else None

    def to_json(self, name: str, *, baseline, total_games: int) -> dict:
        acpl = self.acpl
        excess = (self.scored * (acpl - baseline)
                  if acpl is not None and baseline is not None else None)
        return {
            "bucket": name,
            "moves": self.moves,
            "scored": self.scored,
            "games": len(self.games),
            "acpl": round(acpl, 1) if acpl is not None else None,
            "accuracy": bucket_accuracy(self.accuracies),
            "cpLost": self.cp,
            "pawnsLost": round(self.cp / 100.0, 2),
            "excessCp": round(excess, 1) if excess is not None else None,
            "excessPawnsPerGame": (round(excess / total_games / 100.0, 3)
                                   if excess is not None and total_games else None),
            "pawnsPerGame": (round(self.cp / total_games / 100.0, 3)
                             if total_games else None),
            "bestShare": (round(100.0 * self.best / self.moves, 1)
                          if self.moves else None),
            "labels": dict(sorted(self.labels.items(), key=lambda kv: -kv[1])),
            "judgments": dict(self.judgments),
        }


def overall(moves: list, total_games: int) -> dict:
    """Your baseline: the numbers every bucket is compared against."""
    tally = Tally()
    for move in moves:
        tally.add(move)
    data = tally.to_json("overall", baseline=None, total_games=total_games)
    data["excessCp"] = 0.0
    data["excessPawnsPerGame"] = 0.0
    return data


def slice_by(moves: list, dimension, *, baseline: float | None,
             total_games: int) -> list:
    """Every bucket of one dimension, biggest share of your loss first."""
    tallies: dict = {}
    for move in moves:
        for name in dimension.buckets_of(move):
            tallies.setdefault(name, Tally()).add(move)

    rows = [tally.to_json(name, baseline=baseline, total_games=total_games)
            for name, tally in tallies.items()]

    if dimension.order:
        position = {name: index for index, name in enumerate(dimension.order)}
        rows.sort(key=lambda row: (position.get(row["bucket"], 99),
                                   -row["moves"]))
    else:
        rows.sort(key=lambda row: -row["moves"])
    return rows


def build(moves: list, *, total_games: int) -> dict:
    """Every dimension, sliced, with the baseline they are measured against."""
    base = overall(moves, total_games)
    baseline = base["acpl"]

    slices = {}
    for dimension in DIMENSIONS:
        rows = slice_by(moves, dimension, baseline=baseline,
                        total_games=total_games)
        if not rows:
            continue
        slices[dimension.key] = {
            "key": dimension.key,
            "label": dimension.label,
            "note": dimension.note,
            "buckets": rows,
        }

    return {"overall": base, "baselineAcpl": baseline, "slices": slices}


__all__ = [
    "MIN_GAMES",
    "MIN_MOVES",
    "Tally",
    "bucket_accuracy",
    "build",
    "move_rows",
    "overall",
    "slice_by",
]
