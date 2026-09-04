"""One report, from a folder of reviews.

Everything up to here fetches games, reviews them and slices moves. This is
where those become the document: a header saying what was measured, the
findings in order, every slice as a table, and the moments that cost most.

The header matters as much as the findings. A weakness report is a page of
confident numbers about you, and the first question a reader should be able to
answer is "confident from what?" -- how many games, over what period, searched
how deep, and whether every game was searched the same way. All of that is in
the summary, and the report says so plainly when it is not uniform.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from . import aggregate, findings as findings_module
from .bridge import analyzer
from .buckets import BY_KEY
from .features import GLOSSARY


def _estimated_rating(acpl):
    """ChessAnalyzer's fitted curve, when it can be found."""
    review = analyzer("review")
    if review is None or acpl is None:
        return None, ""
    return review.estimated_rating(acpl), review.RATING_FORMULA


def summarise(games: list, reviews: dict, moves: list) -> dict:
    """The header: what was measured, and how you did overall."""
    used = [game for game in games if game.get("id") in reviews]
    dates = [game.get("date") for game in used if game.get("date")]
    results = Counter()
    speeds = Counter()
    colours = Counter()
    ratings = []

    for game in used:
        you = game.get("you")
        colours[you] += 1
        if game.get("speed"):
            speeds[game["speed"]] += 1
        result = game.get("result", "*")
        if result == "1/2-1/2":
            results["draw"] += 1
        elif (result == "1-0") == (you == "white"):
            results["win"] += 1
        else:
            results["loss"] += 1
        try:
            if game.get("youElo"):
                ratings.append(int(game["youElo"]))
        except (TypeError, ValueError):
            pass

    total = len(used)
    played = results["win"] + results["draw"] + results["loss"]
    score = ((results["win"] + results["draw"] / 2) / played) if played else None

    return {
        "games": len(games),
        "reviewed": total,
        "unreviewed": len(games) - total,
        "moves": len(moves),
        "from": min(dates) if dates else "",
        "to": max(dates) if dates else "",
        "record": {"win": results["win"], "draw": results["draw"],
                   "loss": results["loss"]},
        "score": round(score, 4) if score is not None else None,
        "speeds": dict(speeds.most_common()),
        "colours": {"white": colours.get("white", 0),
                    "black": colours.get("black", 0)},
        "rating": {
            "min": min(ratings) if ratings else None,
            "max": max(ratings) if ratings else None,
            "last": ratings[0] if ratings else None,
        },
    }


def build(*, key: str, label: str, source: str, games: list, reviews: dict,
          batch_result: dict | None = None, spec: dict | None = None,
          min_moves: int = aggregate.MIN_MOVES,
          min_games: int = aggregate.MIN_GAMES) -> dict:
    """Games plus their reviews, as the whole report."""
    moves = aggregate.move_rows(games, reviews)
    used_games = len({(move.get("game") or {}).get("id") for move in moves})

    sliced = aggregate.build(moves, total_games=max(1, used_games))
    baseline = sliced["baselineAcpl"]
    found = findings_module.build(
        moves, total_games=max(1, used_games), baseline=baseline,
        min_moves=min_moves, min_games=min_games)

    rating, formula = _estimated_rating(baseline)
    summary = summarise(games, reviews, moves)
    summary["acpl"] = baseline
    summary["accuracy"] = sliced["overall"]["accuracy"]
    summary["estimatedRating"] = rating
    summary["ratingFormula"] = formula
    summary["scoredMoves"] = sliced["overall"]["scored"]

    return {
        "key": key,
        "label": label,
        "source": source,
        "builtAt": datetime.now(timezone.utc)
                           .replace(microsecond=0).isoformat(),
        "spec": spec or {},
        "batch": batch_result or {},
        "summary": summary,
        "baselineAcpl": baseline,
        "overall": sliced["overall"],
        "slices": sliced["slices"],
        "findings": found,
        "worstMoments": findings_module.worst_moments(moves),
        "glossary": GLOSSARY,
        "dimensionNotes": {dimension.key: dimension.note
                           for dimension in BY_KEY.values()},
        "thresholds": {"minMoves": min_moves, "minGames": min_games},
    }


def rebuild(report: dict, *, min_moves: int, min_games: int, games: list,
            reviews: dict) -> dict:
    """The same report at different sample-size thresholds.

    Changing what counts as enough evidence must not need the engine again,
    so this re-slices what is already on disk.
    """
    return build(
        key=report.get("key", ""), label=report.get("label", ""),
        source=report.get("source", ""), games=games, reviews=reviews,
        batch_result=report.get("batch"), spec=report.get("spec"),
        min_moves=min_moves, min_games=min_games)


__all__ = ["build", "rebuild", "summarise"]
