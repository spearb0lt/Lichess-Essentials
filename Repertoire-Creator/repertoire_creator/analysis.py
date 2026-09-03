"""What is wrong with the repertoire, and where it overlaps itself.

Three checks, all of them cheap enough to rerun on every save:

**Gaps** -- positions you can be forced into that you have no answer for.
Structurally that is a node where it is *your* turn and there are no
children.  The mirror case, a node where it is your turn and there are
several children, is not an error but is worth flagging: a repertoire is a
set of decisions, and an undecided position is a decision you have not made.

**Uncovered replies** -- the same idea aimed at the opponent, which needs
outside knowledge of what people actually play.  That comes from the Lichess
opening explorer and lives in :mod:`repertoire_creator.explorer`, because it
needs the network and a token; this module only consumes its answers.

**Transpositions** -- the same position reached by more than one route.  Two
occurrences are usually fine and sometimes the whole point, but if your
chosen move differs between them, one of the two is stale, and that is a bug
you will not notice over the board.
"""

from __future__ import annotations

import chess
import chess.pgn

from .model import format_path, is_my_turn, path_of

#: A line that ends this early on your own move is more likely unfinished
#: than deliberate; the UI shows it as information, not as an error.
SHALLOW_PLIES = 6


def position_key(board: chess.Board) -> str:
    """Identity of a position for transposition purposes.

    ``epd()`` is the FEN without the halfmove clock or move number, so two
    routes into the same position match even when they took a different
    number of moves to get there.
    """
    return board.epd()


# ------------------------------------------------------------------- gaps


def chapter_gaps(game: chess.pgn.Game, color: str) -> list:
    """Structural gaps in one chapter."""
    found = []

    def visit(node, board):
        my_turn = is_my_turn(board, color)
        children = node.variations
        path = path_of(node)
        # Depth from the *chapter* start, not absolute ply: a chapter that
        # begins from a FEN is still one move deep after its first move.
        depth = len(path)
        common = {
            "path": format_path(path),
            "fen": board.fen(),
            "ply": depth,
            "line": line_label(node, board),
        }

        if my_turn and not children and not board.is_game_over():
            found.append({"kind": "missing", **common})
        elif my_turn and len(children) > 1:
            found.append({
                "kind": "undecided",
                "moves": [board.san(child.move) for child in children],
                **common,
            })
        elif not my_turn and not children and depth < SHALLOW_PLIES \
                and not board.is_game_over():
            found.append({"kind": "shallow", **common})

        for child in children:
            after = board.copy(stack=False)
            after.push(child.move)
            visit(child, after)

    visit(game, game.board())
    return found


def line_label(node: chess.pgn.GameNode, board: chess.Board) -> str:
    """The moves leading to ``node``, as readable notation."""
    moves = []
    current = node
    while current.parent is not None:
        moves.append(current)
        current = current.parent
    moves.reverse()

    replay = current.board()
    text = []
    for item in moves:
        number = replay.fullmove_number
        if replay.turn == chess.WHITE:
            text.append(f"{number}.{replay.san(item.move)}")
        elif not text:
            text.append(f"{number}...{replay.san(item.move)}")
        else:
            text.append(replay.san(item.move))
        replay.push(item.move)
    return " ".join(text) or "start"


# --------------------------------------------------------- transpositions


def build_index(chapters: list) -> dict:
    """Map every position in the repertoire to the places it occurs.

    ``chapters`` is a list of ``(chapter_meta, game)``.
    """
    index: dict[str, list] = {}

    def visit(meta, node, board):
        key = position_key(board)
        entry = {
            "chapterId": meta.id,
            "chapterName": meta.name,
            "path": format_path(path_of(node)),
            "fen": board.fen(),
            "line": None,          # filled in lazily, it is not free
            "node": node,
            "board": board,
        }
        index.setdefault(key, []).append(entry)
        for child in node.variations:
            after = board.copy(stack=False)
            after.push(child.move)
            visit(meta, child, after)

    for meta, game in chapters:
        visit(meta, game, game.board())
    return index


def transpositions(chapters: list, color: str) -> list:
    """Positions reached twice or more, conflicts first."""
    index = build_index(chapters)
    out = []

    for key, entries in index.items():
        if len(entries) < 2:
            continue
        # The starting position is in every chapter; saying so is noise.
        if entries[0]["board"].ply() == 0:
            continue

        board = entries[0]["board"]
        my_turn = is_my_turn(board, color)

        replies = {}
        for entry in entries:
            node = entry["node"]
            if node.variations:
                san = entry["board"].san(node.variations[0].move)
            else:
                san = None
            replies.setdefault(san, []).append(entry)

        # A conflict only matters where *you* choose: two different opponent
        # continuations from the same position are just two lines.
        distinct = [san for san in replies if san is not None]
        conflict = my_turn and len(distinct) > 1
        dead_end = my_turn and None in replies and len(entries) > 1

        out.append({
            "key": key,
            "fen": board.fen(),
            "ply": board.ply(),
            "myTurn": my_turn,
            "conflict": conflict,
            "deadEnd": dead_end,
            "moves": sorted(distinct),
            "occurrences": [
                {
                    "chapterId": e["chapterId"],
                    "chapterName": e["chapterName"],
                    "path": e["path"],
                    "line": line_label(e["node"], e["board"]),
                    "reply": (
                        e["board"].san(e["node"].variations[0].move)
                        if e["node"].variations else None
                    ),
                }
                for e in entries
            ],
        })

    out.sort(key=lambda t: (not t["conflict"], not t["deadEnd"], t["ply"]))
    return out


# ------------------------------------------------------------------ counts


def chapter_stats(game: chess.pgn.Game, color: str) -> dict:
    total = my_moves = their_moves = branches = evaluated = 0
    max_ply = 0

    def visit(node, board):
        nonlocal total, my_moves, their_moves, branches, evaluated, max_ply
        for child in node.variations:
            total += 1
            if is_my_turn(board, color):
                my_moves += 1
            else:
                their_moves += 1
            if child.eval() is not None:
                evaluated += 1
            after = board.copy(stack=False)
            after.push(child.move)
            max_ply = max(max_ply, after.ply())
            visit(child, after)
        if len(node.variations) > 1:
            branches += len(node.variations) - 1

    visit(game, game.board())
    return {
        "moves": total,
        "myMoves": my_moves,
        "theirMoves": their_moves,
        "branches": branches,
        "evaluated": evaluated,
        "maxPly": max_ply,
    }


def repertoire_report(chapters: list, color: str) -> dict:
    """The whole health check, ready to hand to the browser."""
    per_chapter = []
    totals = {"moves": 0, "myMoves": 0, "theirMoves": 0, "branches": 0,
              "evaluated": 0, "maxPly": 0}
    all_gaps = []

    for meta, game in chapters:
        stats = chapter_stats(game, color)
        gaps = chapter_gaps(game, color)
        for gap in gaps:
            gap["chapterId"] = meta.id
            gap["chapterName"] = meta.name
        all_gaps.extend(gaps)
        per_chapter.append({
            "chapterId": meta.id,
            "name": meta.name,
            "stats": stats,
            "gaps": sum(1 for g in gaps if g["kind"] == "missing"),
            "undecided": sum(1 for g in gaps if g["kind"] == "undecided"),
        })
        for key in ("moves", "myMoves", "theirMoves", "branches", "evaluated"):
            totals[key] += stats[key]
        totals["maxPly"] = max(totals["maxPly"], stats["maxPly"])

    all_gaps.sort(key=lambda g: ({"missing": 0, "undecided": 1, "shallow": 2}[g["kind"]],
                                 g["ply"]))
    return {
        "totals": totals,
        "chapters": per_chapter,
        "gaps": all_gaps,
        "transpositions": transpositions(chapters, color),
    }
