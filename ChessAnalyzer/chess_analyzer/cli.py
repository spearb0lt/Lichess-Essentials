"""Command line: start the server, or review a game without a browser.

``serve`` is what you almost always want.  The rest exists because a review is
a batch job at heart, and being able to run one from a script -- or check what
the engine picker would offer before opening a browser -- is worth the fifty
lines.

Every path that opens an engine closes it in a ``finally``.  That is not
politeness: python-chess keeps its engine loop on a non-daemon thread, so a
command that skips the close prints its output and then hangs for ever.  See
:data:`chess_analyzer.engines.POOL`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import engines, library, openings, review
from .sources import SourceError, resolve

DEFAULT_PORT = 8779


def _banner(data_dir: Path) -> None:
    """Say what was found, so nobody has to guess why a feature is missing."""
    found = engines.discover()
    print("Chess Analyzer")
    print(f"  games      {data_dir}")
    if found:
        for spec in found[:3]:
            print(f"  engine     {spec.name}  ({spec.path})")
        if len(found) > 3:
            print(f"             ...and {len(found) - 3} more")
    else:
        print("  engine     none found -- the engine picker in the app can "
              "download one,")
        print("             or put a Stockfish binary in "
              "Lichess-Study-to-PDF/engine/")
    print(f"  openings   {'ready' if openings.available() else 'will download on first use'}")


def cmd_serve(args) -> int:
    import uvicorn

    from . import server

    data_dir = Path(args.games or library.DEFAULT_DIR)
    server.set_data_dir(data_dir)
    _banner(server.DATA_DIR)
    print(f"\n  http://{args.host}:{args.port}\n  Ctrl+C to stop\n")
    # uvicorn.run blocks for the life of the server, so a redirected stdout
    # would otherwise hold the whole banner in its buffer until shutdown --
    # which is exactly when nobody needs to be told the address any more.
    sys.stdout.flush()

    try:
        uvicorn.run(server.app, host=args.host, port=args.port,
                    log_level="warning")
    finally:
        # uvicorn runs the shutdown handler that does this, but not on every
        # exit path, and a surviving engine hangs the process. See engines.POOL.
        engines.close()
    return 0


def cmd_review(args) -> int:
    lib = library.Library(args.games or library.DEFAULT_DIR)
    cache = lib.load_cache()
    try:
        record = resolve(args.reference)
        print(f"{record.white} vs {record.black}  "
              f"{record.result}  {record.ply_count} plies")

        settings = review.Settings.from_preset(
            args.preset, engine_id=args.engine, movetime=args.movetime,
            depth=args.depth, multipv=args.multipv)

        width = 40
        state = {"last": -1}

        def progress(done, total, message=None):
            share = int(width * done / max(1, total))
            if share != state["last"]:
                state["last"] = share
                bar = "#" * share + "." * (width - share)
                print(f"\r  [{bar}] {done}/{total}", end="", flush=True)

        result = review.review(record, settings, cache=cache, progress=progress)
        print()

        lib.save(record, result)
        _print_review(result)
        print(f"\n  saved to {lib.path_for(record.id)}")
        return 0
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except engines.EngineError as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return 1
    finally:
        # Mandatory: see the module docstring.
        engines.close()
        cache.save()


def _print_review(result: dict) -> None:
    opening = result["opening"]
    print(f"\n  {opening['eco']} {opening['name']}"
          f"   (book to ply {opening['bookPly']})")
    print(f"  engine: {result['engine']['name']}   "
          f"{result['elapsed']}s   preset: {result['settings']['preset']}")

    for side in ("white", "black"):
        summary = result["summary"][side]
        counts = ", ".join(f"{label} {count}"
                           for label, count in summary["counts"].items() if count)
        judgments = summary["judgments"]
        print(f"\n  {side.upper():5s} accuracy {summary['accuracy']}%   "
              f"ACPL {summary['acpl']}   ~{summary['estimatedRating']}")
        print(f"        {judgments['inaccuracy']} inaccuracies, "
              f"{judgments['mistake']} mistakes, {judgments['blunder']} blunders")
        print(f"        {counts}")
        phases = summary["phases"]
        print("        " + "  ".join(
            f"{name} {value}%" for name, value in phases.items()
            if value is not None))

    rows = {row["ply"]: row for row in result["moves"]}
    if result["moments"]:
        print("\n  turning points")
        for ply in result["moments"]:
            row = rows[ply]
            dots = "." if row["color"] == "white" else "..."
            print(f"    {row['moveNumber']:3d}{dots:3s} {row['san']:8s} "
                  f"{row['label']:11s} -{row['winLoss']:.1f}%   "
                  f"best {row['bestSan']}")


def cmd_engines(args) -> int:
    if args.install:
        def progress(done, total, message=None):
            if message:
                print(f"\n  {message}", end="", flush=True)
            elif total:
                print(f"\r  {done * 100 // total}%", end="", flush=True)
        try:
            spec = engines.install(args.install, progress=progress)
        except engines.EngineError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return 1
        print(f"\n  installed {spec.name} -> {spec.path}")
        return 0

    catalog = engines.catalog(offline=args.offline)
    print("found on this machine:")
    for spec in catalog["found"]:
        print(f"  {spec['id']}\n      {spec['name']}")
    if not catalog["found"]:
        print("  (none)")

    if catalog["downloads"]:
        print("\navailable to download:")
        for spec in catalog["downloads"]:
            size = spec["downloadSize"] / 1e6
            print(f"  {spec['id']:22s} {spec['name']:18s} {size:6.1f} MB")
    if catalog["warning"]:
        print(f"\n  {catalog['warning']}")
    return 0


def cmd_import(args) -> int:
    lib = library.Library(args.games or library.DEFAULT_DIR)
    try:
        record = resolve(args.reference)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    lib.save(record)
    print(f"  {record.white} vs {record.black}  {record.result}")
    print(f"  saved as {record.id} -> {lib.path_for(record.id)}")
    return 0


def cmd_library(args) -> int:
    lib = library.Library(args.games or library.DEFAULT_DIR)
    rows = lib.listing()
    if not rows:
        print("  the library is empty")
        return 0
    for row in rows:
        mark = "*" if row["reviewed"] else " "
        white = (row.get("white") or "?")[:16]
        black = (row.get("black") or "?")[:16]
        print(f"  {mark} {row['id']:26s} {white:16s} vs {black:16s} "
              f"{row.get('result', '*'):8s}")
    print(f"\n  {len(rows)} games, * = reviewed")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="chess-analyzer",
        description="Review any chess game with a local engine.")
    parser.add_argument("--games", help="where the library lives")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="start the browser interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.set_defaults(func=cmd_serve)

    review_parser = subparsers.add_parser(
        "review", help="review a game and print the result")
    review_parser.add_argument(
        "reference", help="a Lichess or Chess.com URL, a game id, or a PGN file's contents")
    review_parser.add_argument("--preset", default="standard",
                               choices=sorted(review.PRESETS))
    review_parser.add_argument("--engine", help="engine id from `engines`")
    review_parser.add_argument("--movetime", type=float)
    review_parser.add_argument("--depth", type=int)
    review_parser.add_argument("--multipv", type=int)
    review_parser.set_defaults(func=cmd_review)

    engines_parser = subparsers.add_parser("engines", help="list or install engines")
    engines_parser.add_argument("--install", metavar="ID",
                                help="install an engine by id, e.g. stockfish:sf_18")
    engines_parser.add_argument("--offline", action="store_true",
                                help="do not ask GitHub what is available")
    engines_parser.set_defaults(func=cmd_engines)

    import_parser = subparsers.add_parser("import", help="add a game to the library")
    import_parser.add_argument("reference")
    import_parser.set_defaults(func=cmd_import)

    library_parser = subparsers.add_parser("library", help="list saved games")
    library_parser.set_defaults(func=cmd_library)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["serve"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
