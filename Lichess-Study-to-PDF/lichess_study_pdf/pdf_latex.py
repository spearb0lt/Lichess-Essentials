"""Typeset a study as a real chess book, via LaTeX.

This is the "book" export mode.  It writes a ``.tex`` file and compiles it with
``pdflatex`` plus ``xskak``/``chessboard`` -- the same toolchain a printed
opening manual uses.  You get Computer Modern, justified two-column text,
figurine notation, and proper hatched diagrams with a side-to-move marker.

Needs a LaTeX installation.  ``find_latex()`` reports whether one is present;
callers should fall back to the pure-Python writers when it is not.

Note on characters: pdflatex speaks Latin-1, and Lichess chapter names are
full of emoji.  Anything it cannot set is transliterated or dropped by
``latex_escape`` rather than being allowed to abort the compile.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from .sidelines import PALETTE, slot_for, tag_for

#: Extra places to look for a TeX distribution on Windows.
_EXTRA_TEX_DIRS = [
    Path.home() / "AppData/Local/Programs/MiKTeX/miktex/bin/x64",
    Path("C:/Program Files/MiKTeX/miktex/bin/x64"),
    Path("C:/texlive/2025/bin/windows"),
    Path("C:/texlive/2024/bin/windows"),
]


class LatexUnavailable(RuntimeError):
    """Raised when no LaTeX engine can be found."""


class LatexBuildError(RuntimeError):
    """Raised when the compile itself fails."""


def find_latex(explicit: str | None = None) -> str | None:
    """Locate ``pdflatex``. Returns its path, or ``None``."""
    if explicit and Path(explicit).is_file():
        return str(Path(explicit).resolve())

    found = shutil.which("pdflatex")
    if found:
        return found

    for directory in _EXTRA_TEX_DIRS:
        for name in ("pdflatex.exe", "pdflatex"):
            candidate = directory / name
            try:
                if candidate.is_file():
                    return str(candidate.resolve())
            except OSError:
                continue
    return None


# ------------------------------------------------------------------ escaping

_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}

#: Characters worth keeping in a readable form rather than dropping.
_TRANSLITERATE = {
    "\u2018": "`", "\u2019": "'", "\u201c": "``", "\u201d": "''",
    "\u2013": "--", "\u2014": "---", "\u2026": r"\ldots{}",
    "\u2212": "-", "\u00b1": r"$\pm$", "\u2213": r"$\mp$",
    "\u221e": r"$\infty$", "\u25a1": r"$\Box$", "\u2299": r"$\odot$",
    "\u2192": r"$\rightarrow$", "\u2191": r"$\uparrow$",
    "\u21c6": r"$\leftrightarrows$", "\u00d7": r"$\times$",
    "\u00bd": r"$\frac{1}{2}$", "\u2264": r"$\leq$", "\u2265": r"$\geq$",
    "\u00b7": r"$\cdot$", "\u2022": r"$\bullet$",
    "\u2264": r"$\leq$", "\u2260": r"$\neq$",
}


def latex_escape(value: str) -> str:
    """Make arbitrary study prose safe for pdflatex.

    Emoji and other characters outside Latin-1 are dropped: pdflatex cannot
    set them at all, and a failed compile is worse than a missing glyph.
    """
    if not value:
        return ""

    out = []
    for char in value:
        if char in _LATEX_SPECIALS:
            out.append(_LATEX_SPECIALS[char])
            continue
        if char in _TRANSLITERATE:
            out.append(_TRANSLITERATE[char])
            continue
        code = ord(char)
        if code < 128:
            out.append(char)
            continue
        if code <= 0xFF:
            # Latin-1 letters survive; inputenc handles them.
            out.append(char)
            continue
        # Try to strip an accent down to ASCII before giving up.
        folded = unicodedata.normalize("NFKD", char)
        ascii_only = "".join(c for c in folded if ord(c) < 128)
        if ascii_only.strip():
            out.append(ascii_only)
    text = "".join(out)
    return re.sub(r"[ \t]+", " ", text).strip()


_PIECE_SYMBOLS = {
    "K": r"\symking{}", "Q": r"\symqueen{}", "R": r"\symrook{}",
    "B": r"\symbishop{}", "N": r"\symknight{}",
}


def figurine(san: str) -> str:
    """Render SAN with figurine piece symbols, the way chess books do."""
    if not san:
        return ""
    if san.startswith("O-O") or san.startswith("0-0"):
        text = san.replace("0", "O")
        return text.replace("#", r"\#")

    out = []
    # Leading piece letter becomes a figurine; the rest is plain.
    if san[0] in _PIECE_SYMBOLS:
        out.append(_PIECE_SYMBOLS[san[0]])
        rest = san[1:]
    else:
        rest = san

    # Promotions: "=Q" also becomes a figurine.
    index = 0
    while index < len(rest):
        char = rest[index]
        if char == "=" and index + 1 < len(rest) and rest[index + 1] in _PIECE_SYMBOLS:
            out.append("=")
            out.append(_PIECE_SYMBOLS[rest[index + 1]])
            index += 2
            continue
        if char == "#":
            out.append(r"\#")
        else:
            out.append(char)
        index += 1
    return "".join(out)


_COLOR_MAP = {"green": "green", "red": "red", "yellow": "orange", "blue": "blue"}


def _diagram(step, chapter, size: float = 32.0) -> str:
    """A ``\\chessboard`` call for one position, with its Lichess shapes."""
    options = [
        "setfen={%s}" % step.fen,
        "showmover=true",
        # chessboard calls the rank/file coordinates "label", not "coordinates".
        "label=false",
        "boardfontsize=%.1fpt" % size,
    ]
    if chapter.flipped:
        options.append("inverse=true")

    # chessboard applies pgfstyle/color to the marks that follow, so group by
    # colour and emit a fresh style each time.
    by_color: dict[str, list] = {}
    for color, from_sq, to_sq in step.arrows:
        by_color.setdefault(_COLOR_MAP.get(color, "green"), []).append(
            f"{from_sq}-{to_sq}"
        )
    for color, moves in by_color.items():
        options.append("pgfstyle=straightmove")
        options.append(f"color={color}")
        options.append("markmoves={%s}" % ",".join(moves))

    circles_by_color: dict[str, list] = {}
    for color, square in step.circles:
        circles_by_color.setdefault(_COLOR_MAP.get(color, "green"), []).append(square)
    for color, squares in circles_by_color.items():
        options.append("pgfstyle=circle")
        options.append(f"color={color}")
        options.append("markfields={%s}" % ",".join(squares))

    return "\\chessboard[%s]" % ",\n  ".join(options)


#: LaTeX is full of literal ``%`` comments, so this template uses @TOKEN@
#: placeholders rather than percent-formatting.
PREAMBLE = r"""\documentclass[@FONTSIZE@,twocolumn,@PAPER@]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[margin=1.8cm,top=2.2cm,bottom=2.0cm]{geometry}
\usepackage{xskak}
\usepackage{chessboard}
\usepackage{microtype}
\usepackage{fancyhdr}
\usepackage{needspace}
\usepackage{xcolor}
\usepackage[hidelinks]{hyperref}

@SIDECOLORS@
\definecolor{lspcomment}{HTML}{3A352E}

\setlength{\parindent}{0pt}
\setlength{\parskip}{3pt plus 1pt}
\setlength{\columnsep}{1.4em}

\pagestyle{fancy}
\fancyhf{}
\fancyhead[C]{\small\itshape\nouppercase{\leftmark}}
\fancyfoot[C]{\small\thepage}
\renewcommand{\headrulewidth}{0.4pt}

%% A sideline: indented by nesting depth and set in its own colour -- a wash
%% behind it, a rule down its left edge so the nesting still reads when the
%% page is printed in greyscale, and matching ink for the moves.
%% #1 = depth, #2 = palette slot, #3 = content.
\newcommand{\lspvarblock}[3]{%
  \par\vspace{2pt}%
  \noindent\hspace*{#1em}%
  \begingroup\setlength{\fboxsep}{2.5pt}%
  \colorbox{lsptint#2}{%
    \textcolor{lsprule#2}{\vrule width 1.3pt}%
    \hspace{0.5em}%
    \begin{minipage}[t]{\dimexpr\linewidth-#1em-1.8em-2\fboxsep\relax}%
      \small\color{lspink#2}#3%
    \end{minipage}%
  }%
  \endgroup
  \par\vspace{2pt}%
}

%% Fallback for a sideline too long to sit in an unbreakable minipage.  A
%% colorbox cannot break across a column, so this one keeps the colour and
%% the indent and gives up the wash.
\newcommand{\lspvarlong}[3]{%
  \par\begingroup\small\color{lspink#2}%
  \setlength{\leftskip}{\dimexpr#1em+1.8em\relax}%
  \noindent#3\par\endgroup
}

%% The sideline's number, printed where it opens, so the colour has a name.
\newcommand{\lsptag}[1]{{\scriptsize\textsf{#1}}~}

%% The move table: number, White's move, Black's move.
\newenvironment{lspmoves}{%
  \par\nopagebreak
  \begin{tabular}{@{}r@{\hspace{0.9em}}l@{\hspace{1.4em}}l@{}}%
}{%
  \end{tabular}\par
}

\newcommand{\lspeval}[1]{{\footnotesize\textsf{#1}}}
\newcommand{\lspchapterhead}[2]{%
  \needspace{4\baselineskip}%
  \begin{center}\bfseries #1\\[2pt]\normalfont\small #2\end{center}%
}
"""


class LatexBook:
    """Builds the ``.tex`` source for a study."""

    def __init__(self, study, evals=None, options=None):
        from .pdf import PdfOptions, _wants_diagram

        self.study = study
        self.evals = evals or {}
        self.options = options or PdfOptions()
        self._wants_diagram = _wants_diagram

    @property
    def chapters(self):
        if self.options.chapter_filter is None:
            return self.study.chapters
        wanted = set(self.options.chapter_filter)
        return [c for c in self.study.chapters if c.index in wanted]

    def _eval_note(self, step) -> str:
        if not self.options.show_evals:
            return ""
        ev = self.evals.get(step.fen)
        if ev is None or not getattr(ev, "known", False):
            return ""
        return " \\lspeval{[%s]}" % latex_escape(ev.text())

    # ------------------------------------------------------------- sections

    def _preamble(self) -> str:
        # A chess book is portrait, always. The slideshow mode's landscape
        # default is for on-screen stepping and makes no sense here.
        # One xcolor definition per tone per slot: the sideline commands
        # build the colour names from the slot number they are handed.
        definitions = []
        for color in PALETTE:
            for prefix, value in (("lspink", color.ink),
                                  ("lsprule", color.rule),
                                  ("lsptint", color.tint)):
                definitions.append(
                    r"\definecolor{%s%d}{HTML}{%s}"
                    % (prefix, color.slot, value.lstrip("#").upper())
                )
        return (PREAMBLE
                .replace("@SIDECOLORS@", "\n".join(definitions))
                .replace("@FONTSIZE@", "10pt")
                .replace("@PAPER@", "a4paper"))

    def _title_page(self) -> str:
        chapters = self.chapters
        moves = sum(c.move_count for c in chapters)
        sidelines = sum(c.variation_count for c in chapters)
        positions = sum(len(c.steps) for c in chapters)

        lines = [
            r"\onecolumn",
            r"\thispagestyle{empty}",
            r"\vspace*{6cm}",
            r"\begin{center}",
            r"{\Huge\bfseries %s}\\[1.4em]" % latex_escape(self.study.name),
            r"{\large %d chapters $\cdot$ %d moves $\cdot$ %d sidelines"
            r" $\cdot$ %d positions}\\[0.8em]" % (
                len(chapters), moves, sidelines, positions),
        ]
        if self.study.source_url:
            lines.append(r"{\small\texttt{%s}}" % latex_escape(self.study.source_url))
        lines += [
            r"\end{center}",
            r"\clearpage",
            r"\tableofcontents",
            r"\clearpage",
            r"\twocolumn",
        ]
        return "\n".join(lines)

    def _chapter(self, chapter) -> str:
        out = []
        name = latex_escape(chapter.name) or "Chapter %d" % (chapter.index + 1)
        headers = chapter.headers or {}
        meta_bits = [
            headers.get("Result", ""),
            headers.get("Date", ""),
            headers.get("ECO", ""),
        ]
        meta = " $\\cdot$ ".join(
            latex_escape(b) for b in meta_bits if b and b not in ("?", "????.??.??")
        )

        # Every chapter opens a fresh page, in every mode.
        out.append(r"\clearpage")
        out.append(r"\phantomsection")
        out.append(r"\addcontentsline{toc}{section}{%s}" % name)
        out.append(r"\markboth{%s --- %s}{}" % (
            latex_escape(self.study.name), name))
        out.append(r"\lspchapterhead{%s}{%s}" % (name, meta))

        out.append(self._chapter_body(chapter))
        out.append(r"\vspace{1em}")
        return "\n".join(out)

    def _chapter_body(self, chapter) -> str:
        options = self.options
        diagram_policy = options.effective_diagrams()
        out = []
        rows: list[list] = []

        def flush_rows():
            if not rows:
                return
            out.append(r"\begin{lspmoves}")
            for number, white, black in rows:
                out.append("%d. & %s & %s \\\\" % (number, white, black))
            out.append(r"\end{lspmoves}")
            rows.clear()

        def add_move(step):
            cell = "\\textbf{%s}%s" % (figurine(step.san), self._eval_note(step))
            if step.white_to_move_before:
                rows.append([step.move_number, cell, r"\ldots"])
            elif rows and rows[-1][0] == step.move_number and rows[-1][2] == r"\ldots":
                rows[-1][2] = cell
            else:
                rows.append([step.move_number, r"\ldots", cell])

        intro = chapter.steps[0].comment if chapter.steps else ""
        if intro:
            out.append(r"\emph{%s}" % latex_escape(intro))

        # A whole sideline is one block: collect consecutive moves at the same
        # depth and emit them together, or every move gets its own ruled strip.
        run: dict | None = None

        def flush_run():
            nonlocal run
            if run and run["parts"]:
                body = " ".join(run["parts"])
                if run["tag"]:
                    body = r"\lsptag{%s}" % tag_for(run["branch"]) + body
                # A minipage cannot break across a column, so hand anything
                # long to the breakable variant instead.
                command = "lspvarlong" if len(body) > 420 else "lspvarblock"
                out.append("\\%s{%d}{%d}{%s}" % (
                    command, min(run["depth"], 4),
                    slot_for(run["branch"]), body))
            run = None

        mainline_seen = 0
        for step in chapter.steps[1:]:
            if options.max_depth is not None and step.depth > options.max_depth:
                continue

            if step.depth == 0:
                flush_run()
                mainline_seen += 1
                add_move(step)
                if step.comment:
                    flush_rows()
                    out.append(latex_escape(step.comment))
                if self._wants_diagram(diagram_policy, step, mainline_seen):
                    flush_rows()
                    out.append(r"\begin{center}")
                    out.append(_diagram(step, chapter,
                                        options.latex_diagram_size))
                    out.append(r"\end{center}")
            else:
                flush_rows()
                if (run is None or run["depth"] != step.depth
                        or run["branch"] != step.branch
                        or step.starts_variation):
                    flush_run()
                    run = {"depth": step.depth, "branch": step.branch,
                           "parts": [], "tag": step.starts_variation}

                # Only the first move of a run needs its number spelled out
                # for Black; after that the moves read continuously.
                first = not run["parts"]
                if step.white_to_move_before:
                    prefix = "%d." % step.move_number
                elif first:
                    prefix = "%d\\ldots{}" % step.move_number
                else:
                    prefix = ""
                run["parts"].append("\\textbf{%s%s}%s" % (
                    prefix, figurine(step.san), self._eval_note(step)))
                if step.comment:
                    run["parts"].append(
                        r"\emph{%s}" % latex_escape(step.comment))

        flush_run()
        flush_rows()
        return "\n".join(out)

    def source(self) -> str:
        parts = [self._preamble(), r"\begin{document}", self._title_page()]
        for chapter in self.chapters:
            parts.append(self._chapter(chapter))
        parts.append(r"\end{document}")
        return "\n".join(parts) + "\n"


def build_latex_pdf(study, path, evals=None, options=None,
                    keep_tex: str | None = None, latex_path: str | None = None) -> str:
    """Write and compile the book PDF. Raises ``LatexUnavailable`` if no TeX."""
    engine = find_latex(latex_path)
    if not engine:
        raise LatexUnavailable(
            "No pdflatex found. Install MiKTeX or TeX Live for the book mode, "
            "or use --mode slideshow / --mode paper instead."
        )

    book = LatexBook(study, evals=evals, options=options)
    source = book.source()

    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lsp-tex-") as workdir:
        work = Path(workdir)
        tex_file = work / "study.tex"
        tex_file.write_text(source, encoding="utf-8")
        if keep_tex:
            Path(keep_tex).write_text(source, encoding="utf-8")

        env = dict(os.environ)
        env.setdefault("MIKTEX_ENABLEINSTALLER", "t")

        last_log = ""
        # Two passes so the table of contents resolves.
        for _ in range(2):
            process = subprocess.run(
                [engine, "-interaction=nonstopmode", "-halt-on-error",
                 "study.tex"],
                cwd=str(work), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=600,
            )
            last_log = process.stdout.decode("utf-8", "replace")
            if not (work / "study.pdf").is_file() and process.returncode != 0:
                break

        produced = work / "study.pdf"
        if not produced.is_file():
            errors = [ln for ln in last_log.splitlines() if ln.startswith("!")]
            detail = "\n".join(errors[:6]) or last_log[-1500:]
            raise LatexBuildError(f"pdflatex failed:\n{detail}")

        shutil.copyfile(produced, target)

    return str(target)
