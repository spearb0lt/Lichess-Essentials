"""Getting a lot of somebody's games, from either site.

This is deliberately *not* the sibling apps' game fetchers.  Those answer
"give me the twenty most recent games to put in a picker", complete with the
PGN of each so a click costs no second request.  Scouting asks a different
question -- "give me four hundred games, I only care about the moves" -- and
the right shape for that is different enough to be worth its own module:

* **Lichess** streams.  ``/api/games/user/{u}`` is one ndjson request whatever
  the count, so this reads it line by line and stops when it has enough
  rather than asking for a number and hoping.  ``moves`` there is SAN, so it
  is walked through a board once to become UCI, which is what the tree wants
  and a fraction of the size to cache.
* **Chess.com** does not stream and has no "last N games" endpoint at all,
  only whole months.  So this walks backwards through the monthly archives,
  newest first, and stops as soon as it has enough -- bounded by a month
  limit so one click can never turn into a hundred requests.

Neither needs a token.  Lichess accepts one and it raises the rate limit,
which is worth having when pulling hundreds of games; nothing here requires
it.

A variant game, or a move that will not play, is dropped rather than guessed
at: one wrong move in a scouting tree is a line you prepare for that your
opponent never plays.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import chess
import chess.pgn
import requests

USER_AGENT = ("player-prepper/0.1 (local opponent-prep tool; "
              "https://github.com/lichess-essentials)")

#: The speed vocabulary this app uses, and how each site spells it.  Lichess
#: can filter server-side; Chess.com cannot, so its games are filtered here.
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

#: Hard ceiling on one scout, so a typo in the games box cannot start a
#: download that runs for an hour.
MAX_GAMES = 2000


class FetchError(RuntimeError):
    """A fetch failed, with a message worth showing the user."""


def _headers(token=None, accept="application/json") -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _check(response, site: str) -> None:
    if response.status_code == 404:
        raise FetchError(f"{site} has no such player.")
    if response.status_code == 429:
        raise FetchError(
            f"{site} rate limited us. Wait a minute and try again"
            + (" -- a Lichess API token raises the limit considerably."
               if site == "Lichess" else "."))
    if not response.ok:
        raise FetchError(f"{site} said {response.status_code}: "
                         f"{response.text[:200]}")


def san_line_to_uci(moves: str, limit: int | None = None) -> list:
    """``"e4 c5 Nf3"`` becomes ``["e2e4", "c7c5", "g1f3"]``.

    Stops at the first move that will not play, which keeps a truncated or
    slightly malformed game usable up to the point it stops making sense
    instead of throwing the whole thing away.
    """
    board = chess.Board()
    out = []
    for token in (moves or "").split():
        if limit is not None and len(out) >= limit:
            break
        try:
            move = board.parse_san(token)
        except (ValueError, AssertionError):
            break
        out.append(move.uci())
        board.push(move)
    return out


def pgn_to_uci(pgn: str, limit: int | None = None) -> list:
    """The mainline of a PGN as UCI. Used for Chess.com, which sends PGN."""
    try:
        game = chess.pgn.read_game(io.StringIO(pgn or ""))
    except Exception:                                        # noqa: BLE001
        return []
    if game is None:
        return []
    board = game.board()
    out = []
    for move in game.mainline_moves():
        if limit is not None and len(out) >= limit:
            break
        if move not in board.legal_moves:
            break
        out.append(move.uci())
        board.push(move)
    return out


def _iso_day(milliseconds) -> str:
    if not milliseconds:
        return ""
    try:
        return datetime.fromtimestamp(
            milliseconds / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


# ------------------------------------------------------------------ lichess


def _lichess_games(username, *, limit, speeds, rated_only, since_ms, token,
                   progress=None, should_stop=None) -> list:
    params = {
        "max": limit,
        "sort": "dateDesc",
        "moves": "true",
        "pgnInJson": "false",
        "clocks": "false",
        "evals": "false",
        "opening": "false",
        "tags": "false",
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
            params=params, timeout=120, stream=True)
    except requests.RequestException as exc:
        raise FetchError(f"Could not reach Lichess: {exc}") from exc

    _check(response, "Lichess")

    rows = []
    lowered = handle.lower()
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

        players = data.get("players") or {}

        def side(colour):
            entry = players.get(colour) or {}
            user = entry.get("user") or {}
            if user.get("name"):
                name = user["name"]
            elif entry.get("aiLevel") is not None:
                name = f"Stockfish level {entry['aiLevel']}"
            else:
                name = "Anonymous"
            return name, str(entry.get("rating") or "")

        white, white_elo = side("white")
        black, black_elo = side("black")
        if lowered not in (white.lower(), black.lower()):
            continue                     # an alias, or an anonymous game

        winner = data.get("winner")
        result = ("1-0" if winner == "white"
                  else "0-1" if winner == "black" else "1/2-1/2")

        rows.append({
            "id": data.get("id", ""),
            "url": f"https://lichess.org/{data.get('id', '')}",
            "white": white, "black": black,
            "whiteElo": white_elo, "blackElo": black_elo,
            "result": result,
            "date": _iso_day(data.get("createdAt")),
            "speed": LICHESS_SPEED_BACK.get(data.get("speed", ""), "blitz"),
            "rated": bool(data.get("rated")),
            "moves": " ".join(san_line_to_uci(data.get("moves", ""))),
        })
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
        raise FetchError(f"Could not reach Chess.com: {exc}") from exc
    _check(response, "Chess.com")
    try:
        return response.json().get("archives", []) or []
    except ValueError as exc:
        raise FetchError("Chess.com returned something that is not JSON.") from exc


def _months_since(months: list, since_ms) -> list:
    """Archive URLs at or after the cutoff month. Unparseable URLs are kept."""
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


def _chesscom_games(username, *, limit, speeds, rated_only, since_ms,
                    progress=None, should_stop=None) -> list:
    months = _months_since(_chesscom_archives(username), since_ms)
    if not months:
        return []

    lowered = username.strip().lstrip("@").lower()
    rows = []

    for url in list(reversed(months))[:MAX_MONTHS]:
        if should_stop and should_stop():
            break
        try:
            response = requests.get(url, headers=_headers(), timeout=90)
        except requests.RequestException as exc:
            raise FetchError(f"Could not reach Chess.com: {exc}") from exc
        _check(response, "Chess.com")
        try:
            games = response.json().get("games", []) or []
        except ValueError:
            continue

        for data in reversed(games):              # newest first inside a month
            if should_stop and should_stop():
                break
            if (data.get("rules") or "chess") != "chess":
                continue
            if rated_only and not data.get("rated"):
                continue

            speed = CHESSCOM_SPEED_BACK.get(data.get("time_class", ""), "blitz")
            if speeds and speed not in speeds:
                continue

            white = data.get("white") or {}
            black = data.get("black") or {}
            names = (white.get("username", ""), black.get("username", ""))
            if lowered not in (names[0].lower(), names[1].lower()):
                continue

            if white.get("result") == "win":
                result = "1-0"
            elif black.get("result") == "win":
                result = "0-1"
            else:
                result = "1/2-1/2"

            moves = pgn_to_uci(data.get("pgn", ""))
            if not moves:
                continue

            rows.append({
                "id": str(data.get("uuid", ""))[:18],
                "url": data.get("url", ""),
                "white": names[0], "black": names[1],
                "whiteElo": str(white.get("rating") or ""),
                "blackElo": str(black.get("rating") or ""),
                "result": result,
                "date": _iso_day((data.get("end_time") or 0) * 1000),
                "speed": speed,
                "rated": bool(data.get("rated")),
                "moves": " ".join(moves),
            })
            if progress:
                progress(len(rows), limit)
            if len(rows) >= limit:
                return rows
    return rows


# ------------------------------------------------------------------- public


def fetch_games(site, username, *, limit=300, speeds=None, rated_only=True,
                since_ms=None, token=None, progress=None,
                should_stop=None) -> dict:
    """Somebody's games, newest first, in this app's one compact shape.

    ``speeds`` is a subset of :data:`SPEEDS`; empty or None means every speed.
    """
    site = (site or "").strip().lower()
    username = (username or "").strip().lstrip("@")
    if not username:
        raise FetchError("Give a username.")

    limit = max(1, min(MAX_GAMES, int(limit)))
    speeds = tuple(s for s in (speeds or ()) if s in SPEEDS)

    if site == "lichess":
        games = _lichess_games(
            username, limit=limit, speeds=speeds, rated_only=rated_only,
            since_ms=since_ms, token=token, progress=progress,
            should_stop=should_stop)
    elif site == "chesscom":
        games = _chesscom_games(
            username, limit=limit, speeds=speeds, rated_only=rated_only,
            since_ms=since_ms, progress=progress, should_stop=should_stop)
    else:
        raise FetchError(f"Unknown site {site!r}. Use lichess or chesscom.")

    if not games:
        raise FetchError(
            f"No standard games found for {username} on "
            f"{'Lichess' if site == 'lichess' else 'Chess.com'} with those "
            "filters. Try widening the speeds, or turning off 'rated only'.")

    # The site's own spelling of the name, taken from the games themselves:
    # what was typed may differ in case, and Chess.com's URLs are lowercase.
    lowered = username.lower()
    display = username
    for game in games:
        for name in (game["white"], game["black"]):
            if name.lower() == lowered:
                display = name
                break

    return {
        "site": site,
        "username": display,
        "fetched": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "filters": {
            "limit": limit,
            "speeds": list(speeds),
            "ratedOnly": bool(rated_only),
            "sinceMs": since_ms,
        },
        "games": games,
    }


__all__ = [
    "MAX_GAMES",
    "MAX_MONTHS",
    "SPEEDS",
    "FetchError",
    "fetch_games",
    "pgn_to_uci",
    "san_line_to_uci",
]
