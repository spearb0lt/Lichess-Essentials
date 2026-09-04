"""The HTTP layer: everything the browser interface talks to.

Design notes worth knowing before changing anything here:

*Disk is the state.*  A report is written to ``history/reports/`` as soon as it
exists and read back from there. Restarting the server costs nothing, and the
reviews -- the expensive part -- are never in memory at all.

*Reviewing is a job, not a request.*  A few hundred games is minutes to hours,
so ``POST /api/run`` returns a job id immediately and the browser polls it.

*Re-slicing is a request.*  Changing the evidence floor re-reads what is on
disk and does no engine work, so it can answer inline. That separation is the
reason the thresholds are adjustable at all.

*The token never touches disk.*  Pasted into the UI it lives in this process
and dies with it. Nothing here needs one; it only raises the Lichess rate
limit while pulling a few hundred games.
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

from . import aggregate, batch, board, exportcsv, pdf, pipeline
from .bridge import FeatureUnavailable, status as bridge_status
from .buckets import DIMENSIONS
from .jobs import RUNNER
from .sources import MAX_GAMES, SPEEDS, SourceError
from .store import Store, StoreError, default_data_dir

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Weakness Report")

#: A shared password gate for hosted deployments. Unset by default, so local
#: use is never asked for credentials.
_AUTH_USER = os.environ.get("WEAKNESS_AUTH_USER")
_AUTH_PASS = os.environ.get("WEAKNESS_AUTH_PASS")


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
                    headers={"WWW-Authenticate": 'Basic realm="Weakness Report"'})


DATA_DIR = default_data_dir()
STORE = Store(DATA_DIR)

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
    try:
        return func()
    except (StoreError, SourceError, FeatureUnavailable, batch.BatchError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _report_or_404(key: str) -> dict:
    report = STORE.load_report(key)
    if report is None:
        raise HTTPException(status_code=404,
                            detail=f"No report saved for {key}. Build one first.")
    return report


# ------------------------------------------------------------------- bodies


class Spec(BaseModel):
    kind: str = "lichess"
    username: str = ""
    path: str = ""
    limit: int = 200
    speeds: list[str] = []
    ratedOnly: bool = True
    sinceMs: int | None = None


class RunBody(BaseModel):
    spec: Spec
    preset: str = batch.DEFAULT_PRESET
    threads: int = batch.DEFAULT_THREADS
    hashMb: int = batch.DEFAULT_HASH_MB
    adopt: str = "matching"
    refresh: bool = False
    review: bool = True
    minMoves: int = aggregate.MIN_MOVES
    minGames: int = aggregate.MIN_GAMES


class ResliceBody(BaseModel):
    minMoves: int = aggregate.MIN_MOVES
    minGames: int = aggregate.MIN_GAMES


class PdfBody(BaseModel):
    slices: list[str] | None = None
    landscapePages: bool = False
    includeMoments: bool = True
    includeMethod: bool = True


class TokenBody(BaseModel):
    token: str = ""


class SettingsBody(BaseModel):
    settings: dict = {}


# -------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    state = bridge_status()
    return {
        "ok": True,
        "dataDir": str(DATA_DIR),
        "analyzer": state["analyzer"],
        "analyzerVia": state["analyzerVia"],
        "analyzerDir": state["analyzerDir"],
        "study": state["study"],
        "stockfish": state["stockfish"],
        "hasToken": bool(_token()),
        "reviewsOnDisk": STORE.review_count(),
        "speeds": list(SPEEDS),
        "maxGames": MAX_GAMES,
        "presets": batch.PRESETS,
        "adoptModes": list(batch.ADOPT_MODES),
        "dimensions": [{"key": d.key, "label": d.label, "note": d.note}
                       for d in DIMENSIONS],
        "defaults": {
            "limit": 200,
            "preset": batch.DEFAULT_PRESET,
            "threads": batch.DEFAULT_THREADS,
            "minMoves": aggregate.MIN_MOVES,
            "minGames": aggregate.MIN_GAMES,
            "ratedOnly": True,
        },
        "settings": STORE.load_settings(),
    }


@app.on_event("shutdown")
def _shutdown() -> None:
    # A warm engine is on a non-daemon thread, and CPython joins those before
    # atexit runs -- so a server that leaves one open never exits.
    engines = None
    try:
        from .bridge import analyzer
        engines = analyzer("engines")
    except Exception:                                        # noqa: BLE001
        engines = None
    if engines is not None:
        try:
            engines.close()
        except Exception:                                    # noqa: BLE001
            pass


# ------------------------------------------------------------------ reports


@app.get("/api/reports")
def get_reports() -> dict:
    return {"reports": STORE.list_reports()}


@app.get("/api/reports/{key}")
def get_report(key: str) -> dict:
    return _report_or_404(key)


@app.delete("/api/reports/{key}")
def delete_report(key: str) -> dict:
    STORE.delete_report(key)
    return {"deleted": key, "reviewsKept": STORE.review_count()}


@app.post("/api/reports/{key}/reslice")
def post_reslice(key: str, body: ResliceBody) -> dict:
    """Rebuild at a different evidence floor. Pure arithmetic, no engine."""
    return _guard(lambda: pipeline.reslice(
        STORE, key, min_moves=max(1, body.minMoves),
        min_games=max(1, body.minGames)))


@app.post("/api/estimate")
def post_estimate(body: RunBody) -> dict:
    """Roughly how long a run would take, so the button can say so."""
    def run() -> dict:
        spec = body.spec.model_dump()
        key = pipeline.dataset_key(spec)
        cached = STORE.load_games(key) or {}
        games = cached.get("games") or []
        ready = batch.outstanding(STORE, games, preset=body.preset,
                                  threads=body.threads, hash_mb=body.hashMb,
                                  adopt=body.adopt)
        estimate = batch.estimate(games, body.preset, already=ready["ready"])
        estimate["cached"] = bool(games)
        estimate["key"] = key
        estimate["ready"] = ready["ready"]
        return estimate
    return _guard(run)


@app.post("/api/run")
def post_run(body: RunBody) -> dict:
    """Fetch, review and aggregate. Returns a job to poll."""
    def start() -> dict:
        spec = body.spec.model_dump()
        key = pipeline.dataset_key(spec)                      # validates early

        def work(job):
            report = pipeline.run(
                STORE, spec, preset=body.preset, threads=body.threads,
                hash_mb=body.hashMb, adopt=body.adopt, refresh=body.refresh,
                min_moves=max(1, body.minMoves),
                min_games=max(1, body.minGames), review=body.review,
                token=_token(), job=job)
            return {"key": key, "summary": report.get("summary")}

        job = RUNNER.start("run", work, label=key)
        return {"job": job.json(with_result=False), "key": key}

    return _guard(start)


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


# -------------------------------------------------------------------- board


@app.get("/api/board")
def get_board(fen: str, size: int = 320, flipped: bool = False,
              arrows: str = "") -> Response:
    def run() -> Response:
        svg = board.board_svg(fen, size=max(120, min(900, int(size))),
                              flipped=bool(flipped), arrows=arrows)
        return Response(content=svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})
    return _guard(run)


# ------------------------------------------------------------------- export


@app.post("/api/reports/{key}/pdf")
def post_pdf(key: str, body: PdfBody) -> FileResponse:
    def run() -> FileResponse:
        report = _report_or_404(key)
        path = pdf.build(report, slices=body.slices,
                         landscape_pages=body.landscapePages,
                         include_moments=body.includeMoments,
                         include_method=body.includeMethod)
        label = (report.get("label") or key).replace(" ", "-")
        return FileResponse(str(path), media_type="application/pdf",
                            filename=f"weakness-{label}.pdf")
    return _guard(run)


@app.get("/api/reports/{key}/csv")
def get_csv(key: str) -> Response:
    """Every slice as one CSV, for anyone who would rather have a spreadsheet."""
    def run() -> Response:
        report = _report_or_404(key)
        text = exportcsv.slices_csv(report)
        name = f"weakness-{(report.get('label') or key).replace(' ', '-')}.csv"
        return Response(
            content=text, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return _guard(run)


# ----------------------------------------------------------- token, settings


@app.get("/api/token")
def get_token() -> dict:
    return {"hasToken": bool(_token())}


@app.post("/api/token")
def post_token(body: TokenBody) -> dict:
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
