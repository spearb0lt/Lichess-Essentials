"""Naming a line, so a report reads like chess and not like coordinates.

A prep report that says *"they meet 1.e4 with the Caro-Kann in 61% of games"*
is worth reading.  The same report saying *"after 1.e4 c6 they score 61%"* is
the same fact and half as useful, because the name is what you already have
opinions about.

The source is Lichess's own openings dataset -- the five TSV files that name
openings on the site -- indexed **by position** rather than by move sequence,
so a line that transposes into a named opening is still named.  That matters
more here than anywhere else in this repository: scouting is precisely the
business of noticing that someone's pet move order arrives at a normal
position.

Fetched once, cached as one small JSON file, and never needed again.  No
token, unlike the opening explorer, and no network after the first run.

A failed download is not an error.  Without the index every line is simply
unnamed, which costs readability and nothing else, so :func:`load` never
raises.
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

SOURCE = "https://raw.githubusercontent.com/lichess-org/chess-openings/master/"
FILES = ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv")

USER_AGENT = "player-prepper/0.1"

_LOCK = threading.Lock()
_INDEX: dict[str, list] | None = None


def _fetch_text(url: str) -> str:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response.text


def _build_index(fetch=None) -> dict[str, list]:
    """The five TSVs, indexed by EPD as ``{epd: [eco, name, ply]}``."""
    fetch = fetch or _fetch_text
    index: dict[str, list] = {}

    for name in FILES:
        text = fetch(SOURCE + name)
        for line_number, line in enumerate(text.splitlines()):
            if line_number == 0 or not line.strip():
                continue                                   # header row
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
            # Shorter names win a collision: two entries on one position means
            # the more specific one carries extra words, and the general name
            # is the one people recognise.
            existing = index.get(board.epd())
            if existing is None or len(opening) < len(existing[1]):
                index[board.epd()] = [eco, opening, ply]
    return index


def load(*, refresh: bool = False, fetch=None) -> dict[str, list]:
    """The position index, from cache or freshly built. Never raises."""
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
                  "lines will be unnamed.")
            return _INDEX

        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            INDEX_FILE.write_text(json.dumps(_INDEX, separators=(",", ":")),
                                  encoding="utf-8")
        except OSError:
            pass
        return _INDEX


def available() -> bool:
    return bool(load())


def lookup(epd: str) -> dict | None:
    """The named opening for exactly this position, if it has one."""
    entry = load().get(epd)
    if entry is None:
        return None
    return {"eco": entry[0], "name": entry[1], "ply": entry[2]}


def name_for(epds) -> dict:
    """The best name for a line, given every position along it in order.

    The *deepest* named position wins, because that is the most specific true
    statement about the line.  The dataset names positions rather than every
    position, so a line can run through unnamed positions and be named again
    later; taking the deepest match rather than the last unbroken run is what
    keeps a nine-move line from being described by its second move.
    """
    index = load()
    best = {"eco": "", "name": "", "ply": -1}
    for ply, epd in enumerate(epds):
        entry = index.get(epd)
        if entry is not None and ply >= best["ply"]:
            best = {"eco": entry[0], "name": entry[1], "ply": ply}
    if best["ply"] < 0:
        return {"eco": "", "name": "", "known": False}
    return {"eco": best["eco"], "name": best["name"], "known": True}


__all__ = ["INDEX_FILE", "available", "load", "lookup", "name_for"]
