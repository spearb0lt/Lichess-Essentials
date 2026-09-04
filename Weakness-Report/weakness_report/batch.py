"""Reviewing a few hundred games, once.

This is the expensive half of the app and everything here exists to make it
cost as little as possible and to make the result mean something.

**Reviews are kept per game, not per report.**  Adding fifty games to a
history of four hundred costs fifty reviews.  Re-running the same report costs
nothing at all.

**Batches are searched to a fixed depth, and with one thread.**  This is the
one place where this app deliberately differs from ChessAnalyzer's defaults,
and the reason is worth stating:

* A **movetime** budget makes every number depend on how busy the machine was.
  That is tolerable for one game you are reading move by move -- ChessAnalyzer
  says as much about its own presets -- and it is not tolerable for a figure
  averaged over four hundred games, because re-running the report next week
  would move every number in it and you would have no way to tell a real
  change from a noisy laptop.
* **Threads > 1** makes Stockfish non-deterministic even at a fixed depth: the
  search is split across threads and the order they finish in changes the
  result. One thread is slower and gives the same answer twice.

Together they make a report **reproducible**, which is what lets you run it
again in three months and believe the difference. Both are settings, so you
can trade either away for speed.

**Adopting other reviews.**  ChessAnalyzer may already have reviewed some of
these games. Those are free -- but only comparable if they were searched the
same way, so by default a review is adopted when its engine settings match
this batch and re-done when they do not. ``adopt="any"`` takes them regardless
and the report says the mix out loud; ``adopt="none"`` ignores them.
"""

from __future__ import annotations

import time

from .bridge import FeatureUnavailable, analyzer, require_analyzer

#: Fixed depths rather than movetimes. See the module docstring.
PRESETS = {
    "sweep": {
        "label": "Sweep",
        "depth": 10,
        "multipv": 2,
        "detail": "Roughly a second a game per 10 moves. Finds blunders and "
                  "big patterns; too shallow to trust a 5-centipawn gap.",
    },
    "standard": {
        "label": "Standard",
        "depth": 14,
        "multipv": 3,
        "detail": "The sensible default for a few hundred games. Comparable "
                  "to ChessAnalyzer's Standard preset in strength.",
    },
    "deep": {
        "label": "Deep",
        "depth": 18,
        "multipv": 3,
        "detail": "Several times slower. Worth it for a history you intend to "
                  "keep and compare against later.",
    },
}

DEFAULT_PRESET = "standard"

#: One thread by default, for reproducibility rather than speed.
DEFAULT_THREADS = 1
DEFAULT_HASH_MB = 256

ADOPT_MODES = ("matching", "any", "none")


class BatchError(RuntimeError):
    """A batch could not run, with a message worth showing."""


def settings_for(preset: str = DEFAULT_PRESET, *, engine_id=None,
                 threads: int = DEFAULT_THREADS, hash_mb: int = DEFAULT_HASH_MB):
    """A ChessAnalyzer ``Settings`` pinned to a depth. Raises without the app."""
    review = require_analyzer("review", "Reviewing games")
    chosen = PRESETS.get(preset) or PRESETS[DEFAULT_PRESET]
    return review.Settings(
        engine_id=engine_id,
        preset=preset if preset in PRESETS else DEFAULT_PRESET,
        movetime=0.0,                 # unused: depth wins in EngineOptions
        depth=chosen["depth"],
        multipv=chosen["multipv"],
        threads=max(1, int(threads)),
        hash_mb=max(16, int(hash_mb)),
    )


def signature(settings) -> str:
    """How a review made with these settings will identify itself."""
    return settings.options().key()


def review_signature(review: dict) -> str:
    """The settings key a saved review was made with, or ``""``."""
    key = ((review or {}).get("engine") or {}).get("settingsKey") or ""
    # "Stockfish 18|t1_h256_pv3_d14" -- the half after the bar is the options.
    return key.split("|", 1)[1] if "|" in key else key


def acceptable(existing: dict | None, wanted: str, adopt: str) -> bool:
    """Can this saved review stand in for one made with the current settings?

    Only if it was searched the same way. Getting this wrong is exactly the
    quiet failure this app is built to avoid: a report mixing depth-10 and
    depth-18 numbers looks no different from one that does not, and every
    figure in it is wrong. So changing the preset re-reviews -- and because
    the position cache is keyed by settings too, the second preset is the
    only thing actually paid for.

    ``adopt="any"`` turns the check off for anyone who would rather have the
    speed. The report then says out loud that its settings were mixed.
    """
    if existing is None:
        return False
    if adopt == "any":
        return True
    return review_signature(existing) == wanted


def outstanding(store, games: list, *, preset: str = DEFAULT_PRESET,
                threads: int = DEFAULT_THREADS,
                hash_mb: int = DEFAULT_HASH_MB, adopt: str = "matching") -> dict:
    """How many of these games actually need the engine at these settings."""
    try:
        wanted = signature(settings_for(preset, threads=threads, hash_mb=hash_mb))
    except FeatureUnavailable:
        wanted = ""
    ready = sum(1 for game in games
                if acceptable(store.load_review(game.get("id")), wanted, adopt))
    return {"total": len(games), "ready": ready,
            "outstanding": len(games) - ready, "signature": wanted}


def _record_for(game: dict):
    """A ChessAnalyzer record for one of our games, keeping our own id."""
    sources = require_analyzer("sources", "Reviewing games")
    return sources.record_from_pgn(
        game["pgn"], source=game.get("source", "pgn"),
        game_id=game["id"], url=game.get("url", ""))


def eval_cache(store):
    """The position cache, ours rather than ChessAnalyzer's.

    Sharing that app's cache file would be a write into another app's folder,
    which this repository's apps do not do to each other. The cost is that the
    first batch cannot reuse positions ChessAnalyzer has already seen; the
    benefit is that neither app can corrupt the other's data.
    """
    library = analyzer("library")
    if library is None:
        return None
    path = store.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return library.EvalCache(path)


def run(store, games: list, *, preset: str = DEFAULT_PRESET, engine_id=None,
        threads: int = DEFAULT_THREADS, hash_mb: int = DEFAULT_HASH_MB,
        adopt: str = "matching", progress=None, should_stop=None) -> dict:
    """Make sure every game has a review. Returns what it had to do.

    ``progress(done, total, message)`` is called per game, and ``should_stop``
    is checked between games and inside each one, so a cancelled batch keeps
    every review it has already finished.
    """
    if adopt not in ADOPT_MODES:
        raise BatchError(f"adopt must be one of: {', '.join(ADOPT_MODES)}")

    review_module = require_analyzer("review", "Reviewing games")
    engines = require_analyzer("engines", "Reviewing games")

    settings = settings_for(preset, engine_id=engine_id, threads=threads,
                            hash_mb=hash_mb)
    wanted = signature(settings)
    cache = eval_cache(store)

    counts = {"total": len(games), "already": 0, "adopted": 0, "reviewed": 0,
              "failed": 0, "skipped": 0}
    signatures: dict = {}
    failures: list = []
    started = time.time()

    try:
        for index, game in enumerate(games):
            if should_stop and should_stop():
                counts["skipped"] = len(games) - index
                break

            game_id = game.get("id")
            if progress:
                progress(index, len(games),
                         f"{game.get('white', '?')} vs {game.get('black', '?')}")

            existing = store.load_review(game_id)
            if acceptable(existing, wanted, adopt):
                counts["already"] += 1
                found = review_signature(existing)
                signatures[found] = signatures.get(found, 0) + 1
                continue

            offered = game.get("existingReview")
            if offered and adopt != "none" and acceptable(offered, wanted, adopt):
                store.save_review(game_id, offered)
                counts["adopted"] += 1
                found = review_signature(offered)
                signatures[found] = signatures.get(found, 0) + 1
                continue

            try:
                result = review_module.review(
                    _record_for(game), settings, cache=cache,
                    should_stop=should_stop)
            except review_module.ReviewCancelled:
                counts["skipped"] = len(games) - index
                break
            except Exception as exc:                          # noqa: BLE001
                # One unreviewable game must not lose the batch. Record it and
                # carry on; the report counts what it actually has.
                counts["failed"] += 1
                failures.append({"id": game_id, "reason": str(exc)[:200]})
                continue

            store.save_review(game_id, result)
            counts["reviewed"] += 1
            signatures[wanted] = signatures.get(wanted, 0) + 1

        if cache is not None:
            cache.save()
    finally:
        # python-chess runs each engine on a non-daemon thread, and CPython
        # joins those before atexit runs -- so a process that leaves one open
        # prints its last line and then hangs for ever with no traceback.
        # Every entry point must do this. See ChessAnalyzer's README.
        try:
            engines.close()
        except Exception:                                     # noqa: BLE001
            pass

    return {
        "counts": counts,
        "settings": settings.json(),
        "signature": wanted,
        "signatures": signatures,
        "uniform": len(signatures) <= 1,
        "failures": failures[:20],
        "elapsed": round(time.time() - started, 1),
    }


def load_reviews(store, games: list) -> dict:
    """``{game id: review}`` for every game that has one on disk."""
    out = {}
    for game in games:
        review = store.load_review(game.get("id"))
        if review is not None:
            out[game["id"]] = review
    return out


def estimate(games: list, preset: str = DEFAULT_PRESET, *,
             already: int = 0) -> dict:
    """A rough time for a batch, so the button can say what it will cost.

    Deliberately crude and labelled as such: engine speed varies by an order
    of magnitude across machines, and a number with a plus-or-minus on it is
    more honest than a spinner with no number at all.
    """
    chosen = PRESETS.get(preset) or PRESETS[DEFAULT_PRESET]
    seconds_each = {10: 3.0, 14: 12.0, 18: 60.0}.get(chosen["depth"], 12.0)
    outstanding = max(0, len(games) - already)
    return {
        "games": len(games),
        "outstanding": outstanding,
        "secondsPerGame": seconds_each,
        "seconds": int(outstanding * seconds_each),
        "rough": True,
    }


__all__ = [
    "ADOPT_MODES",
    "acceptable",
    "DEFAULT_PRESET",
    "PRESETS",
    "BatchError",
    "FeatureUnavailable",
    "estimate",
    "eval_cache",
    "load_reviews",
    "outstanding",
    "review_signature",
    "run",
    "settings_for",
    "signature",
]
