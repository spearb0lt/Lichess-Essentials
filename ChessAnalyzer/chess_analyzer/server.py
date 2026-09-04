"""The HTTP layer: everything the browser interface talks to.

Design notes worth knowing before changing anything here:

*Slow work is a job, not a request.*  Reviewing a game and downloading an
engine both take minutes.  Both return a job id immediately and report
progress through :mod:`chess_analyzer.jobs`, so the browser can show a bar,
cancel, and survive a reload without losing the work.

*The board is drawn in the browser, not here.*  The sibling apps render each
position as an SVG on the server, which is right for them and wrong for this:
stepping through a game with the mouse wheel would be one request per notch.
So the server ships the piece artwork **once** as a sprite sheet -- generated
from python-chess's own vectors, so it is the same artwork the sibling apps
draw -- and the browser positions ``<use>`` elements itself.  Legality still
comes from here, because there is exactly one chess implementation in this
project and it is the Python one.

*The token never touches disk.*  A Lichess token pasted into the UI lives in
this process and dies with it.  Nothing here needs one -- every endpoint the
app uses is public -- but it raises the rate limit for bulk imports, so it is
accepted and never written down.
"""

from __future__ import annotations

import base64
import hmac
import os
import threading
from pathlib import Path

import chess
import chess.svg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import classify, engines, jobs, library, openings, position, review
from .live import MANAGER, LiveError
from .sources import SourceError, chesscom, lichess, resolve
from .sources.common import parse_game

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Chess Analyzer")

#: A shared password gate for hosted deployments. Unset by default, so local
#: use is never asked for credentials -- this only activates when both env
#: vars are set, which a public deployment should do.
_AUTH_USER = os.environ.get("ANALYZER_AUTH_USER")
_AUTH_PASS = os.environ.get("ANALYZER_AUTH_PASS")


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
        # short-circuit before the password check (timing side channel).
        if hmac.compare_digest(user, _AUTH_USER) and hmac.compare_digest(
                password, _AUTH_PASS):
            return await call_next(request)

    return Response(status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Chess Analyzer"'})


DATA_DIR = library.DEFAULT_DIR
LIBRARY = library.Library(DATA_DIR)
CACHE = LIBRARY.load_cache()

#: A token pasted in the browser, for this process only. See the docstring.
_SESSION_TOKEN: str | None = None
_TOKEN_LOCK = threading.Lock()


def set_data_dir(path) -> None:
    global DATA_DIR, LIBRARY, CACHE
    DATA_DIR = Path(path).expanduser().resolve()
    LIBRARY = library.Library(DATA_DIR)
    CACHE = LIBRARY.load_cache()


def _token() -> str | None:
    with _TOKEN_LOCK:
        if _SESSION_TOKEN:
            return _SESSION_TOKEN
    for name in ("LICHESS_TOKEN", "LICHESS_API_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    path = Path.home() / ".lichess_token"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def _guard(work):
    """Turn the app's own exceptions into HTTP errors with readable messages."""
    try:
        return work()
    except (SourceError, engines.EngineError, LiveError, library.LibraryError,
            review.ReviewCancelled) as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ------------------------------------------------------------------ bodies


class ImportBody(BaseModel):
    text: str
    save: bool = True


class ReviewBody(BaseModel):
    preset: str = "standard"
    engineId: str | None = None
    movetime: float | None = None
    depth: int | None = None
    multipv: int | None = None
    threads: int | None = None
    hashMb: int | None = None
    weights: str | None = None
    force: bool = False


class EvalBody(BaseModel):
    fen: str
    engineId: str | None = None
    movetime: float = 0.4
    depth: int | None = None
    multipv: int = 3
    weights: str | None = None


class PlayBody(BaseModel):
    fen: str
    uci: str


class InstallBody(BaseModel):
    id: str
    build: str | None = None


class NetworkBody(BaseModel):
    id: str


class LiveBody(BaseModel):
    kind: str                       # lichess | chesscom | manual | pgn | setup
    reference: str = ""
    engineId: str | None = None
    movetime: float = 0.6
    multipv: int = 3
    startFen: str | None = None     # only for "setup"


class PositionBody(BaseModel):
    """An arrangement from the board editor, before it is a position."""

    pieces: dict = {}
    turn: str = "w"
    castling: str = ""
    enPassant: str = "-"
    halfmove: int = 0
    fullmove: int = 1


class MoveBody(BaseModel):
    uci: str


class PgnBody(BaseModel):
    pgn: str


class TokenBody(BaseModel):
    token: str = ""


class SettingsBody(BaseModel):
    values: dict


# ------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """The page, with its assets stamped by their own modification time.

    Without this, a changed app.js can sit in the browser cache and the app
    keeps running the old one -- which looks exactly like "the fix did not
    work", and is impossible to tell apart from a real bug. The stamp makes a
    changed file a different URL, so it is always fetched.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "style.css"):
        path = WEB_DIR / asset
        stamp = int(path.stat().st_mtime) if path.is_file() else 0
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={stamp}")
    return HTMLResponse(html)


@app.get("/favicon.ico")
def favicon() -> Response:
    path = WEB_DIR / "favicon.ico"
    if path.is_file():
        return FileResponse(path)
    return Response(status_code=204)


@app.get("/api/health")
def health() -> dict:
    found = engines.discover()
    return {
        "ok": True,
        "dataDir": str(DATA_DIR),
        "engines": [spec.json() for spec in found],
        "hasEngine": bool(found),
        "hasToken": bool(_token()),
        "openings": openings.available(),
        "presets": review.PRESETS,
        "labels": [{"key": key, "glyph": glyph}
                   for key, glyph in classify.LABELS],
        "labelRules": classify.CHESSCOM_RULES,
        "ratingFormula": review.RATING_FORMULA,
        "maxMultipv": 5,
        "settings": library.load_settings(DATA_DIR),
        "cache": CACHE.stats(),
    }


@app.on_event("shutdown")
def _shutdown() -> None:
    # Not optional: python-chess keeps its engine loop on a non-daemon thread,
    # so leaving one open stops the process from ever exiting. See
    # engines.POOL.
    MANAGER.close()
    engines.close()
    CACHE.save()


# ------------------------------------------------------------------ pieces


_SPRITE: str | None = None


@app.get("/api/pieces.svg")
def pieces() -> Response:
    """The twelve piece vectors as one reusable sprite sheet.

    python-chess ships the artwork the sibling apps already draw with, so
    taking it from there keeps every board in this repository looking the
    same while letting the browser place pieces without a round trip.
    """
    global _SPRITE
    if _SPRITE is None:
        parts = ['<svg xmlns="http://www.w3.org/2000/svg" '
                 'xmlns:xlink="http://www.w3.org/1999/xlink" '
                 'width="0" height="0"><defs>']
        for symbol, markup in chess.svg.PIECES.items():
            piece = chess.Piece.from_symbol(symbol)
            name = ("white" if piece.color else "black") + "-" + \
                chess.piece_name(piece.piece_type)
            # Wrap each group in a 45x45 symbol so the browser can place it
            # with one <use> and a transform.
            parts.append(f'<symbol id="piece-{name}" viewBox="0 0 45 45">'
                         f'{markup}</symbol>')
        parts.append("</defs></svg>")
        _SPRITE = "".join(parts)
    return Response(content=_SPRITE, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# ------------------------------------------------------------------- board


@app.get("/api/legal")
def get_legal(fen: str, square: str = "") -> dict:
    def run():
        board = chess.Board(fen)
        moves: dict[str, list] = {}
        for move in board.legal_moves:
            moves.setdefault(chess.square_name(move.from_square), []).append(
                chess.square_name(move.to_square))
        if square:
            moves = {square: sorted(set(moves.get(square, [])))}
        else:
            moves = {key: sorted(set(value)) for key, value in moves.items()}
        return {
            "moves": moves,
            "turn": "white" if board.turn == chess.WHITE else "black",
            "check": board.is_check(),
            "gameOver": board.is_game_over(),
        }
    return _guard(run)


@app.post("/api/play")
def post_play(body: PlayBody) -> dict:
    """Play one move on a position. The browser has no chess rules of its own."""
    def run():
        board = chess.Board(body.fen)
        uci = body.uci
        # A pawn reaching the last rank without a promotion suffix is the
        # browser saying "queen", which is what it means every time.
        if len(uci) == 4:
            piece = board.piece_at(chess.parse_square(uci[:2]))
            rank = uci[3]
            if piece and piece.piece_type == chess.PAWN and rank in ("1", "8"):
                uci += "q"
        move = board.parse_uci(uci)
        san = board.san(move)
        board.push(move)
        return {
            "fen": board.fen(),
            "san": san,
            "uci": move.uci(),
            "turn": "white" if board.turn == chess.WHITE else "black",
            "check": board.is_check(),
            "gameOver": board.is_game_over(),
            "outcome": (board.outcome(claim_draw=True).result()
                        if board.is_game_over() else None),
        }
    return _guard(run)


@app.post("/api/eval")
def post_eval(body: EvalBody) -> dict:
    """Analyse one position, for the analysis board and the eval bar."""
    def run():
        board = chess.Board(body.fen)
        options = engines.EngineOptions(
            multipv=max(1, min(5, body.multipv)), movetime=body.movetime,
            depth=body.depth, weights=body.weights)
        engine = engines.POOL.get(body.engineId, weights=body.weights)
        settings_key = f"{engine.name}|{options.key()}"

        lines = CACHE.get(body.fen, settings_key)
        if lines is None:
            lines = engine.analyse(board, options)
            CACHE.put(body.fen, settings_key, lines)

        described = []
        for line in lines:
            spelled = review.describe_pv(board, line.get("pv") or [])
            described.append({
                "rank": line.get("rank", len(described) + 1),
                "uci": (spelled["first"] or {}).get("uci"),
                "san": (spelled["first"] or {}).get("san"),
                "line": spelled["line"],
                "cp": line.get("cp"),
                "mate": line.get("mate"),
                "text": review.eval_text(line.get("cp"), line.get("mate")),
                "depth": line.get("depth", 0),
            })
        from .accuracy import win_percent
        top = lines[0]
        return {
            "lines": described,
            "cp": top.get("cp"),
            "mate": top.get("mate"),
            "text": review.eval_text(top.get("cp"), top.get("mate")),
            "depth": top.get("depth", 0),
            "whiteFraction": round(
                win_percent(top.get("cp"), top.get("mate")) / 100.0, 4),
            "engine": engine.name,
            "gameOver": board.is_game_over(),
        }
    return _guard(run)


@app.post("/api/position")
def post_position(body: PositionBody) -> dict:
    """Assemble an arrangement into a FEN and say whether it is a position.

    Called on every click in the board editor, so it is the thing that turns
    "I put a second white king down" into a sentence rather than into an
    engine that refuses to start ten seconds later.
    """
    def run():
        try:
            fen = position.assemble(
                body.pieces, turn=body.turn, castling=body.castling,
                en_passant=body.enPassant, halfmove=body.halfmove,
                fullmove=body.fullmove)
        except position.PositionError as exc:
            raise HTTPException(400, str(exc)) from exc
        return position.describe(fen)
    return _guard(run)


@app.get("/api/opening")
def get_opening(fen: str) -> dict:
    def run():
        board = chess.Board(fen)
        return {"opening": openings.lookup(board), "inBook": openings.in_book(board)}
    return _guard(run)


# ----------------------------------------------------------------- library


@app.get("/api/library")
def get_library(limit: int = 200) -> dict:
    return {"games": LIBRARY.listing(limit=limit)}


@app.post("/api/import")
def post_import(body: ImportBody) -> dict:
    """One box for a URL, an id, a PGN or a FEN."""
    def run():
        record = resolve(body.text, token=_token())
        if body.save:
            LIBRARY.save(record)
        stored = LIBRARY.load(record.id) or {}
        return {
            "game": record.json(),
            "pgn": record.pgn,
            "review": stored.get("review"),
            "moves": _move_list(record.pgn),
        }
    return _guard(run)


@app.get("/api/games/{game_id}")
def get_game(game_id: str) -> dict:
    stored = LIBRARY.load(game_id)
    if stored is None:
        raise HTTPException(404, "That game is not in the library.")
    return {
        "game": stored.get("record"),
        "pgn": stored.get("pgn", ""),
        "review": stored.get("review"),
        "moves": _move_list(stored.get("pgn", "")),
    }


@app.get("/api/games/{game_id}/pgn")
def get_game_pgn(game_id: str) -> Response:
    stored = LIBRARY.load(game_id)
    if stored is None:
        raise HTTPException(404, "That game is not in the library.")
    return Response(
        content=stored.get("pgn", ""), media_type="application/x-chess-pgn",
        headers={"Content-Disposition": f'attachment; filename="{game_id}.pgn"'})


@app.delete("/api/games/{game_id}")
def delete_game(game_id: str) -> dict:
    return {"deleted": LIBRARY.delete(game_id)}


def _move_list(pgn: str) -> list[dict]:
    """Every move with the position it produced, so the browser can step
    through a game instantly without asking the server for each position."""
    if not pgn.strip():
        return []
    try:
        game = parse_game(pgn)
    except SourceError:
        return []

    board = game.board()
    rows = [{"ply": 0, "san": "", "uci": "", "fen": board.fen(),
             "moveNumber": board.fullmove_number, "color": "", "clock": None}]
    for ply, node in enumerate(game.mainline(), start=1):
        move = node.move
        if move not in board.legal_moves:
            break
        colour = "white" if board.turn == chess.WHITE else "black"
        number = board.fullmove_number
        san = board.san(move)
        board.push(move)
        rows.append({
            "ply": ply, "san": san, "uci": move.uci(), "fen": board.fen(),
            "moveNumber": number, "color": colour, "clock": node.clock(),
        })
    return rows


# ------------------------------------------------------------------ review


@app.post("/api/games/{game_id}/review")
def post_review(game_id: str, body: ReviewBody) -> dict:
    """Start a review. Returns a job to poll."""
    stored = LIBRARY.load(game_id)
    if stored is None:
        raise HTTPException(404, "Import the game before reviewing it.")

    settings = review.Settings.from_preset(
        body.preset, engine_id=body.engineId, movetime=body.movetime,
        depth=body.depth, multipv=body.multipv, threads=body.threads,
        hash_mb=body.hashMb, weights=body.weights)

    existing = stored.get("review")
    if existing and not body.force \
            and existing.get("settings", {}).get("preset") == settings.preset \
            and existing.get("settings", {}).get("engineId") == settings.engine_id:
        return {"job": None, "review": existing, "reused": True}

    from .sources.common import record_from_pgn
    record = record_from_pgn(stored["pgn"], source=stored["record"]["source"],
                             game_id=game_id, url=stored["record"].get("url", ""))

    def work(job):
        result = review.review(
            record, settings, cache=CACHE,
            progress=job.progress, should_stop=job.should_stop)
        LIBRARY.save_review(game_id, result)
        return result

    job = jobs.RUNNER.start("review", work,
                            label=f"{record.white} vs {record.black}")
    return {"job": job.json(), "review": None, "reused": False}


@app.get("/api/games/{game_id}/review")
def get_review(game_id: str) -> dict:
    stored = LIBRARY.load(game_id)
    if stored is None:
        raise HTTPException(404, "That game is not in the library.")
    return {"review": stored.get("review")}


# -------------------------------------------------------------------- jobs


@app.get("/api/jobs")
def get_jobs() -> dict:
    return {"jobs": jobs.RUNNER.listing()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.RUNNER.get(job_id)
    if job is None:
        raise HTTPException(404, "That job has finished and been swept.")
    return job.json()


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str) -> dict:
    return {"cancelled": jobs.RUNNER.cancel(job_id)}


# ----------------------------------------------------------------- engines


@app.get("/api/engines")
def get_engines(offline: int = 0) -> dict:
    return _guard(lambda: engines.catalog(offline=bool(offline)))


@app.post("/api/engines/install")
def post_install(body: InstallBody) -> dict:
    def work(job):
        def progress(done, total, message=None):
            job.progress(done, total, message)
        spec = engines.install(body.id, progress=progress, build=body.build)
        return spec.json()

    job = jobs.RUNNER.start("engine", work, label=body.id)
    return job.json()


@app.post("/api/engines/network")
def post_network(body: NetworkBody) -> dict:
    def work(job):
        path = engines.install_network(body.id, progress=job.progress)
        return {"id": body.id, "path": path}

    job = jobs.RUNNER.start("network", work, label=body.id)
    return job.json()


@app.delete("/api/engines/{spec_id:path}")
def delete_engine(spec_id: str) -> dict:
    return _guard(lambda: {"deleted": engines.uninstall(spec_id)})


# ------------------------------------------------------------------- users


@app.get("/api/users/lichess/{username}/games")
def lichess_games(username: str, limit: int = 20) -> dict:
    return _guard(lambda: {
        "games": lichess.user_games(username, limit=limit, token=_token())})


@app.get("/api/users/lichess/{username}/current")
def lichess_current(username: str) -> dict:
    return _guard(lambda: {"game": lichess.current_game(username, token=_token())})


@app.get("/api/users/chesscom/{username}/games")
def chesscom_games(username: str, limit: int = 20) -> dict:
    return _guard(lambda: {"games": chesscom.user_games(username, limit=limit)})


@app.get("/api/users/chesscom/{username}/ongoing")
def chesscom_ongoing(username: str) -> dict:
    return _guard(lambda: {"games": chesscom.in_progress(username)})


@app.get("/api/lichess/tv")
def lichess_tv() -> dict:
    return _guard(lambda: {"games": lichess.tv_games(token=_token())})


# -------------------------------------------------------------------- live


@app.get("/api/live")
def list_live() -> dict:
    MANAGER.sweep()
    return {"sessions": MANAGER.listing()}


@app.post("/api/live")
def start_live(body: LiveBody) -> dict:
    def run():
        kind = body.kind
        game_id, label = "", ""
        start_fen = None

        if kind == "lichess":
            game_id = lichess.parse_reference(body.reference) or ""
            if not game_id:
                # Not a game reference, so try it as a username: "who is this
                # person playing right now" is the more natural way to ask.
                current = lichess.current_game(body.reference, token=_token())
                if not current.get("live"):
                    raise SourceError(
                        f"{body.reference} is not playing right now. Their last "
                        "game can still be imported and reviewed.")
                game_id = current["gameId"]
                label = f"{current['white']['name']} vs {current['black']['name']}"
            if not game_id:
                raise SourceError("Give a Lichess game URL, a game id, or a username.")

        elif kind == "chesscom":
            reference = chesscom.parse_reference(body.reference)
            if reference is None:
                raise SourceError(
                    "Give a Chess.com game URL, like "
                    "https://www.chess.com/game/live/123456789. Chess.com "
                    "publishes no way to find a live game from a username.")
            game_id = reference[1]

        elif kind == "setup":
            # An arranged position is a manual session that starts somewhere
            # other than move one -- the same board, the same clicking, the
            # same engine. Only the starting point differs, so it would be a
            # fifth code path for no reason.
            if not body.startFen:
                raise SourceError("Arrange a position first.")
            checked = position.describe(body.startFen)
            if not checked["valid"]:
                raise SourceError("That position is not legal:\n  "
                                  + "\n  ".join(checked["problems"]))
            if checked["gameOver"]:
                raise SourceError(checked["outcome"])
            start_fen = checked["fen"]
            kind = "manual"
            label = "Arranged position"

        elif kind not in ("manual", "pgn"):
            raise SourceError(f"Unknown live source {kind!r}.")

        session = MANAGER.start(
            kind, game_id=game_id, label=label or body.reference,
            token=_token(), movetime=body.movetime, multipv=body.multipv,
            engine_id=body.engineId, start_fen=start_fen)
        return session.json()

    return _guard(run)


@app.get("/api/live/{session_id}")
def get_live(session_id: str) -> dict:
    return _guard(lambda: MANAGER.get(session_id).json())


@app.post("/api/live/{session_id}/move")
def live_move(session_id: str, body: MoveBody) -> dict:
    return _guard(lambda: MANAGER.get(session_id).push_move(body.uci))


@app.post("/api/live/{session_id}/undo")
def live_undo(session_id: str) -> dict:
    return _guard(lambda: MANAGER.get(session_id).undo())


@app.post("/api/live/{session_id}/pgn")
def live_pgn(session_id: str, body: PgnBody) -> dict:
    return _guard(lambda: MANAGER.get(session_id).feed_pgn(body.pgn))


@app.post("/api/live/{session_id}/save")
def live_save(session_id: str) -> dict:
    """Freeze a live session into the library so it can be reviewed."""
    def run():
        session = MANAGER.get(session_id)
        state = session.json()
        from .sources.common import build_pgn, record_from_pgn
        pgn = build_pgn({
            "Event": session.label or "Live game",
            "Site": {"lichess": "lichess.org", "chesscom": "Chess.com"}
            .get(session.kind, "Chess Analyzer"),
            "White": state["white"], "Black": state["black"],
            "Result": state["result"],
        }, state["moves"], start_fen=session.start_fen)
        record = record_from_pgn(
            pgn, source=session.kind if session.kind in ("lichess", "chesscom")
            else "pgn",
            game_id=(f"{session.kind}-{session.game_id}" if session.game_id
                     else None),
            finished=state["finished"])
        LIBRARY.save(record)
        return {"game": record.json(), "pgn": record.pgn,
                "moves": _move_list(record.pgn)}
    return _guard(run)


@app.delete("/api/live/{session_id}")
def stop_live(session_id: str) -> dict:
    return {"stopped": MANAGER.stop(session_id)}


# ---------------------------------------------------------------- settings


@app.get("/api/settings")
def get_settings() -> dict:
    return {"settings": library.load_settings(DATA_DIR)}


@app.post("/api/settings")
def post_settings(body: SettingsBody) -> dict:
    current = library.load_settings(DATA_DIR)
    current.update(body.values or {})
    library.save_settings(DATA_DIR, current)
    return {"settings": current}


@app.post("/api/token")
def post_token(body: TokenBody) -> dict:
    """Hold a Lichess token for this process only. Never written to disk."""
    global _SESSION_TOKEN
    with _TOKEN_LOCK:
        _SESSION_TOKEN = body.token.strip() or None
    return {"hasToken": bool(_token())}


@app.get("/api/token")
def get_token() -> dict:
    return {"hasToken": bool(_token())}


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
