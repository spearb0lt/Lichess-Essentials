"""The home page's study list.

The web app opens on an empty page until you paste a URL, which is fine once
and tedious forever.  This module reads a small, hand-editable file of studies
-- ``studies.txt`` beside the package -- and the home page turns it into
buttons.

The format is one study per line::

    ## Openings
    Fried Liver Attack Full Guide | https://lichess.org/study/i7hMEq7h/T5rBUcOn
    https://lichess.org/study/EY8AUyPd

``Name | URL``, where the name is optional; ``#`` comments; ``## Heading``
starts a section.  Deliberately forgiving: a line that cannot be read is
reported rather than raised, so one typo never costs you the whole list.

The file is re-read on every request, so editing it and refreshing the page is
enough -- no restart.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .fetch import StudyFetchError, parse_study_url

#: Where the app writes studies saved from the UI.
SAVED_HEADING = "Saved from the app"

#: Override the location with an environment variable, for anyone who would
#: rather keep their list outside the repository.
ENV_VAR = "LICHESS_STUDIES_FILE"

_DEFAULT_NAME = "studies.txt"


def studies_path() -> Path:
    """The list file: ``$LICHESS_STUDIES_FILE``, else the one in the repo."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / _DEFAULT_NAME


@dataclass(frozen=True)
class StudyEntry:
    """One line of the list, ready for the page to draw."""

    name: str                     # what the button says
    url: str                      # what it opens
    study_id: str
    chapter_id: str | None
    section: str = ""             # the "## Heading" it sits under

    @property
    def private_hint(self) -> bool:
        """A chapter URL is how a private study is reached without a token."""
        return self.chapter_id is not None


@dataclass
class StudyList:
    """Everything the home page needs, including what it could not read."""

    entries: list
    path: Path
    #: ``(line number, text)`` for lines that are not comments and not studies.
    problems: list

    @property
    def sections(self) -> list:
        """``[(heading, [entry, ...]), ...]`` in file order."""
        out: list = []
        for entry in self.entries:
            if not out or out[-1][0] != entry.section:
                out.append((entry.section, []))
            out[-1][1].append(entry)
        return out


def parse_studies(text: str) -> tuple:
    """Parse the file's text. Returns ``(entries, problems)``."""
    entries: list = []
    problems: list = []
    section = ""
    seen: set = set()

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("##"):
            section = line.lstrip("#").strip()
            continue
        if line.startswith("#"):
            continue

        # Split on the last "|": a URL never contains one, a name might.
        if "|" in line:
            name, _, url = line.rpartition("|")
            name, url = name.strip(), url.strip()
        else:
            name, url = "", line

        try:
            ref = parse_study_url(url)
        except StudyFetchError:
            problems.append((number, line))
            continue

        # One entry per study: a second line for the same study is a
        # duplicate, and the first one wins.
        if ref.study_id in seen:
            continue
        seen.add(ref.study_id)

        entries.append(
            StudyEntry(
                name=name or ref.study_id,
                url=url,
                study_id=ref.study_id,
                chapter_id=ref.chapter_id,
                section=section,
            )
        )

    return entries, problems


def load_studies(path: Path | None = None) -> StudyList:
    """Read the list. A missing file is an empty list, not an error."""
    target = Path(path) if path else studies_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return StudyList(entries=[], path=target, problems=[])
    except OSError:
        return StudyList(entries=[], path=target, problems=[])

    entries, problems = parse_studies(text)
    return StudyList(entries=entries, path=target, problems=problems)


def clean_name(value: str) -> str:
    """Make a study name safe to sit on the left of a ``|``."""
    text = re.sub(r"\s+", " ", (value or "").replace("|", "/")).strip()
    return text[:160]


def add_study(url: str, name: str = "", path: Path | None = None) -> tuple:
    """Append a study to the list, under the "saved" heading.

    Returns ``(entry, added)``: ``added`` is False when the study is already
    listed, in which case the existing entry comes back untouched.  Raises
    ``StudyFetchError`` if the URL is not a study.
    """
    ref = parse_study_url(url)
    target = Path(path) if path else studies_path()
    current = load_studies(target)

    for entry in current.entries:
        if entry.study_id == ref.study_id:
            return entry, False

    label = clean_name(name) or ref.study_id
    line = f"{label} | {url.strip()}"

    text = ""
    if target.exists():
        text = target.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    if f"## {SAVED_HEADING}" not in text:
        text += f"\n## {SAVED_HEADING}\n"
    text += line + "\n"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    return (
        StudyEntry(
            name=label,
            url=url.strip(),
            study_id=ref.study_id,
            chapter_id=ref.chapter_id,
            section=SAVED_HEADING,
        ),
        True,
    )
