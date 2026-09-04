"""The HTTP layer: everything the browser interface talks to.

Design notes worth knowing before changing anything here:

*Disk is the state.*  There is no in-memory session holding your repertoire.
Every mutation loads the chapter, changes it, writes the PGN back, and
returns the fresh tree.  Repertoires get edited over weeks, and a server
restart in the middle of that must cost nothing.

*The token never touches disk.*  A token pasted into the UI lives in this
process and dies with it.  If you want it remembered, put it in
``~/.lichess_token`` or ``LICHESS_TOKEN`` yourself -- the app will find it,
but it will not decide on your behalf to write your credentials down.
"""

from __future__ import annotations

import base64
import hmac
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    analysis,
    drill,
    editing,
    engine,
    explorer as explorer_scan,
    export,
    storage,
    sync,
    universal,
)
from .board import board_svg, legal_moves, margin_fraction, parse_shapes
from .bridge import FeatureUnavailable, status as bridge_status
from .gitsync import GitSettings, GitSync
from .lichess import LichessClient, LichessError, SCOPE_URL, TokenMissing, resolve_token
from .model import (
    NAG_CHOICES,
    PathError,
    format_path,
    parse_path,
    tree_json,
)
from .storage import (
    Repertoire,
    StorageError,
    default_data_dir,
    list_repertoires,
    load_settings,
    open_repertoire,
    save_settings,
)

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Repertoire Creator")

#: A shared password gate for hosted deployments. Unset by default, so local
#: use (``repertoire serve``) is never asked for credentials -- this only
#: activates when both env vars are set, which a public deployment should do.
_AUTH_USER = os.environ.get("REPERTOIRE_AUTH_USER")
_AUTH_PASS = os.environ.get("REPERTOIRE_AUTH_PASS")


@app.middleware("http")
async def _require_password(request: Request, call_next):
    if not _AUTH_USER or not _AUTH_PASS:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            user, password = "", ""
        # compare_digest against both fields so a wrong username does not
        # short-circuit before the password comparison (timing side channel).
        if hmac.compare_digest(user, _AUTH_USER) and hmac.compare_digest(
            password, _AUTH_PASS
        ):
            return await call_next(request)

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Repertoire Creator"'},
    )


#: Set by the CLI before uvicorn starts; the default keeps direct
#: ``uvicorn repertoire_creator.server:app`` working.
DATA_DIR = default_data_dir()

#: A token pasted in the browser, for this process only. See the docstring.
_SESSION_TOKEN: str | None = None
_TOKEN_LOCK = threading.Lock()

#: Git auto-commit. Created with the data dir, because it needs to know which
#: folder it is allowed to touch.
GIT: GitSync | None = None


def set_data_dir(path) -> None:
    global DATA_DIR, GIT
    DATA_DIR = Path(path).expanduser().resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    settings = load_settings(DATA_DIR)
    GIT = GitSync(DATA_DIR, GitSettings.from_json(settings.get("git")))
    storage.set_write_observer(_on_write)


def _on_write(path, label: str) -> None:
    """Every write anywhere under the data folder lands here.

    Two things follow from a save and neither should be the caller's job to
    remember: the git commit timer is armed, and the universal book -- which
    is derived from all of these files -- is thrown away so the next lookup
    rebuilds it.
    """
    invalidate_book()
    if GIT is not None:
        GIT.note(label)


def git() -> GitSync:
    if GIT is None:
        set_data_dir(DATA_DIR)
    return GIT


def _token() -> str | None:
    with _TOKEN_LOCK:
        if _SESSION_TOKEN:
            return _SESSION_TOKEN
    return resolve_token()


def client() -> LichessClient:
    return LichessClient(_token())


# ------------------------------------------------------------------ helpers


def _repertoire(slug: str) -> Repertoire:
    try:
        return open_repertoire(DATA_DIR, slug)
    except StorageError as exc:
        raise HTTPException(404, str(exc)) from exc


def _chapters(repertoire: Repertoire, chapter_ids=None) -> list:
    wanted = list(chapter_ids) if chapter_ids else [
        c.id for c in repertoire.meta.chapters
    ]
    out = []
    for chapter_id in wanted:
        meta = repertoire.meta.chapter(chapter_id)
        if meta is None:
            continue
        out.append((meta, repertoire.game(chapter_id)))
    return out


def _chapter_summary(repertoire: Repertoire, meta) -> dict:
    game = repertoire.game(meta.id)
    stats = analysis.chapter_stats(game, repertoire.meta.color)
    gaps = analysis.chapter_gaps(game, repertoire.meta.color)
    return {
        "id": meta.id,
        "name": meta.name,
        "orientation": meta.orientation,
        "file": meta.file,
        "lichessChapterId": meta.lichess_chapter_id,
        "pushedAt": meta.pushed_at,
        "dirty": repertoire.is_dirty(meta.id),
        "stats": stats,
        "gaps": sum(1 for g in gaps if g["kind"] == "missing"),
        "undecided": sum(1 for g in gaps if g["kind"] == "undecided"),
    }


def _repertoire_json(repertoire: Repertoire, *, full: bool = True) -> dict:
    payload = {
        "slug": repertoire.meta.slug,
        "name": repertoire.meta.name,
        "color": repertoire.meta.color,
        "description": repertoire.meta.description,
        "updated": repertoire.meta.updated,
        "lichess": {
            "studyId": repertoire.meta.lichess_study_id,
            "url": repertoire.meta.lichess_url,
            "visibility": repertoire.meta.lichess_visibility,
        },
        "chapterCount": len(repertoire.meta.chapters),
    }
    if full:
        payload["chapters"] = [
            _chapter_summary(repertoire, meta) for meta in repertoire.meta.chapters
        ]
        payload["dirty"] = any(c["dirty"] for c in payload["chapters"])
    return payload


def _chapter_payload(repertoire: Repertoire, chapter_id: str,
                     focus: str | None = None) -> dict:
    meta = repertoire.meta.chapter(chapter_id)
    if meta is None:
        raise HTTPException(404, f"No chapter {chapter_id}")
    game = repertoire.game(chapter_id)
    color = repertoire.meta.color
    return {
        "chapter": _chapter_summary(repertoire, meta),
        "tree": tree_json(game, color),
        "gaps": analysis.chapter_gaps(game, color),
        "focus": focus,
        "color": color,
    }


def _guard(func):
    """Turn the app's own exceptions into tidy HTTP errors."""
    try:
        return func()
    except HTTPException:
        raise
    except PathError as exc:
        raise HTTPException(400, str(exc)) from exc
    except editing.EditError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(400, str(exc)) from exc
    except TokenMissing as exc:
        raise HTTPException(401, str(exc)) from exc
    except LichessError as exc:
        raise HTTPException(502, str(exc)) from exc
    except FeatureUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc


# ------------------------------------------------------------------- models


class NewRepertoire(BaseModel):
    name: str
    color: str = "white"
    description: str = ""


class PatchRepertoire(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None
    visibility: str | None = None


class NewChapter(BaseModel):
    name: str
    orientation: str | None = None
    startFen: str | None = None
    pgn: str | None = None


class PatchChapter(BaseModel):
    name: str | None = None
    orientation: str | None = None
    offset: int | None = None


class PlayBody(BaseModel):
    path: str = ""
    uci: str | None = None
    san: str | None = None


class LineBody(BaseModel):
    path: str = ""
    text: str


class NodeBody(BaseModel):
    path: str


class PromoteBody(BaseModel):
    path: str
    toMain: bool = False


class CommentBody(BaseModel):
    path: str
    text: str = ""


class ShapesBody(BaseModel):
    path: str
    circles: list = []
    arrows: list = []


class NagBody(BaseModel):
    path: str
    nag: int


class EvalBody(BaseModel):
    fen: str
    movetime: float = 0.2
    depth: int | None = None
    useCloud: bool = True


class LinesBody(BaseModel):
    fen: str
    count: int = 2
    movetime: float = 0.35
    depth: int | None = None


class GitSettingsBody(BaseModel):
    enabled: bool | None = None
    push: bool | None = None
    remote: str | None = None
    branch: str | None = None
    debounceSeconds: float | None = None


class NewRecording(BaseModel):
    name: str
    pgn: str | None = None
    startFen: str | None = None


class PatchRecording(BaseModel):
    name: str


class LookupBody(BaseModel):
    fen: str
    previousFen: str | None = None


class UniversalExportBody(BaseModel):
    name: str = "Universal book"
    visibility: str = "unlisted"


class BakeBody(BaseModel):
    chapterIds: list | None = None
    movetime: float = 0.15
    depth: int | None = None
    onlyMissing: bool = True


class TokenBody(BaseModel):
    token: str | None = None


class PushBody(BaseModel):
    chapterIds: list | None = None
    force: bool = False
    visibility: str | None = None


class ImportStudyBody(BaseModel):
    url: str
    color: str = "white"
    name: str | None = None


class ImportPgnBody(BaseModel):
    pgn: str
    name: str
    color: str = "white"


class ScanBody(BaseModel):
    chapterId: str | None = None
    minShare: float = explorer_scan.DEFAULT_MIN_SHARE
    minGames: int = explorer_scan.DEFAULT_MIN_GAMES
    maxPositions: int = explorer_scan.DEFAULT_MAX_POSITIONS
    ratings: list | None = None
    speeds: list | None = None


class DrillSessionBody(BaseModel):
    chapterId: str | None = None
    limit: int = 20
    newLimit: int = 8


class DrillAnswerBody(BaseModel):
    key: str
    correct: bool
    alternative: bool = False
    hinted: bool = False


class ExportBody(BaseModel):
    mode: str = "grid"
    chapterIds: list | None = None
    includeNotation: bool = True
    includeSteps: bool = True
    showEvals: bool = True
    boardSize: float = 424.0
    landscapePages: bool = True
    maxDepth: int | None = None
    diagrams: str | None = None


# -------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    bridge = bridge_status()
    token = _token()
    return {
        "ok": True,
        "dataDir": str(DATA_DIR),
        "sibling": bridge["sibling"],
        "stockfish": bridge["stockfish"],
        "latex": bridge["latex"],
        "hasToken": bool(token),
        "scopeUrl": SCOPE_URL,
        "nags": [{"code": code, "symbol": symbol} for code, symbol in NAG_CHOICES],
        "pdfModes": export.MODES,
        #: The coordinate margin python-chess draws inside the board SVG, as a
        #: fraction of one side. The click overlay has to be inset by exactly
        #: this much or every square is off. See board.margin_fraction.
        "boardMargin": margin_fraction(True),
        "maxLines": engine.MAX_LINES,
        "git": git().status(),
    }


@app.on_event("shutdown")
def _shutdown() -> None:
    engine.close_provider()


# ------------------------------------------------------------- repertoires


@app.get("/api/repertoires")
def get_repertoires() -> dict:
    items = [_repertoire_json(r, full=False) for r in list_repertoires(DATA_DIR)]
    return {"repertoires": items, "dataDir": str(DATA_DIR)}


@app.post("/api/repertoires")
def post_repertoire(body: NewRepertoire) -> dict:
    def run():
        repertoire = Repertoire.create(
            DATA_DIR, body.name, color=body.color, description=body.description
        )
        # A repertoire with no chapters cannot be edited or pushed, and the
        # first thing anyone does is make one, so make it for them.
        repertoire.add_chapter("Main line")
        return _repertoire_json(repertoire)

    return _guard(run)


@app.get("/api/repertoires/{slug}")
def get_repertoire(slug: str) -> dict:
    return _guard(lambda: _repertoire_json(_repertoire(slug)))


@app.patch("/api/repertoires/{slug}")
def patch_repertoire(slug: str, body: PatchRepertoire) -> dict:
    def run():
        repertoire = _repertoire(slug)
        if body.name:
            repertoire.meta.name = body.name.strip()
        if body.description is not None:
            repertoire.meta.description = body.description.strip()
        if body.color in ("white", "black"):
            repertoire.meta.color = body.color
        if body.visibility in ("public", "unlisted", "private"):
            repertoire.meta.lichess_visibility = body.visibility
        repertoire.save_manifest()
        return _repertoire_json(repertoire)

    return _guard(run)


@app.delete("/api/repertoires/{slug}")
def delete_repertoire(slug: str) -> dict:
    def run():
        _repertoire(slug).delete()
        return {"deleted": slug}

    return _guard(run)


@app.get("/api/repertoires/{slug}/pgn")
def get_repertoire_pgn(slug: str) -> Response:
    def run():
        repertoire = _repertoire(slug)
        return Response(
            content=repertoire.study_pgn(),
            media_type="application/x-chess-pgn",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{repertoire.meta.slug}.pgn"'
            },
        )

    return _guard(run)


@app.get("/api/repertoires/{slug}/report")
def get_report(slug: str) -> dict:
    def run():
        repertoire = _repertoire(slug)
        return analysis.repertoire_report(
            _chapters(repertoire), repertoire.meta.color
        )

    return _guard(run)


# ---------------------------------------------------------------- chapters


@app.post("/api/repertoires/{slug}/chapters")
def post_chapter(slug: str, body: NewChapter) -> dict:
    def run():
        repertoire = _repertoire(slug)
        game = None
        if body.pgn:
            game = sync.pgn_to_game(body.pgn)
        meta = repertoire.add_chapter(
            body.name,
            orientation=body.orientation,
            game=game,
            start_fen=body.startFen,
        )
        return {"chapterId": meta.id, "repertoire": _repertoire_json(repertoire)}

    return _guard(run)


@app.get("/api/repertoires/{slug}/chapters/{chapter_id}")
def get_chapter(slug: str, chapter_id: str) -> dict:
    return _guard(lambda: _chapter_payload(_repertoire(slug), chapter_id))


@app.patch("/api/repertoires/{slug}/chapters/{chapter_id}")
def patch_chapter(slug: str, chapter_id: str, body: PatchChapter) -> dict:
    def run():
        repertoire = _repertoire(slug)
        if body.name:
            repertoire.rename_chapter(chapter_id, body.name)
        if body.orientation:
            repertoire.set_orientation(chapter_id, body.orientation)
        if body.offset:
            repertoire.move_chapter(chapter_id, body.offset)
        return _repertoire_json(repertoire)

    return _guard(run)


@app.delete("/api/repertoires/{slug}/chapters/{chapter_id}")
def delete_chapter(slug: str, chapter_id: str) -> dict:
    def run():
        repertoire = _repertoire(slug)
        repertoire.delete_chapter(chapter_id)
        return _repertoire_json(repertoire)

    return _guard(run)


@app.get("/api/repertoires/{slug}/chapters/{chapter_id}/pgn")
def get_chapter_pgn(slug: str, chapter_id: str) -> Response:
    def run():
        repertoire = _repertoire(slug)
        return Response(
            content=repertoire.study_pgn([chapter_id]),
            media_type="application/x-chess-pgn",
        )

    return _guard(run)


# ------------------------------------------------------------------ editing


def _mutate(load, save, payload, mutate) -> dict:
    """Load, mutate, save, and hand back the whole tree again.

    Returning the full tree rather than a patch keeps the client from having
    to model tree surgery: after a promote or a delete every path below the
    edit can have shifted.  The same three closures serve repertoire chapters
    and universal recordings, which are the same kind of object wearing
    different labels.
    """
    def run():
        game = load()
        result = mutate(game) or {}
        save(game)
        out = payload(result.get("path"))
        out["result"] = result
        return out

    return _guard(run)


def _edit(slug: str, chapter_id: str, mutate) -> dict:
    repertoire = _repertoire(slug)
    return _mutate(
        lambda: repertoire.game(chapter_id),
        lambda game: repertoire.save_chapter(chapter_id, game),
        lambda focus: _chapter_payload(repertoire, chapter_id, focus=focus),
        mutate,
    )


@app.post("/api/repertoires/{slug}/chapters/{chapter_id}/play")
def post_play(slug: str, chapter_id: str, body: PlayBody) -> dict:
    def mutate(game):
        path, created = editing.play_move(
            game, parse_path(body.path), uci=body.uci, san=body.san
        )
        return {"path": format_path(path), "created": created}

    return _edit(slug, chapter_id, mutate)


@app.post("/api/repertoires/{slug}/chapters/{chapter_id}/line")
def post_line(slug: str, chapter_id: str, body: LineBody) -> dict:
    return _edit(
        slug, chapter_id,
        lambda game: editing.add_line(game, parse_path(body.path), body.text),
    )


@app.post("/api/repertoires/{slug}/chapters/{chapter_id}/delete-node")
def post_delete_node(slug: str, chapter_id: str, body: NodeBody) -> dict:
    return _edit(
        slug, chapter_id,
        lambda game: {"path": editing.delete_node(game, parse_path(body.path))},
    )


@app.post("/api/repertoires/{slug}/chapters/{chapter_id}/promote")
def post_promote(slug: str, chapter_id: str, body: PromoteBody) -> dict:
    return _edit(
        slug, chapter_id,
        lambda game: {
            "path": editing.promote(game, parse_path(body.path), to_main=body.toMain)
        },
    )


@app.post("/api/repertoires/{slug}/chapters/{chapter_id}/comment")
def post_comment(slug: str, chapter_id: str, body: CommentBody) -> dict:
    def mutate(game):
        editing.set_comment(game, parse_path(body.path), body.text)
        return {"path": body.path}

    return _edit(slug, chapter_id, mutate)


@app.post("/api/repertoires/{slug}/chapters/{chapter_id}/shapes")
def post_shapes(slug: str, chapter_id: str, body: ShapesBody) -> dict:
    def mutate(game):
        editing.set_shapes(
            game, parse_path(body.path),
            [tuple(c) for c in body.circles],
            [tuple(a) for a in body.arrows],
        )
        return {"path": body.path}

    return _edit(slug, chapter_id, mutate)


@app.post("/api/repertoires/{slug}/chapters/{chapter_id}/nag")
def post_nag(slug: str, chapter_id: str, body: NagBody) -> dict:
    def mutate(game):
        editing.toggle_nag(game, parse_path(body.path), body.nag)
        return {"path": body.path}

    return _edit(slug, chapter_id, mutate)


# -------------------------------------------------------------------- board


@app.get("/api/board")
def get_board(fen: str, flip: int = 0, lastmove: str = "", circles: str = "",
              arrows: str = "", size: int = 400, coords: int = 1) -> Response:
    parsed_circles, parsed_arrows = parse_shapes(circles, arrows)
    try:
        svg = board_svg(
            fen, size=size, flipped=bool(flip), last_move=lastmove,
            circles=parsed_circles, arrows=parsed_arrows,
            coordinates=bool(coords),
        )
    except ValueError as exc:
        raise HTTPException(400, f"Bad FEN: {exc}") from exc
    return Response(
        content=svg, media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/legal")
def get_legal(fen: str, square: str = "") -> dict:
    try:
        return legal_moves(fen, square)
    except ValueError as exc:
        raise HTTPException(400, f"Bad FEN: {exc}") from exc


# --------------------------------------------------------------- evaluation


@app.post("/api/eval")
def post_eval(body: EvalBody) -> dict:
    def run():
        value = engine.evaluate_one(
            body.fen, movetime=body.movetime, depth=body.depth,
            use_cloud=body.useCloud,
        )
        return {"eval": value}

    return _guard(run)


@app.post("/api/lines")
def post_lines(body: LinesBody) -> dict:
    """The engine's best few moves here, for the suggestion panel."""
    def run():
        return engine.top_lines(
            body.fen, count=body.count, movetime=body.movetime, depth=body.depth
        )

    return _guard(run)


@app.post("/api/repertoires/{slug}/bake")
def post_bake(slug: str, body: BakeBody) -> dict:
    def run():
        repertoire = _repertoire(slug)
        wanted = body.chapterIds or [c.id for c in repertoire.meta.chapters]
        totals = {"evaluated": 0, "missing": 0, "chapters": 0}
        for chapter_id in wanted:
            game = repertoire.game(chapter_id)
            result = engine.bake_chapter(
                game, movetime=body.movetime, depth=body.depth,
                only_missing=body.onlyMissing,
            )
            if result["evaluated"]:
                repertoire.save_chapter(chapter_id, game)
            totals["evaluated"] += result["evaluated"]
            totals["missing"] += result["missing"]
            totals["chapters"] += 1
        totals["repertoire"] = _repertoire_json(repertoire)
        return totals

    return _guard(run)


# ----------------------------------------------------------------- explorer


@app.post("/api/repertoires/{slug}/scan")
def post_scan(slug: str, body: ScanBody) -> dict:
    def run():
        repertoire = _repertoire(slug)
        api = client()
        wanted = [body.chapterId] if body.chapterId else [
            c.id for c in repertoire.meta.chapters
        ]
        kwargs = {
            "min_share": body.minShare,
            "min_games": body.minGames,
            "max_positions": body.maxPositions,
        }
        if body.ratings:
            kwargs["ratings"] = tuple(body.ratings)
        if body.speeds:
            kwargs["speeds"] = tuple(body.speeds)

        findings, checked, limited = [], 0, False
        for chapter_id in wanted:
            meta = repertoire.meta.chapter(chapter_id)
            if meta is None:
                continue
            result = explorer_scan.scan_chapter(
                repertoire.game(chapter_id), repertoire.meta.color, api, **kwargs
            )
            for item in result["findings"]:
                item["chapterId"] = chapter_id
                item["chapterName"] = meta.name
            findings.extend(result["findings"])
            checked += result["checked"]
            limited = limited or result["rateLimited"]
            if limited:
                break

        findings.sort(key=lambda f: -max(m["share"] for m in f["missing"]))
        return {"findings": findings, "checked": checked, "rateLimited": limited}

    return _guard(run)


# -------------------------------------------------------------------- drill


@app.get("/api/repertoires/{slug}/drill")
def get_drill(slug: str) -> dict:
    def run():
        repertoire = _repertoire(slug)
        cards = drill.collect_cards(_chapters(repertoire), repertoire.meta.color)
        return drill.summarise(cards, repertoire.load_drill())

    return _guard(run)


@app.post("/api/repertoires/{slug}/drill/session")
def post_drill_session(slug: str, body: DrillSessionBody) -> dict:
    def run():
        repertoire = _repertoire(slug)
        cards = drill.collect_cards(_chapters(repertoire), repertoire.meta.color)
        state = repertoire.load_drill()
        session = drill.build_session(
            cards, state, limit=body.limit, new_limit=body.newLimit,
            chapter_id=body.chapterId,
        )
        return {
            "cards": session,
            "summary": drill.summarise(cards, state),
            "color": repertoire.meta.color,
        }

    return _guard(run)


@app.post("/api/repertoires/{slug}/drill/answer")
def post_drill_answer(slug: str, body: DrillAnswerBody) -> dict:
    def run():
        repertoire = _repertoire(slug)
        state = repertoire.load_drill()
        quality = drill.quality_for(
            body.correct, alternative=body.alternative, hinted=body.hinted
        )
        entry = drill.grade(state, body.key, quality)
        repertoire.save_drill(state)
        return {"entry": entry, "quality": quality}

    return _guard(run)


# ------------------------------------------------------------------ lichess


@app.post("/api/lichess/token")
def post_token(body: TokenBody) -> dict:
    def run():
        global _SESSION_TOKEN
        with _TOKEN_LOCK:
            _SESSION_TOKEN = (body.token or "").strip() or None
        api = client()
        if api.token is None:
            return {"hasToken": False}
        info = api.token_info()
        return {"hasToken": True, **info}

    return _guard(run)


@app.get("/api/lichess/token")
def get_token() -> dict:
    def run():
        api = client()
        if api.token is None:
            return {"hasToken": False, "scopeUrl": SCOPE_URL}
        return {"hasToken": True, "scopeUrl": SCOPE_URL, **api.token_info()}

    return _guard(run)


@app.post("/api/repertoires/{slug}/push")
def post_push(slug: str, body: PushBody) -> dict:
    def run():
        repertoire = _repertoire(slug)
        report = sync.push(
            repertoire, client(),
            chapter_ids=body.chapterIds, force=body.force,
            visibility=body.visibility,
        )
        report["repertoire"] = _repertoire_json(repertoire)
        return report

    return _guard(run)


@app.post("/api/import/study")
def post_import_study(body: ImportStudyBody) -> dict:
    def run():
        text = (body.url or "").strip().rstrip("/")
        parts = [p for p in text.split("/") if p]
        study_id = parts[-1] if parts else ""
        # A chapter URL ends in the chapter id, and the study id is before it.
        if len(parts) >= 2 and len(parts[-2]) == 8 and parts[-3:-2] == ["study"]:
            study_id = parts[-2]
        study_id = study_id.replace(".pgn", "")
        if len(study_id) != 8:
            raise StorageError(
                f"{body.url!r} does not contain a study id. Paste a URL like "
                "https://lichess.org/study/abcd1234"
            )
        repertoire = sync.import_study(
            DATA_DIR, client(), study_id, color=body.color, name=body.name
        )
        return _repertoire_json(repertoire)

    return _guard(run)


@app.post("/api/import/pgn")
def post_import_pgn(body: ImportPgnBody) -> dict:
    def run():
        repertoire = sync.import_pgn_text(
            DATA_DIR, body.pgn, name=body.name, color=body.color
        )
        return _repertoire_json(repertoire)

    return _guard(run)


# ------------------------------------------------------------------- export


@app.post("/api/repertoires/{slug}/export")
def post_export(slug: str, body: ExportBody) -> FileResponse:
    def run():
        repertoire = _repertoire(slug)
        path = export.build(
            repertoire,
            mode=body.mode,
            chapter_ids=body.chapterIds,
            include_notation=body.includeNotation,
            include_steps=body.includeSteps,
            show_evals=body.showEvals,
            board_size=body.boardSize,
            landscape_pages=body.landscapePages,
            max_depth=body.maxDepth,
            diagrams=body.diagrams,
        )
        safe = "".join(
            ch for ch in repertoire.meta.name if ch.isalnum() or ch in " -_"
        ).strip()
        return FileResponse(
            path, media_type="application/pdf",
            filename=f"{safe or 'repertoire'}.pdf",
        )

    return _guard(run)


# ---------------------------------------------------------------- settings


@app.get("/api/git")
def get_git() -> dict:
    return git().status()


@app.post("/api/git")
def post_git(body: GitSettingsBody) -> dict:
    """Change the auto-commit settings and remember them on disk."""
    def run():
        sync_git = git()
        settings = sync_git.settings
        if body.enabled is not None:
            settings.enabled = body.enabled
        if body.push is not None:
            settings.push = body.push
        if body.remote:
            settings.remote = body.remote
        if body.branch is not None:
            settings.branch = body.branch or None
        if body.debounceSeconds is not None:
            settings.debounce_seconds = max(1.0, float(body.debounceSeconds))

        stored = load_settings(DATA_DIR)
        stored["git"] = settings.to_json()
        save_settings(DATA_DIR, stored)
        return sync_git.status()

    return _guard(run)


@app.post("/api/git/commit")
def post_git_commit() -> dict:
    """Commit and push right now instead of waiting for the debounce."""
    return _guard(lambda: git().flush())


# --------------------------------------------------------------- universal


#: Rebuilding the book walks every recording and every chapter. That is fast,
#: but the assist asks for it on every move, so it is cached and thrown away
#: whenever anything under the data folder is written.
_BOOK: dict | None = None
_BOOK_LOCK = threading.Lock()


def invalidate_book() -> None:
    global _BOOK
    with _BOOK_LOCK:
        _BOOK = None


def book() -> dict:
    global _BOOK
    with _BOOK_LOCK:
        if _BOOK is None:
            _BOOK = universal.build_book(DATA_DIR, include_chapters=True)
        return _BOOK


def _store() -> universal.UniversalStore:
    return universal.UniversalStore(DATA_DIR)


def _recording_summary(store: universal.UniversalStore, recording) -> dict:
    try:
        game = store.game(recording.id)
        moves = sum(1 for _ in game.mainline_moves())
        total = len(tree_json(game, None)["nodes"]) - 1
    except (LookupError, ValueError):
        moves, total = 0, 0
    return {
        "id": recording.id,
        "name": recording.name,
        "file": recording.file,
        "updated": recording.updated,
        "mainlineMoves": moves,
        "moves": total,
    }


def _recording_payload(store: universal.UniversalStore, recording_id: str,
                       focus: str | None = None) -> dict:
    recording = store.recording(recording_id)
    if recording is None:
        raise HTTPException(404, f"No recording {recording_id}")
    game = store.game(recording_id)
    return {
        "recording": _recording_summary(store, recording),
        "tree": tree_json(game, None),
        "gaps": universal.gaps_in_recording(game, book()),
        "focus": focus,
        "color": None,
    }


@app.get("/api/universal")
def get_universal() -> dict:
    def run():
        store = _store()
        return {
            "recordings": [_recording_summary(store, r) for r in store.recordings],
            "book": universal.book_stats(book()),
        }

    return _guard(run)


@app.post("/api/universal/recordings")
def post_recording(body: NewRecording) -> dict:
    def run():
        store = _store()
        game = sync.pgn_to_game(body.pgn) if body.pgn else None
        recording = store.add(body.name, game=game, start_fen=body.startFen)
        invalidate_book()
        return {"recordingId": recording.id,
                "recordings": [_recording_summary(store, r) for r in store.recordings]}

    return _guard(run)


@app.get("/api/universal/recordings/{recording_id}")
def get_recording(recording_id: str) -> dict:
    return _guard(lambda: _recording_payload(_store(), recording_id))


@app.patch("/api/universal/recordings/{recording_id}")
def patch_recording(recording_id: str, body: PatchRecording) -> dict:
    def run():
        store = _store()
        store.rename(recording_id, body.name)
        return {"recordings": [_recording_summary(store, r) for r in store.recordings]}

    return _guard(run)


@app.delete("/api/universal/recordings/{recording_id}")
def delete_recording(recording_id: str) -> dict:
    def run():
        store = _store()
        store.delete(recording_id)
        invalidate_book()
        return {"recordings": [_recording_summary(store, r) for r in store.recordings]}

    return _guard(run)


def _edit_recording(recording_id: str, mutate) -> dict:
    store = _store()

    def save(game):
        store.save(recording_id, game)
        invalidate_book()

    return _mutate(
        lambda: store.game(recording_id),
        save,
        lambda focus: _recording_payload(store, recording_id, focus=focus),
        mutate,
    )


@app.post("/api/universal/recordings/{recording_id}/play")
def post_recording_play(recording_id: str, body: PlayBody) -> dict:
    def mutate(game):
        path, created = editing.play_move(
            game, parse_path(body.path), uci=body.uci, san=body.san
        )
        return {"path": format_path(path), "created": created}

    return _edit_recording(recording_id, mutate)


@app.post("/api/universal/recordings/{recording_id}/line")
def post_recording_line(recording_id: str, body: LineBody) -> dict:
    return _edit_recording(
        recording_id,
        lambda game: editing.add_line(game, parse_path(body.path), body.text),
    )


@app.post("/api/universal/recordings/{recording_id}/delete-node")
def post_recording_delete_node(recording_id: str, body: NodeBody) -> dict:
    return _edit_recording(
        recording_id,
        lambda game: {"path": editing.delete_node(game, parse_path(body.path))},
    )


@app.post("/api/universal/recordings/{recording_id}/promote")
def post_recording_promote(recording_id: str, body: PromoteBody) -> dict:
    return _edit_recording(
        recording_id,
        lambda game: {
            "path": editing.promote(game, parse_path(body.path), to_main=body.toMain)
        },
    )


@app.post("/api/universal/recordings/{recording_id}/comment")
def post_recording_comment(recording_id: str, body: CommentBody) -> dict:
    def mutate(game):
        editing.set_comment(game, parse_path(body.path), body.text)
        return {"path": body.path}

    return _edit_recording(recording_id, mutate)


@app.post("/api/universal/recordings/{recording_id}/nag")
def post_recording_nag(recording_id: str, body: NagBody) -> dict:
    def mutate(game):
        editing.toggle_nag(game, parse_path(body.path), body.nag)
        return {"path": body.path}

    return _edit_recording(recording_id, mutate)


@app.post("/api/universal/lookup")
def post_lookup(body: LookupBody) -> dict:
    """What does the book say here -- and is this a gap? Drives the assist."""
    def run():
        return universal.lookup(book(), body.fen, body.previousFen)

    return _guard(run)


@app.get("/api/universal/pgn")
def get_universal_pgn() -> Response:
    def run():
        return Response(
            content=_store().all_pgn(),
            media_type="application/x-chess-pgn",
            headers={"Content-Disposition": 'attachment; filename="universal.pgn"'},
        )

    return _guard(run)


@app.post("/api/universal/export")
def post_universal_export(body: UniversalExportBody) -> dict:
    """Publish the recordings as a study, one chapter per opening move."""
    def run():
        store = _store()
        try:
            groups = universal.export_games(store)
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
        if not groups:
            raise StorageError("There are no recordings to publish yet.")

        api = client()
        settings = load_settings(DATA_DIR)
        study_id = (settings.get("universal") or {}).get("studyId")
        created = False
        if not study_id:
            study_id = api.create_study(body.name, visibility=body.visibility)
            created = True
            settings.setdefault("universal", {})["studyId"] = study_id
            save_settings(DATA_DIR, settings)

        placeholder = set()
        if created:
            try:
                placeholder = {c["id"] for c in api.study_chapters(study_id)}
            except LichessError:
                placeholder = set()

        results, made = [], set()
        from .model import game_to_pgn as to_pgn

        # Remember which Lichess chapter each opening move landed in, so a
        # second export updates those chapters rather than stacking a second
        # copy of the book beside the first.
        known = dict((settings.get("universal") or {}).get("chapters") or {})

        for name, game in groups:
            game.headers["Event"] = f"{body.name}: {name}"
            game.headers["StudyName"] = body.name
            game.headers["ChapterName"] = name
            game.headers.setdefault("Result", "*")
            existing = known.get(name)
            try:
                if existing:
                    api.update_moves(study_id, existing, to_pgn(game, headers=False))
                    made.add(existing)
                    results.append({"name": name, "action": "updated", "detail": ""})
                else:
                    chapters = api.import_pgn(study_id, to_pgn(game), name=name)
                    if not chapters:
                        raise LichessError("Lichess created nothing for that chapter.")
                    known[name] = chapters[0].get("id")
                    made.add(known[name])
                    results.append({"name": name, "action": "created", "detail": ""})
            except LichessError as exc:
                results.append({"name": name, "action": "failed", "detail": str(exc)})

        settings.setdefault("universal", {})["chapters"] = known
        save_settings(DATA_DIR, settings)

        for empty in placeholder - made:
            try:
                api.delete_chapter(study_id, empty)
            except LichessError:
                pass

        return {
            "studyId": study_id,
            "studyUrl": f"https://lichess.org/study/{study_id}",
            "studyCreated": created,
            "chapters": results,
            "created": sum(1 for r in results if r["action"] == "created"),
            "failed": sum(1 for r in results if r["action"] == "failed"),
        }

    return _guard(run)


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
