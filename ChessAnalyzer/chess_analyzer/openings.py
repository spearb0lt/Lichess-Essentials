"""Naming the opening, and knowing where theory stops.

Two jobs, one dataset.  Naming is the obvious one -- a review that says
"Sicilian Defense: Alapin Variation" is worth more than one that says "1. e4
c5 2. c3".  The second job matters more: a review must not call your ninth
move of the Ruy Lopez a mistake because the engine mildly prefers something
else.  Moves that are still inside published theory get labelled *book* and
are excluded from the accuracy figures, exactly as Lichess and Chess.com both
do it, and that needs a definition of "inside theory" the app can check.

The source is Lichess's own openings dataset -- 3,810 named positions across
five TSV files, the same data that names openings on the site.  It is fetched
once, indexed by position rather than by move sequence so that transpositions
into a named line are still recognised, and cached as one small JSON file.
No API token, unlike the opening explorer, and no network at all after the
first run.

The alternative, asking the explorer per position, needs an authenticated
request and a rate-limit budget for something that never changes.  A 380 KB
download once is the better trade.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import chess
import chess.pgn
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INDEX_FILE = DATA_DIR / "openings.json"

SOURCE = ("https://raw.githubusercontent.com/lichess-org/"
          "chess-openings/master/")
FILES = ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv")

USER_AGENT = "chess-analyzer/0.1"

#: Theory does not run past here in the dataset, and a move this deep that
#: still matches is far more likely a transposition into a short named line
#: than genuine preparation. Book credit stops at this ply.
MAX_BOOK_PLY = 40

_LOCK = threading.Lock()
_INDEX: dict[str, list] | None = None


def _build_index(fetch=None) -> dict[str, list]:
    """Fetch the five TSVs and index every named position by its EPD.

    Keying on the position rather than the move list is the whole point: a
    Sicilian reached via 1. e4 c5 and via 1. Nf3 c5 2. e4 is the same opening,
    and a move-sequence index would only know the first.
    """
    fetch = fetch or _fetch_text
    index: dict[str, list] = {}

    for name in FILES:
        text = fetch(SOURCE + name)
        for line_number, line in enumerate(text.splitlines()):
            if line_number == 0 or not line.strip():
                continue                      # header row
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            eco, opening, moves = parts[0], parts[1], parts[2]
            game = chess.pgn.read_game(io.StringIO(moves))
            if game is None:
                continue
            board = game.board()
            ply = 0
            for move in game.mainline_moves():
                board.push(move)
                ply += 1
            # Shorter names win a collision: two entries on the same position
            # means the more specific one carries extra words, and the general
            # name is the one people recognise.
            existing = index.get(board.epd())
            if existing is None or len(opening) < len(existing[1]):
                index[board.epd()] = [eco, opening, ply]
    return index


def _fetch_text(url: str) -> str:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response.text


def load(*, refresh: bool = False, fetch=None) -> dict[str, list]:
    """The position index, from cache or freshly built. Never raises.

    A failed download must not stop a review: without the index every move is
    simply "not book", which costs opening names and nothing else.
    """
    global _INDEX
    with _LOCK:
        if _INDEX is not None and not refresh:
            return _INDEX

        if INDEX_FILE.is_file() and not refresh:
            try:
                _INDEX = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
                return _INDEX
            except (OSError, ValueError):
                pass

        try:
            _INDEX = _build_index(fetch=fetch)
        except (requests.RequestException, OSError) as exc:
            _INDEX = {}
            print(f"  openings: could not fetch the dataset ({exc}); "
                  "openings will be unnamed.")
            return _INDEX

        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            INDEX_FILE.write_text(
                json.dumps(_INDEX, separators=(",", ":")), encoding="utf-8")
        except OSError:
            pass
        return _INDEX


def available() -> bool:
    return bool(load())


def lookup(board: chess.Board) -> dict | None:
    """The named opening for exactly this position, if it has one."""
    entry = load().get(board.epd())
    if entry is None:
        return None
    return {"eco": entry[0], "name": entry[1], "ply": entry[2]}


def in_book(board: chess.Board) -> bool:
    """Is this position published theory?"""
    if board.ply() > MAX_BOOK_PLY:
        return False
    return board.epd() in load()


def identify(boards: list[chess.Board]) -> dict:
    """The opening a game played, and where it left theory.

    ``boards`` is every position in the game in order, starting position
    first.  Returns the *deepest* named position reached, because that is the
    most specific true statement about the game -- and ``bookPly``, the number
    of opening moves that were still theory, which is where the review stops
    handing out book credit.

    The dataset names positions, not every position: a line can run through
    unnamed positions and be named again three moves later.  So book depth is
    the deepest ply that matched, not the last unbroken run of matches --
    reaching a named position at move 5 means the first five moves were
    theory, whatever the dataset happens to have chosen to name along the way.
    """
    index = load()
    best = {"eco": "", "name": "", "ply": 0}

    for ply, board in enumerate(boards):
        if ply > MAX_BOOK_PLY:
            break
        entry = index.get(board.epd())
        if entry is not None and ply >= best["ply"]:
            best = {"eco": entry[0], "name": entry[1], "ply": ply}

    return {
        "eco": best["eco"],
        "name": best["name"] or "Unnamed opening",
        "ply": best["ply"],
        "bookPly": best["ply"],
        "known": bool(best["name"]),
    }


__all__ = [
    "INDEX_FILE",
    "MAX_BOOK_PLY",
    "available",
    "identify",
    "in_book",
    "load",
    "lookup",
]
