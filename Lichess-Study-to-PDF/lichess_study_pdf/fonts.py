"""Font resolution for the PDF writers.

ReportLab's built-in Helvetica is WinAnsi-encoded, so chess punctuation that
Lichess emits freely -- the only-move box, plus/minus, infinity, arrows, curly
quotes -- would come out as black boxes.  We therefore look for a real Unicode
TrueType family on the machine and fall back to Helvetica with transliteration
only if nothing suitable exists.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

#: Families tried in order: (family, regular, bold, italic, bold-italic)
_CANDIDATES = [
    ("DejaVuSans", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
     "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"),
    ("NotoSans", "NotoSans-Regular.ttf", "NotoSans-Bold.ttf",
     "NotoSans-Italic.ttf", "NotoSans-BoldItalic.ttf"),
    ("LiberationSans", "LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf",
     "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"),
    ("Arial", "arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
]

_SEARCH_DIRS = [
    Path(__file__).resolve().parent / "assets" / "fonts",
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts"),
    Path("/Library/Fonts"),
    Path.home() / ".fonts",
]

#: Last-resort replacements when the active font cannot draw a character.
_TRANSLITERATE = {
    "□": "[]", "∞": "inf", "⊙": "(.)", "→": "->", "↑": "^", "⇆": "<->",
    "±": "+/-", "∓": "-/+", "−": "-", "–": "-", "—": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
    "×": "x", "·": ".", "½": "1/2", "≤": "<=", "≥": ">=",
}

FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_ITALIC = "Helvetica-Oblique"
FONT_BOLD_ITALIC = "Helvetica-BoldOblique"

_registered_face = None
_charset: set | None = None


def _find(filename: str):
    for directory in _SEARCH_DIRS:
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        candidate = directory / filename
        if candidate.is_file():
            return candidate
        # Case-insensitive retry for Linux font trees.
        try:
            for found in directory.rglob(filename):
                if found.is_file():
                    return found
        except OSError:
            continue
    return None


def setup_fonts() -> str:
    """Register the best Unicode family available. Returns the family name."""
    global FONT_REGULAR, FONT_BOLD, FONT_ITALIC, FONT_BOLD_ITALIC
    global _registered_face, _charset

    if _registered_face is not None:
        return _registered_face

    for family, regular, bold, italic, bold_italic in _CANDIDATES:
        paths = [_find(name) for name in (regular, bold, italic, bold_italic)]
        if not paths[0]:
            continue
        # Substitute the regular face for any missing style.
        paths = [p or paths[0] for p in paths]
        try:
            names = (family, f"{family}-Bold", f"{family}-Italic",
                     f"{family}-BoldItalic")
            for name, path in zip(names, paths):
                pdfmetrics.registerFont(TTFont(name, str(path)))
            pdfmetrics.registerFontFamily(
                family, normal=names[0], bold=names[1],
                italic=names[2], boldItalic=names[3],
            )
        except Exception:
            continue

        FONT_REGULAR, FONT_BOLD, FONT_ITALIC, FONT_BOLD_ITALIC = names
        _registered_face = family
        try:
            # ReportLab exposes the cmap as {codepoint: glyph id}.
            face = pdfmetrics.getFont(names[0]).face
            _charset = set(face.charToGlyph.keys())
        except Exception:
            _charset = None
        return family

    _registered_face = "Helvetica"
    _charset = None
    return _registered_face


def safe_text(value: str) -> str:
    """Replace characters the active font cannot draw."""
    if not value:
        return ""
    setup_fonts()

    out = []
    for char in value:
        code = ord(char)

        # Invisible emoji plumbing -- zero-width joiners and variation
        # selectors. Arial lists these in its cmap but draws them as empty
        # boxes, so being "supported" is not enough; drop them outright.
        if unicodedata.category(char) in ("Cf", "Cs", "Co", "Cn") \
                or 0xFE00 <= code <= 0xFE0F:
            continue

        if _charset is not None:
            if code in _charset:
                out.append(char)
                continue
        elif code < 256:
            out.append(char)
            continue

        replacement = _TRANSLITERATE.get(char)
        if replacement is not None:
            out.append(replacement)
        elif code < 128:
            out.append(char)
        else:
            # Emoji and the like: drop them. A chapter called
            # "?Fritz Variation?" reads worse than "Fritz Variation".
            out.append(" ")
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()
