"""FastAPI backend for the browser interface.

Board images are rendered server-side by the same code the PDF uses, so what
you see in the browser is exactly what you get in the export.

The engine is a **process-wide singleton**.  Spawning Stockfish per request
costs about a second; keeping one warm brings a single-position evaluation
down to roughly 150 ms, which is what makes the live eval bar usable.
"""

from __future__ import annotations

import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import chess
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from reportlab.lib.pagesizes import A4, landscape, portrait

from .evals import Eval, EvalProvider, find_stockfish
from .fetch import (
    StudyFetchError,
    StudyPrivateError,
    fetch_study_pgn,
    parse_study_url,
    resolve_token,
)
from .notation import child_map, notation_blocks
from .parse import parse_study
from .pdf import PdfOptions, build_pdf
from .pdf_latex import LatexBuildError, LatexUnavailable, find_latex
from .render import build_board_svg
from .sidelines import PALETTE
from .studies import add_study, load_studies

WEB_DIR = Path(__file__).resolve().parent / "web"
CACHE_FILE = Path.home() / ".cache" / "lichess-study-pdf" / "evals.json"

app = FastAPI(title="Lichess Study to PDF")

#: Parsed studies kept in memory so export does not refetch.
_STUDIES: dict[str, dict] = {}
_STUDIES_LOCK = threading.Lock()
_MAX_STUDIES = 12

#: One warm engine for the whole process.
_PROVIDER: EvalProvider | None = None
_PROVIDER_LOCK = threading.Lock()


def get_provider() -> EvalProvider:
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = EvalProvider(
                CACHE_FILE,
                use_cloud=True,
                stockfish_path=find_stockfish(),
                movetime=0.2,
            )
        return _PROVIDER


@app.on_event("shutdown")
def _shutdown() -> None:
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is not None:
            _PROVIDER.close()
            _PROVIDER = None


def _remember(study, pgn_text: str, source_url: str) -> str:
    key = uuid.uuid4().hex[:12]
    with _STUDIES_LOCK:
        if len(_STUDIES) >= _MAX_STUDIES:
            oldest = min(_STUDIES, key=lambda k: _STUDIES[k]["at"])
            _STUDIES.pop(oldest, None)
        _STUDIES[key] = {
            "study": study, "pgn": pgn_text, "url": source_url, "at": time.time(),
        }
    return key


def _recall(key: str):
    with _STUDIES_LOCK:
        entry = _STUDIES.get(key)
    if entry is None:
        raise HTTPException(404, "Study not loaded. Paste the URL again.")
    return entry


# ------------------------------------------------------------- serialisation


def _step_json(step) -> dict:
    return {
        "index": step.index,
        "san": step.san,
        "uci": step.uci,
        "fen": step.fen,
        "moveNumber": step.move_number,
        "whiteMoved": step.white_to_move_before,
        "depth": step.depth,
        "label": step.move_label(),
        "lineLabel": step.line_label,
        "comment": step.comment,
        "nags": step.nags,
        "circles": step.circles,
        "arrows": step.arrows,
        "lastMove": list(step.last_move) if step.last_move else None,
        "startsVariation": step.starts_variation,
        # Which sideline this move belongs to; the browser colours by it.
        "branch": step.branch,
        "line": list(step.line),
    }


def _chapter_json(chapter) -> dict:
    return {
        "index": chapter.index,
        "name": chapter.name,
        "url": chapter.url,
        "orientation": chapter.orientation,
        "variant": chapter.variant,
        "initialFen": chapter.initial_fen,
        "moveCount": chapter.move_count,
        "variationCount": chapter.variation_count,
        "steps": [_step_json(s) for s in chapter.steps],
        "children": {str(k): v for k, v in child_map(chapter).items()},
        "branchCount": chapter.branch_count,
        "notation": [
            {"depth": b.depth, "html": b.html, "steps": list(b.step_indices),
             "branch": b.branch}
            for b in notation_blocks(chapter)
        ],
    }


# ------------------------------------------------------------------ models


class LoadRequest(BaseModel):
    url: str
    token: str | None = None


class SaveStudyRequest(BaseModel):
    """Add a study to the home page list."""

    url: str
    name: str | None = None


class EvalOneRequest(BaseModel):
    fen: str
    movetime: float = 0.2
    depth: int | None = None
    useCloud: bool = True


class EvalRequest(BaseModel):
    fens: list[str]
    useCloud: bool = True
    movetime: float = 0.15
    depth: int | None = None


class PlayRequest(BaseModel):
    """A move made by hand on the analysis board."""

    fen: str
    from_: str | None = None
    to: str | None = None
    uci: str | None = None
    promotion: str | None = None

    model_config = {"populate_by_name": True}


class ExportRequest(BaseModel):
    studyKey: str
    mode: str = "grid"
    includeNotation: bool = True
    includeSteps: bool = True
    showEvals: bool = False
    diagrams: str | None = None       # None = automatic, see PdfOptions
    chapters: list[int] | None = None
    maxDepth: int | None = None
    boardSize: float = 424.0
    landscapePages: bool = True
    #: Compute evaluations on the server for every exported position. This is
    #: the only way to get complete eval bars; browser-side evals only cover
    #: whatever you happened to look at.
    computeEvals: bool = True
    evalMovetime: float = 0.12
    evals: dict | None = None


# ------------------------------------------------------------------ routes


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "stockfish": find_stockfish(),
        "latex": find_latex(),
        "hasToken": bool(resolve_token()),
    }


@app.post("/api/study")
def load_study(request: LoadRequest) -> dict:
    try:
        ref = parse_study_url(request.url)
        pgn_text = fetch_study_pgn(ref, request.token)
    except StudyPrivateError as exc:
        raise HTTPException(403, str(exc)) from exc
    except StudyFetchError as exc:
        raise HTTPException(400, str(exc)) from exc

    study = parse_study(pgn_text, ref.url)
    key = _remember(study, pgn_text, ref.url)
    return {
        "key": key,
        "name": study.name,
        "url": ref.url,
        "viaChapters": bool(ref.chapter_id),
        "chapters": [_chapter_json(c) for c in study.chapters],
        # The sideline palette, so the page paints the same colours the PDF
        # writers do.  Slot = (branch - 1) % len(palette).
        "sidelinePalette": [
            {"ink": c.ink, "rule": c.rule, "tint": c.tint} for c in PALETTE
        ],
    }


def _entry_json(entry) -> dict:
    return {
        "name": entry.name,
        "url": entry.url,
        "studyId": entry.study_id,
        "chapterId": entry.chapter_id,
        "section": entry.section,
        "viaChapter": entry.private_hint,
    }


@app.get("/api/studies")
def list_studies() -> dict:
    """The home page list, re-read from disk so edits need no restart."""
    listing = load_studies()
    return {
        "path": str(listing.path),
        "sections": [
            {"heading": heading, "studies": [_entry_json(e) for e in items]}
            for heading, items in listing.sections
        ],
        "count": len(listing.entries),
        # Lines that are neither comments nor studies, so a typo in the file
        # shows up in the UI instead of silently dropping a study.
        "problems": [{"line": n, "text": t} for n, t in listing.problems],
    }


@app.post("/api/studies")
def save_study(request: SaveStudyRequest) -> dict:
    """Append a study to the list. Saving one twice is a no-op."""
    try:
        entry, added = add_study(request.url, request.name or "")
    except StudyFetchError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"Could not write the studies file: {exc}") from exc
    return {"added": added, "study": _entry_json(entry)}


@app.get("/api/board")
def board(
    fen: str,
    flip: int = 0,
    lastmove: str = "",
    circles: str = "",
    arrows: str = "",
    size: int = 400,
    coords: int = 1,
) -> Response:
    """Render one position as SVG. Shapes are ``color:sq`` / ``color:from:to``."""
    parsed_circles = []
    for item in filter(None, circles.split(",")):
        parts = item.split(":")
        if len(parts) == 2:
            parsed_circles.append((parts[0], parts[1]))

    parsed_arrows = []
    for item in filter(None, arrows.split(",")):
        parts = item.split(":")
        if len(parts) == 3:
            parsed_arrows.append((parts[0], parts[1], parts[2]))

    last = None
    if len(lastmove) >= 4:
        try:
            move = chess.Move.from_uci(lastmove[:5])
            last = (move.from_square, move.to_square)
        except ValueError:
            last = None

    try:
        svg = build_board_svg(
            fen, size=size, flipped=bool(flip), last_move=last,
            circles=parsed_circles, arrows=parsed_arrows,
            coordinates=bool(coords),
        )
    except ValueError as exc:
        raise HTTPException(400, f"Bad FEN: {exc}") from exc

    return Response(
        content=svg, media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/eval")
def evaluate_one(request: EvalOneRequest) -> dict:
    """Evaluate a single position. This is what drives the live eval bar."""
    provider = get_provider()
    try:
        chess.Board(request.fen)
    except ValueError as exc:
        raise HTTPException(400, f"Bad FEN: {exc}") from exc

    cached = provider._lookup(request.fen)
    if cached is not None:
        return {"eval": asdict(cached), "cached": True}

    value = None
    if request.useCloud:
        value = provider._from_cloud(request.fen)
    if value is None:
        value = provider._from_engine(
            request.fen, movetime=request.movetime, depth=request.depth
        )
    if value is None or not value.known:
        return {"eval": asdict(Eval()), "cached": False}

    provider._store(request.fen, value)
    provider.save_cache()
    return {"eval": asdict(value), "cached": False}


@app.post("/api/evals")
def evaluate_many(request: EvalRequest) -> dict:
    """Batch evaluation, used to fill the graph in the background."""
    provider = get_provider()
    saved_movetime, saved_depth = provider.movetime, provider.depth
    provider.movetime = request.movetime
    provider.depth = request.depth
    provider.use_cloud = request.useCloud
    try:
        results = provider.evaluate_many(request.fens)
        stats = dict(provider.stats)
        limited = provider.cloud_rate_limited
    finally:
        provider.movetime, provider.depth = saved_movetime, saved_depth
        provider.use_cloud = True

    return {
        "evals": {fen: asdict(ev) for fen, ev in results.items()},
        "stats": stats,
        "cloudRateLimited": limited,
        "hasEngine": bool(find_stockfish()),
    }


@app.get("/api/legal")
def legal_moves(fen: str, square: str = "") -> dict:
    """Legal destinations, for click-to-move on the analysis board."""
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise HTTPException(400, f"Bad FEN: {exc}") from exc

    out: dict[str, list] = {}
    for move in board.legal_moves:
        src = chess.square_name(move.from_square)
        out.setdefault(src, []).append(chess.square_name(move.to_square))

    if square:
        out = {square: sorted(set(out.get(square, [])))}
    else:
        out = {k: sorted(set(v)) for k, v in out.items()}

    return {
        "moves": out,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "check": board.is_check(),
        "gameOver": board.is_game_over(),
    }


@app.post("/api/play")
def play_move(request: PlayRequest) -> dict:
    """Apply a hand-played move. Free play off the study line lives here."""
    try:
        board = chess.Board(request.fen)
    except ValueError as exc:
        raise HTTPException(400, f"Bad FEN: {exc}") from exc

    move = None
    if request.uci:
        try:
            move = chess.Move.from_uci(request.uci)
        except ValueError:
            move = None
    elif request.from_ and request.to:
        text = request.from_ + request.to + (request.promotion or "")
        try:
            move = chess.Move.from_uci(text)
        except ValueError:
            move = None
        # Retry as a promotion when the piece reaches the last rank.
        if move is not None and move not in board.legal_moves and not move.promotion:
            promoted = chess.Move(move.from_square, move.to_square, chess.QUEEN)
            if promoted in board.legal_moves:
                move = promoted

    if move is None or move not in board.legal_moves:
        return {"legal": False}

    san = board.san(move)
    board.push(move)
    outcome = board.outcome()
    return {
        "legal": True,
        "san": san,
        "uci": move.uci(),
        "fen": board.fen(),
        "lastMove": [chess.square_name(move.from_square),
                     chess.square_name(move.to_square)],
        "check": board.is_check(),
        "gameOver": board.is_game_over(),
        "result": outcome.result() if outcome else None,
    }


@app.post("/api/export")
def export(request: ExportRequest) -> FileResponse:
    entry = _recall(request.studyKey)
    study = entry["study"]

    if request.mode != "book" and not request.includeNotation \
            and not request.includeSteps:
        raise HTTPException(
            400, "Enable at least one of notation or stepping pages."
        )

    chapter_filter = tuple(request.chapters) if request.chapters else None
    chapters = [c for c in study.chapters
                if chapter_filter is None or c.index in chapter_filter]

    evals: dict = {}
    if request.showEvals:
        # Start from anything the browser already knows, then fill the gaps
        # server-side so every exported position actually has a bar.
        for fen, payload in (request.evals or {}).items():
            try:
                evals[fen] = Eval(**payload)
            except TypeError:
                continue
        if request.computeEvals:
            provider = get_provider()
            saved = provider.movetime
            provider.movetime = request.evalMovetime
            try:
                fens = [s.fen for c in chapters for s in c.steps]
                evals.update(provider.evaluate_many(fens))
            finally:
                provider.movetime = saved

    page = landscape(A4) if request.landscapePages else portrait(A4)
    options = PdfOptions(
        mode=request.mode,
        include_notation=request.includeNotation,
        include_steps=request.includeSteps,
        show_evals=request.showEvals and bool(evals),
        board_size=request.boardSize,
        page_size=page,
        diagrams=request.diagrams,
        max_depth=request.maxDepth,
        chapter_filter=chapter_filter,
    )

    safe = "".join(ch for ch in study.name if ch.isalnum() or ch in " -_").strip()
    filename = f"{(safe or 'lichess-study')[:70]}.pdf"
    target = Path(tempfile.gettempdir()) / f"lsp-{uuid.uuid4().hex[:10]}.pdf"

    try:
        build_pdf(study, target, evals=evals, options=options)
    except LatexUnavailable as exc:
        raise HTTPException(400, str(exc)) from exc
    except LatexBuildError as exc:
        raise HTTPException(500, f"LaTeX failed to build the book:\n{exc}") from exc

    return FileResponse(target, media_type="application/pdf", filename=filename)


@app.get("/api/pgn/{key}")
def download_pgn(key: str) -> Response:
    entry = _recall(key)
    return Response(
        content=entry["pgn"],
        media_type="application/x-chess-pgn",
        headers={"Content-Disposition": 'attachment; filename="study.pgn"'},
    )


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
