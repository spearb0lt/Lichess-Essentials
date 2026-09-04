"""The report as a document you can print and keep.

Deliberately a new layout rather than a reuse of the study exporter's. That
one lays out *lines of chess*: chapters, move trees, diagrams in reading
order. A weakness report is a different shape -- a headline, a ranked list of
claims, tables of numbers with a baseline to compare them against, and only
then some diagrams. Forcing it through a study layout would have produced a
study about nothing.

What is reused is the part worth reusing: the sibling exporter's board
renderer for the diagrams, and its font resolution, which already solves the
problem that the standard PDF fonts cannot draw half the characters a chess
document needs. Both are optional -- without them the document still builds,
with no diagrams and plain text.

The layout is five sections, in the order you would want to read them:

1. what was measured, and how -- so a reader can judge the numbers
2. the findings, worst first, each with its own evidence
3. what you do well, for the same reason a coach starts there
4. every slice as a table, with a bar against your own average
5. the moves that cost most, as diagrams
6. the method: what each term means and how each number is defined
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape, portrait
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas

from .bridge import study

# The same palette as the other three apps, so a printout looks like the rest.
INK = colors.HexColor("#23201c")
MUTED = colors.HexColor("#7d766c")
FAINT = colors.HexColor("#b3aca1")
RULE = colors.HexColor("#e2ddd3")
ACCENT = colors.HexColor("#4a7c59")
BAD = colors.HexColor("#a8402c")
GOOD = colors.HexColor("#2f7d4f")
WASH = colors.HexColor("#faf8f4")

MARGIN = 16 * mm


class Fonts:
    """Regular, bold and italic, from the sibling app when it is installed."""

    def __init__(self):
        module = study("fonts")
        self.module = module
        if module is not None:
            try:
                module.setup_fonts()
            except Exception:                                # noqa: BLE001
                self.module = None
        source = self.module
        self.regular = getattr(source, "FONT_REGULAR", "Helvetica")
        self.bold = getattr(source, "FONT_BOLD", "Helvetica-Bold")
        self.italic = getattr(source, "FONT_ITALIC", "Helvetica-Oblique")

    def text(self, value) -> str:
        """Whatever the fonts can actually draw."""
        value = "" if value is None else str(value)
        if self.module is not None:
            try:
                return self.module.safe_text(value)
            except Exception:                                # noqa: BLE001
                pass
        return value.encode("latin-1", "replace").decode("latin-1")


class Sheet:
    """A canvas that knows where it is on the page and when to turn it."""

    def __init__(self, path, *, title: str, page_size=None):
        self.fonts = Fonts()
        self.size = page_size or portrait(A4)
        self.canvas = pdfcanvas.Canvas(str(path), pagesize=self.size)
        self.canvas.setTitle(title)
        self.width, self.height = self.size
        self.y = self.height - MARGIN
        self.page = 1
        self.header = title

    # ------------------------------------------------------------- layout

    @property
    def left(self) -> float:
        return MARGIN

    @property
    def right(self) -> float:
        return self.width - MARGIN

    def room(self, needed: float) -> None:
        if self.y - needed < MARGIN + 12:
            self.new_page()

    def new_page(self) -> None:
        self._footer()
        self.canvas.showPage()
        self.page += 1
        self.y = self.height - MARGIN
        self._page_header()

    def _page_header(self) -> None:
        self.canvas.setFont(self.fonts.regular, 7.5)
        self.canvas.setFillColor(FAINT)
        self.canvas.drawString(self.left, self.height - MARGIN + 4,
                               self.fonts.text(self.header))

    def _footer(self) -> None:
        self.canvas.setFont(self.fonts.regular, 7.5)
        self.canvas.setFillColor(FAINT)
        self.canvas.drawRightString(self.right, MARGIN - 8, str(self.page))

    def finish(self) -> None:
        self._footer()
        self.canvas.save()

    # ------------------------------------------------------------- pieces

    def title(self, text: str, size: float = 20) -> None:
        self.room(size + 8)
        self.canvas.setFont(self.fonts.bold, size)
        self.canvas.setFillColor(INK)
        self.canvas.drawString(self.left, self.y - size, self.fonts.text(text))
        self.y -= size + 6

    def heading(self, text: str) -> None:
        self.room(26)
        self.y -= 8
        self.canvas.setFont(self.fonts.bold, 12)
        self.canvas.setFillColor(INK)
        self.canvas.drawString(self.left, self.y - 12, self.fonts.text(text))
        self.y -= 16
        self.canvas.setStrokeColor(RULE)
        self.canvas.setLineWidth(0.6)
        self.canvas.line(self.left, self.y, self.right, self.y)
        self.y -= 8

    def paragraph(self, text: str, *, size: float = 9, colour=MUTED,
                  leading: float = 12, font=None) -> None:
        font = font or self.fonts.regular
        words = self.fonts.text(text).split()
        line = ""
        for word in words:
            trial = f"{line} {word}".strip()
            if self.canvas.stringWidth(trial, font, size) > self.right - self.left:
                self._line(line, size=size, colour=colour, leading=leading,
                           font=font)
                line = word
            else:
                line = trial
        if line:
            self._line(line, size=size, colour=colour, leading=leading, font=font)
        self.y -= 3

    def _line(self, text: str, *, size, colour, leading, font) -> None:
        self.room(leading)
        self.canvas.setFont(font, size)
        self.canvas.setFillColor(colour)
        self.canvas.drawString(self.left, self.y - size, text)
        self.y -= leading

    def gap(self, amount: float = 8) -> None:
        self.y -= amount

    def stat_row(self, items) -> None:
        """A row of big-number tiles across the page."""
        if not items:
            return
        self.room(34)
        width = (self.right - self.left) / len(items)
        top = self.y
        for index, (label, value) in enumerate(items):
            x = self.left + index * width
            self.canvas.setFillColor(WASH)
            self.canvas.setStrokeColor(RULE)
            self.canvas.rect(x + 1, top - 30, width - 4, 29, stroke=1, fill=1)
            self.canvas.setFont(self.fonts.regular, 6.5)
            self.canvas.setFillColor(MUTED)
            self.canvas.drawString(x + 6, top - 10,
                                   self.fonts.text(label.upper()))
            # A long value -- a date range, say -- must shrink rather than
            # run off the edge of its tile and into the next one.
            text = self.fonts.text(value)
            size = 13.0
            while (size > 6.5 and self.canvas.stringWidth(
                    text, self.fonts.bold, size) > width - 12):
                size -= 0.5
            self.canvas.setFont(self.fonts.bold, size)
            self.canvas.setFillColor(INK)
            self.canvas.drawString(x + 6, top - 25, text)
        self.y = top - 36

    def bar(self, x: float, y: float, width: float, share: float,
            colour) -> None:
        """A proportional bar, clamped so a runaway value cannot overdraw."""
        self.canvas.setFillColor(RULE)
        self.canvas.rect(x, y, width, 4, stroke=0, fill=1)
        self.canvas.setFillColor(colour)
        self.canvas.rect(x, y, max(0.0, min(1.0, share)) * width, 4,
                         stroke=0, fill=1)


# ------------------------------------------------------------------ sections


def _cap(text: str) -> str:
    """First letter up, the rest left alone: "as White", not "As white"."""
    text = str(text or "")
    return text[:1].upper() + text[1:]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _clip(text: str, limit: int) -> str:
    """Truncate on a word boundary, so no label ends mid-syllable."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(",;") + "..."


def _fmt(value, digits=1, dash="-"):
    if value is None:
        return dash
    return f"{value:.{digits}f}"


def _cover(sheet: Sheet, report: dict) -> None:
    summary = report.get("summary") or {}
    batch = report.get("batch") or {}
    settings = batch.get("settings") or {}
    record = summary.get("record") or {}

    sheet.title("Weakness report")
    sheet.canvas.setFont(sheet.fonts.regular, 11)
    sheet.canvas.setFillColor(MUTED)
    sheet.canvas.drawString(sheet.left, sheet.y - 11,
                            sheet.fonts.text(report.get("label", "")))
    sheet.y -= 24

    span = (f"{summary.get('from', '?')} to {summary.get('to', '?')}"
            if summary.get("from") else "no dates in the games")
    sheet.stat_row([
        ("games reviewed", str(summary.get("reviewed", 0))),
        ("your moves", str(summary.get("scoredMoves", 0))),
        ("centipawn loss", _fmt(summary.get("acpl"))),
        ("accuracy", _fmt(summary.get("accuracy"))),
    ])
    sheet.stat_row([
        ("record", f"+{record.get('win', 0)} ={record.get('draw', 0)} "
                   f"-{record.get('loss', 0)}"),
        ("as white / black", f"{(summary.get('colours') or {}).get('white', 0)}"
                             f" / {(summary.get('colours') or {}).get('black', 0)}"),
        ("period", span),
        ("~ rating", str(summary.get("estimatedRating") or "-")),
    ])

    depth = settings.get("depth")
    threads = settings.get("threads")
    how = (f"Searched to a fixed depth of {depth} on {threads} thread"
           f"{'' if threads == 1 else 's'}, "
           f"multi-PV {settings.get('multipv')}." if depth else
           "Search settings unknown.")
    uniform = batch.get("uniform", True)
    if not uniform:
        how += (" Not every game was searched the same way, so the figures "
                "below mix settings and are not strictly comparable between "
                "games.")
    sheet.paragraph(how)
    if summary.get("ratingFormula"):
        sheet.paragraph(
            f"The rating is a fit, not a measurement: "
            f"{summary['ratingFormula']}. It moves on one bad game.")
    if summary.get("unreviewed"):
        sheet.paragraph(
            f"{summary['unreviewed']} of the {summary.get('games', 0)} games "
            "fetched have no review and are not counted anywhere in this "
            "document.")


def _findings(sheet: Sheet, report: dict) -> None:
    found = report.get("findings") or {}
    weaknesses = found.get("weaknesses") or []

    sheet.heading("What is costing you most")
    if not weaknesses:
        sheet.paragraph(
            "Nothing cleared the evidence floor of "
            f"{found.get('minMoves', 0)} moves across "
            f"{found.get('minGames', 0)} games. Add more games, or lower the "
            "threshold, before reading anything into the tables below.")
        return

    sheet.paragraph(
        "Ranked by excess loss: how many centipawns this kind of position has "
        "cost you beyond what you cost yourself anyway, which is what makes it "
        "worth studying rather than simply common. Each claim carries the "
        "sample it rests on.")
    sheet.gap(4)

    biggest = max(abs(row.get("excessPawnsPerGame") or 0) for row in weaknesses) or 1
    for index, row in enumerate(weaknesses, start=1):
        sheet.room(40)
        sheet.canvas.setFont(sheet.fonts.bold, 10)
        sheet.canvas.setFillColor(INK)
        sheet.canvas.drawString(
            sheet.left, sheet.y - 10,
            sheet.fonts.text(f"{index}. {_cap(row['bucket'])}"))
        sheet.canvas.setFillColor(BAD)
        sheet.canvas.drawRightString(
            sheet.right, sheet.y - 10,
            sheet.fonts.text(f"+{abs(row['excessPawnsPerGame']):.2f} pawns/game"))
        sheet.y -= 17
        sheet.bar(sheet.left, sheet.y, sheet.right - sheet.left,
                  abs(row["excessPawnsPerGame"]) / biggest, BAD)
        sheet.y -= 6
        sheet.paragraph(
            f"{_plural(row['scored'], 'move')} across "
            f"{_plural(row['games'], 'game')} at "
            f"{_fmt(row['acpl'], 0)} centipawns a move against your "
            f"{_fmt(row['baseline'], 0)}. "
            f"{_plural(row['blunders'], 'blunder')}, "
            f"{_plural(row['mistakes'], 'mistake')}. "
            f"Slice: {row['dimensionLabel']}.",
            size=8, leading=10)
        sheet.gap(2)


def _strengths(sheet: Sheet, report: dict) -> None:
    strengths = (report.get("findings") or {}).get("strengths") or []
    if not strengths:
        return
    sheet.heading("What you do well")
    sheet.paragraph(
        "The same arithmetic the other way up. Worth knowing so you do not "
        "spend a month on something you are already good at.")
    for row in strengths:
        sheet.room(16)
        sheet.canvas.setFont(sheet.fonts.regular, 9)
        sheet.canvas.setFillColor(INK)
        sheet.canvas.drawString(
            sheet.left, sheet.y - 9,
            sheet.fonts.text(f"{_cap(row['bucket'])} - "
                             f"{_fmt(row['acpl'], 0)} against your "
                             f"{_fmt(row['baseline'], 0)}, "
                             f"{row['scored']} moves"))
        sheet.canvas.setFillColor(GOOD)
        sheet.canvas.drawRightString(
            sheet.right, sheet.y - 9,
            sheet.fonts.text(f"-{abs(row['excessPawnsPerGame']):.2f} pawns/game"))
        sheet.y -= 13


def _table(sheet: Sheet, slice_data: dict, baseline) -> None:
    buckets = slice_data.get("buckets") or []
    if not buckets:
        return

    sheet.heading(slice_data.get("label", slice_data.get("key", "")))
    if slice_data.get("note"):
        sheet.paragraph(slice_data["note"], size=8, leading=10)
    sheet.gap(2)

    columns = [
        ("", 0.30), ("moves", 0.09), ("games", 0.08), ("acpl", 0.09),
        ("accuracy", 0.10), ("blunders", 0.09), ("vs your average", 0.25),
    ]
    total = sheet.right - sheet.left
    positions = []
    running = sheet.left
    for name, share in columns:
        positions.append((name, running, share * total))
        running += share * total

    sheet.room(14)
    sheet.canvas.setFont(sheet.fonts.regular, 6.5)
    sheet.canvas.setFillColor(MUTED)
    for name, x, _ in positions:
        sheet.canvas.drawString(x, sheet.y - 7, sheet.fonts.text(name.upper()))
    sheet.y -= 11

    worst = max((abs(row.get("acpl") or 0) - (baseline or 0))
                for row in buckets) or 1

    for row in buckets:
        sheet.room(13)
        sheet.canvas.setFont(sheet.fonts.regular, 8.5)
        sheet.canvas.setFillColor(INK)
        sheet.canvas.drawString(positions[0][1], sheet.y - 8,
                                sheet.fonts.text(row["bucket"]))
        sheet.canvas.setFillColor(MUTED)
        for index, value in enumerate([
                str(row["moves"]), str(row["games"]), _fmt(row["acpl"], 0),
                _fmt(row["accuracy"]),
                str((row.get("judgments") or {}).get("blunder", 0))], start=1):
            sheet.canvas.drawString(positions[index][1], sheet.y - 8,
                                    sheet.fonts.text(value))

        acpl = row.get("acpl")
        if acpl is not None and baseline:
            difference = acpl - baseline
            # Leave the right-hand end of the column for the number itself,
            # or the bar runs underneath it and both become unreadable.
            x = positions[6][1]
            width = positions[6][2] - 30
            middle = x + width / 2
            share = max(-1.0, min(1.0, difference / worst)) if worst else 0.0
            sheet.canvas.setFillColor(RULE)
            sheet.canvas.rect(x, sheet.y - 7, width, 3, stroke=0, fill=1)
            sheet.canvas.setFillColor(BAD if difference > 0 else GOOD)
            length = abs(share) * width / 2
            sheet.canvas.rect(middle if difference > 0 else middle - length,
                              sheet.y - 7, length, 3, stroke=0, fill=1)
            sheet.canvas.setFont(sheet.fonts.regular, 7)
            sheet.canvas.setFillColor(BAD if difference > 0 else GOOD)
            sheet.canvas.drawRightString(sheet.right, sheet.y - 8,
                                         f"{difference:+.0f}")
        sheet.y -= 12


def _moments(sheet: Sheet, report: dict) -> None:
    moments = report.get("worstMoments") or []
    if not moments:
        return
    render = study("render")

    sheet.new_page()
    sheet.heading("The moves that cost most")
    sheet.paragraph(
        "The single worst moves across the whole history, at most two from any "
        "one game so that one collapse does not fill the page. The move you "
        "played is named first, then the engine's.")

    if render is None:
        for row in moments:
            sheet.room(14)
            sheet.canvas.setFont(sheet.fonts.regular, 8.5)
            sheet.canvas.setFillColor(INK)
            sheet.canvas.drawString(
                sheet.left, sheet.y - 8,
                sheet.fonts.text(
                    f"{row['moveNumber']}. {row['san']} ({row['label']}) "
                    f"-{row['pawns']} pawns, best {row['bestSan']} "
                    f"— {row['situation'] or row['phase']}"))
            sheet.y -= 12
        return

    columns = 3
    size = (sheet.right - sheet.left - 2 * 8) / columns
    for index, row in enumerate(moments):
        if index % columns == 0:
            sheet.room(size + 34)
            top = sheet.y
        x = sheet.left + (index % columns) * (size + 8)
        drawing = render.board_drawing(
            row["fen"], size, flipped=row.get("you") == "black")
        if drawing is not None:
            drawing.drawOn(sheet.canvas, x, top - size)

        sheet.canvas.setFont(sheet.fonts.bold, 8)
        sheet.canvas.setFillColor(INK)
        sheet.canvas.drawString(
            x, top - size - 11,
            sheet.fonts.text(f"{row['moveNumber']}. {row['san']}"))
        sheet.canvas.setFillColor(BAD)
        sheet.canvas.drawRightString(
            x + size, top - size - 11,
            sheet.fonts.text(f"-{row['pawns']}"))
        sheet.canvas.setFont(sheet.fonts.regular, 6.5)
        sheet.canvas.setFillColor(MUTED)
        sheet.canvas.drawString(
            x, top - size - 20,
            sheet.fonts.text(f"best {row['bestSan'] or '?'} · {row['label']}"))
        sheet.canvas.drawString(
            x, top - size - 28,
            sheet.fonts.text(_clip(row["situation"] or row["phase"], 44)))

        if index % columns == columns - 1 or index == len(moments) - 1:
            sheet.y = top - size - 34


def _method(sheet: Sheet, report: dict) -> None:
    sheet.new_page()
    sheet.heading("How to read this")
    sheet.paragraph(
        "Centipawn loss is how much the engine thinks a move gave away, in "
        "hundredths of a pawn. ACPL is your average over the moves counted. "
        "Book moves are excluded from it, because playing twelve moves of "
        "theory is not evidence about your play.", size=8.5, leading=11)
    sheet.paragraph(
        "Excess loss is moves x (bucket ACPL - your overall ACPL): how much a "
        "kind of position costs you beyond your own average. It is what the "
        "findings are ranked by, because ranking by ACPL alone crowns whichever "
        "bucket is smallest, and ranking by total loss crowns the middlegame "
        "every time.", size=8.5, leading=11)
    sheet.paragraph(
        "Slices overlap, and none of them is controlled for the others. A "
        "bucket that happens to contain most of your middlegame will inherit "
        "how you play middlegames, so read two findings that cover the same "
        "moves as one observation rather than two. Near-duplicates are already "
        "dropped, by comparing the actual move sets rather than the names, but "
        "a partial overlap can still survive.", size=8.5, leading=11)
    thresholds = report.get("thresholds") or {}
    sheet.paragraph(
        f"Nothing is claimed below {thresholds.get('minMoves', 0)} counted "
        f"moves across {thresholds.get('minGames', 0)} games. Buckets under "
        "that floor are still in the tables, with their counts, so you can see "
        "what was set aside.", size=8.5, leading=11)

    sheet.heading("What the terms mean")
    glossary = report.get("glossary") or {}
    for term, meaning in glossary.items():
        sheet.room(11)
        sheet.canvas.setFont(sheet.fonts.bold, 8)
        sheet.canvas.setFillColor(INK)
        sheet.canvas.drawString(sheet.left, sheet.y - 8, sheet.fonts.text(term))
        sheet.canvas.setFont(sheet.fonts.regular, 8)
        sheet.canvas.setFillColor(MUTED)
        sheet.canvas.drawString(sheet.left + 100, sheet.y - 8,
                                sheet.fonts.text(meaning))
        sheet.y -= 10


# --------------------------------------------------------------------- build


def build(report: dict, out_path=None, *, slices=None,
          landscape_pages: bool = False, include_moments: bool = True,
          include_method: bool = True) -> Path:
    """Write the report as a PDF and return the path."""
    label = report.get("label") or "history"
    if out_path is None:
        safe = "".join(ch for ch in label if ch.isalnum() or ch in " -_").strip()
        out_path = Path(tempfile.gettempdir()) / (
            f"weakness-{(safe or 'report')[:40]}-{uuid.uuid4().hex[:6]}.pdf")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sheet = Sheet(out_path, title=f"Weakness report — {label}",
                  page_size=landscape(A4) if landscape_pages else portrait(A4))

    _cover(sheet, report)
    _findings(sheet, report)
    _strengths(sheet, report)

    wanted = slices if slices is not None else list(
        (report.get("slices") or {}).keys())
    baseline = report.get("baselineAcpl")
    available = report.get("slices") or {}
    if wanted:
        sheet.new_page()
    for key in wanted:
        data = available.get(key)
        if data:
            _table(sheet, data, baseline)

    if include_moments:
        _moments(sheet, report)
    if include_method:
        _method(sheet, report)

    sheet.finish()
    return out_path


__all__ = ["Sheet", "build"]
