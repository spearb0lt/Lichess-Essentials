"""Universal mode: one book of everything you have ever written down.

Chapters are a filing system, and a filing system is the wrong shape for the
question you actually ask at the board -- *given this position, what do I
play?*  That question does not care which chapter the answer was filed in, or
by which move order you arrived.

So the book is keyed by position, not by line.  It is built from two sources:

* **recordings** -- sequences you played and saved in universal mode, stored
  as ordinary PGN files, one game each
* **every chapter of every repertoire** -- so the book answers from your real
  preparation, not only from what you happened to record here

Both are folded into ``{position: [moves you have played from it]}``.  Because
the key is the position, two move orders reaching the same place give the same
answer automatically, which is the whole point.

**What counts as a gap.**  The book has no notion of colour -- a recording is
just moves, and asking you to tag every one would defeat the purpose.  So a
gap is defined structurally and needs no colour at all: you are *in* the book
when the position you just came from was in it, and it is a gap when you are
in the book and the position you have reached has nothing recorded from it.
Reach a position the book has never seen by any route and that is not a gap,
it is simply outside your preparation, and the app says so differently.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.pgn

from .model import format_path, game_to_pgn, path_of, pgn_to_game, split_comment
from .storage import _atomic_write, list_repertoires, slugify

UNIVERSAL_DIR = "universal"
RECORDINGS_DIR = "recordings"
INDEX_FILE = "universal.json"

#: A tree built out of a position-keyed book can branch forever through
#: transpositions; stop well before that becomes a problem.
EXPORT_MAX_PLIES = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def position_key(board: chess.Board) -> str:
    """Identity of a position: the FEN without the move counters."""
    return board.epd()


# ------------------------------------------------------------- recordings


@dataclass
class Recording:
    id: str
    file: str
    name: str
    created: str = ""
    updated: str = ""
    note: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "Recording":
        return cls(
            id=data["id"], file=data["file"], name=data.get("name", ""),
            created=data.get("created", ""), updated=data.get("updated", ""),
            note=data.get("note", ""),
        )

    def to_json(self) -> dict:
        return {
            "id": self.id, "file": self.file, "name": self.name,
            "created": self.created, "updated": self.updated, "note": self.note,
        }


class UniversalStore:
    """The recordings folder and its index."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / UNIVERSAL_DIR
        self.recordings: list[Recording] = []
        self._games: dict[str, chess.pgn.Game] = {}
        self._load()

    # ------------------------------------------------------------ on disk

    def _index_path(self) -> Path:
        return self.root / INDEX_FILE

    def _load(self) -> None:
        path = self._index_path()
        if not path.is_file():
            self.recordings = []
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.recordings = []
            return
        self.recordings = [Recording.from_json(r) for r in data.get("recordings", [])]

    def save_index(self) -> None:
        _atomic_write(
            self._index_path(),
            json.dumps(
                {"recordings": [r.to_json() for r in self.recordings]},
                indent=2, ensure_ascii=False,
            ) + "\n",
            label="universal book",
        )

    def recording(self, recording_id: str) -> Recording | None:
        for item in self.recordings:
            if item.id == recording_id:
                return item
        return None

    def path_for(self, recording: Recording) -> Path:
        return self.root / RECORDINGS_DIR / recording.file

    def game(self, recording_id: str) -> chess.pgn.Game:
        if recording_id in self._games:
            return self._games[recording_id]
        recording = self.recording(recording_id)
        if recording is None:
            raise LookupError(f"No recording {recording_id!r}")
        path = self.path_for(recording)
        if not path.is_file():
            raise LookupError(f"Recording file is missing: {path}")
        game = pgn_to_game(path.read_text(encoding="utf-8"))
        self._games[recording_id] = game
        return game

    def save(self, recording_id: str, game: chess.pgn.Game | None = None) -> None:
        recording = self.recording(recording_id)
        if recording is None:
            raise LookupError(f"No recording {recording_id!r}")
        game = game if game is not None else self.game(recording_id)
        self._games[recording_id] = game

        game.headers["Event"] = f"Universal: {recording.name}"
        game.headers["StudyName"] = "Universal"
        game.headers["ChapterName"] = recording.name
        game.headers.setdefault("Result", "*")
        for tag in ("Date", "Round", "White", "Black", "Site"):
            game.headers.pop(tag, None)

        recording.updated = now_iso()
        _atomic_write(
            self.path_for(recording), game_to_pgn(game) + "\n",
            label=f"universal / {recording.name}",
        )
        self.save_index()

    def add(self, name: str, *, game: chess.pgn.Game | None = None,
            start_fen: str | None = None) -> Recording:
        if game is None:
            game = chess.pgn.Game()
            if start_fen:
                game.setup(chess.Board(start_fen))

        index = len(self.recordings) + 1
        recording = Recording(
            id=uuid.uuid4().hex[:10],
            file=f"{index:04d}-{slugify(name, 'recording')}.pgn",
            name=name.strip() or f"Recording {index}",
            created=now_iso(),
            updated=now_iso(),
        )
        existing = {r.file for r in self.recordings}
        while recording.file in existing:
            recording.file = f"{index:04d}-{recording.id}.pgn"
            index += 1

        self.recordings.append(recording)
        self._games[recording.id] = game
        self.save(recording.id, game)
        return recording

    def rename(self, recording_id: str, name: str) -> Recording:
        recording = self.recording(recording_id)
        if recording is None:
            raise LookupError(f"No recording {recording_id!r}")
        recording.name = name.strip() or recording.name
        self.save(recording_id)
        return recording

    def delete(self, recording_id: str) -> None:
        recording = self.recording(recording_id)
        if recording is None:
            raise LookupError(f"No recording {recording_id!r}")
        try:
            self.path_for(recording).unlink(missing_ok=True)
        except OSError:
            pass
        self.recordings = [r for r in self.recordings if r.id != recording_id]
        self._games.pop(recording_id, None)
        self.save_index()

    def all_pgn(self) -> str:
        blocks = []
        for recording in self.recordings:
            try:
                blocks.append(game_to_pgn(self.game(recording.id)).strip())
            except LookupError:
                continue
        if not blocks:
            raise LookupError("There are no recordings yet.")
        return "\n\n\n".join(blocks) + "\n"


# -------------------------------------------------------------------- book


@dataclass
class BookMove:
    uci: str
    san: str
    sources: list = field(default_factory=list)   # [{kind, name, chapter}]

    def to_json(self) -> dict:
        return {"uci": self.uci, "san": self.san, "sources": self.sources}


def build_book(data_dir: Path, *, include_chapters: bool = True) -> dict:
    """Fold every recording and every chapter into one position-keyed book."""
    book: dict[str, dict[str, BookMove]] = {}

    def absorb(game: chess.pgn.Game, source: dict) -> None:
        def walk(node, board):
            for child in node.variations:
                key = position_key(board)
                san = board.san(child.move)
                uci = child.move.uci()
                slot = book.setdefault(key, {})
                entry = slot.get(uci)
                if entry is None:
                    entry = BookMove(uci=uci, san=san)
                    slot[uci] = entry
                if source not in entry.sources:
                    entry.sources.append(source)
                after = board.copy(stack=False)
                after.push(child.move)
                walk(child, after)

        walk(game, game.board())

    store = UniversalStore(data_dir)
    for recording in store.recordings:
        try:
            game = store.game(recording.id)
        except (LookupError, ValueError):
            continue
        absorb(game, {"kind": "recording", "name": recording.name,
                      "id": recording.id})

    if include_chapters:
        for repertoire in list_repertoires(data_dir):
            for meta in repertoire.meta.chapters:
                try:
                    game = repertoire.game(meta.id)
                except Exception:
                    continue
                absorb(game, {
                    "kind": "chapter",
                    "name": f"{repertoire.meta.name} / {meta.name}",
                    "id": repertoire.meta.slug,
                    "chapter": meta.id,
                    "color": repertoire.meta.color,
                })

    return book


def lookup(book: dict, fen: str, previous_fen: str | None = None) -> dict:
    """What the book says about one position.

    ``previous_fen`` is what makes gap detection meaningful: without knowing
    where you came from, a position with nothing recorded could equally be a
    hole in your preparation or a position you never intended to prepare.
    """
    board = chess.Board(fen)
    key = position_key(board)
    slot = book.get(key) or {}

    came_from_book = False
    if previous_fen:
        try:
            came_from_book = position_key(chess.Board(previous_fen)) in book
        except ValueError:
            came_from_book = False

    if slot:
        status = "known"
    elif came_from_book:
        status = "gap"
    else:
        status = "outside"

    moves = sorted(slot.values(), key=lambda m: (-len(m.sources), m.san))
    return {
        "status": status,
        "fen": fen,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "moves": [m.to_json() for m in moves],
        "inBook": bool(slot),
        "cameFromBook": came_from_book,
        "gameOver": board.is_game_over(),
    }


def book_stats(book: dict) -> dict:
    positions = len(book)
    moves = sum(len(slot) for slot in book.values())
    branching = [len(slot) for slot in book.values() if len(slot) > 1]
    return {
        "positions": positions,
        "moves": moves,
        "branchPoints": len(branching),
    }


def gaps_in_recording(game: chess.pgn.Game, book: dict) -> list:
    """Leaves of a recording that the book cannot continue.

    A recording that simply stops is the commonest source of a gap, and
    listing them is the fastest way to see where the book runs out.
    """
    found = []

    def walk(node, board):
        if not node.variations and not board.is_game_over():
            key = position_key(board)
            if not book.get(key):
                found.append({
                    "path": format_path(path_of(node)),
                    "fen": board.fen(),
                    "ply": len(path_of(node)),
                    "turn": "white" if board.turn == chess.WHITE else "black",
                })
        for child in node.variations:
            after = board.copy(stack=False)
            after.push(child.move)
            walk(child, after)

    walk(game, game.board())
    return found


# ------------------------------------------------------------------ export


def export_games(store: UniversalStore, *, max_chapters: int = 64) -> list:
    """Merge the recordings into one tree per opening move.

    Grouping by first move is what keeps the export inside Lichess's chapter
    limit while still being navigable: a chapter per opening, each holding
    every recorded continuation of it, merged so a line recorded twice
    appears once.

    Only recordings are exported. Chapters that came from a repertoire are
    already publishable from that repertoire, and sending them again would
    put two copies of the same prep on Lichess.
    """
    from .editing import merge_into

    groups: dict[str, chess.pgn.Game] = {}
    labels: dict[str, str] = {}

    for recording in store.recordings:
        try:
            game = store.game(recording.id)
        except (LookupError, ValueError):
            continue
        if not game.variations:
            continue

        start = game.board()
        for child in game.variations:
            san = start.san(child.move)
            key = f"{start.epd()}|{child.move.uci()}"
            target = groups.get(key)
            if target is None:
                target = chess.pgn.Game()
                if start.fen() != chess.STARTING_FEN:
                    target.setup(start)
                groups[key] = target
                labels[key] = san

            # Graft this recording's branch onto the group's tree. A shim
            # node lets merge_into work on the child rather than the root.
            shim = chess.pgn.Game()
            if start.fen() != chess.STARTING_FEN:
                shim.setup(start)
            shim.variations = [child]
            merge_into(target, shim)

    if len(groups) > max_chapters:
        raise ValueError(
            f"The book would need {len(groups)} chapters (one per opening move) "
            f"and a Lichess study holds {max_chapters}. Delete or merge some "
            "recordings first."
        )

    out = []
    for key, game in groups.items():
        out.append((labels[key], game))
    out.sort(key=lambda pair: pair[0])
    return out
