"""Where a scout lives on disk.

Local-first, same as the sibling apps, and for the same reason: a scouting
report is worth keeping.  You play the same people again, and re-downloading
four hundred of someone's games to answer a question you asked last week is
both slow and rude to Lichess.

::

    prep/
      settings.json                  defaults: book source, filters
      games/
        lichess-drnykterstein.json   their games, compact, reusable
      scouts/
        lichess-drnykterstein.json   the last report and what produced it
      books/
        study-i7hMEq7h.pgn           a study fetched as a reference book

Games are cached as UCI move lists rather than PGN.  A move list is a tenth
the size, needs no re-parsing, and is all the tree ever looks at; the
identifying tags are kept beside it so a row still reads like a game.

Writes go through a temporary file and an atomic replace.  A half-written
cache is worse than no cache, because it fails on the next read rather than
on this write.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

GAMES_DIR = "games"
SCOUTS_DIR = "scouts"
BOOKS_DIR = "books"
SETTINGS_FILE = "settings.json"

SITES = ("lichess", "chesscom")


class StoreError(RuntimeError):
    """Something on disk is missing or unreadable, with a message to show."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_data_dir() -> Path:
    """The prep folder: env override, else next to the package."""
    env = os.environ.get("PREPPER_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "prep").resolve()


def slugify(text: str, fallback: str = "item") -> str:
    normalised = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:60] or fallback


def player_key(site: str, username: str) -> str:
    """The one id a player is filed under, everywhere."""
    site = (site or "").strip().lower()
    if site not in SITES:
        raise StoreError(f"Unknown site {site!r}. Use one of: {', '.join(SITES)}")

    raw = (username or "").strip().lstrip("@")
    if not raw:
        raise StoreError("Give a username.")
    name = slugify(raw.lower(), "")
    if not name:
        # Every character was dropped by slugify -- a name of punctuation, or
        # of a script it cannot transliterate. Better to say so than to file
        # everyone like that under one shared key.
        raise StoreError(f"{username!r} has no characters usable in a filename.")
    return f"{site}-{name}"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Store:
    """The prep folder, and every read and write that touches it."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- paths

    def games_path(self, key: str) -> Path:
        return self.root / GAMES_DIR / f"{key}.json"

    def scout_path(self, key: str) -> Path:
        return self.root / SCOUTS_DIR / f"{key}.json"

    def book_path(self, name: str) -> Path:
        return self.root / BOOKS_DIR / f"{slugify(name, 'book')}.pgn"

    # ------------------------------------------------------------- games

    def load_games(self, key: str) -> dict | None:
        """The cached games for a player, or None if never fetched."""
        return _read_json(self.games_path(key))

    def save_games(self, key: str, payload: dict) -> None:
        atomic_write(self.games_path(key),
                     json.dumps(payload, separators=(",", ":")))

    def forget_games(self, key: str) -> None:
        self.games_path(key).unlink(missing_ok=True)

    # ------------------------------------------------------------ scouts

    def load_scout(self, key: str) -> dict | None:
        return _read_json(self.scout_path(key))

    def save_scout(self, key: str, payload: dict) -> None:
        atomic_write(self.scout_path(key), json.dumps(payload, indent=1))

    def delete_scout(self, key: str) -> None:
        """Forget a player entirely -- the report and the cached games."""
        self.scout_path(key).unlink(missing_ok=True)
        self.forget_games(key)

    def list_scouts(self) -> list[dict]:
        """Everyone scouted, newest first, as rows for the sidebar."""
        folder = self.root / SCOUTS_DIR
        rows = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            data = _read_json(path)
            if not data:
                continue
            summary = data.get("summary") or {}
            rows.append({
                "key": path.stem,
                "site": data.get("site", ""),
                "username": data.get("username", ""),
                "games": summary.get("games", 0),
                "scoutedAt": data.get("scoutedAt", ""),
                "book": (data.get("book") or {}).get("label", ""),
            })
        rows.sort(key=lambda row: row.get("scoutedAt", ""), reverse=True)
        return rows

    # ------------------------------------------------------------- books

    def load_book_pgn(self, name: str) -> str | None:
        path = self.book_path(name)
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else None
        except OSError:
            return None

    def save_book_pgn(self, name: str, pgn: str) -> Path:
        path = self.book_path(name)
        atomic_write(path, pgn)
        return path

    # ---------------------------------------------------------- settings

    def load_settings(self) -> dict:
        return _read_json(self.root / SETTINGS_FILE) or {}

    def save_settings(self, settings: dict) -> None:
        atomic_write(self.root / SETTINGS_FILE, json.dumps(settings, indent=1))


__all__ = [
    "SITES",
    "Store",
    "StoreError",
    "atomic_write",
    "default_data_dir",
    "now_iso",
    "player_key",
    "slugify",
]
