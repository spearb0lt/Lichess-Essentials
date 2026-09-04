"""Your preparation, folded into one book keyed by position.

Everything this app says about coverage is measured against this object, and
it is deliberately the same idea as Repertoire-Creator's universal mode: a
book is ``{position: [moves you play from it]}``, never a list of lines.
Keying on the position is what makes a scout survive move-order tricks, which
is most of what scouting is for.  If you meet the Sicilian by 1.e4 c5 2.Nf3
and your opponent's pet order is 1.e4 c5 2.Nf3 by transposition from a
Nimzowitsch, a line-keyed book says "not covered" and is simply wrong.

Three sources, and you can use any combination of them:

**A Repertoire-Creator repertoire.**  Read straight off disk.  That folder is
plain PGN plus a small JSON manifest -- the sibling app's README promises as
much -- so this reads the files rather than importing the app, and the two
never have to be installed together.  Only moves made by the side the
repertoire is *for* are recorded: a white repertoire lists Black's tries too,
and counting those as your own answers would report coverage you do not have.

**A Lichess study.**  Fetched as PGN.  With the sibling exporter installed,
private studies work through its chapter-by-chapter fallback; without it,
public studies still work through the ordinary endpoint.

**Your own games.**  What you actually play, as opposed to what you wrote
down.  Only your moves are recorded, and the count of how often you played
each one is kept -- so the book knows the difference between your main move
and something you tried once.

Colour is never stored, because it does not need to be: a move recorded from
a position where White is to move *is* a white move.  Coverage asks about one
colour at a time and simply looks at whose turn it is.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn
import requests

from .bridge import optional
from .fetch import USER_AGENT, FetchError, fetch_games

#: Preparation past here is not preparation, it is a game.  Everything this
#: app measures happens in the opening, and an unbounded walk through a book
#: built from four hundred games is slow for no benefit.
MAX_BOOK_PLY = 40

COLORS = ("white", "black", "both", "auto")


class BookError(RuntimeError):
    """A book source could not be read, with a message worth showing."""


@dataclass
class BookMove:
    """One move you play from one position."""

    uci: str
    san: str
    count: int = 0                       # times seen; games, for the games source
    sources: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"uci": self.uci, "san": self.san, "count": self.count,
                "sources": self.sources}


@dataclass
class Book:
    """Everything you play, keyed by position."""

    moves: dict = field(default_factory=dict)      # epd -> {uci: BookMove}
    sources: list = field(default_factory=list)    # [{kind, label, moves}]

    @property
    def label(self) -> str:
        return " + ".join(source["label"] for source in self.sources) or "no book"

    def __bool__(self) -> bool:
        return bool(self.moves)

    def at(self, epd: str) -> dict:
        """``{uci: BookMove}`` for this position; empty when you have nothing."""
        return self.moves.get(epd, {})

    def has(self, epd: str) -> bool:
        return bool(self.moves.get(epd))

    def stats(self) -> dict:
        positions = len(self.moves)
        moves = sum(len(slot) for slot in self.moves.values())
        return {
            "positions": positions,
            "moves": moves,
            "branchPoints": sum(1 for slot in self.moves.values() if len(slot) > 1),
            "label": self.label,
            "sources": self.sources,
        }

    # ----------------------------------------------------------- building

    def _add(self, board: chess.Board, move: chess.Move, source: str,
             count: int = 1) -> None:
        key = board.epd()
        slot = self.moves.setdefault(key, {})
        entry = slot.get(move.uci())
        if entry is None:
            entry = BookMove(uci=move.uci(), san=board.san(move))
            slot[move.uci()] = entry
        entry.count += count
        if source not in entry.sources:
            entry.sources.append(source)

    def absorb_game(self, game: chess.pgn.Game, source: str, *,
                    color: str = "both", max_ply: int = MAX_BOOK_PLY) -> int:
        """Every variation of a PGN game tree, filtered to one side's moves.

        ``color`` is "white", "black" or "both".  Recursion is over the whole
        tree, not the mainline, because a repertoire's sidelines are exactly
        the alternatives worth having.
        """
        wanted = None
        if color == "white":
            wanted = chess.WHITE
        elif color == "black":
            wanted = chess.BLACK

        added = 0

        def walk(node, board, ply):
            nonlocal added
            if ply >= max_ply:
                return
            for child in node.variations:
                if child.move is None or child.move not in board.legal_moves:
                    continue
                if wanted is None or board.turn == wanted:
                    self._add(board, child.move, source)
                    added += 1
                after = board.copy(stack=False)
                after.push(child.move)
                walk(child, after, ply + 1)

        walk(game, game.board(), 0)
        return added

    def absorb_uci_line(self, ucis, source: str, *, color: str,
                        max_ply: int = MAX_BOOK_PLY) -> int:
        """One played game, recording only the moves made by ``color``."""
        wanted = chess.WHITE if color == "white" else chess.BLACK
        board = chess.Board()
        added = 0
        for ply, uci in enumerate(ucis):
            if ply >= max_ply:
                break
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                break
            if move not in board.legal_moves:
                break
            if board.turn == wanted:
                self._add(board, move, source)
                added += 1
            board.push(move)
        return added


# ------------------------------------------------------- repertoire folder


def default_repertoire_dir() -> Path:
    """Where Repertoire-Creator keeps its repertoires.

    The same ``REPERTOIRE_DIR`` override the sibling app honours, so pointing
    that at a private folder moves both apps at once.
    """
    env = os.environ.get("REPERTOIRE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    root = Path(__file__).resolve().parent.parent.parent
    return (root / "Repertoire-Creator" / "repertoires").resolve()


def list_repertoires(folder: Path | None = None) -> list:
    """Every repertoire on disk, as rows for the picker.

    Reads the sibling app's files without importing it.  A folder missing its
    manifest is skipped rather than crashing the list, because that folder is
    edited by a person and by another program at the same time.
    """
    import json

    folder = Path(folder) if folder else default_repertoire_dir()
    if not folder.is_dir():
        return []

    rows = []
    for path in sorted(folder.iterdir()):
        manifest = path / "repertoire.json"
        if not path.is_dir() or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({
            "slug": data.get("slug") or path.name,
            "name": data.get("name") or path.name,
            "color": data.get("color", "white"),
            "chapters": len(data.get("chapters") or []),
            "updated": data.get("updated", ""),
            "path": str(path),
        })
    return rows


def _repertoire_games(slug: str, folder: Path | None = None):
    """``(meta, [games])`` for one repertoire, read straight off disk."""
    import json

    folder = Path(folder) if folder else default_repertoire_dir()
    root = folder / slug
    manifest = root / "repertoire.json"
    if not manifest.is_file():
        raise BookError(
            f"No repertoire called {slug!r} in {folder}. "
            "Check the folder, or set REPERTOIRE_DIR.")
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BookError(f"{manifest} could not be read: {exc}") from exc

    games = []
    for chapter in meta.get("chapters") or []:
        path = root / "chapters" / chapter.get("file", "")
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        game = chess.pgn.read_game(io.StringIO(text))
        if game is not None:
            games.append((chapter.get("name") or path.stem, game))
    return meta, games


# ------------------------------------------------------------ lichess study


def fetch_study_pgn(url: str, token: str | None = None) -> str:
    """A Lichess study as PGN.

    Uses the sibling exporter when it is installed, because it knows the
    per-chapter trick that reads a *private* study without a token.  Falls
    back to the ordinary public endpoint when it is not, which works for
    public studies and says so plainly for private ones.
    """
    sibling = optional("fetch")
    if sibling is not None:
        try:
            return sibling.fetch_study_pgn(url, token)
        except Exception as exc:                             # noqa: BLE001
            raise BookError(str(exc)) from exc

    study_id = _study_id(url)
    try:
        response = requests.get(
            f"https://lichess.org/api/study/{study_id}.pgn",
            headers={"User-Agent": USER_AGENT,
                     **({"Authorization": f"Bearer {token}"} if token else {})},
            params={"comments": "true", "variations": "true"}, timeout=45)
    except requests.RequestException as exc:
        raise BookError(f"Could not reach Lichess: {exc}") from exc

    if response.status_code in (401, 403):
        raise BookError(
            "That study is private. Install the sibling exporter to read it "
            "without a token:\n"
            "  pip install -e Lichess-Study-to-PDF\n"
            "or supply a token with the study:read scope.")
    if response.status_code == 404:
        raise BookError(f"Lichess has no study {study_id}.")
    if not response.ok:
        raise BookError(f"Lichess said {response.status_code} for that study.")
    if "[Event " not in response.text:
        raise BookError("Lichess returned no PGN for that study; it may be empty.")
    return response.text


def _study_id(url: str) -> str:
    """The study id out of a URL, a chapter URL, or a bare id."""
    text = (url or "").strip().rstrip("/")
    if not text:
        raise BookError("Give a Lichess study URL.")
    if "lichess.org" in text:
        parts = [p for p in text.split("/") if p]
        try:
            index = parts.index("study")
        except ValueError as exc:
            raise BookError("That Lichess URL is not a study.") from exc
        if index + 1 >= len(parts):
            raise BookError("That study URL has no id in it.")
        return parts[index + 1]
    if len(text) >= 8 and text.replace("-", "").isalnum():
        return text.split("/")[0]
    raise BookError("That does not look like a study URL or id.")


def _study_color(headers: dict, requested: str) -> str:
    """Which side a chapter is for.

    ``auto`` reads the ``Orientation`` tag Lichess writes, which is what the
    board was pointing at when the chapter was made and is usually right.  A
    chapter with no tag records both sides, and the report says so -- better
    than silently guessing, since guessing wrong is the difference between
    "you have an answer" and "you do not".
    """
    if requested in ("white", "black", "both"):
        return requested
    orientation = (headers.get("Orientation") or "").strip().lower()
    return orientation if orientation in ("white", "black") else "both"


# ------------------------------------------------------------------ builder


def build_book(specs, *, repertoire_dir: Path | None = None,
               token: str | None = None, store=None, progress=None) -> Book:
    """One book from a list of source specs. See the module docstring.

    Each spec is ``{"kind": ...}`` plus that kind's own fields::

        {"kind": "repertoire", "slug": "white-ruy-lopez"}
        {"kind": "study", "url": "https://lichess.org/study/...", "color": "auto"}
        {"kind": "games", "site": "lichess", "username": "you", "limit": 200,
         "speeds": ["blitz"], "ratedOnly": true}

    ``store`` is optional; when given, a fetched study is cached there so
    rebuilding a book costs no second request.
    """
    book = Book()

    for index, spec in enumerate(specs or []):
        kind = (spec or {}).get("kind")
        if progress:
            progress(index, len(specs))

        if kind == "repertoire":
            _absorb_repertoire(book, spec, repertoire_dir)
        elif kind == "study":
            _absorb_study(book, spec, token=token, store=store)
        elif kind == "games":
            _absorb_games(book, spec, token=token, store=store)
        else:
            raise BookError(f"Unknown book source {kind!r}.")

    return book


def _absorb_repertoire(book: Book, spec: dict, repertoire_dir) -> None:
    slug = (spec.get("slug") or "").strip()
    if not slug:
        raise BookError("Choose a repertoire.")
    meta, games = _repertoire_games(slug, repertoire_dir)
    color = meta.get("color", "white")
    label = meta.get("name") or slug

    added = 0
    for chapter_name, game in games:
        added += book.absorb_game(game, f"{label} / {chapter_name}", color=color)

    book.sources.append({
        "kind": "repertoire", "slug": slug, "label": label,
        "color": color, "chapters": len(games), "moves": added,
        "note": f"only {color} moves, which is what a {color} repertoire is for",
    })


def _absorb_study(book: Book, spec: dict, *, token, store) -> None:
    url = (spec.get("url") or "").strip()
    if not url:
        raise BookError("Give a Lichess study URL.")
    requested = spec.get("color") or "auto"
    if requested not in COLORS:
        raise BookError(f"Unknown colour {requested!r}.")

    study_id = _study_id(url)
    pgn = None
    cache_name = f"study-{study_id}"
    if store is not None and not spec.get("refresh"):
        pgn = store.load_book_pgn(cache_name)
    if pgn is None:
        pgn = fetch_study_pgn(url, token)
        if store is not None:
            store.save_book_pgn(cache_name, pgn)

    handle = io.StringIO(pgn)
    added = 0
    chapters = 0
    both_sided = 0
    study_name = ""
    while True:
        game = chess.pgn.read_game(handle)
        if game is None:
            break
        headers = dict(game.headers)
        study_name = study_name or headers.get("StudyName", "")
        name = headers.get("ChapterName") or headers.get("Event", "") or "chapter"
        color = _study_color(headers, requested)
        if color == "both":
            both_sided += 1
        added += book.absorb_game(game, f"{study_name or study_id} / {name}",
                                  color=color)
        chapters += 1

    if not chapters:
        raise BookError("That study has no chapters with moves in them.")

    note = ""
    if both_sided:
        note = (f"{both_sided} of {chapters} chapters had no Orientation tag, so "
                "both sides' moves were recorded -- set the colour explicitly "
                "if that overstates your coverage")
    book.sources.append({
        "kind": "study", "url": url, "studyId": study_id,
        "label": study_name or f"study {study_id}",
        "color": requested, "chapters": chapters, "moves": added, "note": note,
    })


def _absorb_games(book: Book, spec: dict, *, token, store) -> None:
    site = (spec.get("site") or "lichess").strip().lower()
    username = (spec.get("username") or "").strip().lstrip("@")
    if not username:
        raise BookError("Give your own username to build a book from your games.")

    from .store import player_key

    key = player_key(site, username)
    payload = None
    if store is not None and not spec.get("refresh"):
        payload = store.load_games(key)

    if payload is None:
        try:
            payload = fetch_games(
                site, username,
                limit=int(spec.get("limit") or 200),
                speeds=spec.get("speeds") or (),
                rated_only=bool(spec.get("ratedOnly", True)),
                token=token)
        except FetchError as exc:
            raise BookError(str(exc)) from exc
        if store is not None:
            store.save_games(key, payload)

    lowered = username.lower()
    display = payload.get("username", username)
    label = f"{display}'s games"
    added = 0
    counted = 0
    for game in payload.get("games", []):
        if game.get("white", "").lower() == lowered:
            color = "white"
        elif game.get("black", "").lower() == lowered:
            color = "black"
        else:
            continue
        added += book.absorb_uci_line(
            (game.get("moves") or "").split(), label, color=color)
        counted += 1

    if not counted:
        raise BookError(f"None of the cached games have {username} in them.")

    book.sources.append({
        "kind": "games", "site": site, "username": display, "label": label,
        "games": counted, "moves": added,
        "note": "what you actually played, not what you wrote down",
    })


__all__ = [
    "COLORS",
    "MAX_BOOK_PLY",
    "Book",
    "BookError",
    "BookMove",
    "build_book",
    "default_repertoire_dir",
    "fetch_study_pgn",
    "list_repertoires",
]
