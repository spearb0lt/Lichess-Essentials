"""Games from Lichess, finished and in progress.

Everything here is the documented public API and none of it needs a token::

    GET /api/games/user/{username}     recent games, ndjson, PGN inside
    GET /game/export/{id}              one game as PGN  (no /api prefix)
    GET /api/user/{username}/current-game   what they are playing now
    GET /api/stream/game/{id}          live positions of any ongoing game

That last one is the interesting one, and the reason live analysis works on
Lichess and not on Chess.com: it is a public ndjson stream of *any* game in
progress -- yours, a friend's, a titled player's on the TV -- one line per
move carrying the new FEN, the move just played and both clocks.  No token,
no scopes, no polling.

A token is accepted anyway, and used when present, because it raises the rate
limit for bulk imports.  Nothing here requires one.

The one wrinkle is ``current-game``: it answers with the player's *last* game
when they are not playing, rather than 404ing, so ``finished`` has to be
checked rather than assumed.
"""

from __future__ import annotations

import json
import re

import requests

from .common import USER_AGENT, GameRecord, SourceError, record_from_pgn

API = "https://lichess.org"

#: Game ids are 8 characters; a move-annotated URL adds a colour and a ply,
#: e.g. /Wi8IPxc3/black#42, and pasting that should still work.
GAME_ID = re.compile(r"(?:lichess\.org/)?([a-zA-Z0-9]{8})(?:/(?:white|black))?(?:#\d+)?/?$")

TERMINAL_STATUSES = {
    "mate", "resign", "stalemate", "timeout", "draw", "outoftime", "cheat",
    "noStart", "unknownFinish", "variantEnd",
}


def _headers(token: str | None = None, accept: str = "application/json") -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, *, token=None, params=None, accept="application/json",
         stream=False, timeout=30):
    try:
        response = requests.get(url, headers=_headers(token, accept),
                                params=params, timeout=timeout, stream=stream)
    except requests.RequestException as exc:
        raise SourceError(f"Could not reach Lichess: {exc}") from exc

    if response.status_code == 404:
        raise SourceError("Lichess has no such game or user.")
    if response.status_code == 429:
        raise SourceError(
            "Lichess rate limited us. Wait a minute and try again -- adding an "
            "API token in settings raises the limit considerably.")
    if not response.ok:
        raise SourceError(f"Lichess said {response.status_code}: "
                          f"{response.text[:200]}")
    return response


def parse_reference(text: str) -> str | None:
    """Pull a game id out of a URL, an id, or a paste of either."""
    candidate = (text or "").strip().rstrip("/")
    if not candidate:
        return None
    match = GAME_ID.search(candidate)
    return match.group(1) if match else None


# ------------------------------------------------------------------ games


def game(game_id: str, *, token: str | None = None) -> GameRecord:
    """One game as a record. Works for finished and ongoing games alike."""
    # Single-game export lives at /game/export, *not* under /api like the
    # rest of the endpoints here. Getting that wrong returns a 404 HTML page.
    response = _get(f"{API}/game/export/{game_id}",
                    token=token, accept="application/x-chess-pgn",
                    params={"clocks": "true", "evals": "false",
                            "opening": "true", "literate": "false"})
    pgn = response.text
    if not pgn.strip():
        raise SourceError(f"Lichess returned nothing for game {game_id}.")

    finished = "[Result \"*\"]" not in pgn
    return record_from_pgn(
        pgn, source="lichess", game_id=f"lichess-{game_id}",
        url=f"https://lichess.org/{game_id}", finished=finished)


def user_games(username: str, *, limit: int = 20, token: str | None = None,
               rated_only: bool = False, perf: str | None = None) -> list[dict]:
    """A player's recent games, newest first, as a list for the picker.

    Only the summary each row needs -- the PGN comes with it, so opening a
    game from this list costs no second request.
    """
    username = (username or "").strip().lstrip("@")
    if not username:
        raise SourceError("Give a Lichess username.")

    params = {
        "max": max(1, min(100, int(limit))),
        "pgnInJson": "true",
        "clocks": "true",
        "opening": "true",
        "sort": "dateDesc",
    }
    if rated_only:
        params["rated"] = "true"
    if perf:
        params["perfType"] = perf

    response = _get(f"{API}/api/games/user/{username}", token=token,
                    params=params, accept="application/x-ndjson", timeout=60)

    rows = []
    for line in response.text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        rows.append(_summary(data))
    return rows


def _summary(data: dict) -> dict:
    """One ndjson game -> the row the browser shows, PGN included."""
    players = data.get("players") or {}

    def side(colour: str) -> dict:
        entry = players.get(colour) or {}
        user = entry.get("user") or {}
        return {
            "name": user.get("name") or entry.get("aiLevel")
            and f"Stockfish level {entry['aiLevel']}" or "Anonymous",
            "rating": entry.get("rating"),
            "diff": entry.get("ratingDiff"),
        }

    status = data.get("status", "")
    return {
        "id": f"lichess-{data.get('id')}",
        "gameId": data.get("id"),
        "source": "lichess",
        "url": f"https://lichess.org/{data.get('id')}",
        "white": side("white"),
        "black": side("black"),
        "result": _result(data),
        "speed": data.get("speed", ""),
        "rated": bool(data.get("rated")),
        "variant": data.get("variant", "standard"),
        "createdAt": data.get("createdAt"),
        "status": status,
        "finished": status in TERMINAL_STATUSES,
        "opening": data.get("opening") or {},
        "plyCount": len((data.get("moves") or "").split()),
        "pgn": data.get("pgn", ""),
    }


def _result(data: dict) -> str:
    winner = data.get("winner")
    if winner == "white":
        return "1-0"
    if winner == "black":
        return "0-1"
    if data.get("status") in TERMINAL_STATUSES:
        return "1/2-1/2"
    return "*"


def current_game(username: str, *, token: str | None = None) -> dict:
    """What this player is playing right now, if anything.

    Lichess answers with the player's most recent game when they are not
    playing, so the returned ``live`` flag is the only thing worth trusting.
    """
    username = (username or "").strip().lstrip("@")
    if not username:
        raise SourceError("Give a Lichess username.")

    response = _get(f"{API}/api/user/{username}/current-game", token=token,
                    params={"moves": "true", "pgnInJson": "true",
                            "opening": "true", "clocks": "true"})
    try:
        data = response.json()
    except ValueError as exc:
        raise SourceError("Lichess returned something that is not JSON.") from exc

    summary = _summary(data)
    summary["live"] = not summary["finished"]
    return summary


# ------------------------------------------------------------------ live


def stream_game(game_id: str, *, token: str | None = None, timeout: int = 600):
    """Yield one dictionary per position of an ongoing game.

    The first line is the game's metadata; every line after it is a position:
    ``{"fen": ..., "lm": "e2e4", "wc": 180, "bc": 178}``.  The generator ends
    when Lichess closes the stream, which it does when the game ends -- so the
    caller does not have to decide when to stop.

    Kept as a generator on purpose: the caller (:mod:`chess_analyzer.live`)
    runs it on a background thread and owns the reconnect policy, because a
    dropped stream mid-game is normal and this layer should not have an
    opinion about it.
    """
    response = _get(f"{API}/api/stream/game/{game_id}", token=token,
                    accept="application/x-ndjson", stream=True,
                    timeout=timeout)
    try:
        for raw in response.iter_lines():
            if not raw:
                continue                    # keep-alive newline
            try:
                yield json.loads(raw)
            except ValueError:
                continue
    finally:
        response.close()


def tv_games(*, token: str | None = None) -> list[dict]:
    """The games Lichess is currently featuring, for a live demo with no id."""
    response = _get(f"{API}/api/tv/channels", token=token)
    try:
        data = response.json()
    except ValueError:
        return []

    rows = []
    for channel, entry in data.items():
        user = entry.get("user") or {}
        rows.append({
            "channel": channel,
            "gameId": entry.get("gameId"),
            "name": user.get("name", "?"),
            "title": user.get("title", ""),
            "rating": entry.get("rating"),
            "color": entry.get("color"),
        })
    rows.sort(key=lambda r: -(r.get("rating") or 0))
    return rows


__all__ = [
    "current_game",
    "game",
    "parse_reference",
    "stream_game",
    "tv_games",
    "user_games",
]
