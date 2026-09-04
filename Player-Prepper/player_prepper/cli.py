"""Command line: run the app, or drive it from a script.

The browser interface is the main way to use this, but a scout is exactly the
sort of thing you want in a shell script the night before a tournament -- one
command per opponent, a PDF each -- so everything the browser does is also a
command.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import engine, export, openings
from .book import BookError, build_book, default_repertoire_dir, list_repertoires
from .bridge import FeatureUnavailable, status as bridge_status
from .fetch import SPEEDS, FetchError
from .pipeline import load_games, run_exploit, run_scout
from .exploit import DEFAULT_LIMIT, rank
from .scout import DEFAULT_MIN_GAMES, pretty_line
from .store import Store, StoreError, default_data_dir, player_key
from .tree import DEFAULT_MAX_PLY

BANNER = "Player Prepper"


def _say(text: str = "") -> None:
    print(text, flush=True)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr, flush=True)
    return 1


def _percent(value) -> str:
    return f"{round((value or 0) * 100)}%"


def _store(args) -> Store:
    path = Path(args.data).expanduser().resolve() if args.data else default_data_dir()
    return Store(path)


def _since_ms(days) -> int | None:
    if not days:
        return None
    moment = datetime.now(timezone.utc) - timedelta(days=int(days))
    return int(moment.timestamp() * 1000)


def _book_specs(args) -> list:
    """The book sources named on the command line, in the order given."""
    specs = []
    for slug in getattr(args, "repertoire", None) or []:
        specs.append({"kind": "repertoire", "slug": slug})
    for url in getattr(args, "study", None) or []:
        specs.append({"kind": "study", "url": url,
                      "color": getattr(args, "study_color", "auto")})
    for handle in getattr(args, "my_games", None) or []:
        site, _, username = handle.partition(":")
        if not username:
            site, username = "lichess", site
        specs.append({"kind": "games", "site": site, "username": username,
                      "limit": getattr(args, "my_games_limit", 200)})
    return specs


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host,
                                 port)) != 0


# ------------------------------------------------------------------ commands


def cmd_serve(args) -> int:
    import uvicorn

    from . import server

    if not _port_free(args.host, args.port):
        _say(f"Port {args.port} is already in use.")
        _say(f"  Something is listening on http://{args.host}:{args.port} - "
             "most likely this app is already running.")
        _say("  Open it in your browser, or start on another port:")
        _say("      ... cli serve --port 8781")
        return 1

    store = _store(args)
    server.set_data_dir(store.root)

    state = bridge_status()
    _say(BANNER)
    _say(f"  prep folder  {store.root}")
    _say(f"  repertoires  {default_repertoire_dir()}")
    _say(f"  engine       {state['stockfish'] or 'not found - gaps get no suggestion'}")
    if not state["sibling"]:
        _say("  pdf export   unavailable (pip install -e Lichess-Study-to-PDF)")
    else:
        _say(f"  pdf export   ready{'' if state['latex'] else ' (book mode needs LaTeX)'}")
    _say(f"  openings     {'named' if openings.available() else 'dataset not fetched yet'}")
    _say(f"  open         http://{args.host}:{args.port}")
    _say()

    uvicorn.run(server.app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_scout(args) -> int:
    store = _store(args)
    specs = _book_specs(args)

    if not specs:
        _say("No book given, so this reports what they play but cannot "
             "measure coverage.")
        _say("  Add --repertoire <slug>, --study <url> or --my-games <user>.")
        _say()

    report = run_scout(
        store, site=args.site, username=args.username, book_specs=specs,
        limit=args.limit, speeds=args.speed or (), rated_only=not args.casual,
        since_ms=_since_ms(args.days), max_ply=args.max_ply,
        min_games=args.min_games, refresh=args.refresh,
        suggest=args.suggest, token=args.token)

    _print_report(report, verbose=args.verbose)
    _say()
    _say(f"Saved to {store.scout_path(player_key(args.site, args.username))}")
    return 0


def cmd_show(args) -> int:
    store = _store(args)
    key = player_key(args.site, args.username)
    report = store.load_scout(key)
    if report is None:
        return _fail(f"No saved scout for {key}. Run `scout` first.")
    if args.json:
        _say(json.dumps(report, indent=1))
        return 0
    _print_report(report, verbose=args.verbose)
    return 0


def cmd_list(args) -> int:
    store = _store(args)
    rows = store.list_scouts()
    if not rows:
        _say(f"No scouts yet in {store.root}")
        return 0
    for row in rows:
        _say(f"{row['key']:<34} {row['games']:>4} games  "
             f"{row['scoutedAt'][:10]}  {row['book'] or 'no book'}")
    return 0


def cmd_forget(args) -> int:
    store = _store(args)
    key = player_key(args.site, args.username)
    store.delete_scout(key)
    _say(f"Forgot {key} (report and cached games).")
    return 0


def cmd_repertoires(args) -> int:
    folder = default_repertoire_dir()
    rows = list_repertoires(folder)
    if not rows:
        _say(f"No repertoires found in {folder}")
        _say("  Set REPERTOIRE_DIR if yours live somewhere else.")
        return 0
    _say(f"From {folder}")
    for row in rows:
        _say(f"  {row['slug']:<28} {row['color']:<6} "
             f"{row['chapters']:>2} chapters  {row['name']}")
    return 0


def cmd_book(args) -> int:
    """Build a book and say what is in it, without scouting anybody."""
    store = _store(args)
    specs = _book_specs(args)
    if not specs:
        return _fail("Give at least one of --repertoire, --study or --my-games.")
    book = build_book(specs, token=args.token, store=store)
    stats = book.stats()
    _say(f"{stats['label']}")
    _say(f"  {stats['positions']} positions, {stats['moves']} moves, "
         f"{stats['branchPoints']} branch points")
    for source in stats["sources"]:
        detail = ", ".join(
            f"{key} {value}" for key, value in source.items()
            if key in ("chapters", "games", "moves", "color"))
        _say(f"  - {source['label']}: {detail}")
        if source.get("note"):
            _say(f"      {source['note']}")
    return 0


def cmd_exploit(args) -> int:
    """Best counters to their real choices, ranked. Needs an engine to be useful."""
    store = _store(args)
    key = player_key(args.site, args.username)

    if not engine.available():
        _say("No engine available, so there is no 'best reply' to report.")
        _say("  Install the sibling app and put a Stockfish binary in")
        _say("  Lichess-Study-to-PDF/engine/. Ranking will still use their")
        _say("  record and how often they play each move.")
        _say()

    colours = ("white", "black") if args.color == "both" else (args.color,)
    for colour in colours:
        try:
            blob = run_exploit(store, key, color=colour,
                               min_games=args.min_games, limit=args.limit,
                               movetime=args.movetime)
        except ValueError as exc:
            _say(f"they play {colour}: {exc}")
            continue

        summary = blob["summary"]
        _say(f"--- they play {colour}: {summary['positions']} positions, "
             f"{summary['games']} games, {summary['analysed']} analysed ---")

        rows = rank(blob["rows"],
                    use_frequency=not args.no_frequency,
                    use_record=not args.no_record,
                    use_edge=not args.no_edge)
        for row in rows[:args.top]:
            best = ((row.get("engine") or {}).get("lines") or [{}])[0]
            reply = (best.get("first") or {}).get("san", "")
            edge = row["factors"]["edge"]
            _say(f"  {row['opportunity'] * 100:5.0f}  "
                 f"{pretty_line(row['line']):<34s} "
                 f"{('-> ' + reply) if reply else '':<10s} "
                 f"{row['games']:>3}g  they {_percent(row['score'])}"
                 + (f"  you {_percent(edge)}" if edge is not None else ""))
        _say()

    _say(f"Saved into {store.scout_path(key)}")
    return 0


def cmd_pdf(args) -> int:
    store = _store(args)
    key = player_key(args.site, args.username)
    report = store.load_scout(key)
    if report is None:
        return _fail(f"No saved scout for {key}. Run `scout` first.")
    out = Path(args.out).expanduser().resolve() if args.out else None
    path = export.build(report, mode=args.mode, out_path=out,
                        include_steps=not args.no_steps,
                        landscape_pages=not args.portrait)
    _say(f"Wrote {path}")
    return 0


def cmd_games(args) -> int:
    """Fetch and cache somebody's games without building a report."""
    store = _store(args)
    payload = load_games(store, args.site, args.username, limit=args.limit,
                         speeds=args.speed or (), rated_only=not args.casual,
                         since_ms=_since_ms(args.days), refresh=args.refresh,
                         token=args.token)
    games = payload.get("games") or []
    _say(f"{len(games)} games for {payload.get('username')} "
         f"cached at {store.games_path(player_key(args.site, args.username))}")
    return 0


# -------------------------------------------------------------------- output


def _print_report(report: dict, *, verbose: bool = False) -> None:
    summary = report.get("summary") or {}
    tally = summary.get("tally") or {}
    site = "Lichess" if report.get("site") == "lichess" else "Chess.com"

    _say(f"{report.get('username')} on {site}")
    _say(f"  {summary.get('games', 0)} games, {summary.get('from', '?')} to "
         f"{summary.get('to', '?')}, scoring {_percent(tally.get('score'))} "
         f"[+{tally.get('w', 0)} ={tally.get('d', 0)} -{tally.get('l', 0)}]")
    rating = summary.get("rating") or {}
    if rating.get("median"):
        _say(f"  rating {rating['min']}-{rating['max']} "
             f"(median {rating['median']}), "
             f"{summary.get('opponents', 0)} different opponents")
    speeds = summary.get("speeds") or {}
    if speeds:
        _say("  " + ", ".join(f"{count} {name}" for name, count in speeds.items()))

    book = report.get("book")
    if book:
        _say(f"  book: {book.get('label')} "
             f"({book.get('positions', 0)} positions)")
    else:
        _say("  book: none, so coverage was not measured")

    for their_color in ("white", "black"):
        section = (report.get("colors") or {}).get(their_color) or {}
        colour_tally = section.get("tally") or {}
        if not colour_tally.get("games"):
            continue

        _say()
        _say(f"--- they play {their_color} ({colour_tally.get('games')} games, "
             f"scoring {_percent(colour_tally.get('score'))}) ---")

        openings_rows = section.get("openings") or []
        if openings_rows:
            _say("  openings")
            for row in openings_rows[:6]:
                _say(f"    {row['games']:>3}g {_percent(row['share']):>4}  "
                     f"{_percent(row['score']):>4}  {row['name']}")

        weak = section.get("weakSpots") or []
        if weak:
            _say("  where they leak points")
            for row in weak[:6]:
                line = pretty_line(row.get("line") or []) or "start"
                _say(f"    {row['games']:>3}g {_percent(row['score']):>4} "
                     f"leak {row['leak']:>5}  {line} -> {row['san']}")

        coverage = section.get("coverage") or {}
        if coverage.get("noBook"):
            continue

        _say(f"  coverage as {coverage.get('youPlay')}: "
             f"{coverage.get('covered', 0)}/{coverage.get('inScope', 0)} games "
             f"stay inside your book ({coverage.get('percent', 0)}%), "
             f"{coverage.get('offBook', 0)} never reach it")
        gaps = coverage.get("gaps") or []
        if gaps:
            _say(f"  gaps ({coverage.get('gapPositions', 0)} positions, "
                 f"{coverage.get('allGapGames', 0)} of their games)")
            shown = gaps if verbose else gaps[:8]
            for gap in shown:
                named = (gap.get("opening") or {}).get("name") or ""
                _say(f"    {gap['games']:>3}g  {gap['lineText'] or '(start)'}"
                     + (f"   [{named}]" if named else ""))
                best = ((gap.get("engine") or {}).get("lines") or [{}])[0]
                if best.get("line"):
                    _say(f"          engine {best.get('text', '')}  {best['line']}")
            if not verbose and len(gaps) > 8:
                _say(f"    ... and {len(gaps) - 8} more (--verbose for all)")


# -------------------------------------------------------------------- parser


def _add_fetch_args(parser) -> None:
    parser.add_argument("--limit", type=int, default=300,
                        help="how many of their games to pull (default 300)")
    parser.add_argument("--speed", action="append", choices=SPEEDS,
                        help="restrict to a speed; repeatable")
    parser.add_argument("--casual", action="store_true",
                        help="include unrated games (rated only by default)")
    parser.add_argument("--days", type=int,
                        help="only games from the last N days")
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch even if their games are cached")
    parser.add_argument("--token", help="Lichess API token (raises the rate limit)")


def _add_book_args(parser) -> None:
    parser.add_argument("--repertoire", action="append",
                        help="a Repertoire-Creator slug; repeatable")
    parser.add_argument("--study", action="append",
                        help="a Lichess study URL; repeatable")
    parser.add_argument("--study-color", default="auto",
                        choices=("auto", "white", "black", "both"),
                        help="which side a study is for (default: its Orientation tag)")
    parser.add_argument("--my-games", action="append", metavar="[SITE:]USER",
                        help="your own games as your book, e.g. chesscom:you")
    parser.add_argument("--my-games-limit", type=int, default=200,
                        help="how many of your own games to read (default 200)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prepper", description=BANNER)
    parser.add_argument("--data", help="prep folder (default: ./prep)")
    subs = parser.add_subparsers(dest="command", required=True)

    serve = subs.add_parser("serve", help="run the browser interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8780)
    serve.set_defaults(func=cmd_serve)

    scout = subs.add_parser("scout", help="scout a player and save the report")
    scout.add_argument("username")
    scout.add_argument("--site", choices=("lichess", "chesscom"),
                       default="lichess")
    _add_fetch_args(scout)
    _add_book_args(scout)
    scout.add_argument("--max-ply", type=int, default=DEFAULT_MAX_PLY,
                       help=f"how deep to scout (default {DEFAULT_MAX_PLY} plies)")
    scout.add_argument("--min-games", type=int, default=DEFAULT_MIN_GAMES,
                       help="smallest sample a weak spot may rest on")
    scout.add_argument("--suggest", type=int, default=0, metavar="N",
                       help="ask the engine about the N biggest gaps")
    scout.add_argument("--verbose", action="store_true",
                       help="print every gap, not the first eight")
    scout.set_defaults(func=cmd_scout)

    show = subs.add_parser("show", help="print a saved report")
    show.add_argument("username")
    show.add_argument("--site", choices=("lichess", "chesscom"), default="lichess")
    show.add_argument("--json", action="store_true", help="the raw report")
    show.add_argument("--verbose", action="store_true")
    show.set_defaults(func=cmd_show)

    listing = subs.add_parser("list", help="list saved scouts")
    listing.set_defaults(func=cmd_list)

    forget = subs.add_parser("forget", help="delete a scout and its cached games")
    forget.add_argument("username")
    forget.add_argument("--site", choices=("lichess", "chesscom"), default="lichess")
    forget.set_defaults(func=cmd_forget)

    reps = subs.add_parser("repertoires", help="list Repertoire-Creator repertoires")
    reps.set_defaults(func=cmd_repertoires)

    book = subs.add_parser("book", help="build a book and say what is in it")
    _add_book_args(book)
    book.add_argument("--token")
    book.set_defaults(func=cmd_book)

    games = subs.add_parser("games", help="fetch and cache games, no report")
    games.add_argument("username")
    games.add_argument("--site", choices=("lichess", "chesscom"), default="lichess")
    _add_fetch_args(games)
    games.set_defaults(func=cmd_games)

    exploit_cmd = subs.add_parser(
        "exploit", help="best counters to their choices, ranked")
    exploit_cmd.add_argument("username")
    exploit_cmd.add_argument("--site", choices=("lichess", "chesscom"),
                             default="lichess")
    exploit_cmd.add_argument("--color", choices=("white", "black", "both"),
                             default="both",
                             help="which colour THEY have (default: both)")
    exploit_cmd.add_argument("--min-games", type=int, default=3,
                             help="smallest sample a candidate may rest on")
    exploit_cmd.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                             help="how many positions to analyse per colour")
    exploit_cmd.add_argument("--movetime", type=float, default=0.6,
                             help="seconds the engine gets per position")
    exploit_cmd.add_argument("--top", type=int, default=12,
                             help="how many rows to print")
    exploit_cmd.add_argument("--no-frequency", action="store_true",
                             help="ignore how often they play it")
    exploit_cmd.add_argument("--no-record", action="store_true",
                             help="ignore how badly it goes for them")
    exploit_cmd.add_argument("--no-edge", action="store_true",
                             help="ignore what the engine gives you")
    exploit_cmd.set_defaults(func=cmd_exploit)

    pdf = subs.add_parser("pdf", help="export a saved report as a PDF")
    pdf.add_argument("username")
    pdf.add_argument("--site", choices=("lichess", "chesscom"), default="lichess")
    pdf.add_argument("--mode", choices=tuple(export.MODES), default="grid")
    pdf.add_argument("--out")
    pdf.add_argument("--no-steps", action="store_true",
                     help="text only: every line and number, no diagrams")
    pdf.add_argument("--portrait", action="store_true")
    pdf.set_defaults(func=cmd_pdf)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (StoreError, BookError, FetchError, FeatureUnavailable,
            ValueError) as exc:
        return _fail(str(exc))
    finally:
        engine.close_provider()


if __name__ == "__main__":
    raise SystemExit(main())
