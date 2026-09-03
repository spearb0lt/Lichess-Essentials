"""Command line entry point."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from reportlab.lib.pagesizes import A4, A3, LETTER, landscape, portrait

from .evals import EvalProvider, find_stockfish
from .fetch import (
    StudyFetchError,
    fetch_study_pgn,
    parse_study_url,
    resolve_token,
)
from .parse import parse_study
from .pdf import PdfOptions, build_pdf

PAGE_SIZES = {"a4": A4, "a3": A3, "letter": LETTER}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\s.-]", "", name).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return (cleaned or "lichess-study")[:80]


def _parse_chapters(spec: str | None, total: int):
    """Turn ``1,3,5-8`` (1-based, as shown in the contents) into 0-based indices."""
    if not spec:
        return None
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                for value in range(int(start), int(end) + 1):
                    wanted.add(value - 1)
            except ValueError as exc:
                raise SystemExit(f"Bad chapter range: {part!r}") from exc
        else:
            try:
                wanted.add(int(part) - 1)
            except ValueError as exc:
                raise SystemExit(f"Bad chapter number: {part!r}") from exc
    valid = {i for i in wanted if 0 <= i < total}
    if not valid:
        raise SystemExit(f"No chapters selected from {spec!r} (study has {total}).")
    return tuple(sorted(valid))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lichess-study-pdf",
        description="Turn a Lichess study into a steppable PDF.",
    )
    sub = parser.add_subparsers(dest="command")

    export = sub.add_parser("export", help="export a study to PDF (default)")
    export.add_argument("url", nargs="?", help="study URL or id")
    export.add_argument("-o", "--output", help="output PDF path")
    export.add_argument("--token", help="Lichess API token (study:read scope)")
    export.add_argument("--pgn", help="read PGN from a file instead of the API")
    export.add_argument("--save-pgn", help="also write the downloaded PGN here")

    export.add_argument(
        "--mode", choices=("grid", "book", "slideshow", "acrobat"),
        default="grid",
        help="grid = twelve small boards per page (default); "
             "book = LaTeX-typeset chess book, needs pdflatex; "
             "slideshow = one big board per page, steps with the arrow keys; "
             "acrobat = layered single page, ADOBE ACROBAT READER ONLY",
    )
    export.add_argument("--grid-columns", type=int, default=4,
                        help="boards across the page in grid mode")
    export.add_argument("--grid-rows", type=int, default=3,
                        help="boards down the page in grid mode")
    export.add_argument("--chapter-only", action="store_true",
                        help="with a chapter URL, export just that one chapter")
    export.add_argument("--latex", help="path to pdflatex for --mode book")
    export.add_argument("--keep-tex", help="also write the generated .tex here")
    export.add_argument("--no-notation", action="store_true",
                        help="skip the full read-through notation section")
    export.add_argument("--no-steps", action="store_true",
                        help="skip the diagram pages (grid or stepping)")
    export.add_argument("--chapters", help="subset, e.g. 1,3,5-8 (1-based)")
    export.add_argument("--max-depth", type=int,
                        help="drop sidelines nested deeper than this")
    export.add_argument(
        "--diagrams", default=None,
        help="diagrams inside the notation section: none | comments | all | "
             "every:N. Default is automatic: none in grid mode (the grid "
             "already shows every position), every:6 otherwise",
    )
    export.add_argument("--board-size", type=float, default=424.0)
    export.add_argument("--page-size", choices=tuple(PAGE_SIZES), default="a4")
    export.add_argument("--portrait", action="store_true",
                        help="portrait pages (landscape is the default)")

    export.add_argument("--no-evals", action="store_true", help="skip evaluation bars")
    export.add_argument("--no-cloud", action="store_true",
                        help="do not use the Lichess cloud eval API")
    export.add_argument("--engine", help="path to a Stockfish binary")
    export.add_argument("--movetime", type=float, default=0.25,
                        help="seconds per position for the local engine")
    export.add_argument("--depth", type=int,
                        help="fixed engine depth instead of a time budget")
    export.add_argument("--eval-cache", help="path to the evaluation cache JSON")

    serve = sub.add_parser("serve", help="run the web interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8777)
    serve.add_argument("--reload", action="store_true")

    sub.add_parser("engine-info", help="show which Stockfish binary would be used")

    return parser


def _load_pgn(args) -> tuple[str, str]:
    if args.pgn:
        path = Path(args.pgn)
        if not path.is_file():
            raise SystemExit(f"No such PGN file: {path}")
        return path.read_text(encoding="utf-8"), ""

    if not args.url:
        raise SystemExit("Give a study URL, or --pgn to read a local file.")

    ref = parse_study_url(args.url)
    token = resolve_token(args.token)
    print(f"Fetching {ref.url}" + ("  (with token)" if token else ""))

    def chapter_progress(done, total, name):
        """Private studies are rebuilt chapter by chapter; show that."""
        if done < total:
            label = (name or "")[:40]
            print("\r  private study: chapter %d/%d %s   " % (
                done + 1, total, label), end="", flush=True)
        else:
            print("\r  rebuilt from %d chapters via the per-chapter API%s" % (
                total, " " * 20))

    pgn = fetch_study_pgn(ref, token, chapter_only=args.chapter_only,
                          progress=chapter_progress)
    if args.save_pgn:
        Path(args.save_pgn).write_text(pgn, encoding="utf-8")
        print(f"  PGN saved to {args.save_pgn}")
    return pgn, ref.url


def command_export(args) -> int:
    try:
        pgn_text, source_url = _load_pgn(args)
    except StudyFetchError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    study = parse_study(pgn_text, source_url)
    chapter_filter = _parse_chapters(args.chapters, len(study.chapters))

    if args.mode != "book" and args.no_notation and args.no_steps:
        raise SystemExit("--no-notation and --no-steps together would produce "
                         "an empty document.")

    page = PAGE_SIZES[args.page_size]
    options = PdfOptions(
        mode=args.mode,
        include_notation=not args.no_notation,
        include_steps=not args.no_steps,
        show_evals=not args.no_evals,
        board_size=args.board_size,
        page_size=portrait(page) if args.portrait else landscape(page),
        diagrams=args.diagrams,
        max_depth=args.max_depth,
        chapter_filter=chapter_filter,
        grid_columns=max(1, args.grid_columns),
        grid_rows=max(1, args.grid_rows),
        latex_path=args.latex,
        keep_tex=args.keep_tex,
    )

    chapters = [c for c in study.chapters
                if chapter_filter is None or c.index in chapter_filter]
    positions = sum(len(c.steps) for c in chapters)
    print(f"Study: {study.name}")
    print(f"  {len(chapters)} chapters, {positions} positions, "
          f"{sum(c.variation_count for c in chapters)} sidelines")

    evals = {}
    if options.show_evals:
        cache_path = Path(args.eval_cache) if args.eval_cache else (
            Path.home() / ".cache" / "lichess-study-pdf" / "evals.json"
        )
        engine_path = find_stockfish(args.engine)
        if not engine_path and args.no_cloud:
            print("  No Stockfish found and cloud disabled - skipping evals.")
            options.show_evals = False
        else:
            if not engine_path:
                print("  No Stockfish binary found; cloud evals only "
                      "(positions Lichess has not cached will be blank).")
            fens = [s.fen for c in chapters for s in c.steps]
            started = time.perf_counter()

            def progress(done, total):
                pct = 100.0 * done / max(1, total)
                print(f"\r  Evaluating {done}/{total} ({pct:.0f}%)",
                      end="", flush=True)

            with EvalProvider(
                cache_path,
                use_cloud=not args.no_cloud,
                stockfish_path=engine_path,
                movetime=args.movetime,
                depth=args.depth,
            ) as provider:
                evals = provider.evaluate_many(fens, progress=progress)
                stats = dict(provider.stats)
                rate_limited = provider.cloud_rate_limited
            print(f"\r  Evaluated {len(fens)} positions in "
                  f"{time.perf_counter() - started:.1f}s "
                  f"(cache {stats['cache']}, cloud {stats['cloud']}, "
                  f"engine {stats['local']}, unknown {stats['missing']})")
            if rate_limited:
                print("  Lichess rate limited the cloud eval API, so the rest "
                      "of the run skipped it. Wait a few minutes before "
                      "retrying if you want cloud evals.")
            if stats["missing"] and not engine_path:
                print(f"  {stats['missing']} positions have no evaluation. "
                      "Install Stockfish for full coverage - run "
                      "'engine-info' for where to put it.")

    output = args.output or f"{_safe_filename(study.name)}.pdf"
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    build_pdf(study, output, evals=evals, options=options)
    size_kb = Path(output).stat().st_size / 1024

    print(f"Wrote {output}  ({size_kb:.0f} KB, "
          f"{time.perf_counter() - started:.1f}s)")
    if args.mode == "acrobat":
        print("")
        print("  !! ACROBAT MODE: this file only works in Adobe Acrobat Reader.")
        print("     Chrome, Edge, Firefox, Preview and phones will show only")
        print("     the FIRST position of each chapter and the buttons will do")
        print("     nothing. Use --mode slideshow or --mode book instead.")
    return 0


def _port_is_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) != 0


def command_serve(args) -> int:
    import uvicorn

    from .pdf_latex import find_latex

    if not _port_is_free(args.host, args.port):
        # Uvicorn's own message for this is buried under a stack trace, and an
        # already-running copy of this app is by far the likeliest cause.
        print(f"Port {args.port} is already in use.", file=sys.stderr)
        print(f"  Something is listening on http://{args.host}:{args.port} - "
              "most likely this app is already running.", file=sys.stderr)
        print(f"  Open it in your browser, or start on another port:",
              file=sys.stderr)
        print(f"      ... cli serve --port {args.port + 1}", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}"
    engine = find_stockfish()
    latex = find_latex()

    # flush=True: uvicorn logs to stderr, and without this the banner would
    # arrive after its startup noise.
    print(f"\n  Lichess Study to PDF is running.", flush=True)
    print(f"  Open {url} in your browser.", flush=True)
    print(f"    engine : {engine or 'not found - eval bars will be blank'}",
          flush=True)
    print(f"    LaTeX  : {latex or 'not found - book mode disabled'}",
          flush=True)
    print("  Press Ctrl+C to stop.\n", flush=True)

    uvicorn.run(
        "lichess_study_pdf.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def command_engine_info(_args) -> int:
    path = find_stockfish()
    if path:
        print(f"Stockfish: {path}")
    else:
        print("No Stockfish binary found.")
        print("Looked at: --engine, $STOCKFISH_PATH, ./engine/, and $PATH.")
        print("Download one from https://stockfishchess.org/download/ and put "
              "it in the engine/ folder, or set STOCKFISH_PATH.")
    return 0


def _make_console_forgiving() -> None:
    """Stop emoji in chapter names from killing the run.

    Windows consoles default to cp1252, and Lichess study titles are full of
    emoji, so an ordinary print() raises UnicodeEncodeError. Replacing the
    unencodable characters is far better than aborting an export over them.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None) -> int:
    _make_console_forgiving()
    parser = _build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Allow `lichess-study-pdf <url>` without naming the subcommand.
    if argv and argv[0] not in ("export", "serve", "engine-info",
                                "-h", "--help"):
        argv.insert(0, "export")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return command_serve(args)
    if args.command == "engine-info":
        return command_engine_info(args)
    if args.command == "export":
        return command_export(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
