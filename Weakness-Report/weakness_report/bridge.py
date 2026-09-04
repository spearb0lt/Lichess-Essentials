"""Borrowing the review, and why there is only ever one copy of it.

A weakness report is an aggregation.  What it aggregates -- an accuracy
figure, a centipawn loss, a move label, where the opening stops and the
middlegame starts -- is ChessAnalyzer's work, tuned and written down over in
that app's README.  This app does not have its own version of any of it, and
that is deliberate: two implementations of "was that a blunder" would sooner
or later disagree about the same move, and a report whose numbers do not match
the app you check them in is worse than no report.

So the rule here is **one algorithm, one implementation**.  What this module
does is make sure it can always be found:

1. ``chess_analyzer`` already installed (``pip install -e ChessAnalyzer``)
2. ``$CHESS_ANALYZER_DIR``, for a checkout somewhere unusual
3. the sibling folder in this repository, added to ``sys.path`` -- the same
   trick ChessAnalyzer's own test suite uses on itself

Because of (3) this needs no install at all in a normal checkout, which is
what makes "optional" and "identical" both true at once: it is optional in the
sense that you never have to do anything, and identical because when it is
found it is literally the same code rather than a second copy of it.

The one thing this app does change is *how* the engine is asked.  A review at
a movetime budget depends on how busy the machine was, which is tolerable for
one game and not tolerable for a number aggregated over four hundred of them:
re-running the report would move every figure.  So batches default to a fixed
**depth** instead, which makes a report reproducible and lets the position
cache be reused across runs.  See :mod:`weakness_report.batch`.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

INSTALL_HINT = (
    "Weakness Report reads its review rules from the sibling ChessAnalyzer\n"
    "app, so that both apps agree about the same game. Keep the folder\n"
    "'ChessAnalyzer' next to this one in the repository, or point\n"
    "CHESS_ANALYZER_DIR at your copy, or from the repository root run:\n"
    "  pip install -e ChessAnalyzer"
)

STUDY_HINT = (
    "This needs the sibling study exporter. From the repository root run:\n"
    "  pip install -e Lichess-Study-to-PDF"
)


class FeatureUnavailable(RuntimeError):
    """A feature needs a sibling app that could not be found."""


_CACHE: dict = {}


def _repo_root() -> Path:
    """The folder holding all four apps -- two levels above this file."""
    return Path(__file__).resolve().parent.parent.parent


def analyzer_dir() -> Path | None:
    """Where the ChessAnalyzer checkout is, if there is one to point at."""
    env = os.environ.get("CHESS_ANALYZER_DIR")
    if env:
        candidate = Path(env).expanduser().resolve()
        return candidate if (candidate / "chess_analyzer").is_dir() else None
    candidate = _repo_root() / "ChessAnalyzer"
    return candidate if (candidate / "chess_analyzer").is_dir() else None


def _ensure_path() -> None:
    """Put the sibling checkout on ``sys.path`` if the package is not installed."""
    if importlib.util.find_spec("chess_analyzer") is not None:
        return
    folder = analyzer_dir()
    if folder is not None and str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
        importlib.invalidate_caches()


def analyzer(name: str):
    """Import ``chess_analyzer.<name>``, or None if the app cannot be found."""
    key = f"chess_analyzer.{name}"
    if key in _CACHE:
        return _CACHE[key]
    _ensure_path()
    try:
        module = importlib.import_module(key)
    except ImportError:
        module = None
    _CACHE[key] = module
    return module


def require_analyzer(name: str, feature: str):
    module = analyzer(name)
    if module is None:
        raise FeatureUnavailable(f"{feature} needs ChessAnalyzer.\n{INSTALL_HINT}")
    return module


def study(name: str):
    """Import ``lichess_study_pdf.<name>``, or None. Used for PDF diagrams."""
    key = f"lichess_study_pdf.{name}"
    if key in _CACHE:
        return _CACHE[key]
    try:
        module = importlib.import_module(key)
    except ImportError:
        module = None
    _CACHE[key] = module
    return module


def require_study(name: str, feature: str):
    module = study(name)
    if module is None:
        raise FeatureUnavailable(f"{feature} needs the study exporter.\n{STUDY_HINT}")
    return module


def how_analyzer_was_found() -> str:
    """"installed", "folder", or "" -- for the banner and /api/health."""
    if analyzer("accuracy") is None:
        return ""
    module = analyzer("accuracy")
    path = Path(getattr(module, "__file__", "") or "")
    folder = analyzer_dir()
    if folder is not None and folder in path.parents:
        return "folder"
    return "installed"


def status() -> dict:
    """What is available, for the startup banner and ``/api/health``."""
    accuracy = analyzer("accuracy")
    engines = analyzer("engines")

    stockfish = None
    if engines is not None:
        try:
            found = engines.discover()
            stockfish = found[0]["path"] if found else None
        except Exception:                                    # noqa: BLE001
            stockfish = None

    evals = study("evals")
    if stockfish is None and evals is not None:
        try:
            stockfish = evals.find_stockfish()
        except Exception:                                    # noqa: BLE001
            stockfish = None

    return {
        "analyzer": accuracy is not None,
        "analyzerVia": how_analyzer_was_found(),
        "analyzerDir": str(analyzer_dir() or ""),
        "study": study("render") is not None,
        "stockfish": stockfish,
    }


__all__ = [
    "INSTALL_HINT",
    "FeatureUnavailable",
    "analyzer",
    "analyzer_dir",
    "how_analyzer_was_found",
    "require_analyzer",
    "require_study",
    "status",
    "study",
]
