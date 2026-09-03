"""Every mutation that can happen to a chapter move tree.

Two rules run through all of it:

*Never create a duplicate move.*  Playing a move that already exists at the
current node navigates to it instead of adding a second identical branch.
That is what makes a repertoire converge as you enter lines by hand rather
than sprouting near-copies of the same variation.

*Merging beats appending.*  Pasting notation, importing a Lichess chapter or
adding a line all funnel through :func:`merge_into`, which grafts a source
tree onto a target node move by move.  Re-pasting a line you already have is
therefore a no-op, and pasting a longer version of it extends the line in
place.
"""

from __future__ import annotations

import io
import re

import chess
import chess.pgn

from .model import (
    PathError,
    build_comment,
    format_path,
    path_of,
    resolve,
    split_comment,
)


class EditError(ValueError):
    """Raised when an edit cannot be applied."""


def board_at(game: chess.pgn.Game, node: chess.pgn.GameNode) -> chess.Board:
    """The position *at* ``node`` -- after its move has been played."""
    return node.board()


# ------------------------------------------------------------------- moves


def _find_child(node: chess.pgn.GameNode, move: chess.Move):
    for index, child in enumerate(node.variations):
        if child.move == move:
            return index, child
    return None, None


def play_move(
    game: chess.pgn.Game,
    path,
    *,
    uci: str | None = None,
    san: str | None = None,
) -> tuple:
    """Play one move from ``path``. Returns ``(new_path, created)``.

    ``created`` is False when the move was already in the tree, which the UI
    uses to tell "you added a line" from "you walked down one you had".
    """
    node = resolve(game, path)
    board = node.board()

    move = None
    if uci:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as exc:
            raise EditError(f"{uci!r} is not a move: {exc}") from exc
        # Promotions arrive from the board as a bare from-to when the user has
        # not chosen a piece yet; default to a queen if that is the only way
        # the move is legal.
        if move not in board.legal_moves and not move.promotion:
            queened = chess.Move(move.from_square, move.to_square, chess.QUEEN)
            if queened in board.legal_moves:
                move = queened
    elif san:
        try:
            move = board.parse_san(san)
        except ValueError as exc:
            raise EditError(f"{san!r} is not legal here: {exc}") from exc
    else:
        raise EditError("A move needs either a UCI or a SAN string.")

    if move not in board.legal_moves:
        raise EditError(f"{uci or san} is not legal in this position.")

    index, existing = _find_child(node, move)
    if existing is not None:
        return path_of(existing), False

    child = node.add_variation(move)
    return path_of(child), True


def delete_node(game: chess.pgn.Game, path) -> str:
    """Remove a node and everything under it. Returns the parent path."""
    if not path:
        raise EditError("The starting position cannot be deleted.")
    node = resolve(game, path)
    parent = node.parent
    parent.remove_variation(node.move)
    return format_path(path_of(parent))


def promote(game: chess.pgn.Game, path, *, to_main: bool = False) -> str:
    """Move a variation up among its siblings.

    ``to_main`` makes it the main line *all the way to the root*, which is
    what "this is the move I actually play" has to mean.  python-chess only
    promotes one level at a time, so we walk up the ancestors doing the same
    to each -- otherwise a promoted move stays buried inside a sideline and
    the PGN still reads as though you play something else.
    """
    if not path:
        raise EditError("The starting position is already the main line.")
    node = resolve(game, path)

    if not to_main:
        node.parent.promote(node.move)
        return format_path(path_of(node))

    current = node
    while current.parent is not None:
        current.parent.promote_to_main(current.move)
        current = current.parent
    return format_path(path_of(node))


def demote(game: chess.pgn.Game, path) -> str:
    if not path:
        raise EditError("The starting position cannot be demoted.")
    node = resolve(game, path)
    node.parent.demote(node.move)
    return format_path(path_of(node))


# ---------------------------------------------------------------- annotation


def set_comment(game: chess.pgn.Game, path, text: str) -> None:
    """Replace the prose of a comment, leaving shapes and evals untouched."""
    node = resolve(game, path)
    _, circles, arrows = split_comment(node.comment)
    score, depth = node.eval(), node.eval_depth()
    node.comment = build_comment(text, circles, arrows)
    if score is not None:
        node.set_eval(score, depth)


def set_shapes(game: chess.pgn.Game, path, circles=(), arrows=()) -> None:
    node = resolve(game, path)
    prose, _, _ = split_comment(node.comment)
    score, depth = node.eval(), node.eval_depth()
    node.comment = build_comment(prose, circles, arrows)
    if score is not None:
        node.set_eval(score, depth)


def set_nags(game: chess.pgn.Game, path, nags) -> list:
    if not path:
        raise EditError("The starting position has no move to annotate.")
    node = resolve(game, path)
    node.nags = {int(n) for n in nags}
    return sorted(node.nags)


def toggle_nag(game: chess.pgn.Game, path, nag: int) -> list:
    if not path:
        raise EditError("The starting position has no move to annotate.")
    node = resolve(game, path)
    nag = int(nag)
    if nag in node.nags:
        node.nags.discard(nag)
    else:
        # The !/?/!! family are mutually exclusive on Lichess, so setting one
        # clears the others rather than stacking "!!?!" on a move.
        node.nags -= {1, 2, 3, 4, 5, 6}
        node.nags.add(nag)
    return sorted(node.nags)


# -------------------------------------------------------------- bulk import


_MOVE_NUMBER_RE = re.compile(r"\b\d+\s*\.(\.\.)?")


def parse_line(text: str, board: chess.Board) -> chess.pgn.Game:
    """Turn pasted notation into a game tree rooted at ``board``.

    Accepts anything python-chess accepts after a FEN header: SAN with or
    without move numbers, nested variations in brackets, comments in braces,
    NAGs.  A bare UCI sequence (``e2e4 e7e5``) works too.
    """
    body = (text or "").strip()
    if not body:
        raise EditError("Nothing to import.")

    # Drop a result token; it would end the game early and swallow the rest.
    body = re.sub(r"\s*(1-0|0-1|1/2-1/2|\*)\s*$", "", body).strip()
    if not body:
        raise EditError("That is a result, not a line.")

    header = ""
    if board.fen() != chess.STARTING_FEN:
        header = f'[FEN "{board.fen()}"]\n[SetUp "1"]\n'
    pgn_text = f"{header}\n{body} *\n"

    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None or not game.variations:
        raise EditError(
            "No legal moves found in that text. Paste notation such as "
            "1. e4 e5 2. Nf3, or a PGN fragment."
        )

    # read_game is forgiving: it stops at the first illegal move and records
    # the problem instead of raising. Surface that rather than silently
    # importing half a line.
    if game.errors:
        first = str(game.errors[0])
        raise EditError(f"Could not read the whole line: {first}")

    return game


def merge_into(target: chess.pgn.GameNode, source: chess.pgn.GameNode) -> dict:
    """Graft ``source``'s children onto ``target``, recursively.

    Returns counts of what happened, so the UI can say "12 new moves, 3
    already there" instead of leaving you to guess, plus ``tip``: the node at
    the end of the *imported* main line, which is where the user wants to be
    standing afterwards.
    """
    stats = {"added": 0, "existing": 0, "comments": 0, "tip": target}

    def walk(dst, src):
        for position, src_child in enumerate(src.variations):
            _, dst_child = _find_child(dst, src_child.move)
            if dst_child is None:
                dst_child = dst.add_variation(src_child.move)
                stats["added"] += 1
            else:
                stats["existing"] += 1

            src_prose, src_circles, src_arrows = split_comment(src_child.comment)
            dst_prose, dst_circles, dst_arrows = split_comment(dst_child.comment)
            # An imported comment fills a gap but never overwrites something
            # you wrote yourself.
            if src_prose and not dst_prose:
                score, depth = dst_child.eval(), dst_child.eval_depth()
                dst_child.comment = build_comment(
                    src_prose,
                    dst_circles or src_circles,
                    dst_arrows or src_arrows,
                )
                if score is not None:
                    dst_child.set_eval(score, depth)
                stats["comments"] += 1
            elif (src_circles or src_arrows) and not (dst_circles or dst_arrows):
                score, depth = dst_child.eval(), dst_child.eval_depth()
                dst_child.comment = build_comment(dst_prose, src_circles, src_arrows)
                if score is not None:
                    dst_child.set_eval(score, depth)

            if src_child.eval() is not None and dst_child.eval() is None:
                dst_child.set_eval(src_child.eval(), src_child.eval_depth())
            dst_child.nags |= set(src_child.nags)
            # Follow the source main line so the caller can land the user at
            # the end of what was just pasted, not at the end of whatever
            # main line already ran through this node.
            if position == 0 and dst is stats["tip"]:
                stats["tip"] = dst_child

            walk(dst_child, src_child)

    walk(target, source)
    return stats


def add_line(game: chess.pgn.Game, path, text: str) -> dict:
    """Parse pasted notation and merge it in at ``path``."""
    node = resolve(game, path)
    parsed = parse_line(text, node.board())
    stats = merge_into(node, parsed)
    stats["path"] = format_path(path_of(stats.pop("tip")))
    return stats
