"""Getting your games in, from any of the three doors.

This is not the same job as Player-Prepper's fetcher, which is why it is not
the same code.  That one wants a few hundred games as compact move lists to
build an opening tree; this one needs the **PGN** of each game, because every
one of them is going to be handed to a review that replays it move by move.
Storing UCI here and rebuilding the PGN later would be work done twice and a
chance to get it wrong.

Three doors, all producing one shape:

* **Lichess** -- one streaming ndjson request with the PGN inside it.
* **Chess.com** -- monthly archives, newest first, until there are enough.
* **A PGN file or folder** -- anything you exported from anywhere, including
  games played over the board.
* **ChessAnalyzer's library** -- games you have already reviewed there. Read
  only: this app never writes into another app's folder.

Which side you had is worked out per game and stored on it, because every
number in the report is from your point of view and nothing downstream should
have to guess.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import chess.pgn
import requests

from .bridge import analyzer
from .store import digest

USER_AGENT = ("weakness-report/0.1 (local game-history review tool; "
              "https://github.com/lichess-essentials)")

SPEEDS = ("bullet", "blitz", "rapid", "classical")

LICHESS_PERF = {
    "bullet": ("ultraBullet", "bullet"),
    "blitz": ("blitz",),
    "rapid": ("rapid",),
    "classical": ("classical", "correspondence"),
}
LICHESS_SPEED_BACK = {
    "ultraBullet": "bullet", "bullet": "bullet", "blitz": "blitz",
    "rapid": "rapid", "classical": "classical", "correspondence": "classical",
}
CHESSCOM_SPEED_BACK = {
    "bullet": "bullet", "blitz": "blitz", "rapid": "rapid", "daily": "classical",
}

#: A dormant account must not turn one click into a hundred requests.
MAX_MONTHS = 36

#: Reviewing is minutes per game, so the ceiling here is about protecting you
#: from a typo rather than about protecting the sites.
MAX_GAMES = 1000


class SourceError(RuntimeError):
    """Games could not be read, with a message worth showing."""


def _headers(token=None, accept="application/json") -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _check(response, site: str) -> None:
    if response.status_code == 404:
        raise SourceError(f"{site} has no such player.")
    if response.status_code == 429:
        raise SourceError(
            f"{site} rate limited us. Wait a minute and try again"
            + (" -- a Lichess API token raises the limit considerably."
               if site == "Lichess" else "."))
    if not response.ok:
        raise SourceError(f"{site} said {response.status_code}: "
                          f"{response.text[:200]}")


def _iso_day(milliseconds) -> str:
    if not milliseconds:
        return ""
    try:
        return datetime.fromtimestamp(
            milliseconds / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _my_colour(white: str, black: str, me: str) -> str:
    lowered = (me or "").strip().lstrip("@").lower()
    if (white or "").lower() == lowered:
        return "white"
    if (black or "").lower() == lowered:
        return "black"
    return ""


def _record(*, game_id, source, url, white, black, white_elo, black_elo,
            result, date, speed, rated, time_control, pgn, me) -> dict:
    """The one shape every door produces."""
    colour = _my_colour(white, black, me)
    return {
        "id": game_id,
        "source": source,
        "url": url,
        "white": white, "black": black,
        "whiteElo": str(white_elo or ""), "blackElo": str(black_elo or ""),
        "result": result,
        "date": date,
        "speed": speed,
        "rated": bool(rated),
        "timeControl": time_control,
        "you": colour,
        "youElo": (white_elo if colour == "white" else black_elo) or "",
        "themElo": (black_elo if colour == "white" else white_elo) or "",
        "them": black if colour == "white" else white,
        "pgn": pgn,
    }


# ------------------------------------------------------------------ lichess


def _lichess(username, *, limit, speeds, rated_only, since_ms, token,
             progress=None, should_stop=None) -> list:
    params = {
        "max": limit,
        "sort": "dateDesc",
        "pgnInJson": "true",
        "clocks": "true",          # the review reads these for time pressure
        "evals": "false",
        "opening": "false",
        "tags": "true",
    }
    if rated_only:
        params["rated"] = "true"
    if since_ms:
        params["since"] = int(since_ms)
    if speeds:
        perfs = [perf for speed in speeds for perf in LICHESS_PERF.get(speed, ())]
        if perfs:
            params["perfType"] = ",".join(perfs)

    handle = username.strip().lstrip("@")
    try:
        response = requests.get(
            f"https://lichess.org/api/games/user/{handle}",
            headers=_headers(token, "application/x-ndjson"),
            params=params, timeout=180, stream=True)
    except requests.RequestException as exc:
        raise SourceError(f"Could not reach Lichess: {exc}") from exc
    _check(response, "Lichess")

    rows = []
    for line in response.iter_lines(decode_unicode=True):
        if should_stop and should_stop():
            break
        if not line or not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if (data.get("variant") or "standard") != "standard":
            continue
        if not data.get("pgn"):
            continue

        players = data.get("players") or {}

        def side(colour):
            entry = players.get(colour) or {}
            user = entry.get("user") or {}
            name = user.get("name") or (
                f"Stockfish level {entry['aiLevel']}"
                if entry.get("aiLevel") is not None else "Anonymous")
            return name, entry.get("rating")

        white, white_elo = side("white")
        black, black_elo = side("black")
        if not _my_colour(white, black, handle):
            continue

        winner = data.get("winner")
        result = ("1-0" if winner == "white"
                  else "0-1" if winner == "black" else "1/2-1/2")
        clock = data.get("clock") or {}

        rows.append(_record(
            game_id=f"lichess-{data.get('id')}", source="lichess",
            url=f"https://lichess.org/{data.get('id', '')}",
            white=white, black=black, white_elo=white_elo, black_elo=black_elo,
            result=result, date=_iso_day(data.get("createdAt")),
            speed=LICHESS_SPEED_BACK.get(data.get("speed", ""), "blitz"),
            rated=data.get("rated"),
            time_control=(f"{clock.get('initial', 0)}+{clock.get('increment', 0)}"
                          if clock else ""),
            pgn=data["pgn"], me=handle))
        if progress:
            progress(len(rows), limit)
        if len(rows) >= limit:
            break

    response.close()
    return rows


# ----------------------------------------------------------------- chess.com


def _chesscom_archives(username) -> list:
    handle = username.strip().lstrip("@").lower()
    try:
        response = requests.get(
            f"https://api.chess.com/pub/player/{handle}/games/archives",
            headers=_headers(), timeout=30)
    except requests.RequestException as exc:
        raise SourceError(f"Could not reach Chess.com: {exc}") from exc
    _check(response, "Chess.com")
    try:
        return response.json().get("archives", []) or []
    except ValueError as exc:
        raise SourceError("Chess.com returned something that is not JSON.") from exc


def _months_since(months: list, since_ms) -> list:
    if not since_ms:
        return list(months)
    cutoff = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
    keep = []
    for url in months:
        parts = url.rstrip("/").rsplit("/", 2)
        try:
            year, month = int(parts[-2]), int(parts[-1])
        except (IndexError, ValueError):
            keep.append(url)
            continue
        if (year, month) >= (cutoff.year, cutoff.month):
            keep.append(url)
    return keep


def _chesscom(username, *, limit, speeds, rated_only, since_ms,
              progress=None, should_stop=None) -> list:
    months = _months_since(_chesscom_archives(username), since_ms)
    handle = username.strip().lstrip("@")
    rows = []

    for url in list(reversed(months))[:MAX_MONTHS]:
        if should_stop and should_stop():
            break
        try:
            response = requests.get(url, headers=_headers(), timeout=120)
        except requests.RequestException as exc:
            raise SourceError(f"Could not reach Chess.com: {exc}") from exc
        _check(response, "Chess.com")
        try:
            games = response.json().get("games", []) or []
        except ValueError:
            continue

        for data in reversed(games):
            if should_stop and should_stop():
                break
            if (data.get("rules") or "chess") != "chess":
                continue
            if rated_only and not data.get("rated"):
                continue
            speed = CHESSCOM_SPEED_BACK.get(data.get("time_class", ""), "blitz")
            if speeds and speed not in speeds:
                continue
            if not data.get("pgn"):
                continue

            white = data.get("white") or {}
            black = data.get("black") or {}
            names = (white.get("username", ""), black.get("username", ""))
            if not _my_colour(names[0], names[1], handle):
                continue

            if white.get("result") == "win":
                result = "1-0"
            elif black.get("result") == "win":
                result = "0-1"
            else:
                result = "1/2-1/2"

            game_url = data.get("url", "")
            reference = game_url.rstrip("/").rsplit("/", 1)[-1] or str(
                data.get("uuid", ""))[:12]

            rows.append(_record(
                game_id=f"chesscom-{reference}", source="chesscom",
                url=game_url, white=names[0], black=names[1],
                white_elo=white.get("rating"), black_elo=black.get("rating"),
                result=result, date=_iso_day((data.get("end_time") or 0) * 1000),
                speed=speed, rated=data.get("rated"),
                time_control=data.get("time_control", ""),
                pgn=data["pgn"], me=handle))
            if progress:
                progress(len(rows), limit)
            if len(rows) >= limit:
                return rows
    return rows


# ------------------------------------------------------------------- a PGN


def _headers_of(game) -> dict:
    return {key: value for key, value in game.headers.items()}


def from_pgn_text(text: str, *, me: str, source: str = "pgn",
                  origin: str = "") -> list:
    """Every game in a PGN blob, as records. Variants and empties are skipped."""
    handle = io.StringIO(text)
    rows = []
    while True:
        try:
            game = chess.pgn.read_game(handle)
        except Exception:                                    # noqa: BLE001
            break
        if game is None:
            break
        headers = _headers_of(game)
        variant = (headers.get("Variant", "") or "").strip().lower()
        if variant not in ("", "standard", "chess", "from position"):
            continue
        if not any(True for _ in game.mainline_moves()):
            continue

        exporter = chess.pgn.StringExporter(headers=True, variations=False,
                                            comments=True)
        pgn = game.accept(exporter)
        white = headers.get("White", "?")
        black = headers.get("Black", "?")
        if me and not _my_colour(white, black, me):
            continue

        site = headers.get("Site", "")
        rows.append(_record(
            game_id=f"pgn-{digest(pgn)}", source=source,
            url=site if site.startswith("http") else origin,
            white=white, black=black,
            white_elo=headers.get("WhiteElo"), black_elo=headers.get("BlackElo"),
            result=headers.get("Result", "*"),
            date=(headers.get("UTCDate") or headers.get("Date", "")
                  ).replace(".", "-"),
            speed="", rated=headers.get("Event", "").lower().find("rated") >= 0,
            time_control=headers.get("TimeControl", ""),
            pgn=pgn, me=me))
    return rows


def from_pgn_path(path, *, me: str, limit: int = MAX_GAMES) -> list:
    """A ``.pgn`` file, or every ``.pgn`` in a folder."""
    target = Path(path).expanduser()
    if not target.exists():
        raise SourceError(f"No such file or folder: {target}")

    files = sorted(target.rglob("*.pgn")) if target.is_dir() else [target]
    if not files:
        raise SourceError(f"No .pgn files in {target}")

    rows = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SourceError(f"Could not read {file}: {exc}") from exc
        rows.extend(from_pgn_text(text, me=me, source="pgn", origin=str(file)))
        if len(rows) >= limit:
            break
    return rows[:limit]


# ------------------------------------------------- ChessAnalyzer's library


def analyzer_library(*, me: str, limit: int = MAX_GAMES) -> list:
    """Games already in ChessAnalyzer, reviews included where they exist.

    Read only.  Each row carries ``existingReview`` when that app has already
    analysed the game, which is the whole point: those cost no engine time
    here at all.
    """
    library_module = analyzer("library")
    if library_module is None:
        raise SourceError(
            "ChessAnalyzer could not be found, so its library cannot be read.")

    library = library_module.Library()
    rows = []
    for row in library.listing(limit=limit):
        saved = library.load(row["id"])
        if not saved:
            continue
        record = saved.get("record") or {}
        pgn = record.get("pgn") or saved.get("pgn")
        if not pgn:
            continue
        white = record.get("white", "?")
        black = record.get("black", "?")
        if me and not _my_colour(white, black, me):
            continue

        entry = _record(
            game_id=record.get("id") or row["id"], source="analyzer",
            url=record.get("url", ""), white=white, black=black,
            white_elo=record.get("whiteElo"), black_elo=record.get("blackElo"),
            result=record.get("result", "*"), date=record.get("date", ""),
            speed=(record.get("speed") or "").lower(),
            rated=True, time_control=record.get("timeControl", ""),
            pgn=pgn, me=me)
        entry["existingReview"] = saved.get("review") or None
        rows.append(entry)
    return rows


# ------------------------------------------------------------------- public


def fetch(spec: dict, *, token: str | None = None, progress=None,
          should_stop=None) -> dict:
    """One dataset of your games. ``spec`` names the door and its options."""
    kind = (spec.get("kind") or "").strip().lower()
    me = (spec.get("username") or "").strip().lstrip("@")
    limit = max(1, min(MAX_GAMES, int(spec.get("limit") or 200)))
    speeds = tuple(s for s in (spec.get("speeds") or ()) if s in SPEEDS)
    rated_only = bool(spec.get("ratedOnly", True))
    since_ms = spec.get("sinceMs")

    if kind == "lichess":
        if not me:
            raise SourceError("Give your Lichess username.")
        rows = _lichess(me, limit=limit, speeds=speeds, rated_only=rated_only,
                        since_ms=since_ms, token=token, progress=progress,
                        should_stop=should_stop)
        label = f"{me} on Lichess"
    elif kind == "chesscom":
        if not me:
            raise SourceError("Give your Chess.com username.")
        rows = _chesscom(me, limit=limit, speeds=speeds, rated_only=rated_only,
                         since_ms=since_ms, progress=progress,
                         should_stop=should_stop)
        label = f"{me} on Chess.com"
    elif kind == "pgn":
        rows = from_pgn_path(spec.get("path", ""), me=me, limit=limit)
        label = f"{Path(spec.get('path', 'PGN')).name}"
    elif kind == "analyzer":
        rows = analyzer_library(me=me, limit=limit)
        label = "ChessAnalyzer library"
    else:
        raise SourceError(f"Unknown source {kind!r}. "
                          "Use lichess, chesscom, pgn or analyzer.")

    if not rows:
        raise SourceError(
            "No standard games found with those filters. Widen the speeds, "
            "turn off 'rated only', or check the username spelling.")

    # The site's own spelling of your name, taken from the games themselves.
    display = me
    lowered = me.lower()
    for row in rows:
        for name in (row["white"], row["black"]):
            if name.lower() == lowered:
                display = name
                break

    return {
        "kind": kind,
        "username": display,
        "label": label,
        "fetched": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "spec": {"kind": kind, "username": display, "limit": limit,
                 "speeds": list(speeds), "ratedOnly": rated_only,
                 "sinceMs": since_ms, "path": spec.get("path", "")},
        "games": rows,
    }


__all__ = [
    "MAX_GAMES",
    "SPEEDS",
    "SourceError",
    "analyzer_library",
    "fetch",
    "from_pgn_path",
    "from_pgn_text",
]
