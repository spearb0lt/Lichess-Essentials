"""One run, start to finish.

The browser and the command line both want the same four steps in the same
order, and neither should own them:

1. **the games** -- fetched from a site, read from a PGN, or taken from
   ChessAnalyzer's library, then cached so the next run costs no network
2. **the reviews** -- adopted where they exist and computed where they do not,
   which is the only expensive step and the only one that can be interrupted
3. **the report** -- pure arithmetic over what is on disk, no engine, no
   network, so changing a threshold never costs a search
4. **saved**, so opening the app tomorrow costs nothing

Step 3 being pure is the design's one real constraint, and it is what makes
the evidence thresholds adjustable in the browser: re-slicing four hundred
games at a different sample floor is milliseconds.
"""

from __future__ import annotations

from . import aggregate, batch, report as report_module, sources
from .bridge import FeatureUnavailable
from .store import Store, StoreError, slugify


def _noop(*_args, **_kwargs) -> None:
    return None


def dataset_key(spec: dict) -> str:
    """The name a dataset is filed under. Stable for the same request."""
    kind = (spec.get("kind") or "").strip().lower()
    username = (spec.get("username") or "").strip().lstrip("@").lower()
    if kind in ("lichess", "chesscom"):
        if not username:
            raise StoreError("Give your username.")
        return f"{kind}-{slugify(username, 'you')}"
    if kind == "pgn":
        path = (spec.get("path") or "").strip()
        if not path:
            raise StoreError("Give the path to a PGN file or folder.")
        return f"pgn-{slugify(path.replace('/', '-').replace(chr(92), '-'), 'file')}"
    if kind == "analyzer":
        return "analyzer-library"
    raise StoreError(f"Unknown source {kind!r}.")


def load_games(store: Store, spec: dict, *, refresh: bool = False,
               token: str | None = None, job=None) -> dict:
    """The dataset's games, from the cache when it will do and the source when
    it will not.

    A cached payload is reused only when it was fetched with a request at
    least as wide as this one, so "give me 500 games" is never quietly
    answered with the 50 pulled last week.
    """
    key = dataset_key(spec)
    cached = None if refresh else store.load_games(key)

    if cached is not None and _covers(cached, spec):
        if job:
            job.say(f"{len(cached.get('games') or [])} games from the cache")
        return cached

    if job:
        job.progress(0, int(spec.get("limit") or 200), "Fetching games...")

    payload = sources.fetch(
        spec, token=token,
        progress=(lambda done, total: job.progress(done, total)) if job else None,
        should_stop=job.should_stop if job else None)
    store.save_games(key, payload)
    return payload


def _covers(cached: dict, spec: dict) -> bool:
    """Is a cached pull wide enough to answer this request?"""
    previous = cached.get("spec") or {}
    games = cached.get("games") or []

    wanted = int(spec.get("limit") or 200)
    if len(games) < wanted and len(games) >= int(previous.get("limit") or 0):
        return False

    previous_speeds = set(previous.get("speeds") or ())
    if previous_speeds and not set(spec.get("speeds") or ()).issubset(previous_speeds):
        return False
    if previous.get("ratedOnly") and not spec.get("ratedOnly", True):
        return False

    since = spec.get("sinceMs")
    previous_since = previous.get("sinceMs")
    if previous_since and (not since or since < previous_since):
        return False
    return True


def run(store: Store, spec: dict, *, preset: str = batch.DEFAULT_PRESET,
        threads: int = batch.DEFAULT_THREADS, hash_mb: int = batch.DEFAULT_HASH_MB,
        engine_id=None, adopt: str = "matching", refresh: bool = False,
        min_moves: int = aggregate.MIN_MOVES, min_games: int = aggregate.MIN_GAMES,
        review: bool = True, token: str | None = None, job=None) -> dict:
    """Fetch, review and aggregate one dataset. See the module docstring."""
    say = job.say if job else _noop
    key = dataset_key(spec)

    payload = load_games(store, spec, refresh=refresh, token=token, job=job)
    games = payload.get("games") or []
    if job and job.should_stop():
        raise RuntimeError("Stopped before anything was reviewed.")

    result = {}
    if review:
        say(f"Reviewing {len(games)} games...")
        result = batch.run(
            store, games, preset=preset, engine_id=engine_id, threads=threads,
            hash_mb=hash_mb, adopt=adopt,
            progress=(lambda done, total, message="":
                      job.progress(done, total, message)) if job else None,
            should_stop=job.should_stop if job else None)

    say("Aggregating...")
    reviews = batch.load_reviews(store, games)
    built = report_module.build(
        key=key, label=payload.get("label", key), source=payload.get("kind", ""),
        games=games, reviews=reviews, batch_result=result,
        spec=payload.get("spec"), min_moves=min_moves, min_games=min_games)

    store.save_report(key, built)
    say("Done.")
    return built


def reslice(store: Store, key: str, *, min_moves: int, min_games: int) -> dict:
    """Rebuild a saved report at a different evidence floor. No engine."""
    saved = store.load_report(key)
    if saved is None:
        raise StoreError(f"No report saved for {key}. Build one first.")
    payload = store.load_games(key)
    if payload is None:
        raise StoreError(
            f"The cached games for {key} are gone, so it cannot be re-sliced. "
            "Run it again.")
    games = payload.get("games") or []
    built = report_module.build(
        key=key, label=saved.get("label", key), source=saved.get("source", ""),
        games=games, reviews=batch.load_reviews(store, games),
        batch_result=saved.get("batch"), spec=saved.get("spec"),
        min_moves=min_moves, min_games=min_games)
    store.save_report(key, built)
    return built


__all__ = [
    "FeatureUnavailable",
    "dataset_key",
    "load_games",
    "reslice",
    "run",
]
