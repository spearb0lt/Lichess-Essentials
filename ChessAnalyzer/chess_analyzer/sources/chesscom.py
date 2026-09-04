"""Games from Chess.com, and the awkward truth about live ones.

Two very different endpoints, and the difference matters enough to state
plainly in the UI rather than hide behind a spinner.

**The documented public API** (``api.chess.com/pub/...``) is excellent for
finished games: monthly archives carry the full PGN of every game you have
played, no token, no rate limit worth worrying about.  What it does *not*
carry is a live game.  ``/pub/player/{u}/games`` is documented as "games in
progress" but returns **daily/correspondence games only** -- a blitz game you
are playing right now is not in it, and no documented endpoint has it.

**The internal endpoint** ``chess.com/callback/live/game/{id}`` does have it.
Given the id from a game's URL it returns the move list, whose turn it is,
the clocks and the PGN tags, for live games including ones still running.
It is undocumented, which means: it is not covered by chess.com's API terms,
it can change or vanish without notice, and this app treats every call to it
as optional.  A failure there degrades to "paste the PGN" rather than
breaking the feature, and the UI says which route a game came in by.

The move list arrives as TCN rather than SAN; :mod:`chess_analyzer.tcn`
decodes it, verified against 194 games from the documented archive.

One operational detail worth knowing: chess.com sits behind Cloudflare and
returns a 403 challenge page to any request without a ``User-Agent``.  Not a
rate limit, not a ban -- just a missing header, which is a confusing hour to
spend if you do not know.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import requests

from .. import tcn
from .common import USER_AGENT, GameRecord, SourceError, build_pgn, record_from_pgn

PUB = "https://api.chess.com/pub"
CALLBACK = "https://www.chess.com/callback"

#: /game/live/123, /game/daily/123, /analysis/game/live/123?tab=review, or a
#: bare number pasted out of any of them.
GAME_URL = re.compile(r"chess\.com/(?:analysis/)?game/(live|daily)/(\d+)")
BARE_ID = re.compile(r"^(\d{6,})$")


def _get(url: str, *, params=None, timeout=30, quiet404=False):
    try:
        response = requests.get(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise SourceError(f"Could not reach Chess.com: {exc}") from exc

    if response.status_code == 404:
        if quiet404:
            return None
        raise SourceError("Chess.com has no such player or game.")
    if response.status_code == 403:
        raise SourceError(
            "Chess.com refused the request (403). That is usually Cloudflare "
            "rather than a ban -- try again in a minute.")
    if response.status_code == 429:
        raise SourceError("Chess.com rate limited us. Wait a minute.")
    if not response.ok:
        raise SourceError(f"Chess.com said {response.status_code}.")
    return response


def parse_reference(text: str) -> tuple[str, str] | None:
    """Pull ``(kind, id)`` out of a chess.com game URL or a bare id."""
    candidate = (text or "").strip()
    match = GAME_URL.search(candidate)
    if match:
        return match.group(1), match.group(2)
    bare = BARE_ID.match(candidate)
    if bare:
        # A bare number is far more likely to be a live game than a daily one;
        # the caller retries as daily when this misses.
        return "live", bare.group(1)
    return None


# --------------------------------------------------------- finished games


def archives(username: str) -> list[str]:
    """Every month this player has games in, oldest first."""
    username = (username or "").strip().lstrip("@").lower()
    if not username:
        raise SourceError("Give a Chess.com username.")
    response = _get(f"{PUB}/player/{username}/games/archives")
    try:
        return response.json().get("archives", [])
    except ValueError as exc:
        raise SourceError("Chess.com returned something that is not JSON.") from exc


def user_games(username: str, *, limit: int = 20) -> list[dict]:
    """Recent games, newest first, walking back through monthly archives.

    Chess.com has no "last N games" endpoint, only whole months, so this
    fetches the newest month and keeps stepping back until it has enough.
    Bounded at six months so a dormant account cannot turn one click into
    dozens of requests.
    """
    months = archives(username)
    if not months:
        return []

    rows: list[dict] = []
    for url in reversed(months[-6:]):
        response = _get(url, timeout=45)
        try:
            games = response.json().get("games", [])
        except ValueError:
            continue
        for data in reversed(games):
            rows.append(_summary(data, username))
            if len(rows) >= limit:
                return rows
    return rows


def _summary(data: dict, username: str = "") -> dict:
    """One archive entry -> the row the browser shows, PGN included."""
    def side(colour: str) -> dict:
        entry = data.get(colour) or {}
        return {
            "name": entry.get("username", "?"),
            "rating": entry.get("rating"),
            "diff": None,           # chess.com does not publish the delta
        }

    white = (data.get("white") or {}).get("result", "")
    black = (data.get("black") or {}).get("result", "")
    if white == "win":
        result = "1-0"
    elif black == "win":
        result = "0-1"
    elif white or black:
        result = "1/2-1/2"
    else:
        result = "*"

    end = data.get("end_time")
    reference = parse_reference(data.get("url", "")) or ("live", "")
    return {
        "id": f"chesscom-{reference[1]}" if reference[1]
        else f"chesscom-{data.get('uuid', '')[:12]}",
        "gameId": reference[1],
        "gameKind": reference[0],
        "source": "chesscom",
        "url": data.get("url", ""),
        "white": side("white"),
        "black": side("black"),
        "result": result,
        "speed": data.get("time_class", ""),
        "rated": bool(data.get("rated")),
        "variant": data.get("rules", "chess"),
        "createdAt": (end or 0) * 1000,
        "status": (data.get("white") or {}).get("result", ""),
        "finished": True,
        "opening": {"eco": data.get("eco", "").rsplit("/", 1)[-1]}
        if data.get("eco") else {},
        "plyCount": 0,
        "pgn": data.get("pgn", ""),
        "you": username,
    }


def game(game_id: str, *, kind: str = "live") -> GameRecord:
    """One game by id, live or daily, finished or still being played.

    Uses the internal callback endpoint -- see the module docstring for what
    that means.  Raises :class:`SourceError` with an actionable message rather
    than a stack trace when chess.com declines.
    """
    order = [kind] + [other for other in ("live", "daily") if other != kind]
    problems = []

    for attempt in order:
        response = _get(f"{CALLBACK}/{attempt}/game/{game_id}", quiet404=True)
        if response is None:
            problems.append(f"no {attempt} game with id {game_id}")
            continue
        try:
            payload = response.json()
        except ValueError:
            problems.append(f"{attempt}: response was not JSON")
            continue

        data = payload.get("game") or {}
        moves = data.get("moveList")
        if moves is None:
            problems.append(f"{attempt}: no move list in the response")
            continue

        return _record(game_id, attempt, data, payload.get("players") or [])

    raise SourceError(
        "Chess.com would not give up that game (" + "; ".join(problems) + ").\n"
        "That endpoint is undocumented and can change without notice. Use "
        "Share > PGN on the game and paste it instead -- that always works.")


def _record(game_id: str, kind: str, data: dict, players: list) -> GameRecord:
    start_fen = data.get("initialSetup") or None
    try:
        ucis = tcn.to_uci(data.get("moveList", ""), start_fen=start_fen)
    except tcn.TCNError as exc:
        raise SourceError(f"Could not decode the chess.com move list: {exc}") from exc

    headers = dict(data.get("pgnHeaders") or {})
    headers.setdefault("Event", "Live Chess" if kind == "live" else "Daily Chess")
    headers.setdefault("Site", "Chess.com")
    if not headers.get("White") and len(players) >= 2:
        headers["White"] = players[0].get("username", "?")
        headers["Black"] = players[1].get("username", "?")

    finished = bool(data.get("isFinished"))
    if not finished:
        headers["Result"] = "*"

    pgn = build_pgn(headers, ucis, start_fen=start_fen)
    clocks = {}
    if data.get("whiteRemainingTime") is not None:
        clocks = {
            "white": (data.get("whiteRemainingTime") or 0) / 10.0,
            "black": (data.get("blackRemainingTime") or 0) / 10.0,
        }

    record = record_from_pgn(
        pgn, source="chesscom", game_id=f"chesscom-{game_id}",
        url=f"https://www.chess.com/game/{kind}/{game_id}",
        finished=finished, speed=data.get("typeName", ""), clocks=clocks)
    record.turn = data.get("turnColor") or record.turn
    return record


# ------------------------------------------------------------- in progress


def in_progress(username: str) -> list[dict]:
    """Daily games this player has running.

    This is everything the documented API will tell you about an unfinished
    game, and it is correspondence only. Live games are absent by design, not
    by oversight -- see the module docstring.
    """
    username = (username or "").strip().lstrip("@").lower()
    if not username:
        raise SourceError("Give a Chess.com username.")

    response = _get(f"{PUB}/player/{username}/games")
    try:
        games = response.json().get("games", [])
    except ValueError:
        return []

    rows = []
    for data in games:
        reference = parse_reference(data.get("url", "")) or ("daily", "")
        move_by = data.get("move_by")
        rows.append({
            "id": f"chesscom-{reference[1]}",
            "gameId": reference[1],
            "gameKind": reference[0],
            "source": "chesscom",
            "url": data.get("url", ""),
            "white": {"name": (data.get("white") or "").rsplit("/", 1)[-1]},
            "black": {"name": (data.get("black") or "").rsplit("/", 1)[-1]},
            "turn": data.get("turn", ""),
            "fen": data.get("fen", ""),
            "pgn": data.get("pgn", ""),
            "speed": "daily",
            "finished": False,
            "moveBy": (datetime.fromtimestamp(move_by, tz=timezone.utc).isoformat()
                       if move_by else ""),
            "lastActivity": data.get("last_activity"),
        })
    return rows


def profile(username: str) -> dict:
    """Just enough to confirm a username exists before a bigger fetch."""
    response = _get(f"{PUB}/player/{(username or '').strip().lstrip('@').lower()}")
    try:
        data = response.json()
    except ValueError:
        return {}
    return {
        "username": data.get("username", ""),
        "name": data.get("name", ""),
        "avatar": data.get("avatar", ""),
        "url": data.get("url", ""),
        "country": (data.get("country") or "").rsplit("/", 1)[-1],
    }


__all__ = [
    "archives",
    "game",
    "in_progress",
    "parse_reference",
    "profile",
    "user_games",
]
