"""Chess-book style notation: every move, every sideline, every comment.

The stepping pages are for replaying a study; this section is for reading it.
Nothing is dropped -- main line, nested sidelines, NAGs and prose all appear,
with sidelines indented by depth the way a printed opening manual does it.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape


@dataclass
class NotationBlock:
    """A run of moves at one variation depth, as Platypus-ready markup."""

    depth: int
    html: str
    step_indices: tuple = ()


def _move_markup(step, depth: int, force_number: bool) -> str:
    """Render one move as ``12.Nf3`` / ``12...Nf6`` with NAG symbols."""
    parts = []
    if step.white_to_move_before:
        parts.append(f"{step.move_number}.")
    elif force_number:
        parts.append(f"{step.move_number}...")

    san = escape(step.san) + escape(step.nags)
    number = escape("".join(parts))

    if depth == 0:
        return f"<b>{number}{san}</b>"
    return f"{number}{san}"


def _comment_markup(text: str, depth: int) -> str:
    color = "#4a4a4a" if depth == 0 else "#6a6a6a"
    return f'<i><font color="{color}">{escape(text)}</font></i>'


def notation_blocks(chapter, *, max_depth: int | None = None) -> list:
    """Flatten a chapter into indented notation blocks.

    A new block starts whenever the variation depth changes or a fresh sideline
    begins, which is what produces the indented, nested look.
    """
    blocks: list[NotationBlock] = []
    current_depth = 0
    parts: list[str] = []
    indices: list[int] = []
    # A black move needs its "12..." prefix at the start of a block and after
    # anything that interrupts the flow (a comment or a nested variation).
    force_number = True

    def flush():
        nonlocal parts, indices
        if parts:
            blocks.append(
                NotationBlock(
                    depth=current_depth,
                    html=" ".join(parts),
                    step_indices=tuple(indices),
                )
            )
        parts = []
        indices = []

    intro = chapter.steps[0].comment if chapter.steps else ""
    if intro:
        blocks.append(NotationBlock(depth=0, html=_comment_markup(intro, 0)))

    for step in chapter.steps[1:]:
        if max_depth is not None and step.depth > max_depth:
            continue

        if step.depth != current_depth or step.starts_variation:
            flush()
            current_depth = step.depth
            force_number = True

        parts.append(_move_markup(step, step.depth, force_number))
        indices.append(step.index)
        force_number = False

        if step.comment:
            parts.append(_comment_markup(step.comment, step.depth))
            force_number = True

    flush()
    return blocks


def line_summary(chapter, step, *, max_moves: int = 0) -> list:
    """The moves leading to ``step``, oldest first, for the notation column."""
    steps = [chapter.steps[i] for i in step.line if i]
    if max_moves and len(steps) > max_moves:
        steps = steps[-max_moves:]
    return steps


def child_map(chapter) -> dict:
    """Map each step index to the indices of the steps that follow it."""
    kids: dict[int, list] = {}
    for step in chapter.steps[1:]:
        parent = step.line[-2] if len(step.line) >= 2 else 0
        kids.setdefault(parent, []).append(step.index)
    return kids


def continuation(chapter, step, kids: dict, limit: int = 14) -> list:
    """The next few moves along the same line, for look-ahead context."""
    out = []
    current = step
    while len(out) < limit:
        following = None
        for index in kids.get(current.index, ()):
            candidate = chapter.steps[index]
            if not candidate.starts_variation:
                following = candidate
                break
        if following is None:
            break
        out.append(following)
        current = following
    return out


def alternatives(chapter, step, kids: dict) -> list:
    """Sibling sidelines available instead of ``step`` itself."""
    parent = step.line[-2] if len(step.line) >= 2 else 0
    return [
        chapter.steps[i]
        for i in kids.get(parent, ())
        if i != step.index
    ]
