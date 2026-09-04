"""Optional borrowing from the sibling Lichess-Study-to-PDF app.

Two things that app already does properly, and that this one would otherwise
duplicate badly:

* **engine evaluation**, with its cloud-then-local-then-cache ladder, used
  here to suggest a move for a gap you have no answer for
* **PDF layout**, used here to print a prep sheet

and one it does that nothing else can: downloading a **private** study by
walking its chapters, which is how a study you keep to yourself can still be
used as your reference book.

All three are optional.  Without the sibling installed, Player Prepper still
scouts, still measures coverage and still lists gaps -- it just cannot
suggest a move for one, print a PDF, or read a private study.  Every feature
that needs it says so rather than failing obscurely.

Install it from the repository root::

    .lichess/Scripts/python.exe -m pip install -e Lichess-Study-to-PDF
"""

from __future__ import annotations

import importlib

INSTALL_HINT = (
    "This needs the sibling app. From the repository root run:\n"
    r"  Windows: .lichess\Scripts\python.exe -m pip install -e Lichess-Study-to-PDF"
    "\n"
    "  macOS/Linux: ./.lichess/bin/python -m pip install -e Lichess-Study-to-PDF"
)


class FeatureUnavailable(RuntimeError):
    """Raised when a feature needs the sibling app and it is not installed."""


_CACHE: dict[str, object] = {}


def optional(name: str):
    """Import ``lichess_study_pdf.<name>``, or return None."""
    if name in _CACHE:
        return _CACHE[name]
    try:
        module = importlib.import_module(f"lichess_study_pdf.{name}")
    except ImportError:
        module = None
    _CACHE[name] = module
    return module


def require(name: str, feature: str):
    module = optional(name)
    if module is None:
        raise FeatureUnavailable(f"{feature} is unavailable.\n{INSTALL_HINT}")
    return module


def status() -> dict:
    """What is actually available, for the startup banner and /api/health."""
    evals = optional("evals")
    stockfish = None
    if evals is not None:
        try:
            stockfish = evals.find_stockfish()
        except Exception:                          # noqa: BLE001
            stockfish = None

    latex = None
    pdf_latex = optional("pdf_latex")
    if pdf_latex is not None:
        try:
            latex = pdf_latex.find_latex()
        except Exception:                          # noqa: BLE001
            latex = None

    return {
        "sibling": optional("pdf") is not None,
        "stockfish": stockfish,
        "latex": latex,
        "privateStudies": optional("fetch") is not None,
    }
