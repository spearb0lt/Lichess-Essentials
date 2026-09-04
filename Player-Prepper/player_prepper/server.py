"""The HTTP layer: everything the browser interface talks to.

Design notes worth knowing before changing anything here:

*Disk is the state.*  There is no in-memory session holding a scout.  A report
is written to ``prep/scouts/`` as soon as it exists and read back from there,
so restarting the server costs nothing and two browser tabs cannot disagree.

*Scouting is a job, not a request.*  Four hundred games is minutes of network,
so ``POST /api/scout`` returns a job id immediately and the browser polls it.
Nothing here blocks a worker thread on the network.

*The token never touches disk.*  A token pasted into the UI lives in this
process and dies with it.  It is only ever used to raise the Lichess rate
limit and to read a private study; nothing here needs one.  If you want it
remembered, put it in ``~/.lichess_token`` or ``LICHESS_TOKEN`` yourself --
the app will find it, but it will not decide on your behalf to write your
credentials down.
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

from . import engine, exploit, export, openings, pipeline
from .board import (
    board_svg,
    legal_moves,
    line_positions,
    margin_fraction,
    play_move,
)
from .book import BookError, build_book, default_repertoire_dir, list_repertoires
from .bridge import FeatureUnavailable, status as bridge_status
from .fetch import MAX_GAMES, SPEEDS, FetchError
from .jobs import RUNNER
from .scout import DEFAULT_MIN_GAMES
from .store import SITES, Store, StoreError, default_data_dir, player_key
from .tree import DEFAULT_MAX_PLY, build_trees, walk_to

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Player Prepper")

#: A shared password gate for hosted deployments. Unset by default, so local
#: use is never asked for credentials -- this only activates when both env
#: vars are set, which a public deployment should do.
_AUTH_USER = os.environ.get("PREPPER_AUTH_USER")
_AUTH_PASS = os.environ.get("PREPPER_AUTH_PASS")


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
        # compare_digest on both fields so a wrong username does not
        # short-circuit before the password comparison (timing side channel).
        if hmac.compare_digest(user, _AUTH_USER) and hmac.compare_digest(
                password, _AUTH_PASS):
            return await call_next(request)

    return Response(status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Player Prepper"'})


#: Set by the CLI before uvicorn starts; the default keeps a direct
#: ``uvicorn player_prepper.server:app`` working.
DATA_DIR = default_data_dir()
STORE = Store(DATA_DIR)

#: A token pasted in the browser, for this process only. See the docstring.
_SESSION_TOKEN: str | None = None
_TOKEN_LOCK = threading.Lock()


def set_data_dir(path) -> None:
    global DATA_DIR, STORE
    DATA_DIR = Path(path).expanduser().resolve()
    STORE = Store(DATA_DIR)


def _token() -> str | None:
    with _TOKEN_LOCK:
        if _SESSION_TOKEN:
            return _SESSION_TOKEN
    env = os.environ.get("LICHESS_TOKEN")
    if env:
        return env.strip()
    saved = Path.home() / ".lichess_token"
    try:
        if saved.is_file():
            return saved.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def _guard(func):
    """Run something that talks to disk or a site, as an honest HTTP error."""
    try:
        return func()
    except (StoreError, BookError, FetchError, FeatureUnavailable) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _report_or_404(key: str) -> dict:
    report = STORE.load_scout(key)
    if report is None:
        raise HTTPException(status_code=404,
                            detail=f"No scout saved for {key}. Scout them first.")
    return report


# ------------------------------------------------------------------- bodies


class BookSource(BaseModel):
    kind: str
    slug: str | None = None
    url: str | None = None
    color: str | None = None
    site: str | None = None
    username: str | None = None
    limit: int | None = None
    speeds: list[str] | None = None
    ratedOnly: bool | None = None
    refresh: bool | None = None


class ScoutBody(BaseModel):
    site: str = "lichess"
    username: str
    book: list[BookSource] = []
    limit: int = 300
    speeds: list[str] = []
    ratedOnly: bool = True
    sinceMs: int | None = None
    maxPly: int = DEFAULT_MAX_PLY
    minGames: int = DEFAULT_MIN_GAMES
    refresh: bool = False
    suggest: int = 0


class BookBody(BaseModel):
    book: list[BookSource] = []


class SuggestBody(BaseModel):
    fen: str
    count: int = 3
    movetime: float = 0.4


class PlayBody(BaseModel):
    fen: str
    uci: str


class ExploitBody(BaseModel):
    color: str = "white"                 # the colour THEY have
    minGames: int = 3
    limit: int = exploit.DEFAULT_LIMIT
    movetime: float = 0.6
    lines: int = 2


class TokenBody(BaseModel):
    token: str = ""


class PdfBody(BaseModel):
    mode: str = "grid"
    includeNotation: bool = True
    includeSteps: bool = True
    landscapePages: bool = True
    boardSize: float = 424.0


class SettingsBody(BaseModel):
    settings: dict = {}


def _specs(sources) -> list:
    """Pydantic book sources into the plain dicts :mod:`book` expects."""
    out = []
    for source in sources or []:
        data = source.model_dump() if hasattr(source, "model_dump") else dict(source)
        out.append({key: value for key, value in data.items() if value is not None})
    return out


# -------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    bridge = bridge_status()
    return {
        "ok": True,
        "dataDir": str(DATA_DIR),
        "repertoireDir": str(default_repertoire_dir()),
        "sibling": bridge["sibling"],
        "stockfish": bridge["stockfish"],
        "latex": bridge["latex"],
        "privateStudies": bridge["privateStudies"],
        "engine": engine.available(),
        "openings": openings.available(),
        "hasToken": bool(_token()),
        "sites": list(SITES),
        "speeds": list(SPEEDS),
        "maxGames": MAX_GAMES,
        "pdfModes": export.MODES,
        "exploitFactors": list(exploit.FACTORS),
        #: The coordinate margin python-chess draws inside the board SVG, as a
        #: fraction of one side. The click overlay has to be inset by exactly
        #: this much or every square near an edge is wrong. See
        #: board.margin_fraction.
        "boardMargin": margin_fraction(True),
        "defaults": {
            "limit": 300,
            "maxPly": DEFAULT_MAX_PLY,
            "minGames": DEFAULT_MIN_GAMES,
            "ratedOnly": True,
            "suggest": 8,
        },
        "settings": STORE.load_settings(),
    }


@app.on_event("shutdown")
def _shutdown() -> None:
    engine.close_provider()


# --------------------------------------------------------------------- book


@app.get("/api/repertoires")
def get_repertoires() -> dict:
    return {"repertoires": list_repertoires(),
            "folder": str(default_repertoire_dir())}


@app.post("/api/book")
def post_book(body: BookBody) -> dict:
    """Build a book and report what is in it, without scouting anybody.

    This is what makes the book picker honest: you can see how many positions
    a source actually contributes before you measure coverage against it.
    """
    def run() -> dict:
        book = build_book(_specs(body.book), token=_token(), store=STORE)
        return book.stats()
    return _guard(run)


# ------------------------------------------------------------------- scouts


@app.get("/api/scouts")
def get_scouts() -> dict:
    return {"scouts": STORE.list_scouts()}


@app.get("/api/scouts/{key}")
def get_scout(key: str) -> dict:
    return _report_or_404(key)


@app.delete("/api/scouts/{key}")
def delete_scout(key: str) -> dict:
    STORE.delete_scout(key)
    return {"deleted": key}


@app.post("/api/scout")
def post_scout(body: ScoutBody) -> dict:
    """Start a scout. Returns a job to poll -- see the module docstring."""
    def run() -> dict:
        key = player_key(body.site, body.username)          # validates early
        specs = _specs(body.book)

        def work(job):
            report = pipeline.run_scout(
                STORE, site=body.site, username=body.username,
                book_specs=specs, limit=body.limit, speeds=body.speeds,
                rated_only=body.ratedOnly, since_ms=body.sinceMs,
                max_ply=body.maxPly, min_games=body.minGames,
                refresh=body.refresh, suggest=body.suggest,
                token=_token(), job=job)
            return {"key": key, "summary": report.get("summary")}

        job = RUNNER.start("scout", work, label=f"{body.site}/{body.username}")
        return {"job": job.json(with_result=False), "key": key}

    return _guard(run)


@app.get("/api/jobs")
def get_jobs() -> dict:
    return {"jobs": RUNNER.listing()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = RUNNER.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job.json()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    if not RUNNER.cancel(job_id):
        raise HTTPException(status_code=404, detail="No such job.")
    return {"cancelled": job_id}


# --------------------------------------------------------------------- tree


@app.get("/api/scouts/{key}/tree")
def get_tree(key: str, color: str = "white", line: str = "") -> dict:
    """Walk their tree by hand: what did they play from this exact position?

    Rebuilt from the cached games rather than stored in the report -- a full
    tree is a megabyte of JSON that nobody reads all of, and rebuilding it
    from a few hundred cached move lists takes milliseconds.
    """
    def run() -> dict:
        report = _report_or_404(key)
        payload = STORE.load_games(key)
        if payload is None:
            raise HTTPException(
                status_code=404,
                detail="The cached games for that scout are gone. Scout again.")
        if color not in ("white", "black"):
            raise ValueError("color must be white or black")

        max_ply = int((report.get("settings") or {}).get("maxPly")
                      or DEFAULT_MAX_PLY)
        trees = build_trees(payload.get("games") or [],
                            report.get("username", ""), max_ply=max_ply)
        moves = [part for part in (line or "").split(",") if part]
        result = walk_to(trees[color], moves)
        result["color"] = color
        result["lineUci"] = moves
        return result

    return _guard(run)


# -------------------------------------------------------------------- board


@app.get("/api/board")
def get_board(fen: str, size: int = 360, flipped: bool = False,
              lastMove: str = "", arrows: str = "") -> Response:
    def run() -> Response:
        svg = board_svg(fen, size=max(120, min(900, int(size))),
                        flipped=bool(flipped), last_move=lastMove,
                        arrows=arrows)
        return Response(content=svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})
    return _guard(run)


# ------------------------------------------------------------------- engine


@app.post("/api/suggest")
def post_suggest(body: SuggestBody) -> dict:
    """The engine's best moves in one position, asked for a gap on demand."""
    if not engine.available():
        raise HTTPException(
            status_code=400,
            detail="Engine suggestions need the sibling app.\n"
                   "From the repository root: pip install -e Lichess-Study-to-PDF")

    def run() -> dict:
        return engine.top_lines(body.fen, count=body.count,
                                movetime=body.movetime)
    return _guard(run)


@app.get("/api/eval")
def get_eval(fen: str) -> dict:
    """One position's evaluation, for the bar beside the board.

    Answers ``{"known": false}`` rather than an error when there is no engine,
    because the bar is decoration on a report that stands without it and a red
    banner every time you click a line would be noise.
    """
    def run() -> dict:
        value = engine.evaluate_one(fen)
        return value or {"known": False}
    return _guard(run)


# ---------------------------------------------------------- playing a move


@app.get("/api/legal")
def get_legal(fen: str) -> dict:
    """Legal destinations per origin square, so the page can offer them."""
    return _guard(lambda: legal_moves(fen))


@app.post("/api/play")
def post_play(body: PlayBody) -> dict:
    """Play one move from a position. The browser has no chess library."""
    return _guard(lambda: play_move(body.fen, body.uci))


@app.get("/api/line")
def get_line(moves: str = "") -> dict:
    """Every position along a line, so the wheel can step it without asking."""
    ucis = [part for part in (moves or "").split(",") if part]
    return _guard(lambda: line_positions(ucis))


# ------------------------------------------------------------------ exploit


@app.post("/api/scouts/{key}/exploit")
def post_exploit(key: str, body: ExploitBody) -> dict:
    """Analyse their choices for the best counter. Returns a job to poll.

    This is the one part of the app that needs a lot of engine time, which is
    why it runs on demand rather than as part of every scout: the tab asks for
    it the first time you open it, and the answers are saved into the report
    so it never runs twice for the same player.
    """
    def run() -> dict:
        report = _report_or_404(key)
        if body.color not in ("white", "black"):
            raise ValueError("color must be white or black")

        # Count the candidates up front so the job can be refused, with a
        # message, before a browser starts polling something that will fail.
        section = (report.get("colors") or {}).get(body.color) or {}
        rows = exploit.candidates(section, min_games=max(1, body.minGames),
                                  limit=max(1, min(60, body.limit)))
        if not rows:
            raise ValueError(
                "Nothing to analyse: they have no move played at least "
                f"{body.minGames} times in this colour. Lower the minimum, or "
                "scout more of their games.")

        def work(job):
            blob = pipeline.run_exploit(
                STORE, key, color=body.color, min_games=body.minGames,
                limit=body.limit, movetime=body.movetime, lines=body.lines,
                job=job)
            return {"key": key, "color": body.color,
                    "summary": blob.get("summary")}

        job = RUNNER.start("exploit", work, label=f"{key}/{body.color}")
        return {"job": job.json(with_result=False), "positions": len(rows)}

    return _guard(run)


# ------------------------------------------------------------------- export


@app.get("/api/scouts/{key}/pgn")
def get_pgn(key: str) -> Response:
    def run() -> Response:
        report = _report_or_404(key)
        text = export.build_pgn(report)
        name = f"prep-{report.get('username', 'opponent')}.pgn"
        return Response(
            content=text, media_type="application/x-chess-pgn",
            headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return _guard(run)


@app.post("/api/scouts/{key}/pdf")
def post_pdf(key: str, body: PdfBody) -> FileResponse:
    def run() -> FileResponse:
        report = _report_or_404(key)
        path = export.build(
            report, mode=body.mode, include_notation=body.includeNotation,
            include_steps=body.includeSteps,
            landscape_pages=body.landscapePages, board_size=body.boardSize)
        return FileResponse(
            str(path), media_type="application/pdf",
            filename=f"prep-{report.get('username', 'opponent')}.pdf")
    return _guard(run)


# ----------------------------------------------------------- token, settings


@app.get("/api/token")
def get_token() -> dict:
    return {"hasToken": bool(_token())}


@app.post("/api/token")
def post_token(body: TokenBody) -> dict:
    """Keep a token for this process only. It is never written to disk."""
    global _SESSION_TOKEN
    with _TOKEN_LOCK:
        _SESSION_TOKEN = body.token.strip() or None
    return {"hasToken": bool(_token())}


@app.get("/api/settings")
def get_settings() -> dict:
    return {"settings": STORE.load_settings()}


@app.post("/api/settings")
def post_settings(body: SettingsBody) -> dict:
    STORE.save_settings(body.settings or {})
    return {"settings": STORE.load_settings()}


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
