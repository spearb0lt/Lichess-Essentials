"""The report: what they play, where they leak points, and what you have no answer for.

Three questions, in the order they are worth asking.

**What do they play?**  Counted off their own games, per colour, keyed by
position so a move order that transposes is not counted as something new.

**Where do they leak points?**  Their own results, ranked by how many points
a move has actually cost them.  No engine and no opinion -- if they score 34%
over thirty games in a line, that is where to steer, whatever anyone thinks
of the move.

**What have you got nothing for?**  This is the part that needs your book,
and it is the part worth getting exactly right, so the rule is written out
here rather than left to be inferred from the code.

Walk each of their games, ply by ply, up to the scouting depth:

* **Their move**: nothing to check.  Whatever they played, you will have to
  meet it -- push it and carry on.
* **Your move, and your book has something here**: if the move actually played
  is one of yours, carry on.  If it is not, this game *left your repertoire*
  through the other player's choice, not yours -- their real opponent played
  something you would never play -- so it is dropped from the count entirely.
  Counting it would punish you for a line you will never reach.
* **Your move, and your book has nothing here**: a **gap**.  The game stops
  there and the position is recorded, weighted by how many of their games
  arrive at it.

That last rule is the whole product.  A gap is not "a position you have not
studied" -- there are billions of those.  It is a position *this opponent
actually steers into*, that *your own repertoire actually reaches*, and that
you have written nothing about.  The count beside it is the number of their
games that would have put you there.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

import chess

from . import openings
from .book import Book
from .tree import (
    DEFAULT_MAX_PLY,
    Tally,
    build_trees,
    outcome_for,
    top_moves,
    weak_spots,
)

#: A ranking over fewer games than this is noise wearing a percentage sign.
DEFAULT_MIN_GAMES = 5

#: How many rows each ranked section carries into the report.
TOP_LIMIT = 24
WEAK_LIMIT = 12
GAP_LIMIT = 24


def other(color: str) -> str:
    return "black" if color == "white" else "white"


def epds_along(line_uci) -> list:
    """Every position along a line, for naming it."""
    board = chess.Board()
    out = [board.epd()]
    for uci in line_uci or []:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        board.push(move)
        out.append(board.epd())
    return out


def name_line(line_uci) -> dict:
    """``{eco, name, known}`` for a line, from the openings dataset."""
    return openings.name_for(epds_along(line_uci))


def pretty_line(line_san) -> str:
    """SAN moves as ``1.e4 c5 2.Nf3``, which is how people read a line."""
    parts = []
    for index, san in enumerate(line_san or []):
        if index % 2 == 0:
            parts.append(f"{index // 2 + 1}.{san}")
        elif not parts:
            parts.append(f"{index // 2 + 1}...{san}")
        else:
            parts.append(san)
    return " ".join(parts)


# ----------------------------------------------------------------- coverage


class Gap:
    """One position their games reach that your book has no answer for."""

    __slots__ = ("epd", "fen", "ply", "line", "line_uci", "tally",
                 "samples", "last_date")

    def __init__(self, board: chess.Board, ply: int, line_san, line_uci):
        self.epd = board.epd()
        self.fen = board.fen()
        self.ply = ply
        self.line = list(line_san)
        self.line_uci = list(line_uci)
        self.tally = Tally()
        self.samples: list = []
        self.last_date = ""

    def hit(self, outcome: str, url: str, date: str) -> None:
        self.tally.add(outcome)
        if url and len(self.samples) < 3 and url not in self.samples:
            self.samples.append(url)
        if date > self.last_date:
            self.last_date = date

    def to_json(self, *, my_color: str) -> dict:
        named = name_line(self.line_uci)
        return {
            "epd": self.epd,
            "fen": self.fen,
            "ply": self.ply,
            "line": self.line,
            "lineUci": self.line_uci,
            "lineText": pretty_line(self.line),
            "youPlay": my_color,
            "theirLastMove": self.line[-1] if self.line else "",
            "opening": named,
            "games": self.tally.games,
            "theirScore": round(self.tally.score, 4),
            "tally": self.tally.to_json(),
            "samples": list(self.samples),
            "lastDate": self.last_date,
            "engine": None,
        }


def measure_coverage(games, username: str, their_color: str, book: Book, *,
                     max_ply: int = DEFAULT_MAX_PLY) -> dict:
    """Walk their games against your book. See the module docstring for the rule."""
    my_color = other(their_color)
    my_turn = chess.WHITE if my_color == "white" else chess.BLACK
    lowered = (username or "").strip().lstrip("@").lower()

    counts = {"games": 0, "inScope": 0, "covered": 0, "gapGames": 0,
              "offBook": 0}
    depths: list = []
    gaps: dict = {}

    for game in games or []:
        if game.get(their_color, "").lower() != lowered:
            continue
        counts["games"] += 1

        outcome = outcome_for(game.get("result", "*"), their_color)
        url = game.get("url", "")
        date = game.get("date", "")

        moves = (game.get("moves") or "").split()
        board = chess.Board()
        line_san: list = []
        line_uci: list = []
        status = "covered"
        gap_key = None
        ply = 0

        while True:
            # The horizon. Stop without demanding an answer: not having one
            # past the depth you asked to scout is not a gap, it is the
            # question you did not ask.
            if ply >= max_ply:
                break

            # Every position reached is checked, including the one a game ends
            # on. A game that stops exactly on your move -- somebody resigned
            # -- still reached a position you would have had to answer, and
            # skipping it silently counted that game as covered.
            if board.turn == my_turn and not board.is_game_over():
                mine = book.at(board.epd())
                if not mine:
                    status = "gap"
                    gap = gaps.get(board.epd())
                    if gap is None:
                        gap = Gap(board, ply, line_san, line_uci)
                        gaps[board.epd()] = gap
                    gap_key = gap
                    break
                if ply < len(moves) and moves[ply] not in mine:
                    # Their opponent played something you never would. This
                    # game is about somebody else's repertoire, not yours.
                    status = "offBook"
                    break

            if ply >= len(moves):
                break

            try:
                move = chess.Move.from_uci(moves[ply])
            except ValueError:
                break
            if move not in board.legal_moves:
                break
            line_san = line_san + [board.san(move)]
            line_uci = line_uci + [moves[ply]]
            board.push(move)
            ply += 1

        if status == "offBook":
            counts["offBook"] += 1
            continue

        counts["inScope"] += 1
        if status == "gap":
            counts["gapGames"] += 1
            if gap_key is not None:
                gap_key.hit(outcome, url, date)
        else:
            counts["covered"] += 1
            depths.append(len(line_uci))

    ranked = sorted(gaps.values(),
                    key=lambda gap: (-gap.tally.games, gap.ply))

    percent = (counts["covered"] / counts["inScope"] * 100
               if counts["inScope"] else 0.0)

    return {
        "youPlay": my_color,
        "theyPlay": their_color,
        "maxPly": max_ply,
        **counts,
        "percent": round(percent, 1),
        "medianDepth": int(statistics.median(depths)) if depths else 0,
        "gapPositions": len(ranked),
        "gaps": [gap.to_json(my_color=my_color) for gap in ranked[:GAP_LIMIT]],
        "allGapGames": sum(gap.tally.games for gap in ranked),
    }


def empty_coverage(their_color: str, max_ply: int) -> dict:
    """The shape a report still needs when there is no book to measure against."""
    return {
        "youPlay": other(their_color), "theyPlay": their_color,
        "maxPly": max_ply, "games": 0, "inScope": 0, "covered": 0,
        "gapGames": 0, "offBook": 0, "percent": 0.0, "medianDepth": 0,
        "gapPositions": 0, "gaps": [], "allGapGames": 0, "noBook": True,
    }


# ------------------------------------------------------------------ summary


def _opening_of(moves, max_ply: int) -> dict:
    board = chess.Board()
    epds = [board.epd()]
    for ply, uci in enumerate(moves):
        if ply >= max_ply:
            break
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        board.push(move)
        epds.append(board.epd())
    return openings.name_for(epds)


def opening_breakdown(games, username: str, color: str, *,
                      max_ply: int = DEFAULT_MAX_PLY, limit: int = 12) -> list:
    """What they actually open with in this colour, grouped by opening name.

    More readable than a tree dump and the thing you want first: "they meet
    1.e4 with the Caro-Kann in 61% of games" is a sentence you can act on.
    """
    lowered = (username or "").strip().lstrip("@").lower()
    buckets: dict = {}
    total = 0

    for game in games or []:
        if game.get(color, "").lower() != lowered:
            continue
        moves = (game.get("moves") or "").split()
        if not moves:
            continue
        named = _opening_of(moves, max_ply)
        key = named["name"] or "Unnamed"
        entry = buckets.setdefault(key, {"name": key, "eco": named["eco"],
                                         "tally": Tally()})
        entry["tally"].add(outcome_for(game.get("result", "*"), color))
        total += 1

    rows = []
    for entry in buckets.values():
        tally = entry["tally"]
        rows.append({
            "name": entry["name"], "eco": entry["eco"],
            "share": round(tally.games / total, 4) if total else 0.0,
            **tally.to_json(),
        })
    rows.sort(key=lambda row: (-row["games"], row["name"]))
    return rows[:limit]


def summarise(games, username: str) -> dict:
    """The header of a report: how many, how recent, how they did."""
    lowered = (username or "").strip().lstrip("@").lower()
    overall = Tally()
    per_color = {"white": Tally(), "black": Tally()}
    speeds: dict = {}
    ratings: list = []
    dates: list = []
    opponents = set()

    for game in games or []:
        if game.get("white", "").lower() == lowered:
            color, rating, foe = "white", game.get("whiteElo"), game.get("black")
        elif game.get("black", "").lower() == lowered:
            color, rating, foe = "black", game.get("blackElo"), game.get("white")
        else:
            continue

        outcome = outcome_for(game.get("result", "*"), color)
        overall.add(outcome)
        per_color[color].add(outcome)
        speeds[game.get("speed", "")] = speeds.get(game.get("speed", ""), 0) + 1
        if foe:
            opponents.add(foe.lower())
        if game.get("date"):
            dates.append(game["date"])
        try:
            if rating:
                ratings.append(int(rating))
        except (TypeError, ValueError):
            pass

    return {
        "games": overall.games,
        "from": min(dates) if dates else "",
        "to": max(dates) if dates else "",
        "tally": overall.to_json(),
        "colors": {name: tally.to_json() for name, tally in per_color.items()},
        "speeds": dict(sorted(speeds.items(), key=lambda kv: -kv[1])),
        "opponents": len(opponents),
        "rating": {
            "min": min(ratings) if ratings else None,
            "max": max(ratings) if ratings else None,
            "median": int(statistics.median(ratings)) if ratings else None,
        },
    }


# ------------------------------------------------------------------- report


def build_report(payload: dict, *, book: Book | None = None,
                 max_ply: int = DEFAULT_MAX_PLY,
                 min_games: int = DEFAULT_MIN_GAMES,
                 progress=None) -> dict:
    """A whole scouting report from a cached games payload plus your book."""
    username = payload.get("username", "")
    games = payload.get("games") or []

    trees = build_trees(games, username, max_ply=max_ply)

    colors = {}
    for index, their_color in enumerate(("white", "black")):
        if progress:
            progress(index, 2)
        tree = trees[their_color]
        section = {
            "theyPlay": their_color,
            "youPlay": other(their_color),
            "tally": tree.tally.to_json(),
            "openings": opening_breakdown(games, username, their_color,
                                          max_ply=max_ply),
            "topMoves": _named(top_moves(tree, min_games=1, limit=TOP_LIMIT)),
            "weakSpots": _named(weak_spots(tree, min_games=min_games,
                                           limit=WEAK_LIMIT)),
        }
        if book:
            section["coverage"] = measure_coverage(
                games, username, their_color, book, max_ply=max_ply)
        else:
            section["coverage"] = empty_coverage(their_color, max_ply)
        colors[their_color] = section

    return {
        "site": payload.get("site", ""),
        "username": username,
        "scoutedAt": datetime.now(timezone.utc)
                             .replace(microsecond=0).isoformat(),
        "gamesFetched": payload.get("fetched", ""),
        "settings": {
            "maxPly": max_ply,
            "minGames": min_games,
            "filters": payload.get("filters") or {},
        },
        "book": book.stats() if book else None,
        "summary": summarise(games, username),
        "colors": colors,
    }


def _named(rows) -> list:
    """Attach an opening name and a readable line to each ranked row."""
    for row in rows:
        row["opening"] = name_line(row.get("lineUci") or [])
        row["lineText"] = pretty_line(row.get("line") or [])
    return rows


def all_gaps(report: dict) -> list:
    """Every gap in a report, both colours, biggest first.

    The rows are the report's own gap dictionaries, not copies, so filling in
    an engine suggestion through this list updates the report -- which is how
    a suggestion reaches the saved JSON and the PDF.
    """
    rows = []
    for their_color, section in (report.get("colors") or {}).items():
        for gap in (section.get("coverage") or {}).get("gaps") or []:
            gap["theyPlay"] = their_color
            rows.append(gap)
    rows.sort(key=lambda gap: -gap.get("games", 0))
    return rows


__all__ = [
    "DEFAULT_MIN_GAMES",
    "GAP_LIMIT",
    "Gap",
    "all_gaps",
    "build_report",
    "empty_coverage",
    "measure_coverage",
    "name_line",
    "opening_breakdown",
    "other",
    "pretty_line",
    "summarise",
]
