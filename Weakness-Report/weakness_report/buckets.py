"""Every way the report cuts your history, in one place.

A dimension is a name and a function from one of your moves to the buckets it
belongs to.  That is the whole mechanism, and everything the report says comes
out of it: slice, count, compare against your own average, rank.

Two details worth knowing before adding one.

*A move can be in several buckets of the same dimension.*  ``of`` returns a
tuple, not a single key, which is what lets ``situation`` say that a move was
played in a queenless middlegame *and* while a pawn down.  Every other
dimension returns exactly one, and the arithmetic does not care.

*A dimension may decline.*  Returning an empty tuple drops the move from that
dimension entirely, which is how "rook ending" avoids counting the twenty
moves before the ending began, and how time pressure avoids inventing a bucket
for games with no clock in the PGN.

The ``situation`` dimension is deliberately a **curated list rather than a
cross product**.  Phase x queens x centre x material is 108 buckets, nearly
all of them too small to say anything about; these are the dozen crosses that
people actually recognise and can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dimension:
    """One way of slicing your moves."""

    key: str
    label: str
    note: str
    of: object                       # move -> tuple of bucket names
    order: tuple = field(default_factory=tuple)

    def buckets_of(self, move: dict) -> tuple:
        try:
            value = self.of(move)
        except Exception:                                    # noqa: BLE001
            return ()
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,) if value else ()
        return tuple(item for item in value if item)


def _features(move: dict) -> dict:
    return move.get("features") or {}


def _game(move: dict) -> dict:
    return move.get("game") or {}


# -------------------------------------------------------------- situations

#: Below this many pieces we are past the point where "closed centre" is a
#: useful thing to say about a position.
ENDGAME_PIECES = 12


def situations(move: dict) -> tuple:
    """The recognisable kinds of position this move was played in.

    These are the crosses that make a readable sentence: "queenless
    middlegames" rather than "middlegame, queens off, semi-open centre,
    material level".
    """
    features = _features(move)
    phase = move.get("phase")
    out = []

    if phase == "middlegame":
        if features.get("queens") == "queens off":
            out.append("queenless middlegames")
        elif features.get("queens") == "queens on":
            out.append("middlegames with queens on")

    if features.get("oppositeCastling"):
        out.append("opposite-side castling")

    if phase != "opening" and features.get("kingSide") == "centre":
        out.append("your king still in the centre")

    if features.get("centre") == "closed centre" and phase != "endgame":
        out.append("closed positions")
    if features.get("centre") == "open centre" and phase != "endgame":
        out.append("open positions")

    if phase == "endgame":
        ending = features.get("ending")
        if ending:
            out.append(f"{ending}s" if not ending.endswith("s") else ending)

    if features.get("material") == "material ahead":
        out.append("when you are ahead")
    elif features.get("material") == "material behind":
        out.append("when you are behind")

    return tuple(out)


# ------------------------------------------------------------ time pressure

#: Seconds left on your clock when you moved.  Absolute rather than a share of
#: the time control, because "under thirty seconds" is a thing you feel the
#: same way whatever the game started at.
CLOCK_BANDS = (
    (10, "under 10 seconds"),
    (30, "10 to 30 seconds"),
    (60, "30 to 60 seconds"),
    (180, "1 to 3 minutes"),
    (float("inf"), "over 3 minutes"),
)


def clock_band(move: dict) -> tuple:
    seconds = move.get("clock")
    if seconds is None:
        return ()
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ()
    for limit, name in CLOCK_BANDS:
        if seconds < limit:
            return (name,)
    return ()


# -------------------------------------------------------------- the opponent

#: A hundred points either way is roughly "the same player" for this purpose.
RATING_GAP = 100


def opponent_band(move: dict) -> tuple:
    game = _game(move)
    try:
        mine = int(game.get("youElo") or 0)
        theirs = int(game.get("themElo") or 0)
    except (TypeError, ValueError):
        return ()
    if not mine or not theirs:
        return ()
    difference = theirs - mine
    if difference > RATING_GAP:
        return ("against stronger players",)
    if difference < -RATING_GAP:
        return ("against weaker players",)
    return ("against similar players",)


def opening_family(move: dict) -> tuple:
    """The opening's family name, not its deepest variation.

    "Sicilian Defense: Najdorf, English Attack" is one game; "Sicilian
    Defense" is a bucket you can have a weakness in.
    """
    name = (_game(move).get("opening") or {}).get("name") or ""
    if not name or name == "Unnamed opening":
        return ()
    return (name.split(":")[0].strip(),)


# ------------------------------------------------------------- the full list

DIMENSIONS = (
    Dimension(
        "situation", "Kinds of position",
        "Recognisable crosses of phase, material and structure -- a move can "
        "be in more than one.",
        situations),
    Dimension(
        "phase", "Game phase",
        "Where the opening stops and the middlegame starts is found from the "
        "position, not from a fixed move number.",
        lambda move: (move.get("phase"),),
        order=("opening", "middlegame", "endgame")),
    Dimension(
        "queens", "Queens",
        "Whether the queens are still on when you moved.",
        lambda move: (_features(move).get("queens"),),
        order=("queens on", "one queen", "queens off")),
    Dimension(
        "centre", "Pawn structure",
        "How jammed the centre is, counted as pawn pairs standing head to head.",
        lambda move: (_features(move).get("centre"),),
        order=("open centre", "semi-open centre", "closed centre")),
    Dimension(
        "material", "Material",
        "Whether you were ahead, level or behind when you moved.",
        lambda move: (_features(move).get("material"),),
        order=("material ahead", "material level", "material behind")),
    Dimension(
        "kingSide", "Your king",
        "Which wing your king was standing on, counted only once the opening "
        "is over -- before that the bucket is really 'have you castled yet', "
        "and it compares your opening against your middlegame rather than one "
        "wing against another.",
        lambda move: ((_features(move).get("kingSide"),)
                      if move.get("phase") != "opening" else ()),
        order=("kingside", "centre", "queenside")),
    Dimension(
        "ending", "Kind of ending",
        "Only counted once the position is actually an ending.",
        lambda move: ((_features(move).get("ending"),)
                      if move.get("phase") == "endgame" else ())),
    Dimension(
        "clock", "Time pressure",
        "Seconds left on your clock when you played the move. Games whose PGN "
        "carries no clock are left out of this slice entirely.",
        clock_band,
        order=tuple(name for _, name in CLOCK_BANDS)),
    Dimension(
        "opening", "Opening",
        "Grouped by opening family, so a variation does not become its own "
        "bucket of one game.",
        opening_family),
    Dimension(
        "colour", "Colour",
        "The side you had.",
        lambda move: (("as White" if _game(move).get("you") == "white"
                       else "as Black"),),
        order=("as White", "as Black")),
    Dimension(
        "speed", "Time control",
        "Bullet, blitz, rapid or classical, as the site classified it.",
        lambda move: ((_game(move).get("speed") or "").strip(),)),
    Dimension(
        "opponent", "Opponent strength",
        "Rated more than 100 points above you, below you, or about the same.",
        opponent_band,
        order=("against stronger players", "against similar players",
               "against weaker players")),
    Dimension(
        "label", "Move label",
        "How ChessAnalyzer labelled the move. Shown as counts rather than "
        "ranked, because a label is an outcome, not a kind of position.",
        lambda move: ((move.get("label") or "").strip(),)),
    Dimension(
        "moveNumber", "Move number",
        "Ten-move bands, which sometimes shows a slump the phase split hides.",
        lambda move: (_band(move.get("moveNumber")),)),
)

BY_KEY = {dimension.key: dimension for dimension in DIMENSIONS}

#: The dimensions a finding may be drawn from.  ``label`` is excluded because
#: "you lose most centipawns on moves labelled blunder" is true by definition,
#: and ``opening`` because an opening weakness is a different kind of claim,
#: reported in its own section.
RANKABLE = ("situation", "phase", "queens", "centre", "material", "kingSide",
            "ending", "clock", "colour", "speed", "opponent", "moveNumber")


def _band(move_number) -> str:
    try:
        number = int(move_number)
    except (TypeError, ValueError):
        return ""
    if number <= 10:
        return "moves 1-10"
    if number <= 20:
        return "moves 11-20"
    if number <= 30:
        return "moves 21-30"
    if number <= 40:
        return "moves 31-40"
    return "moves 41+"


__all__ = [
    "BY_KEY",
    "CLOCK_BANDS",
    "DIMENSIONS",
    "RANKABLE",
    "Dimension",
    "clock_band",
    "opening_family",
    "opponent_band",
    "situations",
]
