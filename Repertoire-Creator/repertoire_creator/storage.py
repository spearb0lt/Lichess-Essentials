"""Where a repertoire lives on disk, and how it is read and written.

Local-first, on purpose.  A repertoire is a plain folder of PGN files plus a
small JSON manifest, so it diffs in git, opens in any chess GUI, and survives
this app being deleted::

    repertoires/
      white-italian/
        repertoire.json          manifest: colour, chapter order, Lichess ids
        drill.json               spaced-repetition state (optional)
        chapters/
          0001-main-line.pgn
          0002-two-knights.pgn

Writes go through a temporary file and an atomic replace, because losing a
repertoire to a half-written file during a crash is not an acceptable
failure mode for something a person spends months building.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.pgn

from .model import (
    ChapterMeta,
    RepertoireMeta,
    game_to_pgn,
    move_hash,
    pgn_to_game,
)

MANIFEST = "repertoire.json"
CHAPTERS_DIR = "chapters"
DRILL_FILE = "drill.json"
SETTINGS_FILE = "settings.json"

#: Lichess refuses studies with more than this many chapters, so there is no
#: point letting a repertoire grow past it and only discovering at push time.
MAX_CHAPTERS = 64


class StorageError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_data_dir() -> Path:
    """The repertoires folder: env override, else next to the package."""
    env = os.environ.get("REPERTOIRE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "repertoires").resolve()


def slugify(text: str, fallback: str = "repertoire") -> str:
    normalised = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:60] or fallback


#: Called after every successful write, with a short human label.  This is
#: how git auto-commit learns that something changed without storage having
#: to know that git exists.  Set it with :func:`set_write_observer`.
_WRITE_OBSERVER = None


def set_write_observer(callback) -> None:
    """Register a ``callback(path, label)`` run after each file is written."""
    global _WRITE_OBSERVER
    _WRITE_OBSERVER = callback


def _atomic_write(path: Path, text: str, label: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)
    if _WRITE_OBSERVER is not None:
        try:
            _WRITE_OBSERVER(path, label or path.stem)
        except Exception:
            # A misbehaving observer must never cost you an edit.
            pass


# ---------------------------------------------------------------- settings


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / SETTINGS_FILE


def load_settings(data_dir: Path) -> dict:
    path = settings_path(data_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data_dir: Path, settings: dict) -> None:
    _atomic_write(
        settings_path(data_dir),
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        label="settings",
    )


class Repertoire:
    """One repertoire folder, loaded into memory.

    Chapter games are read lazily and cached; ``save_chapter`` writes the PGN
    back immediately, so the disk is always the source of truth and an editor
    crash costs at most the edit in flight.
    """

    def __init__(self, root: Path, meta: RepertoireMeta):
        self.root = Path(root)
        self.meta = meta
        self._games: dict[str, chess.pgn.Game] = {}

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def load(cls, root: Path) -> "Repertoire":
        root = Path(root)
        manifest = root / MANIFEST
        if not manifest.is_file():
            raise StorageError(f"No repertoire manifest at {manifest}")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise StorageError(f"{manifest} is not valid JSON: {exc}") from exc
        return cls(root, RepertoireMeta.from_json(data))

    @classmethod
    def create(
        cls,
        data_dir: Path,
        name: str,
        color: str = "white",
        description: str = "",
    ) -> "Repertoire":
        if color not in ("white", "black"):
            raise StorageError("Colour must be 'white' or 'black'.")
        data_dir = Path(data_dir)
        slug = slugify(name)
        root = data_dir / slug
        # Two repertoires can share a name; they cannot share a folder.
        suffix = 2
        while root.exists():
            root = data_dir / f"{slug}-{suffix}"
            suffix += 1

        meta = RepertoireMeta(
            slug=root.name,
            name=name.strip() or root.name,
            color=color,
            description=description.strip(),
            created=now_iso(),
            updated=now_iso(),
        )
        repertoire = cls(root, meta)
        (root / CHAPTERS_DIR).mkdir(parents=True, exist_ok=True)
        repertoire.save_manifest()
        return repertoire

    def save_manifest(self) -> None:
        self.meta.updated = now_iso()
        _atomic_write(
            self.root / MANIFEST,
            json.dumps(self.meta.to_json(), indent=2, ensure_ascii=False) + "\n",
            label=self.meta.name,
        )

    def delete(self) -> None:
        """Remove the whole repertoire folder. Definitive."""
        import shutil

        shutil.rmtree(self.root, ignore_errors=False)

    # ------------------------------------------------------------- chapters

    def chapter_path(self, meta: ChapterMeta) -> Path:
        return self.root / CHAPTERS_DIR / meta.file

    def game(self, chapter_id: str) -> chess.pgn.Game:
        """The chapter move tree, read from disk once and then cached."""
        if chapter_id in self._games:
            return self._games[chapter_id]
        meta = self.meta.chapter(chapter_id)
        if meta is None:
            raise StorageError(f"No chapter {chapter_id!r} in {self.meta.slug}")
        path = self.chapter_path(meta)
        if not path.is_file():
            raise StorageError(f"Chapter file is missing: {path}")
        try:
            game = pgn_to_game(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise StorageError(f"{path} could not be parsed: {exc}") from exc
        self._games[chapter_id] = game
        return game

    def save_chapter(self, chapter_id: str, game: chess.pgn.Game | None = None) -> None:
        meta = self.meta.chapter(chapter_id)
        if meta is None:
            raise StorageError(f"No chapter {chapter_id!r} in {self.meta.slug}")
        game = game if game is not None else self.game(chapter_id)
        self._games[chapter_id] = game
        self._apply_headers(meta, game)
        _atomic_write(
            self.chapter_path(meta), game_to_pgn(game) + "\n",
            label=f"{self.meta.name} / {meta.name}",
        )
        self.save_manifest()

    def _apply_headers(self, meta: ChapterMeta, game: chess.pgn.Game) -> None:
        """Keep the PGN tags in step with the manifest.

        ``Event`` is written the way Lichess writes it (``Study: Chapter``)
        so the PDF exporter and any other Lichess-shaped tool reads the names
        back correctly.
        """
        game.headers["Event"] = f"{self.meta.name}: {meta.name}"
        game.headers["StudyName"] = self.meta.name
        game.headers["ChapterName"] = meta.name
        game.headers["Orientation"] = meta.orientation
        game.headers["Site"] = self.meta.lichess_url or "local repertoire"
        game.headers.setdefault("Result", "*")
        for tag in ("Date", "Round", "White", "Black"):
            # Lichess writes these as placeholders; a repertoire has no
            # players or date, and empty placeholders read better than
            # a fake game between two question marks.
            game.headers.pop(tag, None)

    def add_chapter(
        self,
        name: str,
        *,
        orientation: str | None = None,
        game: chess.pgn.Game | None = None,
        start_fen: str | None = None,
    ) -> ChapterMeta:
        if len(self.meta.chapters) >= MAX_CHAPTERS:
            raise StorageError(
                f"A Lichess study holds at most {MAX_CHAPTERS} chapters, and this "
                "repertoire already has that many. Split it into a second "
                "repertoire before adding more."
            )
        if game is None:
            game = chess.pgn.Game()
            if start_fen:
                try:
                    board = chess.Board(start_fen)
                except ValueError as exc:
                    raise StorageError(f"Bad starting FEN: {exc}") from exc
                game.setup(board)

        index = len(self.meta.chapters) + 1
        meta = ChapterMeta(
            id=uuid.uuid4().hex[:10],
            file=f"{index:04d}-{slugify(name, 'chapter')}.pgn",
            name=name.strip() or f"Chapter {index}",
            orientation=orientation or self.meta.color,
        )
        # A filename collision would silently overwrite a chapter, so make it
        # unique rather than trusting the index to be free.
        existing = {c.file for c in self.meta.chapters}
        while meta.file in existing:
            meta.file = f"{index:04d}-{meta.id}.pgn"
            index += 1

        self.meta.chapters.append(meta)
        self._games[meta.id] = game
        self.save_chapter(meta.id, game)
        return meta

    def rename_chapter(self, chapter_id: str, name: str) -> ChapterMeta:
        meta = self.meta.chapter(chapter_id)
        if meta is None:
            raise StorageError(f"No chapter {chapter_id!r}")
        meta.name = name.strip() or meta.name
        self.save_chapter(chapter_id)
        return meta

    def set_orientation(self, chapter_id: str, orientation: str) -> ChapterMeta:
        if orientation not in ("white", "black"):
            raise StorageError("Orientation must be 'white' or 'black'.")
        meta = self.meta.chapter(chapter_id)
        if meta is None:
            raise StorageError(f"No chapter {chapter_id!r}")
        meta.orientation = orientation
        self.save_chapter(chapter_id)
        return meta

    def delete_chapter(self, chapter_id: str) -> None:
        meta = self.meta.chapter(chapter_id)
        if meta is None:
            raise StorageError(f"No chapter {chapter_id!r}")
        path = self.chapter_path(meta)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Could not delete {path}: {exc}") from exc
        self.meta.chapters = [c for c in self.meta.chapters if c.id != chapter_id]
        self._games.pop(chapter_id, None)
        self.save_manifest()

    def move_chapter(self, chapter_id: str, offset: int) -> None:
        """Reorder chapters locally. Lichess has no reorder API -- see README."""
        ids = [c.id for c in self.meta.chapters]
        if chapter_id not in ids:
            raise StorageError(f"No chapter {chapter_id!r}")
        index = ids.index(chapter_id)
        target = max(0, min(len(ids) - 1, index + offset))
        if target == index:
            return
        chapters = self.meta.chapters
        chapters.insert(target, chapters.pop(index))
        self.save_manifest()

    # ---------------------------------------------------------------- views

    def chapter_hash(self, chapter_id: str) -> str:
        return move_hash(self.game(chapter_id))

    def is_dirty(self, chapter_id: str) -> bool:
        """True when the chapter differs from what was last pushed."""
        meta = self.meta.chapter(chapter_id)
        if meta is None or not meta.lichess_chapter_id:
            return True
        return meta.pushed_hash != self.chapter_hash(chapter_id)

    def study_pgn(self, chapter_ids=None) -> str:
        """The whole repertoire as one multi-game PGN, Lichess study style."""
        wanted = list(chapter_ids) if chapter_ids else [c.id for c in self.meta.chapters]
        blocks = []
        for chapter_id in wanted:
            meta = self.meta.chapter(chapter_id)
            if meta is None:
                continue
            game = self.game(chapter_id)
            self._apply_headers(meta, game)
            blocks.append(game_to_pgn(game).strip())
        if not blocks:
            raise StorageError("This repertoire has no chapters yet.")
        return "\n\n\n".join(blocks) + "\n"

    # ---------------------------------------------------------------- drill

    def drill_path(self) -> Path:
        return self.root / DRILL_FILE

    def load_drill(self) -> dict:
        path = self.drill_path()
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def save_drill(self, state: dict) -> None:
        _atomic_write(
            self.drill_path(),
            json.dumps(state, indent=1, ensure_ascii=False) + "\n",
            label=f"{self.meta.name} drill progress",
        )


# --------------------------------------------------------------- collection


def list_repertoires(data_dir: Path) -> list:
    """Every readable repertoire in the data folder, newest activity first."""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return []
    found = []
    for child in sorted(data_dir.iterdir()):
        if not child.is_dir() or not (child / MANIFEST).is_file():
            continue
        try:
            found.append(Repertoire.load(child))
        except StorageError:
            continue
    found.sort(key=lambda r: r.meta.updated or "", reverse=True)
    return found


def open_repertoire(data_dir: Path, slug: str) -> Repertoire:
    root = Path(data_dir) / slug
    # Guard against a slug like "../../etc" arriving from the browser.
    if root.resolve().parent != Path(data_dir).resolve():
        raise StorageError(f"Bad repertoire name: {slug!r}")
    return Repertoire.load(root)
