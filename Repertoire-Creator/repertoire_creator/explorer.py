"""Finding the opponent moves your repertoire does not answer.

Structural gap detection (in :mod:`repertoire_creator.analysis`) can only see
holes you have already half-dug: a position where it is your turn and you
wrote nothing.  It cannot know that after your main line the opponent has a
third reply, played in one game in twenty, that you have never considered.

That needs outside knowledge, and the Lichess opening explorer has it.  We
ask it, per position, which moves real players of a chosen strength actually
choose, and report the popular ones missing from your tree.

Two things keep this honest.  It is opt-in and bounded -- a scan looks at a
capped number of positions, paced so the explorer does not rate limit us --
and the threshold is yours to set, because "popular enough to prepare for"
means something different at 1500 than at 2400.
"""

from __future__ import annotations

import time

import chess
import chess.pgn

from .analysis import line_label
from .lichess import LichessClient, LichessError, RateLimited
from .model import format_path, is_my_turn, path_of

#: A move played in fewer than this share of games is noise for most people.
DEFAULT_MIN_SHARE = 0.02
#: Below this many games the percentages are not worth trusting.
DEFAULT_MIN_GAMES = 200
#: A hard cap so a scan of a large repertoire cannot run for an hour.
DEFAULT_MAX_POSITIONS = 60
#: Seconds between explorer calls.
PACE = 0.6


def _total(entry: dict) -> int:
    return int(entry.get("white", 0)) + int(entry.get("draws", 0)) + int(entry.get("black", 0))


def scan_chapter(
    game: chess.pgn.Game,
    color: str,
    client: LichessClient,
    *,
    min_share: float = DEFAULT_MIN_SHARE,
    min_games: int = DEFAULT_MIN_GAMES,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    ratings=(1600, 1800, 2000, 2200, 2500),
    speeds=("blitz", "rapid", "classical"),
    max_depth: int = 24,
) -> dict:
    """Report popular opponent replies missing from one chapter.

    Only positions where the *opponent* is to move and you already have at
    least one answer are worth asking about: a position with no answers at
    all is already reported as a structural gap, and asking the explorer
    about it would say the same thing twice.
    """
    candidates = []

    def visit(node, board):
        depth = len(path_of(node))
        if depth <= max_depth and not is_my_turn(board, color) and node.variations:
            candidates.append((node, board.copy(stack=False)))
        for child in node.variations:
            after = board.copy(stack=False)
            after.push(child.move)
            visit(child, after)

    visit(game, game.board())
    # Shallow positions matter most: a hole at move 3 will be hit constantly,
    # a hole at move 14 hardly ever.
    candidates.sort(key=lambda pair: len(path_of(pair[0])))
    truncated = len(candidates) > max_positions
    candidates = candidates[:max_positions]

    findings = []
    checked = 0
    rate_limited = False

    for node, board in candidates:
        covered = {child.move.uci() for child in node.variations}
        try:
            data = client.explorer(
                board.fen(), speeds=speeds, ratings=ratings, moves=12
            )
        except RateLimited:
            rate_limited = True
            break
        except LichessError:
            # A single position failing should not abandon the scan.
            continue
        checked += 1
        time.sleep(PACE)

        played = data.get("moves") or []
        # The response carries the position totals at the top level; fall
        # back to summing the listed moves if that is ever absent.
        position_total = _total(data) or sum(_total(entry) for entry in played)
        if position_total < min_games:
            continue

        missing = []
        for entry in played:
            uci = entry.get("uci")
            if not uci or uci in covered:
                continue
            games = _total(entry)
            share = games / position_total if position_total else 0.0
            if share < min_share:
                continue
            missing.append({
                "san": entry.get("san"),
                "uci": uci,
                "games": games,
                "share": round(share, 4),
            })

        if missing:
            findings.append({
                "path": format_path(path_of(node)),
                "fen": board.fen(),
                "ply": len(path_of(node)),
                "line": line_label(node, board),
                "positionGames": position_total,
                "covered": sorted(
                    board.san(child.move) for child in node.variations
                ),
                "missing": missing[:6],
            })

    findings.sort(key=lambda f: (-max(m["share"] for m in f["missing"]), f["ply"]))
    return {
        "findings": findings,
        "checked": checked,
        "candidates": len(candidates),
        "truncated": truncated,
        "rateLimited": rate_limited,
        "minShare": min_share,
        "minGames": min_games,
    }
