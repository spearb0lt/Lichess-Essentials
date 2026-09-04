"""One scout, start to finish.

The browser and the command line both want the same five steps in the same
order, and neither should own them:

1. their games -- from the cache, or fetched and then cached
2. your book -- from a repertoire, a study, your own games, or several at once
3. the report -- pure arithmetic, no network, no engine
4. engine suggestions for the biggest gaps, if you asked and there is an engine
5. save it, so opening the app tomorrow costs nothing

Step 3 is deliberately isolated in :mod:`player_prepper.scout` and stays that
way: everything that can fail for an external reason happens in 1, 2 and 4,
which makes the part that produces the numbers testable with no network, no
engine and no disk.
"""

from __future__ import annotations

from . import exploit as exploit_module
from .book import BookError, build_book
from .fetch import FetchError, fetch_games
from .scout import DEFAULT_MIN_GAMES, all_gaps, build_report
from .store import Store, StoreError, player_key
from .tree import DEFAULT_MAX_PLY


def _noop(*_args, **_kwargs) -> None:
    return None


def load_games(store: Store, site: str, username: str, *, limit: int = 300,
               speeds=None, rated_only: bool = True, since_ms=None,
               refresh: bool = False, token: str | None = None,
               job=None) -> dict:
    """Their games, from the cache when it will do and the site when it will not.

    A cached payload is reused only when it was fetched with filters at least
    as wide as the ones asked for now -- otherwise "give me 500 games" would
    be silently answered with the 50 fetched last week.
    """
    key = player_key(site, username)
    cached = None if refresh else store.load_games(key)

    if cached is not None and _cache_covers(cached, limit, speeds, rated_only,
                                            since_ms):
        if job:
            job.say(f"{len(cached.get('games') or [])} games from the cache")
        return cached

    if job:
        job.progress(0, limit, f"Fetching games from "
                               f"{'Lichess' if site == 'lichess' else 'Chess.com'}...")

    payload = fetch_games(
        site, username, limit=limit, speeds=speeds, rated_only=rated_only,
        since_ms=since_ms, token=token,
        progress=(lambda done, total: job.progress(done, total)) if job else None,
        should_stop=job.should_stop if job else None)

    store.save_games(key, payload)
    return payload


def _cache_covers(cached: dict, limit, speeds, rated_only, since_ms) -> bool:
    """Is a cached payload wide enough to answer this request?"""
    filters = cached.get("filters") or {}
    games = cached.get("games") or []

    # Fewer games than asked for, and the cache was not itself truncated by
    # the site running out -- so a bigger ask really would fetch more.
    if len(games) < int(limit) and len(games) >= int(filters.get("limit") or 0):
        return False

    cached_speeds = set(filters.get("speeds") or ())
    wanted = set(speeds or ())
    if cached_speeds and not wanted.issubset(cached_speeds):
        return False           # the cache was filtered to fewer speeds
    if filters.get("ratedOnly") and not rated_only:
        return False           # the cache dropped casual games; this wants them

    cached_since = filters.get("sinceMs")
    if cached_since and (not since_ms or since_ms < cached_since):
        return False           # the cache starts later than this wants
    return True


def run_scout(store: Store, *, site: str, username: str, book_specs=None,
              limit: int = 300, speeds=None, rated_only: bool = True,
              since_ms=None, max_ply: int = DEFAULT_MAX_PLY,
              min_games: int = DEFAULT_MIN_GAMES, refresh: bool = False,
              suggest: int = 0, token: str | None = None,
              job=None) -> dict:
    """Scout one player and save the report. See the module docstring."""
    say = job.say if job else _noop

    payload = load_games(store, site, username, limit=limit, speeds=speeds,
                         rated_only=rated_only, since_ms=since_ms,
                         refresh=refresh, token=token, job=job)
    if job and job.should_stop():
        raise RuntimeError("Stopped before the report was built.")

    book = None
    if book_specs:
        say("Building your book...")
        book = build_book(book_specs, token=token, store=store)

    say("Measuring coverage...")
    report = build_report(payload, book=book, max_ply=max_ply,
                          min_games=min_games)
    report["gamesUsed"] = len(payload.get("games") or [])

    if suggest:
        from . import engine                       # optional, imported late
        if engine.available():
            gaps = all_gaps(report)
            say(f"Asking the engine about {min(suggest, len(gaps))} gaps...")
            filled = engine.fill_suggestions(
                gaps, limit=int(suggest),
                progress=(lambda done, total:
                          job.progress(done, total)) if job else None,
                should_stop=job.should_stop if job else None)
            report["suggestionsFilled"] = filled
        else:
            report["suggestionsFilled"] = 0
            report["suggestionsNote"] = (
                "No engine available, so gaps have no suggested move. "
                "Install the sibling app and put a Stockfish binary in "
                "Lichess-Study-to-PDF/engine/.")

    store.save_scout(player_key(site, username), report)
    say("Done.")
    return report


def run_exploit(store: Store, key: str, *, color: str = "white",
                min_games: int = 3, limit: int = exploit_module.DEFAULT_LIMIT,
                movetime: float = 0.6, lines: int = 2, job=None) -> dict:
    """Analyse their choices for the best counter, and save the answers.

    Lives here rather than in the server because the command line wants the
    same five steps: pick the candidates, ask the engine, score them, merge
    the result back into the report on disk, save.

    The report is re-read immediately before writing. This is the one part of
    the app that takes minutes, and a gap suggestion saved from elsewhere
    during that time must not be thrown away by our copy of the report.
    """
    from . import engine                            # optional, imported late

    say = job.say if job else _noop

    report = store.load_scout(key)
    if report is None:
        raise StoreError(f"No scout saved for {key}. Scout them first.")
    if color not in ("white", "black"):
        raise ValueError("color must be white or black")

    section = (report.get("colors") or {}).get(color) or {}
    rows = exploit_module.candidates(section, min_games=max(1, min_games),
                                     limit=max(1, min(60, limit)))
    if not rows:
        raise ValueError(
            f"Nothing to analyse: they have no move played at least {min_games} "
            "times in this colour. Lower the minimum, or scout more of their "
            "games.")

    say(f"Asking the engine about {len(rows)} positions...")
    engine.fill_exploit(
        rows, count=max(1, min(engine.MAX_LINES, lines)), movetime=movetime,
        progress=(lambda done, total: job.progress(done, total)) if job else None,
        should_stop=job.should_stop if job else None)

    # Score once, with every factor on. The stored per-row factors are what
    # the browser multiplies when you switch one off, so the arithmetic lives
    # in one place and a toggle costs no engine time and no round trip.
    exploit_module.rank(rows)

    blob = {
        "rows": rows,
        "summary": exploit_module.summarise(rows),
        "minGames": min_games,
        "movetime": movetime,
        "engine": engine.available(),
    }

    fresh = store.load_scout(key) or report
    colors = fresh.setdefault("colors", {})
    colors.setdefault(color, section)["exploit"] = blob
    store.save_scout(key, fresh)
    return blob


__all__ = [
    "BookError",
    "FetchError",
    "load_games",
    "run_exploit",
    "run_scout",
]
