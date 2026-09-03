"""Resolve Lichess study URLs and download them as PGN.

Public studies need no credentials.  Private studies normally require a token
with the ``study:read`` scope -- but Lichess is inconsistent here, and that
inconsistency is worth exploiting:

    GET /api/study/<study>.pgn            -> 403 for a private study
    GET /api/study/<study>/<chapter>.pgn  -> 200, full PGN, no token

The per-chapter endpoint does not enforce the study's privacy.  So when the
whole-study download is refused we fall back to fetching chapters one at a
time.  The chapter's own HTML page carries the complete chapter list, so a
single chapter URL is enough to rebuild the entire study.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests

API_ROOT = "https://lichess.org"
USER_AGENT = "lichess-study-to-pdf/0.2 (+https://github.com/)"

#: A study id is 8 url-safe characters; a chapter id has the same shape.
_ID = r"[A-Za-z0-9]{8}"
_URL_RE = re.compile(rf"lichess\.org/(?:study|broadcast)/(?:embed/)?({_ID})(?:/({_ID}))?")
_BARE_RE = re.compile(rf"^({_ID})(?:/({_ID}))?$")

TOKEN_ENV_VARS = ("LICHESS_TOKEN", "LICHESS_API_TOKEN")
TOKEN_FILES = (
    Path.home() / ".lichess_token",
    Path.home() / ".config" / "lichess" / "token",
)


class StudyFetchError(RuntimeError):
    """Raised when a study cannot be downloaded."""


class StudyPrivateError(StudyFetchError):
    """Raised when a study exists but the caller is not allowed to read it."""


@dataclass(frozen=True)
class StudyRef:
    study_id: str
    chapter_id: str | None = None

    @property
    def url(self) -> str:
        base = f"{API_ROOT}/study/{self.study_id}"
        return f"{base}/{self.chapter_id}" if self.chapter_id else base


def parse_study_url(value: str) -> StudyRef:
    """Accept a full study URL, a chapter URL, an embed URL, or a bare id."""
    text = (value or "").strip()
    if not text:
        raise StudyFetchError("No study URL or id supplied.")
    text = re.sub(r"\.pgn\b", "", text)
    text = re.split(r"[?#]", text, maxsplit=1)[0].rstrip("/")

    match = _URL_RE.search(text)
    if match:
        return StudyRef(match.group(1), match.group(2))

    match = _BARE_RE.match(text)
    if match:
        return StudyRef(match.group(1), match.group(2))

    raise StudyFetchError(
        f"Could not find a Lichess study id in {value!r}. "
        "Expected something like https://lichess.org/study/i7hMEq7h"
    )


def resolve_token(explicit: str | None = None) -> str | None:
    """Find an API token from an argument, the environment, or a dotfile."""
    if explicit:
        return explicit.strip()
    for var in TOKEN_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    for path in TOKEN_FILES:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    return None


def _headers(token: str | None) -> dict:
    headers = {"Accept": "application/x-chess-pgn", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


_PGN_PARAMS = {
    "clocks": "false",
    "comments": "true",
    "variations": "true",
    "orientation": "true",
    "source": "true",
}


# --------------------------------------------------------- chapter discovery


def _extract_json_array(text: str, key: str) -> str | None:
    """Pull out ``"key": [ ... ]`` with brace matching that respects strings."""
    match = re.search(r'"%s"\s*:\s*\[' % re.escape(key), text)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def discover_chapters(
    study_id: str,
    chapter_id: str,
    *,
    token: str | None = None,
    timeout: float = 30.0,
    session=None,
) -> list:
    """List a study's chapters by reading one chapter's HTML page.

    Returns ``[(chapter_id, name), ...]`` in study order, or an empty list when
    the page layout is not what we expect.  The caller must cope with that --
    Lichess can change its markup at any time.
    """
    http = session or requests
    url = f"{API_ROOT}/study/{study_id}/{chapter_id}"
    try:
        response = http.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=timeout,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []

    raw = _extract_json_array(response.text, "chapters")
    if not raw:
        return []
    try:
        chapters = json.loads(raw)
    except ValueError:
        return []

    out = []
    for entry in chapters:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            if re.fullmatch(_ID, entry["id"]):
                out.append((entry["id"], entry.get("name") or ""))
    return out


def fetch_chapter_pgn(
    study_id: str,
    chapter_id: str,
    *,
    token: str | None = None,
    timeout: float = 30.0,
    session=None,
) -> str | None:
    """Download a single chapter. Returns ``None`` if it is not readable."""
    http = session or requests
    url = f"{API_ROOT}/api/study/{study_id}/{chapter_id}.pgn"
    try:
        response = http.get(
            url, params=_PGN_PARAMS, headers=_headers(token), timeout=timeout
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    text = response.text
    return text if "[Event " in text else None


def fetch_study_by_chapters(
    ref: StudyRef,
    *,
    token: str | None = None,
    timeout: float = 30.0,
    session=None,
    progress=None,
) -> str:
    """Rebuild a study from its individual chapters.

    This is the workaround for private studies: the per-chapter PGN endpoint
    answers even when the whole-study one refuses.
    """
    if not ref.chapter_id:
        raise StudyPrivateError(
            "This study is private and no chapter was given, so its chapters "
            "cannot be listed.\n"
            "Two ways forward:\n"
            "  1. Paste a *chapter* URL instead - open the study on Lichess, "
            "click a chapter, and copy that address. It looks like\n"
            f"     {API_ROOT}/study/{ref.study_id}/XXXXXXXX\n"
            "     Every other chapter is then found automatically.\n"
            "  2. Use a token with the 'study:read' scope:\n"
            "     https://lichess.org/account/oauth/token/create?scopes[]=study:read"
        )

    session = session or requests.Session()
    chapters = discover_chapters(
        ref.study_id, ref.chapter_id, token=token, timeout=timeout, session=session
    )
    if not chapters:
        # Could not read the chapter index; at least return the one we know.
        chapters = [(ref.chapter_id, "")]

    parts = []
    failed = []
    for index, (chapter_id, name) in enumerate(chapters):
        if progress:
            progress(index, len(chapters), name)
        text = fetch_chapter_pgn(
            ref.study_id, chapter_id, token=token, timeout=timeout, session=session
        )
        if text:
            parts.append(text.strip())
        else:
            failed.append(chapter_id)
    if progress:
        progress(len(chapters), len(chapters), "")

    if not parts:
        raise StudyPrivateError(
            f"This study is private and none of its {len(chapters)} chapters "
            "could be downloaded either. A token with the 'study:read' scope "
            "would fix it:\n"
            "  https://lichess.org/account/oauth/token/create?scopes[]=study:read"
        )
    if failed:
        # Not fatal: report through the return value's absence of those games.
        pass
    return "\n\n\n".join(parts) + "\n"


def fetch_study_pgn(
    ref: StudyRef | str,
    token: str | None = None,
    *,
    timeout: float = 30.0,
    session: requests.Session | None = None,
    chapter_only: bool = False,
    allow_chapter_fallback: bool = True,
    progress=None,
) -> str:
    """Download a study (or a single chapter) as PGN text.

    When the whole-study endpoint refuses a private study, fall back to
    downloading each chapter separately -- see the module docstring.
    """
    if isinstance(ref, str):
        ref = parse_study_url(ref)

    session = session or requests.Session()

    if chapter_only:
        if not ref.chapter_id:
            raise StudyFetchError("--chapter-only needs a chapter URL.")
        text = fetch_chapter_pgn(
            ref.study_id, ref.chapter_id, token=token, timeout=timeout,
            session=session,
        )
        if text:
            return text
        raise StudyFetchError(f"Could not download chapter {ref.chapter_id}.")

    url = f"{API_ROOT}/api/study/{ref.study_id}.pgn"
    try:
        response = session.get(
            url, params=_PGN_PARAMS, headers=_headers(token), timeout=timeout
        )
    except requests.RequestException as exc:  # pragma: no cover - network
        raise StudyFetchError(f"Network error contacting Lichess: {exc}") from exc

    if response.status_code == 200:
        text = response.text
        if "[Event " not in text:
            raise StudyFetchError(
                "Lichess returned a response that does not look like PGN. "
                "The study may be empty."
            )
        return text

    if response.status_code in (401, 403):
        if allow_chapter_fallback:
            return fetch_study_by_chapters(
                ref, token=token, timeout=timeout, session=session,
                progress=progress,
            )
        hint = ("This study is private."
                if not token else "This token cannot read that study.")
        raise StudyPrivateError(
            f"{hint} Create a token with the 'study:read' scope at\n"
            "  https://lichess.org/account/oauth/token/create?scopes[]=study:read\n"
            "then pass --token, or set LICHESS_TOKEN, or save it to ~/.lichess_token"
        )
    if response.status_code == 404:
        raise StudyFetchError(f"No such study: {ref.study_id}")
    if response.status_code == 429:
        raise StudyFetchError("Rate limited by Lichess. Wait a minute and retry.")
    raise StudyFetchError(f"Lichess returned HTTP {response.status_code} for {url}")
