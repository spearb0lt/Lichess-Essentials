"""Command line: run the app, or drive it from a script.

The browser interface is the main way to use this, but everything it does is
also a command, so a repertoire can be pushed or exported from a shell script
or a scheduled job without a browser in the loop.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import analysis, engine, export, sync
from .bridge import FeatureUnavailable, status as bridge_status
from .gitsync import GitSettings, GitSync
from .lichess import LichessClient, LichessError, SCOPE_URL
from .storage import (
    Repertoire,
    StorageError,
    default_data_dir,
    list_repertoires,
    load_settings,
    open_repertoire,
    set_write_observer,
)

BANNER = "Repertoire Creator"


def _say(text: str = "") -> None:
    print(text, flush=True)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr, flush=True)
    return 1


def _count(number: int, noun: str, plural: str | None = None) -> str:
    return f"{number} {noun if number == 1 else (plural or noun + 's')}"


#: Set up alongside the data folder so a command-line edit commits like a
#: browser one does. Without this, `repertoire bake` would quietly leave the
#: repertoire uncommitted while the same edit through the UI committed itself.
_GIT: GitSync | None = None


def _data_dir(args) -> Path:
    global _GIT
    path = Path(args.data).expanduser().resolve() if args.data else default_data_dir()
    path.mkdir(parents=True, exist_ok=True)

    if _GIT is None:
        settings = load_settings(path)
        _GIT = GitSync(path, GitSettings.from_json(settings.get("git")))
        set_write_observer(lambda _path, label: _GIT.note(label))
    return path


def _commit_pending() -> None:
    """Commit whatever this command changed, rather than on a timer."""
    if _GIT is None or not _GIT.settings.enabled or not _GIT.status()["pending"]:
        return
    result = _GIT.flush()
    if result.get("lastError"):
        print(f"git: {result['lastError']}", file=sys.stderr, flush=True)
    elif result.get("lastAction") in ("committed", "pushed"):
        _say(f"git: {result['lastAction']} {result['lastMessage']}")


def _open(args) -> Repertoire:
    return open_repertoire(_data_dir(args), args.slug)


def _chapters(repertoire: Repertoire) -> list:
    return [(meta, repertoire.game(meta.id)) for meta in repertoire.meta.chapters]


# ------------------------------------------------------------------ commands


def cmd_serve(args) -> int:
    import uvicorn

    from . import server

    data_dir = _data_dir(args)
    server.set_data_dir(data_dir)

    state = bridge_status()
    _say(f"{BANNER}")
    _say(f"  repertoires  {data_dir}")
    _say(f"  engine       {state['stockfish'] or 'not found - eval bar disabled'}")
    if not state["sibling"]:
        _say("  pdf export   unavailable (pip install -e Lichess-Study-to-PDF)")
    else:
        _say(f"  pdf export   ready{'' if state['latex'] else ' (book mode needs LaTeX)'}")
    _say(f"  open         http://{args.host}:{args.port}")
    _say()

    uvicorn.run(server.app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_new(args) -> int:
    repertoire = Repertoire.create(
        _data_dir(args), args.name, color=args.color, description=args.description or ""
    )
    repertoire.add_chapter(args.chapter or "Main line")
    _say(f"Created {repertoire.meta.slug} ({repertoire.meta.color}) at {repertoire.root}")
    return 0


def cmd_list(args) -> int:
    items = list_repertoires(_data_dir(args))
    if not items:
        _say(f"No repertoires yet in {_data_dir(args)}")
        return 0
    for repertoire in items:
        study = repertoire.meta.lichess_url or "not on Lichess"
        dirty = sum(1 for c in repertoire.meta.chapters if repertoire.is_dirty(c.id))
        count = _count(len(repertoire.meta.chapters), "chapter")
        _say(
            f"{repertoire.meta.slug:<28} {repertoire.meta.color:<5} "
            f"{count:<12} {dirty:>2} unpushed  {study}"
        )
    return 0


def cmd_add_chapter(args) -> int:
    repertoire = _open(args)
    meta = repertoire.add_chapter(args.name, orientation=args.orientation)
    _say(f"Added chapter {meta.name} ({meta.id})")
    return 0


def cmd_report(args) -> int:
    repertoire = _open(args)
    report = analysis.repertoire_report(_chapters(repertoire), repertoire.meta.color)
    totals = report["totals"]
    _say(f"{repertoire.meta.name} - playing {repertoire.meta.color}")
    _say(
        f"  {totals['moves']} moves  "
        f"{totals['myMoves']} yours / {totals['theirMoves']} theirs  "
        f"{totals['branches']} branches  "
        f"{totals['evaluated']} evaluated  depth {totals['maxPly']}"
    )

    missing = [g for g in report["gaps"] if g["kind"] == "missing"]
    undecided = [g for g in report["gaps"] if g["kind"] == "undecided"]
    conflicts = [t for t in report["transpositions"] if t["conflict"]]

    _say()
    _say(f"Gaps: {_count(len(missing), 'position')} with no reply of yours")
    for gap in missing[:15]:
        _say(f"  {gap['chapterName']}: {gap['line']}")
    if len(missing) > 15:
        _say(f"  ... and {len(missing) - 15} more")

    if undecided:
        _say()
        _say(f"Undecided: {_count(len(undecided), 'position')} "
             "where you kept several moves")
        for gap in undecided[:10]:
            _say(f"  {gap['chapterName']}: {gap['line']} -> {', '.join(gap['moves'])}")

    if conflicts:
        _say()
        _say(f"Transposition conflicts: {len(conflicts)}")
        for item in conflicts[:10]:
            routes = "; ".join(
                f"{o['chapterName']} {o['line']} -> {o['reply'] or 'nothing'}"
                for o in item["occurrences"]
            )
            _say(f"  {routes}")
    return 0


def cmd_bake(args) -> int:
    repertoire = _open(args)
    wanted = [args.chapter] if args.chapter else [c.id for c in repertoire.meta.chapters]
    total = 0
    for chapter_id in wanted:
        meta = repertoire.meta.chapter(chapter_id)
        if meta is None:
            return _fail(f"no chapter {chapter_id}")
        game = repertoire.game(chapter_id)

        def progress(done, count, name=meta.name):
            print(f"\r  {name}: {done}/{count}", end="", file=sys.stderr, flush=True)

        result = engine.bake_chapter(
            game, movetime=args.movetime, only_missing=not args.all,
            progress=progress,
        )
        print("", file=sys.stderr)
        if result["evaluated"]:
            repertoire.save_chapter(chapter_id, game)
        total += result["evaluated"]
        _say(f"{meta.name}: {result['evaluated']} evaluated, {result['missing']} unknown")
    _say(f"Wrote {total} evaluations into the PGN.")
    return 0


def cmd_push(args) -> int:
    repertoire = _open(args)
    client = LichessClient(args.token)

    def progress(done, count, name):
        if name:
            print(f"\r  {done + 1}/{count} {name}", end="", file=sys.stderr, flush=True)

    report = sync.push(
        repertoire, client, force=args.force, visibility=args.visibility,
        progress=progress,
    )
    print("", file=sys.stderr)

    for item in report["chapters"]:
        mark = {"created": "+", "updated": "~", "skipped": "=", "failed": "!"}[item["action"]]
        detail = f"  {item['detail']}" if item["detail"] else ""
        _say(f"  {mark} {item['name']}{detail}")
    _say(
        f"{report['created']} created, {report['updated']} updated, "
        f"{report['skipped']} unchanged, {report['failed']} failed"
    )
    _say(report["studyUrl"] or "")
    return 1 if report["failed"] else 0


def cmd_import(args) -> int:
    client = LichessClient(args.token)
    text = args.url.strip().rstrip("/")
    parts = [p for p in text.split("/") if p]
    study_id = parts[-1].replace(".pgn", "") if parts else ""
    if len(parts) >= 3 and parts[-3] == "study" and len(parts[-2]) == 8:
        study_id = parts[-2]
    if len(study_id) != 8:
        return _fail(f"{args.url!r} does not contain a study id")

    repertoire = sync.import_study(
        _data_dir(args), client, study_id, color=args.color, name=args.name
    )
    _say(
        f"Imported {len(repertoire.meta.chapters)} chapters into "
        f"{repertoire.meta.slug} at {repertoire.root}"
    )
    return 0


def cmd_pdf(args) -> int:
    repertoire = _open(args)
    out = Path(args.out).expanduser().resolve() if args.out else None
    path = export.build(
        repertoire, mode=args.mode, show_evals=not args.no_evals, out_path=out
    )
    _say(f"Wrote {path}")
    return 0


def cmd_token(args) -> int:
    client = LichessClient(args.token)
    if client.token is None:
        _say("No token found.")
        _say(f"Create one with study:read and study:write here:\n  {SCOPE_URL}")
        return 1
    info = client.token_info()
    _say(f"user    {info['userId']}")
    _say(f"scopes  {', '.join(info['scopes']) or 'none'}")
    _say(f"push    {'yes' if info['canWrite'] else 'no - needs study:write'}")
    return 0 if info["canWrite"] else 1


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repertoire", description=BANNER,
    )
    parser.add_argument("--data", help="repertoires folder (default: ./repertoires)")
    subs = parser.add_subparsers(dest="command", required=True)

    serve = subs.add_parser("serve", help="run the browser interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8778)
    serve.set_defaults(func=cmd_serve)

    new = subs.add_parser("new", help="create a repertoire")
    new.add_argument("name")
    new.add_argument("--color", choices=("white", "black"), default="white")
    new.add_argument("--chapter", help="name of the first chapter")
    new.add_argument("--description", default="")
    new.set_defaults(func=cmd_new)

    listing = subs.add_parser("list", help="list repertoires")
    listing.set_defaults(func=cmd_list)

    add = subs.add_parser("add-chapter", help="add a chapter")
    add.add_argument("slug")
    add.add_argument("name")
    add.add_argument("--orientation", choices=("white", "black"))
    add.set_defaults(func=cmd_add_chapter)

    report = subs.add_parser("report", help="gaps, conflicts and counts")
    report.add_argument("slug")
    report.set_defaults(func=cmd_report)

    bake = subs.add_parser("bake", help="write engine evaluations into the PGN")
    bake.add_argument("slug")
    bake.add_argument("--chapter", help="one chapter id (default: all)")
    bake.add_argument("--movetime", type=float, default=0.15)
    bake.add_argument("--all", action="store_true",
                      help="re-evaluate moves that already have an eval")
    bake.set_defaults(func=cmd_bake)

    push = subs.add_parser("push", help="publish to Lichess")
    push.add_argument("slug")
    push.add_argument("--token")
    push.add_argument("--force", action="store_true",
                      help="push chapters even if unchanged since the last push")
    push.add_argument("--visibility", choices=("public", "unlisted", "private"))
    push.set_defaults(func=cmd_push)

    imp = subs.add_parser("import", help="start a repertoire from a Lichess study")
    imp.add_argument("url")
    imp.add_argument("--color", choices=("white", "black"), default="white")
    imp.add_argument("--name")
    imp.add_argument("--token")
    imp.set_defaults(func=cmd_import)

    pdf = subs.add_parser("pdf", help="export a PDF")
    pdf.add_argument("slug")
    pdf.add_argument("--mode", choices=tuple(export.MODES), default="grid")
    pdf.add_argument("--out")
    pdf.add_argument("--no-evals", action="store_true")
    pdf.set_defaults(func=cmd_pdf)

    token = subs.add_parser("token", help="check the Lichess token")
    token.add_argument("--token")
    token.set_defaults(func=cmd_token)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (StorageError, LichessError, FeatureUnavailable, ValueError) as exc:
        return _fail(str(exc))
    finally:
        # `serve` runs its own commit timer for the life of the process; every
        # other command is short, so flush before the process goes away.
        if args.command != "serve":
            _commit_pending()


if __name__ == "__main__":
    raise SystemExit(main())
