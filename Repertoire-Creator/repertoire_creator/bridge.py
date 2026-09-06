"""Optional borrowing from the sibling Lichess-Study-to-PDF app.

That app already solves two problems properly -- engine evaluation with a
cloud-then-local-then-cache ladder, and PDF layout -- and reimplementing
either here would mean two copies drifting apart.  So this app imports them
when they are installed and degrades with a clear message when they are not,
rather than refusing to start.

Install the sibling from the repository root::

    .lichess/Scripts/python.exe -m pip install -e Lichess-Study-to-PDF
"""

from __future__ import annotations

import importlib

from . import paths


def _hints() -> tuple[str, str]:
    """How to get the sibling app, phrased for how this one was installed.

    From a checkout the answer is an editable install of the folder next
    door; from PyPI there is no folder, and the answer is this app's own
    ``pdf`` extra. Resolved once at import because it cannot change while
    the process is running.
    """
    if paths.source_root(__file__) is not None:
        return (
            "This needs the sibling app. From the repository root run:\n"
            r"  Windows: .lichess\Scripts\python.exe -m pip install -e Lichess-Study-to-PDF"
            "\n"
            "  macOS/Linux: ./.lichess/bin/python -m pip install -e Lichess-Study-to-PDF",
            "pip install -e Lichess-Study-to-PDF",
        )
    return (
        "This needs the study exporter, which is not installed. Run:\n"
        '  pip install "repertoire-creator[pdf]"\n'
        "or install every app at once:\n"
        "  pip install lichess-essentials",
        'pip install "repertoire-creator[pdf]"',
    )


#: Full multi-line message for an error a user has to act on, and a short
#: one-line form for a status banner that has only one line to spare.
INSTALL_HINT, INSTALL_SHORT = _hints()


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
    stockfish = evals.find_stockfish() if evals else None

    latex = None
    pdf_latex = optional("pdf_latex")
    if pdf_latex is not None:
        try:
            latex = pdf_latex.find_latex()
        except Exception:
            latex = None

    return {
        "sibling": optional("pdf") is not None,
        "stockfish": stockfish,
        "latex": latex,
    }
