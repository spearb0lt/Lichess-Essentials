"""Turning buckets into things worth saying, and saying each of them once.

A slice is a table.  A finding is a sentence: *queenless middlegames cost you
0.42 pawns a game more than your average -- 187 moves across 41 games, at 71
centipawns a move against your 54.*

Two things stand between the table and the sentence.

**Sample size.**  A bucket of eleven moves with a terrible average is not a
weakness, it is eleven moves.  Nothing is reported below a floor of both moves
and games, and every finding carries its own counts so you can judge it
yourself rather than take the ranking's word for it.

**Overlap.**  "Middlegame", "queens off" and "queenless middlegames" are three
dimensions that can be the same two hundred moves.  Ranked naively, a report's
top three findings are the same finding three times, which reads like three
problems and is one.  So findings are chosen greedily: the biggest first, then
anything that shares most of its moves with something already said is dropped.
The comparison is on the actual move sets, not on the names, because names do
not know that "closed positions" and "your king still in the centre" were the
same games for you.
"""

from __future__ import annotations

from .aggregate import MIN_GAMES, MIN_MOVES, Tally, bucket_accuracy
from .buckets import BY_KEY, RANKABLE

#: Two buckets sharing this much of the smaller one are the same finding
#: wearing different words.
OVERLAP = 0.70

#: How many claims a report makes. Past a dozen nobody acts on any of them.
MAX_FINDINGS = 10
MAX_STRENGTHS = 5


def _sets(moves: list, dimension) -> dict:
    """``{bucket: (Tally, {move indices})}`` for one dimension."""
    out: dict = {}
    for index, move in enumerate(moves):
        for name in dimension.buckets_of(move):
            tally, members = out.setdefault(name, (Tally(), set()))
            tally.add(move)
            members.add(index)
    return out


def _overlaps(members: set, chosen: list) -> bool:
    """Is this bucket the same finding as one already reported?

    Measured as Jaccard similarity -- shared moves over the union -- and
    emphatically *not* as containment. Containment would call every small,
    specific bucket a duplicate of the big vague one that happens to hold it:
    "queens on" contains all of your middlegame and all of your endgame, and
    suppressing both in its favour throws away the two findings you could
    actually act on in favour of the one you could not.
    """
    for other in chosen:
        union = len(members | other)
        if union and len(members & other) / union >= OVERLAP:
            return True
    return False


def sentence(finding: dict) -> str:
    """The claim, in words, with its own evidence attached.

    Deliberately built with no verb agreeing with the bucket name. Bucket
    names are a mix of plurals ("queenless middlegames"), singulars
    ("opening") and whole clauses ("when you are ahead"), and any sentence
    with a verb in it reads wrong for at least one of them.
    """
    bucket = str(finding.get("bucket") or "")
    per_game = abs(finding["excessPawnsPerGame"] or 0)
    worse = (finding["excessCp"] or 0) > 0
    moves = finding["scored"]
    games = finding["games"]
    return (
        f"{bucket[:1].upper()}{bucket[1:]} — {per_game:.2f} pawns a game "
        f"{'worse' if worse else 'better'} than your average, over "
        f"{moves} move{'' if moves == 1 else 's'} in "
        f"{games} game{'' if games == 1 else 's'} "
        f"({finding['acpl']:.0f} centipawns a move against your "
        f"{finding['baseline']:.0f})."
    )


def build(moves: list, *, total_games: int, baseline: float | None,
          min_moves: int = MIN_MOVES, min_games: int = MIN_GAMES) -> dict:
    """Ranked weaknesses and strengths, deduplicated by the moves they cover."""
    if baseline is None or not moves:
        return {"weaknesses": [], "strengths": [], "skipped": [],
                "minMoves": min_moves, "minGames": min_games}

    candidates = []
    skipped = []

    for key in RANKABLE:
        dimension = BY_KEY.get(key)
        if dimension is None:
            continue
        for name, (tally, members) in _sets(moves, dimension).items():
            acpl = tally.acpl
            if acpl is None:
                continue
            row = {
                "dimension": key,
                "dimensionLabel": dimension.label,
                "bucket": name,
                "moves": tally.moves,
                "scored": tally.scored,
                "games": len(tally.games),
                "acpl": round(acpl, 1),
                "baseline": round(baseline, 1),
                "accuracy": bucket_accuracy(tally.accuracies),
                "cpLost": tally.cp,
                "excessCp": round(tally.scored * (acpl - baseline), 1),
                "excessPawnsPerGame": (
                    round(tally.scored * (acpl - baseline) / total_games / 100.0, 3)
                    if total_games else 0.0),
                "blunders": tally.judgments.get("blunder", 0),
                "mistakes": tally.judgments.get("mistake", 0),
                "bestShare": (round(100.0 * tally.best / tally.moves, 1)
                              if tally.moves else None),
                "_members": members,
            }
            if tally.scored < min_moves or len(tally.games) < min_games:
                skipped.append({k: v for k, v in row.items() if k != "_members"})
                continue
            candidates.append(row)

    # Weaknesses first, and strengths are then picked against the same list of
    # already-claimed move sets. Without that, two dimensions covering nearly
    # the same moves can land one in each list, and the report says a thing is
    # your weakness and very nearly the same thing is your strength.
    taken: list = []
    weaknesses = _pick([row for row in candidates if row["excessCp"] > 0],
                       key=lambda row: -row["excessCp"], limit=MAX_FINDINGS,
                       taken=taken)
    strengths = _pick([row for row in candidates if row["excessCp"] < 0],
                      key=lambda row: row["excessCp"], limit=MAX_STRENGTHS,
                      taken=taken)

    for row in weaknesses + strengths:
        row["sentence"] = sentence(row)

    skipped.sort(key=lambda row: -row["moves"])
    return {
        "weaknesses": weaknesses,
        "strengths": strengths,
        "skipped": skipped[:20],
        "minMoves": min_moves,
        "minGames": min_games,
    }


def _pick(rows: list, *, key, limit: int, taken: list) -> list:
    """Greedy: take the strongest, then anything that is not it again.

    ``taken`` is shared across calls and appended to, so a later list cannot
    re-report a move set an earlier one already claimed.
    """
    chosen: list = []
    for row in sorted(rows, key=key):
        if len(chosen) >= limit:
            break
        if _overlaps(row["_members"], taken):
            continue
        taken.append(row["_members"])
        chosen.append({k: v for k, v in row.items() if k != "_members"})
    return chosen


def worst_moments(moves: list, *, limit: int = 12) -> list:
    """The single moves that cost you most, for the diagrams in the PDF."""
    scored = [move for move in moves
              if not move["inBook"] and move.get("cpLoss") is not None
              and not move.get("forced")]
    scored.sort(key=lambda move: -(move.get("cpLoss") or 0))

    out = []
    seen_games = {}
    for move in scored:
        game = move.get("game") or {}
        # At most two moments from one game: a single collapse should not fill
        # the page, and the point is a pattern across your history.
        if seen_games.get(game.get("id"), 0) >= 2:
            continue
        seen_games[game.get("id")] = seen_games.get(game.get("id"), 0) + 1
        out.append({
            "san": move["san"],
            "bestSan": move.get("bestSan"),
            "cpLoss": move["cpLoss"],
            "pawns": round((move["cpLoss"] or 0) / 100.0, 2),
            "label": move.get("label", ""),
            "phase": move.get("phase", ""),
            "moveNumber": move.get("moveNumber"),
            "fen": move.get("fenBefore", ""),
            "you": game.get("you", "white"),
            "url": game.get("url", ""),
            "them": game.get("them", ""),
            "date": game.get("date", ""),
            "opening": (game.get("opening") or {}).get("name", ""),
            "situation": ", ".join(BY_KEY["situation"].buckets_of(move)) or "",
        })
        if len(out) >= limit:
            break
    return out


__all__ = [
    "MAX_FINDINGS",
    "OVERLAP",
    "build",
    "sentence",
    "worst_moments",
]
