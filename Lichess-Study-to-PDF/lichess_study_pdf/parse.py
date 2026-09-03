"""Turn study PGN into a flat, renderable sequence of positions.

The traversal order is *depth-first in PGN reading order*: a move, then any
sidelines that branch from the same parent, then the continuation of the line
we were on.  That is exactly the order the moves appear in the PGN text, so
stepping through the export feels like reading the notation aloud.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import chess
import chess.pgn
import chess.variant

# Lichess stores drawable shapes inside PGN comments.
_CSL_RE = re.compile(r"\[%csl\s+([^\]]*)\]")          # circled / tinted squares
_CAL_RE = re.compile(r"\[%cal\s+([^\]]*)\]")          # arrows
_OTHER_CMD_RE = re.compile(r"\[%\w+\s*[^\]]*\]")      # clk, emt, eval, ...

_SHAPE_COLORS = {"G": "green", "R": "red", "Y": "yellow", "B": "blue"}

#: Numeric Annotation Glyphs worth showing. Lichess writes only a small subset.
NAG_SYMBOLS = {
    1: "!", 2: "?", 3: "!!", 4: "??", 5: "!?", 6: "?!",
    7: "□",          # forced / only move
    10: "=", 13: "∞",
    14: "+/=", 15: "=/+", 16: "±", 17: "∓",
    18: "+-", 19: "-+",
    22: "⊙", 23: "⊙",
    36: "→", 40: "↑", 132: "⇆",
}


def nag_text(nags) -> str:
    """Render a set of NAG codes as the symbols a chess reader expects."""
    return "".join(NAG_SYMBOLS[n] for n in sorted(nags) if n in NAG_SYMBOLS)


def split_comment(raw):
    """Separate prose from Lichess drawing commands.

    Returns ``(text, circles, arrows)`` where circles are ``(color, square)``
    and arrows are ``(color, from_square, to_square)``.
    """
    if not raw:
        return "", [], []

    circles = []
    arrows = []

    for chunk in _CSL_RE.findall(raw):
        for item in chunk.split(","):
            item = item.strip()
            if len(item) == 3 and item[0] in _SHAPE_COLORS:
                circles.append((_SHAPE_COLORS[item[0]], item[1:].lower()))

    for chunk in _CAL_RE.findall(raw):
        for item in chunk.split(","):
            item = item.strip()
            if len(item) == 5 and item[0] in _SHAPE_COLORS:
                arrows.append(
                    (_SHAPE_COLORS[item[0]], item[1:3].lower(), item[3:5].lower())
                )

    text = _CSL_RE.sub("", raw)
    text = _CAL_RE.sub("", text)
    text = _OTHER_CMD_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text, circles, arrows


@dataclass
class Step:
    """One position in the walk: the board *after* ``san`` was played."""

    index: int                      # position within the chapter, 0 = start
    ply: int                        # sequence number within the walk
    move_number: int                # 1-based full move number
    white_to_move_before: bool      # True if White played this move
    san: str                        # "" for the chapter's starting position
    uci: str
    fen: str
    fen_before: str
    depth: int                      # 0 = main line, 1+ = nested sideline
    line_label: str                 # human-readable breadcrumb
    comment: str = ""
    nags: str = ""
    circles: list = field(default_factory=list)
    arrows: list = field(default_factory=list)
    last_move: tuple | None = None  # (from_square, to_square) for highlighting
    starts_variation: bool = False
    #: Indices of every step forming the line from the chapter start to here,
    #: this step included.  Lets the renderer show "the line you are in".
    line: tuple = ()
    #: Which sideline this step belongs to: 0 for the main line, otherwise the
    #: 1-based number of its branch within the chapter.  Every move of one
    #: sideline shares a number, a nested sideline gets its own, and siblings
    #: at a branch point are always numbered consecutively -- which is what
    #: lets the renderers colour them apart.  See ``sidelines.py``.
    branch: int = 0

    @property
    def is_mainline(self) -> bool:
        return self.depth == 0

    def move_label(self) -> str:
        """Return e.g. 12.Nf3 or 12...Nf6, with NAG symbols attached."""
        if not self.san:
            return "start"
        dots = "." if self.white_to_move_before else "..."
        return f"{self.move_number}{dots}{self.san}{self.nags}"


@dataclass
class Chapter:
    index: int
    name: str
    study_name: str
    url: str
    orientation: str                # "white" or "black"
    variant: str
    initial_fen: str
    steps: list
    intro_comment: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def flipped(self) -> bool:
        return self.orientation == "black"

    @property
    def move_count(self) -> int:
        return sum(1 for s in self.steps if s.san)

    @property
    def variation_count(self) -> int:
        return sum(1 for s in self.steps if s.starts_variation)

    @property
    def branch_count(self) -> int:
        """Highest sideline number in the chapter (0 if there are none)."""
        return max((s.branch for s in self.steps), default=0)


@dataclass
class Study:
    name: str
    chapters: list
    source_url: str = ""

    @property
    def total_steps(self) -> int:
        return sum(len(c.steps) for c in self.chapters)


def walk_chapter(game: chess.pgn.Game) -> list:
    """Flatten a game tree into PGN reading order.

    For a node whose children are ``[main, alt1, alt2]`` the PGN text reads
    ``main (alt1 ...) (alt2 ...) <rest of main>``.  We emit in that same order
    so the reader never has to jump around.
    """
    board = game.board()
    steps = []

    intro_text, intro_circles, intro_arrows = split_comment(game.comment)
    steps.append(
        Step(
            index=0,
            ply=0,
            move_number=board.fullmove_number,
            white_to_move_before=board.turn == chess.WHITE,
            san="",
            uci="",
            fen=board.fen(),
            fen_before=board.fen(),
            depth=0,
            line_label="Main line",
            comment=intro_text,
            circles=intro_circles,
            arrows=intro_arrows,
            line=(0,),
        )
    )

    def emit(node, parent_board, depth, label, starts_variation, line, branch):
        move = node.move
        san = parent_board.san(move)
        fen_before = parent_board.fen()
        move_number = parent_board.fullmove_number
        white_moved = parent_board.turn == chess.WHITE

        new_board = parent_board.copy(stack=False)
        new_board.push(move)

        text, circles, arrows = split_comment(node.comment)
        steps.append(
            Step(
                index=len(steps),
                ply=len(steps),
                move_number=move_number,
                white_to_move_before=white_moved,
                san=san,
                uci=move.uci(),
                fen=new_board.fen(),
                fen_before=fen_before,
                depth=depth,
                line_label=label,
                comment=text,
                nags=nag_text(node.nags),
                circles=circles,
                arrows=arrows,
                last_move=(move.from_square, move.to_square),
                starts_variation=starts_variation,
                line=line,
                branch=branch,
            )
        )
        return new_board

    # Hands out sideline numbers.  A branch point takes a consecutive run of
    # them for all of its alternatives *before* anything descends into the
    # first one, so siblings stay adjacent in the palette however deeply the
    # earlier ones nest.
    branch_counter = 0

    def descend(node, parent_board, depth, label, prefix, branch):
        nonlocal branch_counter
        current_node = node
        current_board = parent_board
        current_prefix = prefix

        while current_node.variations:
            children = list(current_node.variations)
            main = children[0]

            # 1. the move that continues the line we are on -- same sideline
            main_prefix = current_prefix + (len(steps),)
            main_board = emit(
                main, current_board, depth, label, False, main_prefix, branch
            )

            # 2. every sideline branching from the same parent, each its own
            alts = children[1:]
            numbers = range(branch_counter + 1, branch_counter + 1 + len(alts))
            branch_counter += len(alts)

            for alt, alt_branch in zip(alts, numbers):
                move_number = current_board.fullmove_number
                dots = "." if current_board.turn == chess.WHITE else "..."
                alt_san = current_board.san(alt.move)
                alt_label = f"{label} > {move_number}{dots}{alt_san}"
                alt_prefix = current_prefix + (len(steps),)
                alt_board = emit(
                    alt, current_board, depth + 1, alt_label, True, alt_prefix,
                    alt_branch,
                )
                descend(alt, alt_board, depth + 1, alt_label, alt_prefix,
                        alt_branch)

            # 3. carry on down the line we were following
            current_node = main
            current_board = main_board
            current_prefix = main_prefix

    descend(game, board, 0, "Main line", (0,), 0)
    return steps


def parse_study(pgn_text: str, source_url: str = "") -> Study:
    """Parse a multi-chapter study export into chapters of steps."""
    handle = io.StringIO(pgn_text)
    chapters = []
    study_name = ""

    while True:
        game = chess.pgn.read_game(handle)
        if game is None:
            break

        headers = dict(game.headers)
        study_name = study_name or headers.get("StudyName", "") or ""

        name = headers.get("ChapterName") or ""
        if not name:
            event = headers.get("Event", "")
            # Lichess builds the Event tag as "<study>: <chapter>".
            name = event.split(":")[-1].strip() if ":" in event else event
        name = name or f"Chapter {len(chapters) + 1}"

        orientation = (headers.get("Orientation") or "white").lower()
        if orientation not in ("white", "black"):
            orientation = "white"

        board = game.board()
        steps = walk_chapter(game)
        intro, _, _ = split_comment(game.comment)

        chapters.append(
            Chapter(
                index=len(chapters),
                name=name,
                study_name=headers.get("StudyName", "") or study_name,
                url=headers.get("ChapterURL", "") or source_url,
                orientation=orientation,
                variant=headers.get("Variant", "Standard") or "Standard",
                initial_fen=board.fen(),
                steps=steps,
                intro_comment=intro,
                headers=headers,
            )
        )

    if not chapters:
        raise ValueError("No chapters found in the PGN.")

    if not study_name:
        first_event = chapters[0].headers.get("Event", "Lichess study")
        study_name = first_event.split(":")[0].strip()

    return Study(
        name=study_name or "Lichess study",
        chapters=chapters,
        source_url=source_url,
    )
