"""Where a history lives on disk.

The expensive thing in this app is not the games, it is the **reviews**: a few
hundred games is hours of engine time the first time and nothing at all the
second, so the layout is built around never doing that work twice.

::

    history/
      settings.json              defaults, remembered
      games/
        lichess-you.json         the games of one dataset, PGN included
      reviews/
        lichess-Wi8IPxc3.json    one review per game -- the expensive part
      reports/
        lichess-you.json         the finished aggregation
      data/
        positions.json           the engine's position cache

Reviews are filed **by game id, not by dataset**, on purpose.  The same game
can turn up in a Lichess pull, in a PGN you exported, and in ChessAnalyzer's
library; keying by game means it is reviewed once whichever door it came
through, and re-running a report after adding fifty new games only costs the
fifty.

Writes go through a temporary file and an atomic replace.  A half-written
review is worse than a missing one, because a missing one is simply redone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

GAMES_DIR = "games"
REVIEWS_DIR = "reviews"
REPORTS_DIR = "reports"
DATA_DIR = "data"
SETTINGS_FILE = "settings.json"
CACHE_FILE = "positions.json"


class StoreError(RuntimeError):
    """Something on disk is missing or unreadable, with a message to show."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_data_dir() -> Path:
    """The history folder: env override, else next to the package."""
    env = os.environ.get("WEAKNESS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "history").resolve()


def slugify(text: str, fallback: str = "item") -> str:
    normalised = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:70] or fallback


def digest(text: str) -> str:
    """A stable short id for a blob of text, insensitive to whitespace."""
    normalised = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]


def safe_name(value: str) -> str:
    """A filename that cannot escape its folder, whatever the id looked like."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-.")
    return cleaned[:100] or "unnamed"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Store:
    """The history folder, and every read and write that touches it."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- paths

    def games_path(self, key: str) -> Path:
        return self.root / GAMES_DIR / f"{safe_name(key)}.json"

    def review_path(self, game_id: str) -> Path:
        return self.root / REVIEWS_DIR / f"{safe_name(game_id)}.json"

    def report_path(self, key: str) -> Path:
        return self.root / REPORTS_DIR / f"{safe_name(key)}.json"

    def cache_path(self) -> Path:
        return self.root / DATA_DIR / CACHE_FILE

    # -------------------------------------------------------------- games

    def load_games(self, key: str):
        return _read_json(self.games_path(key))

    def save_games(self, key: str, payload: dict) -> None:
        atomic_write(self.games_path(key),
                     json.dumps(payload, separators=(",", ":")))

    def forget_games(self, key: str) -> None:
        self.games_path(key).unlink(missing_ok=True)

    # ------------------------------------------------------------ reviews

    def has_review(self, game_id: str) -> bool:
        return self.review_path(game_id).is_file()

    def load_review(self, game_id: str):
        return _read_json(self.review_path(game_id))

    def save_review(self, game_id: str, review: dict) -> None:
        atomic_write(self.review_path(game_id),
                     json.dumps(review, separators=(",", ":")))

    def review_ids(self) -> set:
        folder = self.root / REVIEWS_DIR
        if not folder.is_dir():
            return set()
        return {path.stem for path in folder.glob("*.json")}

    def review_count(self) -> int:
        return len(self.review_ids())

    # ------------------------------------------------------------ reports

    def load_report(self, key: str):
        return _read_json(self.report_path(key))

    def save_report(self, key: str, report: dict) -> None:
        atomic_write(self.report_path(key), json.dumps(report, indent=1))

    def delete_report(self, key: str) -> None:
        """Forget a dataset. Reviews are kept -- they are the expensive part."""
        self.report_path(key).unlink(missing_ok=True)
        self.forget_games(key)

    def list_reports(self) -> list:
        """Every report, newest first, as rows for the sidebar."""
        folder = self.root / REPORTS_DIR
        rows = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            data = _read_json(path)
            if not data:
                continue
            summary = data.get("summary") or {}
            rows.append({
                "key": path.stem,
                "label": data.get("label", path.stem),
                "source": data.get("source", ""),
                "games": summary.get("games", 0),
                "reviewed": summary.get("reviewed", 0),
                "builtAt": data.get("builtAt", ""),
            })
        rows.sort(key=lambda row: row.get("builtAt", ""), reverse=True)
        return rows

    # ---------------------------------------------------------- settings

    def load_settings(self) -> dict:
        return _read_json(self.root / SETTINGS_FILE) or {}

    def save_settings(self, settings: dict) -> None:
        atomic_write(self.root / SETTINGS_FILE, json.dumps(settings, indent=1))


__all__ = [
    "Store",
    "StoreError",
    "atomic_write",
    "default_data_dir",
    "digest",
    "now_iso",
    "safe_name",
    "slugify",
]
