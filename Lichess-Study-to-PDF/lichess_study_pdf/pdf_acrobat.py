"""Single-page-per-chapter export driven by embedded PDF JavaScript.

How it works: every position is drawn into its own Form XObject, and each of
those forms is attached to a PDF *optional content group* (a layer).  All the
layers sit stacked on one page with only the first switched on; the Next and
Previous buttons run JavaScript that flips one layer off and the next one on.

Scope, stated plainly: **only Adobe Acrobat Reader executes PDF JavaScript.**
Chrome, Edge, Firefox's pdf.js, macOS Preview and mobile viewers will show the
first position as a static page and the buttons will do nothing.  Use the
``slideshow`` mode for those -- it needs no scripting at all.
"""

from __future__ import annotations

import pikepdf
from reportlab.lib import colors
from reportlab.pdfgen import canvas as rl_canvas

from . import fonts
from .pdf import ACCENT, FAINT, INK, MUTED, PANEL, StudyPdf, _wrap_plain

#: Document-level script. Layers are looked up once and cached by name, so a
#: click is O(1) no matter how many positions the study has.
_DOC_JS = """
var LSP_DOC = this;
var LSP_MAP = null;
var LSP_AT = {};

function lspMap() {
    if (LSP_MAP) return LSP_MAP;
    LSP_MAP = {};
    try {
        var ocgs = LSP_DOC.getOCGs();
        if (ocgs) {
            for (var i = 0; i < ocgs.length; i++) {
                LSP_MAP[ocgs[i].name] = ocgs[i];
            }
        }
    } catch (e) {}
    return LSP_MAP;
}

function lspGo(chapter, target, count) {
    var map = lspMap();
    if (target < 0) target = 0;
    if (target > count - 1) target = count - 1;
    var current = LSP_AT[chapter];
    if (current === undefined) current = 0;
    if (current === target) return;
    var off = map["pos_" + chapter + "_" + current];
    var on = map["pos_" + chapter + "_" + target];
    if (off) off.state = false;
    if (on) on.state = true;
    LSP_AT[chapter] = target;
}

function lspStep(chapter, delta, count) {
    var current = LSP_AT[chapter];
    if (current === undefined) current = 0;
    lspGo(chapter, current + delta, count);
}
"""


class AcrobatStudyPdf(StudyPdf):
    """Reuses the slideshow page painter, but into layered form XObjects."""

    def __init__(self, study, evals=None, options=None):
        super().__init__(study, evals=evals, options=options)
        self._in_form = False
        #: (page_index, form_name, ocg_name) for the pikepdf pass.
        self.layers: list[tuple[int, str, str]] = []
        #: page_index -> number of positions, for bounds checking in JS.
        self.page_counts: dict[int, int] = {}

    def _step_footer(self, chapter, step, position, total, x, width) -> None:
        # Inside a form XObject annotations are meaningless, and the button row
        # is drawn once on the page rather than per layer.
        if self._in_form:
            return
        super()._step_footer(chapter, step, position, total, x, width)

    def _chapter_page(self, chapter) -> None:
        c = self.canvas
        options = self.options
        width = self.page_width
        page_index = self._page_number
        total = len(chapter.steps)
        self.page_counts[page_index] = total

        from .notation import child_map

        kids = child_map(chapter)
        self._anchor_chapter(chapter)

        # Every position becomes a layer on this one page.
        self._in_form = True
        for position, step in enumerate(chapter.steps):
            form_name = f"pos_{chapter.index}_{position}"
            c.beginForm(form_name, 0, 0, self.page_width, self.page_height)
            self._step_page(chapter, step, position, total, kids)
            c.endForm()
        self._in_form = False

        for position in range(total):
            c.doForm(f"pos_{chapter.index}_{position}")
            self.layers.append(
                (page_index, f"pos_{chapter.index}_{position}",
                 f"pos_{chapter.index}_{position}")
            )

        self._draw_buttons(chapter, total, width)
        self._finish_page()

    def _draw_buttons(self, chapter, total, width):
        """Paint the control strip; pikepdf wires the JavaScript to it later."""
        c = self.canvas
        buttons = self._button_rects(width)

        c.saveState()
        for label, (x0, y0, x1, y1), _ in buttons:
            c.setFillColor(PANEL)
            c.setStrokeColor(colors.HexColor("#d9d3c8"))
            c.setLineWidth(0.8)
            c.roundRect(x0, y0, x1 - x0, y1 - y0, 4, stroke=1, fill=1)
            c.setFillColor(INK)
            c.setFont(fonts.FONT_BOLD, 10)
            c.drawCentredString((x0 + x1) / 2.0, y0 + (y1 - y0) / 2.0 - 3.4,
                                fonts.safe_text(label))
        c.setFillColor(MUTED)
        c.setFont(fonts.FONT_REGULAR, 7.6)
        c.drawString(
            34, 18,
            fonts.safe_text(
                "Adobe Acrobat Reader only - other viewers show the first "
                "position and ignore the buttons."
            ),
        )
        c.restoreState()

    def _button_rects(self, width):
        """Returns ``(label, rect, js)`` for the control strip."""
        y0, height = 24.0, 22.0
        specs = [
            ("|<", "lspGo(%d, 0, %d);"),
            ("< Prev", "lspStep(%d, -1, %d);"),
            ("Next >", "lspStep(%d, 1, %d);"),
            (">|", "lspGo(%d, 999999, %d);"),
        ]
        out = []
        x = width - 34 - (4 * 62 + 3 * 8)
        for label, template in specs:
            out.append((label, (x, y0, x + 62, y0 + height), template))
            x += 62 + 8
        return out

    def _title_page(self) -> None:
        """Stamp the viewer warning where nobody can miss it."""
        super()._title_page()

        # The title page has already been flushed, so put the banner at the top
        # of the page that follows it.
        c = self.canvas
        width, height = self.page_width, self.page_height
        c.saveState()
        c.setFillColor(colors.HexColor("#fff2e0"))
        c.setStrokeColor(colors.HexColor("#d99a3a"))
        c.setLineWidth(1.2)
        c.roundRect(34, height - 118, width - 68, 70, 6, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#8a4b12"))
        c.setFont(fonts.FONT_BOLD, 13)
        c.drawString(50, height - 72,
                     fonts.safe_text("This file only works in Adobe Acrobat Reader."))
        c.setFont(fonts.FONT_REGULAR, 9.5)
        for offset, line in enumerate([
            "Every position is a PDF layer switched by embedded JavaScript, and "
            "only Acrobat runs it.",
            "In Chrome, Edge, Firefox, macOS Preview or on a phone you will see "
            "only the FIRST position of each",
            "chapter and the buttons will do nothing. Re-export as Slideshow or "
            "Book for those viewers.",
        ]):
            c.drawString(50, height - 88 - offset * 11, fonts.safe_text(line))
        c.restoreState()
        self._finish_page()

    def build(self, path) -> str:
        c = rl_canvas.Canvas(
            str(path), pagesize=self.options.page_size, pageCompression=1
        )
        c.setTitle(self._text(self.study.name))
        c.setCreator("lichess-study-to-pdf")
        self.canvas = c

        self._title_page()
        self._contents_page()

        for chapter in self.chapters:
            if self.options.include_notation:
                self._notation_section(chapter)
            self._chapter_page(chapter)

        c.showOutline()
        c.save()
        self.canvas = None
        return str(path)


def _wire_javascript(path, layers, page_counts, chapter_indices, button_rects):
    """Second pass: attach layers to forms and JavaScript to the buttons."""
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        all_ocgs = []
        on_list = []
        off_list = []

        by_page: dict[int, list] = {}
        for page_index, form_name, ocg_name in layers:
            by_page.setdefault(page_index, []).append((form_name, ocg_name))

        for page_index, entries in by_page.items():
            page = pdf.pages[page_index]
            resources = page.get("/Resources")
            xobjects = resources.get("/XObject") if resources is not None else None
            if xobjects is None:
                continue

            for position, (form_name, ocg_name) in enumerate(entries):
                key = f"/FormXob.{form_name}"
                if key not in xobjects:
                    continue
                ocg = pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.OCG,
                        Name=pikepdf.String(ocg_name),
                    )
                )
                xobjects[key]["/OC"] = ocg
                all_ocgs.append(ocg)
                (on_list if position == 0 else off_list).append(ocg)

            # Buttons become link annotations carrying JavaScript actions.
            chapter_index = chapter_indices[page_index]
            count = page_counts[page_index]
            annots = page.get("/Annots")
            annots = pdf.make_indirect(annots if annots is not None
                                       else pikepdf.Array([]))
            for label, rect, template in button_rects:
                js = template % (chapter_index, count)
                annots.append(
                    pdf.make_indirect(
                        pikepdf.Dictionary(
                            Type=pikepdf.Name.Annot,
                            Subtype=pikepdf.Name.Link,
                            Rect=pikepdf.Array([float(v) for v in rect]),
                            Border=pikepdf.Array([0, 0, 0]),
                            F=4,
                            A=pikepdf.Dictionary(
                                S=pikepdf.Name.JavaScript,
                                JS=pikepdf.String(js),
                            ),
                        )
                    )
                )
            page["/Annots"] = annots

        if all_ocgs:
            pdf.Root["/OCProperties"] = pikepdf.Dictionary(
                OCGs=pikepdf.Array(all_ocgs),
                D=pikepdf.Dictionary(
                    BaseState=pikepdf.Name.ON,
                    ON=pikepdf.Array(on_list),
                    OFF=pikepdf.Array(off_list),
                    Order=pikepdf.Array([]),
                    ListMode=pikepdf.Name.VisiblePages,
                ),
            )

        action = pdf.make_indirect(
            pikepdf.Dictionary(
                S=pikepdf.Name.JavaScript, JS=pikepdf.String(_DOC_JS)
            )
        )
        names = pdf.Root.get("/Names")
        if names is None:
            names = pdf.make_indirect(pikepdf.Dictionary())
            pdf.Root["/Names"] = names
        names["/JavaScript"] = pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String("LichessStudyPdf"), action])
        )

        pdf.save(path)
    return str(path)


def build_acrobat_pdf(study, path, evals=None, options=None) -> str:
    builder = AcrobatStudyPdf(study, evals=evals, options=options)

    # Record which chapter each layered page belongs to as we go.
    chapter_indices: dict[int, int] = {}
    original = builder._chapter_page

    def traced(chapter):
        chapter_indices[builder._page_number] = chapter.index
        original(chapter)

    builder._chapter_page = traced
    builder.build(path)

    return _wire_javascript(
        path,
        builder.layers,
        builder.page_counts,
        chapter_indices,
        builder._button_rects(builder.page_width),
    )
