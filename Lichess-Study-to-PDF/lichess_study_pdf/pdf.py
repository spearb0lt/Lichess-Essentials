"""PDF writers.

Two shapes of output, chosen per export:

``slideshow``
    One page per position.  Pressing the reader's own next-page key steps the
    board forward, so it works in every PDF viewer -- Chrome, Edge, Preview,
    pdf.js, phones -- with no scripting at all.

``acrobat``
    A single page per chapter whose board is redrawn by embedded PDF
    JavaScript.  Compact, but only Adobe Acrobat Reader runs PDF JS; see
    ``pdf_acrobat.py``.

Both share the title page, the clickable contents, and the full notation
section produced here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.graphics import renderPDF
from reportlab.platypus import Paragraph

from . import fonts
from .notation import (
    alternatives,
    child_map,
    continuation,
    notation_blocks,
)
from .render import board_drawing, draw_eval_bar

# ------------------------------------------------------------------ palette

INK = colors.HexColor("#1d1b18")
MUTED = colors.HexColor("#8b857d")
FAINT = colors.HexColor("#b9b3aa")
RULE = colors.HexColor("#ded8ce")
PANEL = colors.HexColor("#f8f6f2")
ACCENT = colors.HexColor("#4a7c59")
VARIATION_INK = colors.HexColor("#8a5a2b")
HILITE = colors.HexColor("#d8e6c4")
HILITE_EDGE = colors.HexColor("#8fae6f")


@dataclass
class PdfOptions:
    """Everything the writers let you tune from the CLI or the web UI."""

    #: grid      = twelve small boards per page (default)
    #: book      = LaTeX-typeset chess book, needs pdflatex
    #: slideshow = one big board per page, steps with the arrow keys
    #: acrobat   = layered single page, Adobe Reader only
    mode: str = "grid"
    include_notation: bool = True      # the read-through chess-book section
    include_steps: bool = True         # the diagram pages (grid or stepping)
    show_evals: bool = True
    board_size: float = 424.0
    page_size: tuple = field(default_factory=lambda: landscape(A4))
    #: Diagrams *inside the notation section*: none | comments | all | every:N.
    #: ``None`` means auto -- see ``effective_diagrams``.
    diagrams: str | None = None
    notation_diagram_size: float = 132.0
    #: The contact-sheet layout: 4 x 3 small boards to a page.
    grid_columns: int = 4
    grid_rows: int = 3
    grid_board_size: float = 122.0
    #: Board size for the LaTeX book mode, in TeX points.
    latex_diagram_size: float = 15.0
    latex_path: str | None = None      # explicit pdflatex binary
    keep_tex: str | None = None        # also save the generated .tex here
    max_depth: int | None = None       # drop sidelines deeper than this
    lookahead: int = 10                # moves of context shown after the current one
    chapter_filter: tuple | None = None

    def effective_diagrams(self) -> str:
        """Resolve the ``auto`` diagram policy for the current mode.

        In grid mode every position already has its own board, so putting
        diagrams inside the notation section as well duplicates them and
        breaks the flowing text into pages carrying only one or two boards.
        The notation section is there to be *read*; the grid is the diagrams.
        """
        if self.diagrams is not None:
            return self.diagrams
        return "none" if self.mode == "grid" else "every:6"


def _wants_diagram(policy: str, step, index_in_mainline: int) -> bool:
    if policy == "none" or not step.san:
        return False
    if policy == "all":
        return True
    if policy == "comments":
        return bool(step.comment) or bool(step.circles) or bool(step.arrows)
    if policy.startswith("every:"):
        try:
            n = int(policy.split(":", 1)[1])
        except ValueError:
            return False
        return n > 0 and index_in_mainline % n == 0
    return False


class StudyPdf:
    """Draws a parsed study onto a ReportLab canvas."""

    def __init__(self, study, evals=None, options: PdfOptions | None = None):
        self.study = study
        self.evals = evals or {}
        self.options = options or PdfOptions()
        fonts.setup_fonts()

        self.page_width, self.page_height = self.options.page_size
        self.canvas: rl_canvas.Canvas | None = None
        self._page_number = 0
        #: Chapters that already own a named destination, so the contents
        #: links always resolve no matter which sections are enabled.
        self._anchored: set = set()

        self.body_style = ParagraphStyle(
            "body",
            fontName=fonts.FONT_REGULAR,
            fontSize=9.2,
            leading=12.4,
            textColor=INK,
        )
        self.comment_style = ParagraphStyle(
            "comment",
            fontName=fonts.FONT_REGULAR,
            fontSize=9.4,
            leading=13.0,
            textColor=colors.HexColor("#33302c"),
        )
        self.notation_style = ParagraphStyle(
            "notation",
            fontName=fonts.FONT_REGULAR,
            fontSize=9.0,
            leading=12.6,
            textColor=INK,
            spaceAfter=3,
        )

    # ------------------------------------------------------------- helpers

    @property
    def chapters(self):
        if self.options.chapter_filter is None:
            return self.study.chapters
        wanted = set(self.options.chapter_filter)
        return [c for c in self.study.chapters if c.index in wanted]

    def _text(self, value: str) -> str:
        return fonts.safe_text(value or "")

    def _eval_for(self, step):
        if not self.options.show_evals:
            return None
        return self.evals.get(step.fen)

    def _finish_page(self) -> None:
        self.canvas.showPage()
        self._page_number += 1

    def _anchor_chapter(self, chapter) -> None:
        """Give the chapter a destination on the current page, exactly once."""
        if chapter.index in self._anchored:
            return
        self._anchored.add(chapter.index)
        self.canvas.bookmarkPage(f"chapter-{chapter.index}")
        self.canvas.addOutlineEntry(
            self._text(f"{chapter.index + 1}. {chapter.name}"),
            f"chapter-{chapter.index}", 1,
        )

    def _rule(self, y, x0=None, x1=None, color=RULE, width=0.6) -> None:
        c = self.canvas
        c.saveState()
        c.setStrokeColor(color)
        c.setLineWidth(width)
        c.line(x0 if x0 is not None else 32, y,
               x1 if x1 is not None else self.page_width - 32, y)
        c.restoreState()

    def _paragraph(self, html, style, x, y_top, width, max_height):
        """Draw a paragraph anchored at its top edge. Returns the height used."""
        para = Paragraph(html, style)
        _, height = para.wrap(width, max_height)
        para.drawOn(self.canvas, x, y_top - height)
        return height

    # ------------------------------------------------------------ sections

    def _title_page(self) -> None:
        c = self.canvas
        width, height = self.page_width, self.page_height

        c.setFillColor(PANEL)
        c.rect(0, height - 150, width, 150, stroke=0, fill=1)

        c.setFillColor(INK)
        c.setFont(fonts.FONT_BOLD, 27)
        title = self._text(self.study.name)
        for line in _wrap_plain(title, fonts.FONT_BOLD, 27, width - 120):
            c.drawString(56, height - 78, line)
            break  # a single headline line; the rest is summarised below

        c.setFont(fonts.FONT_REGULAR, 11)
        c.setFillColor(MUTED)
        chapters = self.chapters
        positions = sum(len(ch.steps) for ch in chapters)
        moves = sum(ch.move_count for ch in chapters)
        variations = sum(ch.variation_count for ch in chapters)
        c.drawString(
            56,
            height - 104,
            self._text(
                f"{len(chapters)} chapters  ·  {moves} moves  ·  "
                f"{variations} sidelines  ·  {positions} positions"
            ),
        )
        if self.study.source_url:
            c.setFillColor(ACCENT)
            c.setFont(fonts.FONT_REGULAR, 10)
            c.drawString(56, height - 122, self._text(self.study.source_url))
            c.linkURL(
                self.study.source_url,
                (56, height - 128, 56 + 380, height - 112),
                relative=0,
            )

        # How to use the export.
        y = height - 200
        c.setFillColor(INK)
        c.setFont(fonts.FONT_BOLD, 12)
        c.drawString(56, y, self._text("How to use this PDF"))
        y -= 20
        c.setFont(fonts.FONT_REGULAR, 10)
        c.setFillColor(colors.HexColor("#3a3630"))
        tips = []
        if self.options.include_steps:
            tips += [
                "Each position gets its own page - press the next-page key "
                "(space, arrow, PageDown) to play the moves forward.",
                "Sidelines are stepped through in place, exactly where they "
                "appear in the notation, then the main line resumes.",
            ]
        if self.options.include_notation:
            tips.append(
                "Every chapter also has a full notation section with all "
                "moves, sidelines and comments for reading straight through."
            )
        if self.options.show_evals and self.evals:
            tips.append(
                "The bar beside each board is the engine evaluation, White "
                "at the bottom."
            )
        tips.append("Chapter names in the contents are clickable.")
        for tip in tips:
            for line in _wrap_plain(self._text("- " + tip),
                                    fonts.FONT_REGULAR, 10, width - 130):
                c.drawString(56, y, line)
                y -= 14
            y -= 3

        self._finish_page()

    def _contents_page(self) -> None:
        c = self.canvas
        c.bookmarkPage("toc")
        c.addOutlineEntry("Contents", "toc", 0)

        y = self.page_height - 60
        c.setFillColor(INK)
        c.setFont(fonts.FONT_BOLD, 17)
        c.drawString(40, y, self._text("Contents"))
        y -= 10
        self._rule(y)
        y -= 22

        c.setFont(fonts.FONT_REGULAR, 10)
        for chapter in self.chapters:
            if y < 60:
                self._finish_page()
                y = self.page_height - 60
                c.setFont(fonts.FONT_REGULAR, 10)

            label = self._text(f"{chapter.index + 1}.  {chapter.name}")
            c.setFillColor(INK)
            c.drawString(48, y, label)

            meta = self._text(
                f"{chapter.move_count} moves"
                + (f"  ·  {chapter.variation_count} sidelines"
                   if chapter.variation_count else "")
            )
            c.setFillColor(MUTED)
            c.setFont(fonts.FONT_REGULAR, 8.6)
            c.drawRightString(self.page_width - 40, y, meta)
            c.setFont(fonts.FONT_REGULAR, 10)

            c.setStrokeColor(colors.HexColor("#efece6"))
            c.setLineWidth(0.5)
            c.line(48, y - 5, self.page_width - 40, y - 5)

            destination = f"chapter-{chapter.index}"
            c.linkAbsolute(
                "", destination,
                (40, y - 6, self.page_width - 40, y + 11),
            )
            y -= 20

        self._finish_page()

    # --------------------------------------------------- notation section

    def _notation_section(self, chapter) -> None:
        c = self.canvas
        options = self.options
        gutter = 26
        margin = 40
        column_width = (self.page_width - 2 * margin - gutter) / 2.0
        top = self.page_height - 74
        bottom = 52

        blocks = notation_blocks(chapter, max_depth=options.max_depth)
        kids = child_map(chapter)
        policy = options.effective_diagrams()

        # Decide which positions earn an inline diagram.
        mainline_counter = 0
        diagram_for: dict[int, bool] = {}
        for step in chapter.steps[1:]:
            if step.is_mainline:
                mainline_counter += 1
            diagram_for[step.index] = _wants_diagram(
                policy, step, mainline_counter
            )

        column = 0
        x = margin
        y = top
        first_page = True

        def start_page():
            nonlocal x, y, column, first_page
            self._chapter_header(chapter, subtitle="Full notation")
            if first_page:
                self._anchor_chapter(chapter)
                first_page = False
            column = 0
            x = margin
            y = top

        def next_column() -> None:
            nonlocal column, x, y
            if column == 0:
                column = 1
                x = margin + column_width + gutter
                y = top
            else:
                self._finish_page()
                start_page()

        start_page()

        for block in blocks:
            indent = min(block.depth, 4) * 11.0
            style = ParagraphStyle(
                f"n{block.depth}",
                parent=self.notation_style,
                leftIndent=indent,
                fontSize=9.0 if block.depth == 0 else 8.4,
                leading=12.6 if block.depth == 0 else 11.4,
                textColor=INK if block.depth == 0 else VARIATION_INK,
            )
            # notation_blocks() also feeds the browser, where emoji are fine,
            # so it does no font sanitising of its own. Markup tags are pure
            # ASCII, so running the whole string through safe_text is safe.
            para = Paragraph(self._text(block.html), style)
            _, height = para.wrap(column_width, self.page_height)

            if y - height < bottom:
                next_column()
                # A block taller than a whole column has to be split by
                # ReportLab itself; ask for the available height instead.
                if y - height < bottom:
                    pieces = para.split(column_width, y - bottom)
                    if pieces:
                        for piece in pieces:
                            _, piece_height = piece.wrap(column_width, y - bottom)
                            if y - piece_height < bottom:
                                next_column()
                                piece.wrap(column_width, y - bottom)
                                _, piece_height = piece.wrap(column_width, y - bottom)
                            piece.drawOn(c, x, y - piece_height)
                            y -= piece_height + 4
                        continue

            para.drawOn(c, x, y - height)
            y -= height + 4

            # Inline diagrams for the marked positions in this block.
            for step_index in block.step_indices:
                if not diagram_for.get(step_index):
                    continue
                step = chapter.steps[step_index]
                size = min(options.notation_diagram_size, column_width - 20)
                caption = 13
                if y - size - caption < bottom:
                    next_column()
                drawing = board_drawing(
                    step.fen, size,
                    flipped=chapter.flipped,
                    last_move=step.last_move,
                    circles=step.circles,
                    arrows=step.arrows,
                    coordinates=False,
                )
                if drawing is None:
                    continue
                dx = x + (column_width - size) / 2.0
                renderPDF.draw(drawing, c, dx, y - size)
                c.setFont(fonts.FONT_REGULAR, 7.6)
                c.setFillColor(MUTED)
                # Whoever did not just move is the side on the clock.
                to_play = "Black" if step.white_to_move_before else "White"
                c.drawCentredString(
                    x + column_width / 2.0, y - size - 9,
                    self._text(f"after {step.move_label()} - {to_play} to play"),
                )
                y -= size + caption + 6

        self._finish_page()

    def _chapter_header(self, chapter, subtitle="") -> None:
        c = self.canvas
        width = self.page_width
        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 8)
        c.drawString(40, self.page_height - 40, self._text(self.study.name))

        c.setFillColor(INK)
        c.setFont(fonts.FONT_BOLD, 13)
        c.drawString(
            40, self.page_height - 58,
            self._text(f"{chapter.index + 1}. {chapter.name}"),
        )
        if subtitle:
            c.setFillColor(MUTED)
            c.setFont(fonts.FONT_REGULAR, 9)
            c.drawRightString(width - 40, self.page_height - 58,
                              self._text(subtitle))
        self._rule(self.page_height - 66, 40, width - 40)

    # --------------------------------------------------------- grid pages

    def _grid_pages(self, chapter) -> None:
        """A contact sheet: twelve small boards to a page, in reading order.

        Every position still gets its own diagram, but twelve fit on a page
        instead of one, which is both denser to read and about twelve times
        shorter than the one-per-page stepping layout.
        """
        options = self.options
        columns, rows = options.grid_columns, options.grid_rows
        per_page = columns * rows

        margin = 34.0
        top = self.page_height - 74.0
        bottom = 56.0
        cell_w = (self.page_width - 2 * margin) / columns
        cell_h = (top - bottom) / rows
        board = min(options.grid_board_size, cell_w - 24.0, cell_h - 30.0)

        steps = chapter.steps
        total = len(steps)
        for start in range(0, total, per_page):
            batch = steps[start:start + per_page]
            self._grid_header(chapter, start, len(batch), total)

            for slot, step in enumerate(batch):
                row, column = divmod(slot, columns)
                cell_x = margin + column * cell_w
                cell_top = top - row * cell_h
                self._grid_cell(chapter, step, cell_x, cell_top,
                                cell_w, cell_h, board)

            self._grid_footer(chapter, start, len(batch), total)
            self._finish_page()

    def _grid_header(self, chapter, start, count, total) -> None:
        c = self.canvas
        self._anchor_chapter(chapter)

        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 8)
        c.drawString(34, self.page_height - 40, self._text(self.study.name))

        c.setFillColor(INK)
        c.setFont(fonts.FONT_BOLD, 12.5)
        c.drawString(34, self.page_height - 58,
                     self._text(f"{chapter.index + 1}. {chapter.name}"))

        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 8.6)
        c.drawRightString(
            self.page_width - 34, self.page_height - 58,
            self._text(f"positions {start + 1}-{start + count} of {total}"),
        )
        self._rule(self.page_height - 66, 34, self.page_width - 34)

    def _grid_footer(self, chapter, start, count, total) -> None:
        c = self.canvas
        self._rule(46, 34, self.page_width - 34, colors.HexColor("#e8e4dc"))
        c.setFillColor(FAINT)
        c.setFont(fonts.FONT_REGULAR, 7.8)
        c.drawString(34, 32, self._text(
            "Main line in black, sidelines in brown with a rule down the left."
        ))

        bar_w = 150.0
        bar_x = self.page_width - 34 - bar_w
        c.setFillColor(colors.HexColor("#eeeae2"))
        c.rect(bar_x, 31, bar_w, 3.4, stroke=0, fill=1)
        c.setFillColor(ACCENT)
        c.rect(bar_x, 31, bar_w * (start + count) / max(1, total), 3.4,
               stroke=0, fill=1)
        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 7.8)
        c.drawRightString(bar_x - 10, 32, self._text("Contents"))
        c.linkAbsolute("", "toc", (bar_x - 60, 27, bar_x - 6, 40))

    def _grid_cell(self, chapter, step, cell_x, cell_top,
                   cell_w, cell_h, board) -> None:
        c = self.canvas
        sideline = step.depth > 0
        ink = VARIATION_INK if sideline else INK

        evaluation = self._eval_for(step)
        bar_w = 5.0
        show_bar = self.options.show_evals and evaluation is not None \
            and evaluation.known

        span = board + (bar_w + 4 if show_bar else 0)
        board_x = cell_x + (cell_w - span) / 2.0 + (bar_w + 4 if show_bar else 0)
        board_y = cell_top - board

        # A rule down the left edge marks a sideline, so depth survives even
        # when the page is printed in greyscale.
        if sideline:
            c.setFillColor(VARIATION_INK)
            c.rect(cell_x + 2, board_y - 22, 1.8, board + 20, stroke=0, fill=1)
            depth_x = cell_x + 6
            c.setFont(fonts.FONT_REGULAR, 6)
            c.setFillColor(FAINT)
            for level in range(min(step.depth, 4)):
                c.drawString(depth_x + level * 3.2, cell_top - 6, "•")

        drawing = board_drawing(
            step.fen, board,
            flipped=chapter.flipped,
            last_move=step.last_move,
            circles=step.circles,
            arrows=step.arrows,
            coordinates=False,
        )
        if drawing is not None:
            renderPDF.draw(drawing, c, board_x, board_y)

        if show_bar:
            draw_eval_bar(c, board_x - bar_w - 4, board_y, bar_w, board,
                          evaluation, flipped=chapter.flipped, label=False)

        # Caption: the move, then the evaluation, then a trimmed comment.
        text_x = cell_x + 8
        text_w = cell_w - 16
        y = board_y - 10

        label = step.move_label() if step.san else "start"
        c.setFont(fonts.FONT_BOLD, 8.6)
        c.setFillColor(ink)
        c.drawString(text_x, y, self._text(label))

        if show_bar:
            c.setFont(fonts.FONT_REGULAR, 7.4)
            c.setFillColor(ACCENT if evaluation.white_fraction() >= 0.5
                           else colors.HexColor("#8a3324"))
            c.drawRightString(cell_x + cell_w - 8, y,
                              self._text(evaluation.text()))

        if step.comment:
            y -= 9
            c.setFont(fonts.FONT_REGULAR, 6.6)
            c.setFillColor(colors.HexColor("#5f584f"))
            lines = _wrap_plain(self._text(step.comment),
                                fonts.FONT_REGULAR, 6.6, text_w)
            room = int(max(0, (y - (cell_top - cell_h) + 4) // 8))
            for index, line in enumerate(lines[:max(0, min(2, room))]):
                if index == 1 and len(lines) > 2:
                    line = line[:max(0, len(line) - 1)] + "…"
                c.drawString(text_x, y, line)
                y -= 8

    # ----------------------------------------------------- stepping pages

    def _step_pages(self, chapter) -> None:
        kids = child_map(chapter)
        total = len(chapter.steps)
        for position, step in enumerate(chapter.steps):
            self._step_page(chapter, step, position, total, kids)
            self._finish_page()

    def _step_page(self, chapter, step, position, total, kids) -> None:
        c = self.canvas
        options = self.options
        width, height = self.page_width, self.page_height

        board = options.board_size
        board_x, board_y = 34.0, 62.0
        eval_x = board_x + board + 12
        eval_w = 14.0
        col_x = eval_x + eval_w + 22
        col_w = width - 34 - col_x

        # Anchor the very first page of the chapter for the contents links.
        if position == 0 and not getattr(self, "_in_form", False):
            self._anchor_chapter(chapter)

        # --- header
        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 8)
        c.drawString(board_x, height - 38, self._text(self.study.name))
        c.setFillColor(INK)
        c.setFont(fonts.FONT_BOLD, 12.5)
        c.drawString(board_x, height - 56,
                     self._text(f"{chapter.index + 1}. {chapter.name}"))
        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 8.6)
        c.drawRightString(width - 34, height - 56,
                          self._text(f"position {position + 1} of {total}"))
        self._rule(height - 64, board_x, width - 34)

        # --- board
        drawing = board_drawing(
            step.fen, board,
            flipped=chapter.flipped,
            last_move=step.last_move,
            circles=step.circles,
            arrows=step.arrows,
            coordinates=True,
        )
        if drawing is not None:
            renderPDF.draw(drawing, c, board_x, board_y)

        # --- eval bar
        evaluation = self._eval_for(step)
        if options.show_evals:
            draw_eval_bar(c, eval_x, board_y, eval_w, board, evaluation,
                          flipped=chapter.flipped)

        # --- current move headline
        y = height - 92
        c.setFillColor(INK if step.is_mainline else VARIATION_INK)
        c.setFont(fonts.FONT_BOLD, 21)
        headline = step.move_label() if step.san else "Starting position"
        c.drawString(col_x, y, self._text(headline))

        if evaluation is not None and evaluation.known:
            c.setFont(fonts.FONT_BOLD, 11)
            c.setFillColor(ACCENT if evaluation.white_fraction() >= 0.5
                           else colors.HexColor("#8a3324"))
            c.drawRightString(col_x + col_w, y, self._text(evaluation.text()))
            c.setFont(fonts.FONT_REGULAR, 7)
            c.setFillColor(FAINT)
            c.drawRightString(
                col_x + col_w, y - 11,
                self._text(f"depth {evaluation.depth} · {evaluation.source}"),
            )

        # --- breadcrumb
        y -= 20
        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 8)
        crumb = step.line_label if step.depth else "Main line"
        if step.depth:
            crumb = f"sideline (depth {step.depth}) · {crumb}"
        for line in _wrap_plain(self._text(crumb), fonts.FONT_REGULAR, 8, col_w)[:2]:
            c.drawString(col_x, y, line)
            y -= 10

        y -= 4
        self._rule(y, col_x, col_x + col_w, colors.HexColor("#e8e4dc"))
        y -= 16

        # --- the line, with the current move highlighted
        y = self._draw_move_strip(chapter, step, kids, col_x, y, col_w)

        # --- alternatives available instead of this move
        siblings = alternatives(chapter, step, kids) if step.san else []
        if siblings:
            y -= 6
            c.setFillColor(MUTED)
            c.setFont(fonts.FONT_REGULAR, 7.6)
            c.drawString(col_x, y, self._text("Also played here:"))
            y -= 11
            c.setFillColor(VARIATION_INK)
            c.setFont(fonts.FONT_REGULAR, 8.4)
            text = "   ".join(s.move_label() for s in siblings[:6])
            for line in _wrap_plain(self._text(text), fonts.FONT_REGULAR, 8.4, col_w)[:2]:
                c.drawString(col_x, y, line)
                y -= 10

        # --- comment panel
        floor = board_y
        if step.comment:
            # Start the panel right under the move strip, then let it hug the
            # text instead of stretching to the bottom of the page.
            panel_top = y - 10
            available = panel_top - floor
            if available > 34:
                para = Paragraph(
                    _escape(self._text(step.comment)), self.comment_style
                )
                _, text_height = para.wrap(col_w - 24, available - 18)
                panel_height = min(available, text_height + 20)
                panel_bottom = panel_top - panel_height

                c.setFillColor(PANEL)
                c.setStrokeColor(colors.HexColor("#e6e1d8"))
                c.setLineWidth(0.6)
                c.roundRect(col_x, panel_bottom, col_w, panel_height,
                            5, stroke=1, fill=1)
                c.setFillColor(ACCENT)
                c.rect(col_x, panel_bottom, 2.6, panel_height, stroke=0, fill=1)
                para.drawOn(c, col_x + 13, panel_top - 11 - text_height)

        # --- footer with navigation links
        self._step_footer(chapter, step, position, total, board_x, width)

    def _draw_move_strip(self, chapter, step, kids, x, y, width) -> float:
        """Lay out the current line as wrapped move tokens, current one boxed."""
        c = self.canvas
        options = self.options

        past = [chapter.steps[i] for i in step.line if i]
        ahead = continuation(chapter, step, kids, options.lookahead)

        tokens = []
        for item in past:
            tokens.append((item, item is step or item.index == step.index, False))
        for item in ahead:
            tokens.append((item, False, True))

        # Keep the window centred on the current move.
        max_tokens = 46
        if len(tokens) > max_tokens:
            current_at = next(
                (i for i, (_, is_current, _) in enumerate(tokens) if is_current),
                len(past) - 1,
            )
            start = max(0, current_at - (max_tokens - options.lookahead - 2))
            tokens = tokens[start:start + max_tokens]

        font_size = 9.2
        # Tall enough that the highlight box never touches the line above it.
        line_height = 15.0
        pad_x, pad_y = 3.0, 2.0
        cursor_x = x
        bottom_limit = 250.0

        for item, is_current, is_future in tokens:
            label = self._text(item.move_label())
            font = fonts.FONT_BOLD if is_current else fonts.FONT_REGULAR
            token_width = pdfmetrics.stringWidth(label, font, font_size)

            if cursor_x + token_width + pad_x * 2 > x + width:
                cursor_x = x
                y -= line_height
                if y < bottom_limit:
                    c.setFillColor(FAINT)
                    c.setFont(fonts.FONT_REGULAR, 8)
                    c.drawString(cursor_x, y, self._text("..."))
                    y -= line_height
                    break

            if is_current:
                c.setFillColor(HILITE)
                c.setStrokeColor(ACCENT)
                c.setLineWidth(0.7)
                c.roundRect(cursor_x - pad_x, y - pad_y,
                            token_width + pad_x * 2, font_size + pad_y * 2,
                            2.5, stroke=1, fill=1)
                c.setFillColor(INK)
            elif is_future:
                c.setFillColor(FAINT)
            elif item.depth:
                c.setFillColor(VARIATION_INK)
            else:
                c.setFillColor(colors.HexColor("#4a453e"))

            c.setFont(font, font_size)
            c.drawString(cursor_x, y, label)
            cursor_x += token_width + 7.5

            if item.comment and not is_current:
                # A small dot marks moves that carry prose.
                c.setFillColor(colors.HexColor("#c2b8a6"))
                c.circle(cursor_x - 4.0, y + font_size - 1.5, 1.1, stroke=0, fill=1)

        return y - line_height

    def _step_footer(self, chapter, step, position, total, x, width) -> None:
        c = self.canvas
        self._rule(46, x, width - 34, colors.HexColor("#e8e4dc"))

        c.setFillColor(FAINT)
        c.setFont(fonts.FONT_REGULAR, 7.8)
        c.drawString(x, 32, self._text(
            "Next page = next move   ·   previous page = take back"
        ))

        # Progress through the chapter.
        bar_w = 150.0
        bar_x = width - 34 - bar_w
        c.setFillColor(colors.HexColor("#eeeae2"))
        c.rect(bar_x, 31, bar_w, 3.4, stroke=0, fill=1)
        fraction = (position + 1) / max(1, total)
        c.setFillColor(ACCENT)
        c.rect(bar_x, 31, bar_w * fraction, 3.4, stroke=0, fill=1)

        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 7.8)
        c.drawRightString(bar_x - 10, 32, self._text("Contents"))
        c.linkAbsolute("", "toc", (bar_x - 60, 27, bar_x - 6, 40))

    # ---------------------------------------------------------------- build

    def build(self, path) -> str:
        c = rl_canvas.Canvas(
            str(path),
            pagesize=self.options.page_size,
            pageCompression=1,
        )
        c.setTitle(self._text(self.study.name))
        c.setSubject(self._text("Lichess study export"))
        c.setCreator("lichess-study-to-pdf")
        self.canvas = c

        self._title_page()
        self._contents_page()

        for chapter in self.chapters:
            if self.options.include_notation:
                self._notation_section(chapter)
            if self.options.include_steps:
                if self.options.mode == "slideshow":
                    self._step_pages(chapter)
                else:
                    self._grid_pages(chapter)

        c.showOutline()
        c.save()
        self.canvas = None
        return str(path)


# ----------------------------------------------------------------- helpers


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _wrap_plain(text: str, font_name: str, font_size: float, width: float) -> list:
    """Greedy word wrap for plain canvas strings."""
    words = (text or "").split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def build_pdf(study, path, evals=None, options: PdfOptions | None = None) -> str:
    options = options or PdfOptions()

    if options.mode == "book":
        from .pdf_latex import build_latex_pdf

        return build_latex_pdf(
            study, path, evals=evals, options=options,
            keep_tex=options.keep_tex, latex_path=options.latex_path,
        )

    if options.mode == "acrobat":
        from .pdf_acrobat import build_acrobat_pdf

        return build_acrobat_pdf(study, path, evals=evals, options=options)

    return StudyPdf(study, evals=evals, options=options).build(path)
