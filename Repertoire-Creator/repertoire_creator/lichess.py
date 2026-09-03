"""The Lichess side: publishing a local repertoire as a study.

Everything here is the documented public API, and the shape of the sync is
dictated by what that API can and cannot do.

Can::

    POST   /api/study                              create the study
    POST   /api/study/{id}/import-pgn              create chapters from PGN
    POST   /api/study/{id}/{chapter}/moves         replace a chapter move tree
    POST   /api/study/{id}/{chapter}/tags          replace PGN tags
    DELETE /api/study/{id}/{chapter}               delete a chapter
    GET    /api/study/{id}.pgn                     read it all back
    GET    /api/study/by/{user}                    list your studies

Cannot: rename a study, change its visibility after creation, reorder
chapters, or delete a study.  Those stay manual on lichess.org, and the app
says so rather than pretending otherwise.

The consequence for sync: a chapter we have pushed before is updated in place
through ``/moves``, so pushing twice does not leave you with duplicates.  A
chapter we have never pushed goes through ``import-pgn``, whose response
tells us the new chapter id to remember.  Both directions mean **local is the
source of truth** -- a push overwrites whatever is on Lichess for that
chapter, including edits you made there in the browser.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

API = "https://lichess.org"
EXPLORER = "https://explorer.lichess.org"
USER_AGENT = "repertoire-creator/0.1 (+https://github.com/)"

TOKEN_ENV_VARS = ("LICHESS_TOKEN", "LICHESS_API_TOKEN")
TOKEN_FILES = (
    Path.home() / ".lichess_token",
    Path.home() / ".config" / "lichess" / "token",
)

#: Lichess enforces both of these; hitting them at push time after a long
#: build is a bad time to find out, so the app checks earlier too.
MAX_CHAPTERS = 64
MAX_STUDIES_PER_DAY = 30

SCOPE_URL = (
    "https://lichess.org/account/oauth/token/create"
    "?scopes[]=study:read&scopes[]=study:write"
    "&description=Repertoire%20Creator"
)


class LichessError(RuntimeError):
    """Any failure talking to Lichess, with a message worth showing a user."""


class TokenMissing(LichessError):
    pass


class RateLimited(LichessError):
    pass


def resolve_token(explicit: str | None = None) -> str | None:
    """A token from the argument, the environment, or a dotfile."""
    if explicit and explicit.strip():
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


@dataclass
class PushedChapter:
    local_id: str
    name: str
    chapter_id: str | None
    action: str                # created | updated | skipped | failed
    detail: str = ""


class LichessClient:
    """A thin, deliberately explicit wrapper. No retries you did not ask for."""

    def __init__(self, token: str | None = None, *, timeout: float = 30.0,
                 pace: float = 1.0):
        self.token = resolve_token(token)
        self.timeout = timeout
        #: Seconds between write requests. Lichess rate limits writes, and a
        #: 20-chapter push fired as fast as the socket allows earns a 429 for
        #: the whole batch.
        self.pace = pace
        self.session = requests.Session()
        self._last_write = 0.0

    # ------------------------------------------------------------- plumbing

    def _headers(self, accept: str = "application/json") -> dict:
        if not self.token:
            raise TokenMissing(
                "No Lichess API token. Create one with the study:read and "
                f"study:write scopes here:\n  {SCOPE_URL}\n"
                "Then paste it in the app, set LICHESS_TOKEN, or save it to "
                "~/.lichess_token"
            )
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "User-Agent": USER_AGENT,
        }

    def _throttle(self) -> None:
        wait = self._last_write + self.pace - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_write = time.monotonic()

    def _check(self, response, what: str):
        if response.status_code == 429:
            raise RateLimited(
                f"Lichess rate limited the {what}. Wait a full minute before "
                "trying again -- retrying sooner extends the block."
            )
        if response.status_code in (401, 403):
            raise LichessError(
                f"Lichess refused the {what} (HTTP {response.status_code}). "
                "The token needs the study:write scope, and you must own the "
                f"study or be a contributor on it.\n  {SCOPE_URL}"
            )
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = payload.get("error") or json.dumps(payload)[:300]
            except ValueError:
                detail = (response.text or "")[:300]
            raise LichessError(f"Lichess refused the {what}: {detail or response.status_code}")
        return response

    # ---------------------------------------------------------------- token

    def token_info(self) -> dict:
        """Ask Lichess who this token belongs to and what it may do.

        Uses the public ``/api/token/test`` endpoint, which is designed to
        take tokens in the request body for exactly this purpose.
        """
        if not self.token:
            raise TokenMissing("No token to check.")
        response = self.session.post(
            f"{API}/api/token/test",
            data=self.token.encode("utf-8"),
            headers={"Content-Type": "text/plain", "User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        self._check(response, "token check")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LichessError("Lichess returned a non-JSON token check.") from exc

        info = payload.get(self.token)
        if not info:
            raise LichessError("Lichess does not recognise that token.")
        scopes = [s for s in (info.get("scopes") or "").split(",") if s]
        return {
            "userId": info.get("userId"),
            "scopes": scopes,
            "expires": info.get("expires"),
            "canWrite": "study:write" in scopes,
            "canRead": "study:read" in scopes,
        }

    # --------------------------------------------------------------- studies

    def list_studies(self, username: str) -> list:
        response = self.session.get(
            f"{API}/api/study/by/{username}",
            headers=self._headers("application/x-ndjson"),
            timeout=self.timeout,
        )
        self._check(response, "study list")
        out = []
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def create_study(self, name: str, *, visibility: str = "unlisted") -> str:
        """Create a study and return its id.

        Lichess also creates one empty chapter inside it, which the first
        push adopts rather than leaving behind as an "Empty chapter".
        """
        if visibility not in ("public", "unlisted", "private"):
            raise LichessError("Visibility must be public, unlisted or private.")
        name = (name or "").strip()
        if not 2 <= len(name) <= 100:
            raise LichessError("A study name must be 2 to 100 characters.")

        self._throttle()
        response = self.session.post(
            f"{API}/api/study",
            data={
                "name": name,
                "visibility": visibility,
                # All five are required by the API. These match the defaults
                # a study gets when you create one in the browser.
                "computer": "everyone",
                "explorer": "everyone",
                "cloneable": "everyone",
                "shareable": "everyone",
                "chat": "everyone",
                "sticky": "false",
            },
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._check(response, "study creation")
        try:
            study_id = response.json()["id"]
        except (ValueError, KeyError) as exc:
            raise LichessError("Lichess created a study but did not say which.") from exc
        return study_id

    def study_chapters(self, study_id: str) -> list:
        """Chapter ids and names, read out of the study PGN export.

        There is no JSON chapter-list endpoint, but every exported chapter
        carries its own URL in a tag, and the id is the last path segment.
        """
        pgn = self.export_study(study_id)
        out = []
        name = None
        for line in pgn.splitlines():
            if line.startswith("[ChapterName "):
                name = line.split('"')[1] if '"' in line else None
            elif line.startswith("[ChapterURL ") or line.startswith("[Site "):
                if '"' not in line:
                    continue
                url = line.split('"')[1]
                parts = [p for p in url.rstrip("/").split("/") if p]
                if len(parts) >= 2 and parts[-2] == study_id:
                    out.append({"id": parts[-1], "name": name})
                    name = None
        return out

    def export_study(self, study_id: str, *, comments: bool = True,
                     variations: bool = True, orientation: bool = True) -> str:
        response = self.session.get(
            f"{API}/api/study/{study_id}.pgn",
            params={
                "clocks": "false",
                "comments": "true" if comments else "false",
                "variations": "true" if variations else "false",
                "orientation": "true" if orientation else "false",
            },
            headers=self._headers("application/x-chess-pgn"),
            timeout=self.timeout,
        )
        if response.status_code == 404:
            raise LichessError(f"No study {study_id} (or it is not visible to you).")
        self._check(response, "study export")
        return response.text

    # -------------------------------------------------------------- chapters

    def import_pgn(
        self,
        study_id: str,
        pgn: str,
        *,
        name: str | None = None,
        orientation: str | None = None,
        mode: str | None = None,
    ) -> list:
        """Create chapters from PGN. Returns the created chapters.

        One PGN game makes one chapter.  Games separated by blank lines make
        several, but we push one at a time so a failure names the chapter
        that caused it.
        """
        data = {"pgn": pgn}
        if name:
            data["name"] = name[:100]
        if orientation in ("white", "black"):
            data["orientation"] = orientation
        if mode in ("practice", "conceal", "gamebook"):
            data["mode"] = mode

        self._throttle()
        response = self.session.post(
            f"{API}/api/study/{study_id}/import-pgn",
            data=data,
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._check(response, f"import of {name or 'a chapter'}")
        try:
            return response.json().get("chapters", [])
        except ValueError as exc:
            raise LichessError("Lichess imported the PGN but sent no chapter list.") from exc

    def update_moves(self, study_id: str, chapter_id: str, pgn: str) -> None:
        """Replace a chapter move tree. Tags are left alone."""
        self._throttle()
        response = self.session.post(
            f"{API}/api/study/{study_id}/{chapter_id}/moves",
            data={"pgn": pgn},
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._check(response, f"move update of chapter {chapter_id}")

    def update_tags(self, study_id: str, chapter_id: str, tags: dict) -> None:
        """Replace PGN tags. Only the tags you send are touched."""
        lines = "\n".join(f'[{key} "{value}"]' for key, value in tags.items())
        self._throttle()
        response = self.session.post(
            f"{API}/api/study/{study_id}/{chapter_id}/tags",
            data={"pgn": lines + "\n"},
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._check(response, f"tag update of chapter {chapter_id}")

    def delete_chapter(self, study_id: str, chapter_id: str) -> None:
        self._throttle()
        response = self.session.delete(
            f"{API}/api/study/{study_id}/{chapter_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        self._check(response, f"deletion of chapter {chapter_id}")

    # -------------------------------------------------------------- explorer

    def explorer(
        self,
        fen: str,
        *,
        speeds=("blitz", "rapid", "classical"),
        ratings=(1600, 1800, 2000, 2200, 2500),
        moves: int = 12,
    ) -> dict:
        """Which moves people actually play here.

        The explorer lives on its own host and, as of the current API spec,
        requires an authenticated request -- the same token used for pushing
        satisfies it, whatever its scopes.
        """
        response = self.session.get(
            f"{EXPLORER}/lichess",
            params={
                "variant": "standard",
                "fen": fen,
                "speeds": ",".join(speeds),
                "ratings": ",".join(str(r) for r in ratings),
                "moves": moves,
                "topGames": 0,
                "recentGames": 0,
            },
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code == 429:
            raise RateLimited(
                "The opening explorer rate limited us. Wait a minute, then "
                "scan again -- it is a separate limit from the main API."
            )
        self._check(response, "opening explorer lookup")
        try:
            return response.json()
        except ValueError as exc:
            raise LichessError("The explorer returned something that is not JSON.") from exc
