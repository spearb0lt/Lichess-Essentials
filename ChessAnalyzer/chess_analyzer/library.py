"""What stays on disk: the games you imported and the analysis they cost.

A full review is minutes of CPU.  Paying that twice for the same game because
you closed the tab is the single most annoying thing an analysis tool can do,
so both halves are kept.

*Games* live one JSON file each under ``games/``, holding the record, the PGN
and the finished review.  One file per game rather than one index: a
half-written index loses the library, a half-written game file loses that
game, and the directory listing is the index.

*Positions* live in a shared cache keyed by FEN **and** the engine settings
that produced the answer, because a 0.1-second answer and a 20-depth answer
are not interchangeable and silently reusing one for the other would make a
review lie about its own depth.  The cache is what makes the second review of
a game with the same settings instant, and what lets the live eval bar and a
running review share work rather than duplicate it.

Writes go through a temporary file and a rename, so a crash mid-save leaves
the previous version rather than a truncated one.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "games"

#: Positions kept in the shared cache. At roughly 300 bytes each this is a
#: few tens of megabytes -- large enough to hold hundreds of reviewed games,
#: small enough not to become a thing you have to manage.
CACHE_LIMIT = 120_000


class LibraryError(RuntimeError):
    """A save or load that could not be done, with a message to show."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _safe_id(game_id: str) -> str:
    """A game id that cannot escape the library directory."""
    cleaned = "".join(
        character for character in (game_id or "")
        if character.isalnum() or character in "-_"
    )
    if not cleaned:
        raise LibraryError("That game has no usable id.")
    return cleaned[:80]


class Library:
    """The games directory, as an object so tests can point it elsewhere."""

    def __init__(self, directory: Path | str | None = None):
        self.dir = Path(directory or DEFAULT_DIR).expanduser().resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- games

    def path_for(self, game_id: str) -> Path:
        return self.dir / f"{_safe_id(game_id)}.json"

    def save(self, record, review: dict | None = None) -> dict:
        """Write a game, keeping any review already stored for it."""
        path = self.path_for(record.id)
        existing = self.load(record.id) or {}
        payload = {
            "record": record.json(),
            "pgn": record.pgn,
            "review": review if review is not None else existing.get("review"),
            "savedAt": time.time(),
        }
        with self._lock:
            _atomic_write(path, json.dumps(payload, separators=(",", ":")))
        return payload

    def save_review(self, game_id: str, review: dict) -> None:
        stored = self.load(game_id)
        if stored is None:
            raise LibraryError("That game is not in the library.")
        stored["review"] = review
        stored["savedAt"] = time.time()
        with self._lock:
            _atomic_write(self.path_for(game_id),
                          json.dumps(stored, separators=(",", ":")))

    def load(self, game_id: str) -> dict | None:
        path = self.path_for(game_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def delete(self, game_id: str) -> bool:
        path = self.path_for(game_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def listing(self, *, limit: int = 200) -> list[dict]:
        """Every saved game, newest first, with just enough to draw a row."""
        rows = []
        for path in self.dir.glob("*.json"):
            if path.name.startswith("."):
                continue                    # the position cache, not a game
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            record = payload.get("record")
            if not record or not record.get("id"):
                continue                    # not a game file
            review = payload.get("review") or {}
            summary = review.get("summary") or {}
            rows.append({
                **record,
                "savedAt": payload.get("savedAt", 0),
                "reviewed": bool(review),
                "accuracy": {
                    "white": (summary.get("white") or {}).get("accuracy"),
                    "black": (summary.get("black") or {}).get("accuracy"),
                },
                "engine": (review.get("engine") or {}).get("name", ""),
            })
        rows.sort(key=lambda row: -(row.get("savedAt") or 0))
        return rows[:limit]

    # ------------------------------------------------------- position cache

    @property
    def cache_path(self) -> Path:
        # The leading dot matters: game files are named from an id that has
        # had every character but letters, digits, dash and underscore
        # stripped out, so no game can ever be written to this name. Without
        # it, a game whose id was "positions" would overwrite the cache -- and
        # the cache would show up in the library as a game with no players.
        return self.dir / ".positions.json"

    def load_cache(self) -> "EvalCache":
        return EvalCache(self.cache_path)


class EvalCache:
    """Analysed positions, keyed by position *and* engine settings."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._data: dict[str, list] = {}
        self._lock = threading.Lock()
        self._dirty = False
        self.hits = 0
        self.misses = 0
        self._read()

    def _read(self) -> None:
        if self.path and self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}

    @staticmethod
    def key(fen: str, settings_key: str) -> str:
        return f"{fen}|{settings_key}"

    def get(self, fen: str, settings_key: str) -> list | None:
        with self._lock:
            found = self._data.get(self.key(fen, settings_key))
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    def put(self, fen: str, settings_key: str, lines: list) -> None:
        with self._lock:
            if len(self._data) >= CACHE_LIMIT:
                # Oldest-inserted first: dicts keep insertion order, and
                # dropping the first tenth is cheaper and less surprising than
                # tracking access times for something this disposable.
                for stale in list(self._data)[:CACHE_LIMIT // 10]:
                    self._data.pop(stale, None)
            self._data[self.key(fen, settings_key)] = lines
            self._dirty = True

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        with self._lock:
            payload = json.dumps(self._data, separators=(",", ":"))
            self._dirty = False
        try:
            _atomic_write(self.path, payload)
        except OSError:
            pass

    def stats(self) -> dict:
        return {"positions": len(self._data), "hits": self.hits,
                "misses": self.misses}


# ------------------------------------------------------------------ settings


SETTINGS_FILE = "settings.json"


def load_settings(directory: Path) -> dict:
    path = Path(directory) / SETTINGS_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(directory: Path, settings: dict) -> None:
    """Persist app settings. Never stores a token -- see the server docstring."""
    trimmed = {key: value for key, value in settings.items()
               if key not in ("token", "lichessToken")}
    _atomic_write(Path(directory) / SETTINGS_FILE,
                  json.dumps(trimmed, indent=2))


__all__ = [
    "CACHE_LIMIT",
    "DEFAULT_DIR",
    "EvalCache",
    "Library",
    "LibraryError",
    "load_settings",
    "save_settings",
]
