"""Command line: run the app, or drive it from a script.

Reviewing four hundred games is exactly the sort of thing you start before
going to bed, so everything the browser does is also a command -- and the
long one prints progress as it goes rather than sitting silent for an hour.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import aggregate, batch, exportcsv, pdf, pipeline
from .bridge import FeatureUnavailable, analyzer, status as bridge_status
from .sources import SPEEDS, SourceError
from .store import Store, StoreError, default_data_dir

BANNER = "Weakness Report"


def _say(text: str = "") -> None:
    print(text, flush=True)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr, flush=True)
    return 1


def _store(args) -> Store:
    path = Path(args.data).expanduser().resolve() if args.data else default_data_dir()
    return Store(path)


def _since_ms(days):
    if not days:
        return None
    moment = datetime.now(timezone.utc) - timedelta(days=int(days))
    return int(moment.timestamp() * 1000)


def _spec(args) -> dict:
    return {
        "kind": args.source,
        "username": getattr(args, "username", "") or "",
        "path": getattr(args, "path", "") or "",
        "limit": args.limit,
        "speeds": args.speed or [],
        "ratedOnly": not args.casual,
        "sinceMs": _since_ms(args.days),
    }


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host,
                                 port)) != 0


def _duration(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


class _Progress:
    """Prints a one-line progress bar without scrolling the terminal."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.last = 0.0

    def __call__(self, done, total, message="") -> None:
        if self.quiet or not total:
            return
        now = time.time()
        if now - self.last < 0.2 and done < total:
            return
        self.last = now
        share = done / total
        filled = int(share * 24)
        bar = "#" * filled + "." * (24 - filled)
        text = f"\r  [{bar}] {done}/{total}  {message[:40]:<40s}"
        sys.stdout.write(text)
        sys.stdout.flush()

    def done(self) -> None:
        if not self.quiet:
            sys.stdout.write("\r" + " " * 78 + "\r")
            sys.stdout.flush()


class _Job:
    """The little of a job's interface that the CLI needs."""

    def __init__(self, progress):
        self._progress = progress

    def progress(self, done, total, message=None):
        self._progress(done, total, message or "")

    def say(self, message):
        self._progress.done()
        _say(f"  {message}")

    def should_stop(self):
        return False


# ------------------------------------------------------------------ commands


def cmd_serve(args) -> int:
    import uvicorn

    from . import server

    if not _port_free(args.host, args.port):
        _say(f"Port {args.port} is already in use.")
        _say(f"  Something is listening on http://{args.host}:{args.port} - "
             "most likely this app is already running.")
        _say("  Open it in your browser, or start on another port:")
        _say("      ... cli serve --port 8782")
        return 1

    store = _store(args)
    server.set_data_dir(store.root)
    state = bridge_status()

    _say(BANNER)
    _say(f"  history      {store.root}")
    _say(f"  reviews      {store.review_count()} already on disk")
    if state["analyzer"]:
        _say(f"  review rules ChessAnalyzer ({state['analyzerVia']})")
    else:
        _say("  review rules NOT FOUND - games cannot be reviewed")
        _say("               keep the ChessAnalyzer folder beside this one, or")
        _say("               set CHESS_ANALYZER_DIR, or pip install -e ChessAnalyzer")
    _say(f"  engine       {state['stockfish'] or 'not found - nothing can be reviewed'}")
    _say(f"  pdf          {'ready' if state['study'] else 'no diagrams (pip install -e Lichess-Study-to-PDF)'}")
    _say(f"  open         http://{args.host}:{args.port}")
    _say()

    uvicorn.run(server.app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_run(args) -> int:
    store = _store(args)
    progress = _Progress(quiet=args.quiet)
    job = _Job(progress)

    started = time.time()
    report = pipeline.run(
        store, _spec(args), preset=args.preset, threads=args.threads,
        hash_mb=args.hash_mb, adopt=args.adopt, refresh=args.refresh,
        min_moves=args.min_moves, min_games=args.min_games,
        review=not args.no_review, token=args.token, job=job)
    progress.done()

    _print_report(report, top=args.top)
    _say()
    _say(f"Built in {_duration(time.time() - started)}. "
         f"Saved to {store.report_path(report['key'])}")
    return 0


def cmd_show(args) -> int:
    store = _store(args)
    report = store.load_report(args.key)
    if report is None:
        return _fail(f"No report called {args.key}. Run `list` to see what there is.")
    if args.json:
        _say(json.dumps(report, indent=1))
        return 0
    _print_report(report, top=args.top, slices=args.slice)
    return 0


def cmd_list(args) -> int:
    store = _store(args)
    rows = store.list_reports()
    if not rows:
        _say(f"No reports yet in {store.root}")
        return 0
    for row in rows:
        _say(f"{row['key']:<28} {row['reviewed']:>4} reviewed  "
             f"{row['builtAt'][:10]}  {row['label']}")
    _say()
    _say(f"{store.review_count()} reviews on disk, shared by every report.")
    return 0


def cmd_forget(args) -> int:
    store = _store(args)
    store.delete_report(args.key)
    _say(f"Forgot the report {args.key}. "
         f"{store.review_count()} reviews kept -- they are the expensive part.")
    return 0


def cmd_reslice(args) -> int:
    store = _store(args)
    report = pipeline.reslice(store, args.key, min_moves=args.min_moves,
                              min_games=args.min_games)
    _print_report(report, top=args.top)
    return 0


def cmd_pdf(args) -> int:
    store = _store(args)
    report = store.load_report(args.key)
    if report is None:
        return _fail(f"No report called {args.key}.")
    out = Path(args.out).expanduser().resolve() if args.out else None
    path = pdf.build(report, out, slices=args.slice or None,
                     landscape_pages=args.landscape,
                     include_moments=not args.no_diagrams,
                     include_method=not args.no_method)
    _say(f"Wrote {path}")
    return 0


def cmd_csv(args) -> int:
    store = _store(args)
    report = store.load_report(args.key)
    if report is None:
        return _fail(f"No report called {args.key}.")
    text = exportcsv.slices_csv(report)
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        _say(f"Wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_estimate(args) -> int:
    store = _store(args)
    spec = _spec(args)
    key = pipeline.dataset_key(spec)
    cached = store.load_games(key) or {}
    games = cached.get("games") or []
    ready = batch.outstanding(store, games, preset=args.preset,
                              adopt=args.adopt)
    already = ready["ready"]
    if not games:
        _say(f"No games cached for {key} yet, so there is nothing to measure.")
        _say("  Run `games` first, or just run it and watch the progress bar.")
        return 0
    guess = batch.estimate(games, args.preset, already=already)
    _say(f"{len(games)} games cached, {already} already reviewed.")
    _say(f"  {guess['outstanding']} to review at preset '{args.preset}': "
         f"roughly {_duration(guess['seconds'])} "
         f"({guess['secondsPerGame']:.0f}s a game, very rough).")
    return 0


def cmd_games(args) -> int:
    store = _store(args)
    progress = _Progress(quiet=args.quiet)
    payload = pipeline.load_games(store, _spec(args), refresh=args.refresh,
                                  token=args.token, job=_Job(progress))
    progress.done()
    _say(f"{len(payload.get('games') or [])} games cached for "
         f"{payload.get('label')}")
    return 0


# -------------------------------------------------------------------- output


def _fmt(value, digits=1, dash="-"):
    return dash if value is None else f"{value:.{digits}f}"


def _print_report(report: dict, *, top: int = 8, slices=None) -> None:
    summary = report.get("summary") or {}
    record = summary.get("record") or {}
    settings = (report.get("batch") or {}).get("settings") or {}

    _say(report.get("label", report.get("key", "")))
    _say(f"  {summary.get('reviewed', 0)} games reviewed, "
         f"{summary.get('scoredMoves', 0)} of your moves counted, "
         f"{summary.get('from', '?')} to {summary.get('to', '?')}")
    _say(f"  record +{record.get('win', 0)} ={record.get('draw', 0)} "
         f"-{record.get('loss', 0)}   "
         f"acpl {_fmt(summary.get('acpl'))}   "
         f"accuracy {_fmt(summary.get('accuracy'))}   "
         f"~{summary.get('estimatedRating') or '-'}")
    if settings.get("depth"):
        _say(f"  searched to depth {settings['depth']} on "
             f"{settings.get('threads')} thread(s)"
             + ("" if (report.get('batch') or {}).get('uniform', True)
                else "  [MIXED SETTINGS - games are not strictly comparable]"))
    if summary.get("unreviewed"):
        _say(f"  {summary['unreviewed']} fetched games have no review and are "
             "not counted")

    found = report.get("findings") or {}
    _say()
    _say(f"--- costing you most (at least {found.get('minMoves')} moves in "
         f"{found.get('minGames')} games) ---")
    for row in (found.get("weaknesses") or [])[:top]:
        _say(f"  {row['sentence']}")
    if not found.get("weaknesses"):
        _say("  Nothing cleared the evidence floor. Add games or lower it.")

    if found.get("strengths"):
        _say()
        _say("--- what you do well ---")
        for row in found["strengths"][:3]:
            _say(f"  {row['sentence']}")

    if slices:
        for key in slices:
            data = (report.get("slices") or {}).get(key)
            if not data:
                _say(f"\n(no slice called {key})")
                continue
            _say()
            _say(f"--- {data['label']} ---")
            for row in data["buckets"]:
                _say(f"  {row['bucket']:<32s} {row['moves']:>5} moves "
                     f"{row['games']:>4}g  acpl {_fmt(row['acpl'], 0):>5}  "
                     f"acc {_fmt(row['accuracy']):>5}")

    moments = report.get("worstMoments") or []
    if moments:
        _say()
        _say("--- the moves that cost most ---")
        for row in moments[:5]:
            _say(f"  {row['moveNumber']:>3}. {row['san']:<8s} -{row['pawns']:<6} "
                 f"best {row['bestSan'] or '?':<8s} {row['situation'] or row['phase']}")


# -------------------------------------------------------------------- parser


def _add_source_args(parser) -> None:
    parser.add_argument("--source", default="lichess",
                        choices=("lichess", "chesscom", "pgn", "analyzer"),
                        help="where the games come from")
    parser.add_argument("username", nargs="?", default="",
                        help="your username on that site")
    parser.add_argument("--path", help="a .pgn file or a folder of them")
    parser.add_argument("--limit", type=int, default=200,
                        help="how many of your games (default 200)")
    parser.add_argument("--speed", action="append", choices=SPEEDS,
                        help="restrict to a speed; repeatable")
    parser.add_argument("--casual", action="store_true",
                        help="include unrated games")
    parser.add_argument("--days", type=int, help="only the last N days")
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch even if the games are cached")
    parser.add_argument("--token", help="Lichess API token (raises the rate limit)")
    parser.add_argument("--quiet", action="store_true", help="no progress bar")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weakness", description=BANNER)
    parser.add_argument("--data", help="history folder (default: ./history)")
    subs = parser.add_subparsers(dest="command", required=True)

    serve = subs.add_parser("serve", help="run the browser interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8781)
    serve.set_defaults(func=cmd_serve)

    run = subs.add_parser("run", help="fetch, review and build a report")
    _add_source_args(run)
    run.add_argument("--preset", default=batch.DEFAULT_PRESET,
                     choices=tuple(batch.PRESETS),
                     help="how deep to search each position")
    run.add_argument("--threads", type=int, default=batch.DEFAULT_THREADS,
                     help="engine threads; 1 keeps the report reproducible")
    run.add_argument("--hash-mb", type=int, default=batch.DEFAULT_HASH_MB)
    run.add_argument("--adopt", default="matching", choices=batch.ADOPT_MODES,
                     help="use ChessAnalyzer's existing reviews (default: only "
                          "those searched the same way)")
    run.add_argument("--no-review", action="store_true",
                     help="aggregate what is already reviewed and nothing more")
    run.add_argument("--min-moves", type=int, default=aggregate.MIN_MOVES)
    run.add_argument("--min-games", type=int, default=aggregate.MIN_GAMES)
    run.add_argument("--top", type=int, default=8)
    run.set_defaults(func=cmd_run)

    games = subs.add_parser("games", help="fetch and cache games, no review")
    _add_source_args(games)
    games.set_defaults(func=cmd_games)

    estimate = subs.add_parser("estimate", help="how long a review would take")
    _add_source_args(estimate)
    estimate.add_argument("--preset", default=batch.DEFAULT_PRESET,
                          choices=tuple(batch.PRESETS))
    estimate.add_argument("--adopt", default="matching",
                          choices=batch.ADOPT_MODES)
    estimate.set_defaults(func=cmd_estimate)

    show = subs.add_parser("show", help="print a saved report")
    show.add_argument("key")
    show.add_argument("--json", action="store_true")
    show.add_argument("--top", type=int, default=8)
    show.add_argument("--slice", action="append",
                      help="also print this slice in full; repeatable")
    show.set_defaults(func=cmd_show)

    listing = subs.add_parser("list", help="list saved reports")
    listing.set_defaults(func=cmd_list)

    forget = subs.add_parser("forget", help="delete a report (reviews are kept)")
    forget.add_argument("key")
    forget.set_defaults(func=cmd_forget)

    reslice = subs.add_parser("reslice",
                              help="rebuild at a different evidence floor")
    reslice.add_argument("key")
    reslice.add_argument("--min-moves", type=int, default=aggregate.MIN_MOVES)
    reslice.add_argument("--min-games", type=int, default=aggregate.MIN_GAMES)
    reslice.add_argument("--top", type=int, default=8)
    reslice.set_defaults(func=cmd_reslice)

    pdf_cmd = subs.add_parser("pdf", help="export a report as a PDF")
    pdf_cmd.add_argument("key")
    pdf_cmd.add_argument("--out")
    pdf_cmd.add_argument("--slice", action="append",
                         help="only these slices; repeatable")
    pdf_cmd.add_argument("--landscape", action="store_true")
    pdf_cmd.add_argument("--no-diagrams", action="store_true")
    pdf_cmd.add_argument("--no-method", action="store_true")
    pdf_cmd.set_defaults(func=cmd_pdf)

    csv_cmd = subs.add_parser("csv", help="every slice as one CSV")
    csv_cmd.add_argument("key")
    csv_cmd.add_argument("--out")
    csv_cmd.set_defaults(func=cmd_csv)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _say("\nStopped. Every review already finished is kept.")
        return 130
    except (StoreError, SourceError, FeatureUnavailable, batch.BatchError,
            ValueError) as exc:
        return _fail(str(exc))
    finally:
        # A warm engine sits on a non-daemon thread, and CPython joins those
        # before atexit runs, so a command that leaves one open never returns.
        engines = analyzer("engines")
        if engines is not None:
            try:
                engines.close()
            except Exception:                                # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
