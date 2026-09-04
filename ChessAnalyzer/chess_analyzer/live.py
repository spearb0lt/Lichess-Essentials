"""Watching a game that has not finished yet.

Four ways in, because the four situations are genuinely different.  (A fifth,
arranging a position by hand, is not a fifth kind of session: it is a
``manual`` one that starts somewhere other than move one -- see ``start_fen``.)

``lichess``
    A real push stream.  ``/api/stream/game/{id}`` is public and sends a line
    per move, so this is exact and costs one connection.

``chesscom``
    Polling, because there is nothing to subscribe to.  The undocumented
    callback endpoint is asked every couple of seconds and the move list is
    diffed.  If chess.com stops answering, the session says so and keeps the
    moves it already has rather than dying.

``manual``
    You click the moves as they are played.  No network at all, so it works
    for a Chess.com blitz game, a broadcast you are watching, or a board in
    front of you.  It is also the only one of the four that cannot fall
    behind.

``pgn``
    You paste the PGN and re-paste it as it grows.  The session keeps the
    longest prefix it has seen, so a paste that arrives out of order or
    truncated cannot rewind the game.

All four converge on the same thing: a move list, whose turn it is, and a
current position.  A separate analysis thread watches that position and keeps
an evaluation next to it, so the browser polls one endpoint and gets both.

The split into two threads is the point.  Analysing inside the reader would
mean a slow engine delays the next move arriving, and reading inside the
analyser would mean a quiet game blocks the eval bar.  They share a position
and an event, and nothing else.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

import chess

from .engines import POOL, EngineError, EngineOptions
from .review import describe_pv, eval_text
from .sources import SourceError, chesscom, lichess, parse_game

#: How often the Chess.com poller asks. Two seconds is fast enough to follow a
#: blitz game and slow enough that an hour of watching is ~1,800 requests to
#: an endpoint nobody promised us.
CHESSCOM_INTERVAL = 2.0

#: A dead session is cleaned up this long after its game ends, so the browser
#: has time to notice the result before the session disappears.
LINGER = 300.0

#: A session nobody has asked about for this long is retired. Closing the tab
#: is the normal way to stop watching, and no unload handler is reliable
#: enough to depend on -- so an unwatched session is one that stops itself,
#: rather than a thread and an engine slot leaked until the server restarts.
IDLE = 120.0

#: Sessions are not free (a thread and an engine slot each), and the only
#: reason to have many is a mistake.
MAX_SESSIONS = 8


class LiveError(RuntimeError):
    """A live session that could not be started, with a message to show."""


@dataclass
class LiveState:
    """What the browser is shown. Guarded by the session's lock."""

    moves: list = field(default_factory=list)      # UCI
    fen: str = chess.STARTING_FEN
    last_move: str = ""
    turn: str = "white"
    clocks: dict = field(default_factory=dict)
    finished: bool = False
    result: str = "*"
    white: str = "?"
    black: str = "?"
    status: str = "connecting"
    error: str = ""
    updated: float = 0.0


class LiveSession:
    """One game being followed, with an evaluation kept beside it."""

    def __init__(self, kind: str, *, label: str = "", game_id: str = "",
                 token: str | None = None, movetime: float = 0.6,
                 multipv: int = 3, engine_id: str | None = None,
                 start_fen: str | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.label = label or kind
        self.game_id = game_id
        self.token = token
        self.engine_id = engine_id
        self.options = EngineOptions(multipv=multipv, movetime=movetime)
        #: Where this game starts. Normally the initial position; for a
        #: position you arranged by hand, that arrangement. Everything that
        #: replays moves -- the board, the move list, take-back -- replays from
        #: here, so this is the one place the distinction lives.
        self.start_fen = start_fen or chess.STARTING_FEN

        self.state = LiveState(fen=self.start_fen)
        self.analysis: dict | None = None
        self.created = time.time()
        self.ended_at: float | None = None
        self.last_polled = time.time()

        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "LiveSession":
        readers = {
            "lichess": self._read_lichess,
            "chesscom": self._read_chesscom,
        }
        reader = readers.get(self.kind)
        if reader is not None:
            self._spawn(reader, f"live-read-{self.id}")
        else:
            # Manual and PGN sessions are driven by the browser, so there is
            # nothing to read -- but they still want an eval bar.
            self._set(status="ready")
        self._spawn(self._analyse_loop, f"live-eval-{self.id}")
        return self

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._changed.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # --------------------------------------------------------------- state

    def _set(self, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self.state, key, value)
            self.state.updated = time.time()
            if self.state.finished and self.ended_at is None:
                self.ended_at = time.time()
        self._changed.set()

    def _apply_moves(self, ucis: list[str]) -> bool:
        """Adopt a move list, refusing anything shorter than what we have.

        A truncated or reordered read must never rewind the game: the longest
        prefix wins, always.
        """
        with self._lock:
            if len(ucis) < len(self.state.moves):
                return False
            if ucis == self.state.moves:
                return False

        board = chess.Board(self.start_fen)
        accepted = []
        for uci in ucis:
            try:
                move = board.parse_uci(uci)
            except (ValueError, AssertionError):
                break
            accepted.append(move.uci())
            board.push(move)

        self._set(
            moves=accepted,
            fen=board.fen(),
            last_move=accepted[-1] if accepted else "",
            turn="white" if board.turn == chess.WHITE else "black",
        )
        return True

    def push_move(self, uci: str) -> dict:
        """A move played by hand, for manual sessions."""
        if self.kind not in ("manual", "pgn"):
            raise LiveError(
                "This session follows a real game; moves arrive from the "
                "source rather than from clicks.")
        with self._lock:
            moves = list(self.state.moves)

        board = chess.Board(self.start_fen)
        for existing in moves:
            board.push_uci(existing)
        try:
            move = board.parse_uci(uci)
        except (ValueError, AssertionError) as exc:
            raise LiveError(f"{uci} is not legal here: {exc}") from exc

        moves.append(move.uci())
        self._apply_moves(moves)
        return self.json()

    def undo(self) -> dict:
        if self.kind not in ("manual", "pgn"):
            raise LiveError("Only a manual session can take a move back.")
        with self._lock:
            moves = list(self.state.moves)[:-1]
            self.state.moves = []          # force _apply_moves to accept
        self._apply_moves(moves)
        return self.json()

    def feed_pgn(self, pgn: str) -> dict:
        """A pasted PGN, for the watch-by-paste route."""
        game = parse_game(pgn)
        board = game.board()
        if board.fen() != self.start_fen:
            # Replaying this PGN's moves from a different starting position
            # would either fail on the first move or, worse, succeed and
            # silently build a game nobody played.
            raise LiveError(
                "That PGN starts from a different position than this session. "
                "Start a new session from the PGN instead.")
        ucis = []
        for move in game.mainline_moves():
            if move not in board.legal_moves:
                break
            ucis.append(move.uci())
            board.push(move)

        headers = game.headers
        grew = self._apply_moves(ucis)
        self._set(
            white=headers.get("White", self.state.white),
            black=headers.get("Black", self.state.black),
            result=headers.get("Result", "*"),
            finished=headers.get("Result", "*") != "*",
            status="watching" if not grew else "updated",
        )
        return self.json()

    # -------------------------------------------------------------- readers

    def _read_lichess(self) -> None:
        """Follow the public ndjson stream, reconnecting if it drops."""
        attempts = 0
        while not self._stop.is_set() and attempts < 5:
            try:
                for line in lichess.stream_game(self.game_id, token=self.token):
                    if self._stop.is_set():
                        return
                    attempts = 0            # a good line resets the budget
                    self._consume_lichess(line)
                # Lichess closes the stream when the game ends.
                self._set(status="finished", finished=True)
                return
            except SourceError as exc:
                attempts += 1
                self._set(status="reconnecting", error=str(exc))
                # A dropped stream mid-game is routine; back off a little and
                # pick up where we left off rather than losing the session.
                if self._stop.wait(min(10.0, 2.0 * attempts)):
                    return
        if not self._stop.is_set():
            self._set(status="lost",
                      error="Lost the Lichess stream and could not get it back.")

    def _consume_lichess(self, line: dict) -> None:
        if "players" in line:
            players = line.get("players") or {}
            self._set(
                white=((players.get("white") or {}).get("user") or {})
                .get("name", "Anonymous"),
                black=((players.get("black") or {}).get("user") or {})
                .get("name", "Anonymous"),
                status="watching",
            )
            return

        fen = line.get("fen")
        if not fen:
            return

        # The stream sends positions, not a move list, so the move list is
        # rebuilt by appending: the FEN is authoritative, the moves are for
        # the notation panel.
        with self._lock:
            moves = list(self.state.moves)
        last = line.get("lm")
        if last and (not moves or moves[-1] != last):
            moves.append(last)
            self._apply_moves(moves)
        else:
            board_turn = "white" if " w " in fen else "black"
            self._set(fen=fen, turn=board_turn, last_move=last or "")

        clocks = {}
        if line.get("wc") is not None:
            clocks = {"white": line.get("wc"), "black": line.get("bc")}
        if clocks:
            self._set(clocks=clocks)
        if line.get("status") in lichess.TERMINAL_STATUSES:
            self._set(finished=True, status="finished",
                      result=line.get("winner") and
                      ("1-0" if line["winner"] == "white" else "0-1") or "1/2-1/2")

    def _read_chesscom(self) -> None:
        """Poll the callback endpoint and diff the move list."""
        failures = 0
        while not self._stop.is_set():
            try:
                record = chesscom.game(self.game_id, kind="live")
                failures = 0
            except SourceError as exc:
                failures += 1
                if failures >= 4:
                    self._set(
                        status="lost",
                        error=f"{exc}\nThe moves so far are kept; switch to "
                              "Paste PGN to keep following the game.")
                    return
                self._set(status="retrying", error=str(exc))
                if self._stop.wait(CHESSCOM_INTERVAL * failures):
                    return
                continue

            game = parse_game(record.pgn)
            board = game.board()
            ucis = []
            for move in game.mainline_moves():
                ucis.append(move.uci())
                board.push(move)

            self._apply_moves(ucis)
            self._set(white=record.white, black=record.black,
                      clocks=record.clocks, result=record.result,
                      finished=record.finished,
                      status="finished" if record.finished else "watching",
                      error="")
            if record.finished:
                return
            if self._stop.wait(CHESSCOM_INTERVAL):
                return

    # ------------------------------------------------------------- analysis

    def _analyse_loop(self) -> None:
        """Keep an evaluation next to whatever position is current."""
        while not self._stop.is_set():
            self._changed.wait(timeout=5.0)
            self._changed.clear()
            if self._stop.is_set():
                return

            with self._lock:
                fen = self.state.fen
                ply = len(self.state.moves)
            if not fen:
                continue

            try:
                board = chess.Board(fen)
            except ValueError:
                continue

            try:
                engine = POOL.get(self.engine_id)
                lines = engine.analyse(board, self.options)
            except EngineError as exc:
                with self._lock:
                    self.analysis = {"ply": ply, "error": str(exc), "lines": []}
                continue

            described = []
            for line in lines:
                spelled = describe_pv(board, line.get("pv") or [])
                if spelled["first"] is None and not board.is_game_over():
                    continue
                described.append({
                    "rank": line.get("rank", len(described) + 1),
                    "uci": (spelled["first"] or {}).get("uci"),
                    "san": (spelled["first"] or {}).get("san"),
                    "line": spelled["line"],
                    "cp": line.get("cp"),
                    "mate": line.get("mate"),
                    "text": eval_text(line.get("cp"), line.get("mate")),
                    "depth": line.get("depth", 0),
                })

            top = lines[0]
            with self._lock:
                # A move played while we were thinking makes this answer stale
                # the moment it lands; drop it rather than show it.
                if ply != len(self.state.moves):
                    continue
                self.analysis = {
                    "ply": ply,
                    "fen": fen,
                    "lines": described,
                    "cp": top.get("cp"),
                    "mate": top.get("mate"),
                    "text": eval_text(top.get("cp"), top.get("mate")),
                    "depth": top.get("depth", 0),
                    "whiteFraction": _fraction(top),
                    "error": "",
                }

    # ---------------------------------------------------------------- output

    def positions(self) -> list[dict]:
        """Every position of the game so far, in the shape the board reads.

        The browser needs these to let you scroll back through a game that is
        still being played. Replaying a couple of hundred moves per poll is
        nothing next to the engine call happening beside it, and the
        alternative -- sending only the current position -- means the board
        can only ever show the latest move.
        """
        board = chess.Board(self.start_fen)
        rows = [{"ply": 0, "san": "", "uci": "", "fen": board.fen(),
                 "moveNumber": board.fullmove_number, "color": ""}]
        for ply, uci in enumerate(self.state.moves, start=1):
            try:
                move = board.parse_uci(uci)
            except (ValueError, AssertionError):
                break
            colour = "white" if board.turn == chess.WHITE else "black"
            number = board.fullmove_number
            san = board.san(move)
            board.push(move)
            rows.append({"ply": ply, "san": san, "uci": uci, "fen": board.fen(),
                         "moveNumber": number, "color": colour})
        return rows

    def json(self) -> dict:
        self.last_polled = time.time()
        with self._lock:
            state = self.state
            payload = {
                "id": self.id,
                "kind": self.kind,
                "label": self.label,
                "gameId": self.game_id,
                "startFen": self.start_fen,
                "arranged": self.start_fen != chess.STARTING_FEN,
                "moves": list(state.moves),
                "positions": self.positions(),
                "fen": state.fen,
                "lastMove": state.last_move,
                "turn": state.turn,
                "clocks": dict(state.clocks),
                "finished": state.finished,
                "result": state.result,
                "white": state.white,
                "black": state.black,
                "status": state.status,
                "error": state.error,
                "updated": state.updated,
                "plyCount": len(state.moves),
                "analysis": dict(self.analysis) if self.analysis else None,
                "stale": bool(self.analysis
                              and self.analysis.get("ply") != len(state.moves)),
            }
        return payload


def _fraction(line: dict) -> float:
    from .accuracy import win_percent
    return round(win_percent(line.get("cp"), line.get("mate")) / 100.0, 4)


class LiveManager:
    """Every session this process is running."""

    def __init__(self):
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, **kwargs) -> LiveSession:
        self.sweep()
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise LiveError(
                    f"{MAX_SESSIONS} live sessions are already running. Close "
                    "one before starting another.")

        session = LiveSession(kind, **kwargs).start()
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> LiveSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise LiveError("That live session is gone. Start a new one.")
        return session

    def stop(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.stop()
        return True

    def listing(self) -> list[dict]:
        with self._lock:
            return [session.json() for session in self._sessions.values()]

    def sweep(self) -> None:
        """Retire sessions whose games ended a while ago."""
        now = time.time()
        with self._lock:
            stale = [
                key for key, session in self._sessions.items()
                if (session.ended_at is not None
                    and now - session.ended_at > LINGER)
                or now - session.last_polled > IDLE
            ]
            for key in stale:
                self._sessions.pop(key).stop()

    def close(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.stop()
            self._sessions.clear()


MANAGER = LiveManager()


__all__ = ["LiveError", "LiveManager", "LiveSession", "MANAGER"]
