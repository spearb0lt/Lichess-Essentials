"""What your opponent actually plays, counted.

A few hundred of somebody's games folded into one tree keyed by position, with
the result of every game that passed through each move attached to it.  Two
trees per player, not one: the same position means different things depending
on which side of it they were sitting, and a report that mixes those is
answering a question nobody asked.

Every number here is from **their** point of view.  A score of 0.61 on a move
means they scored 61% with it -- wins plus half the draws -- regardless of
their colour.  That is the only convention in this file and it is worth
keeping in mind while reading it, because the natural instinct when preparing
is to think in your own favour and the arithmetic here never does.

Two things this file deliberately does not do:

*It does not judge a move.*  There is no engine here.  A move that scores 30%
over forty games is a fact about their results, not a claim that the move is
bad -- and it is the more useful fact, because you are playing them, not the
move.

*It does not pretend small samples are large.*  Every ranking takes a minimum
game count, and the raw counts travel with every number so a 100% score over
two games is visibly what it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess

#: How deep a scouting tree goes.  Twelve moves is past the point where
#: preparation becomes a middlegame, and every ply beyond it multiplies the
#: leaf count for information no one acts on.
DEFAULT_MAX_PLY = 24

#: Results, from the scouted player's point of view.
WIN, DRAW, LOSS = "w", "d", "l"


def outcome_for(result: str, color: str) -> str:
    """A PGN result plus which side they had, as w/d/l for them."""
    if result == "1-0":
        return WIN if color == "white" else LOSS
    if result == "0-1":
        return WIN if color == "black" else LOSS
    return DRAW


def score_of(w: int, d: int, l: int) -> float:
    """Wins plus half the draws, as a fraction. Zero games scores 0.5."""
    total = w + d + l
    return (w + d / 2) / total if total else 0.5


@dataclass
class Tally:
    """Games and results, from the scouted player's point of view."""

    games: int = 0
    w: int = 0
    d: int = 0
    l: int = 0

    def add(self, outcome: str) -> None:
        self.games += 1
        setattr(self, outcome, getattr(self, outcome) + 1)

    @property
    def score(self) -> float:
        return score_of(self.w, self.d, self.l)

    def to_json(self) -> dict:
        return {"games": self.games, "w": self.w, "d": self.d, "l": self.l,
                "score": round(self.score, 4)}


@dataclass
class MoveStat(Tally):
    """One move they played from one position."""

    uci: str = ""
    san: str = ""
    last_date: str = ""
    sample: str = ""                 # a game URL, so a claim can be checked

    def to_json(self) -> dict:
        data = super().to_json()
        data.update({"uci": self.uci, "san": self.san,
                     "lastDate": self.last_date, "sample": self.sample})
        return data


@dataclass
class Node(Tally):
    """One position they reached, and every move they played from it."""

    epd: str = ""
    fen: str = ""
    ply: int = 0
    turn: str = "white"
    line: list = field(default_factory=list)        # SAN, shortest route here
    line_uci: list = field(default_factory=list)
    moves: dict = field(default_factory=dict)       # uci -> MoveStat

    def to_json(self, *, with_moves: bool = True) -> dict:
        data = super().to_json()
        data.update({
            "epd": self.epd, "fen": self.fen, "ply": self.ply, "turn": self.turn,
            "line": list(self.line), "lineUci": list(self.line_uci),
        })
        if with_moves:
            data["moves"] = [
                move.to_json() for move in
                sorted(self.moves.values(), key=lambda m: (-m.games, m.san))
            ]
        return data


class Tree:
    """Their games in one colour, as a position-keyed tree."""

    def __init__(self, color: str, max_ply: int = DEFAULT_MAX_PLY):
        self.color = color                  # the colour THEY had
        self.max_ply = max_ply
        self.nodes: dict = {}
        self.games = 0
        self.tally = Tally()
        self.root_epd = chess.Board().epd()

    # ------------------------------------------------------------- building

    def add_game(self, ucis, *, result: str, date: str = "",
                 url: str = "") -> None:
        """Walk one of their games into the tree."""
        outcome = outcome_for(result, self.color)
        self.games += 1
        self.tally.add(outcome)

        board = chess.Board()
        line_san: list = []
        line_uci: list = []

        for ply, uci in enumerate(ucis):
            if ply >= self.max_ply:
                break
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                break
            if move not in board.legal_moves:
                break

            node = self._node(board, ply, line_san, line_uci)
            node.add(outcome)

            # Only *their* moves are choices worth counting. Their opponent's
            # moves shape the tree but say nothing about them, so they get a
            # node and no move statistics.
            if (board.turn == chess.WHITE) == (self.color == "white"):
                stat = node.moves.get(uci)
                if stat is None:
                    stat = MoveStat(uci=uci, san=board.san(move))
                    node.moves[uci] = stat
                stat.add(outcome)
                if date >= stat.last_date:
                    stat.last_date = date
                    stat.sample = url

            line_san = line_san + [board.san(move)]
            line_uci = line_uci + [uci]
            board.push(move)

        # The final position too, so a line that ends inside the window still
        # has a node -- otherwise the deepest move points at nothing.
        self._node(board, len(line_uci), line_san, line_uci).add(outcome)

    def _node(self, board: chess.Board, ply: int, line_san, line_uci) -> Node:
        epd = board.epd()
        node = self.nodes.get(epd)
        if node is None:
            node = Node(
                epd=epd, fen=board.fen(), ply=ply,
                turn="white" if board.turn == chess.WHITE else "black",
                line=list(line_san), line_uci=list(line_uci))
            self.nodes[epd] = node
        elif len(line_uci) < len(node.line_uci):
            # Transpositions reach one position by several routes; show the
            # shortest, which is the one a reader recognises.
            node.line = list(line_san)
            node.line_uci = list(line_uci)
            node.ply = ply
        return node

    # -------------------------------------------------------------- reading

    def node(self, epd: str):
        return self.nodes.get(epd)

    def root(self):
        return self.nodes.get(self.root_epd)

    def their_nodes(self, min_games: int = 1) -> list:
        """Positions where it was their move, busiest first."""
        want_white = self.color == "white"
        return sorted(
            (node for node in self.nodes.values()
             if node.turn == ("white" if want_white else "black")
             and node.games >= min_games and node.moves),
            key=lambda node: (-node.games, node.ply))

    def to_json(self, *, min_games: int = 1, max_nodes: int = 400) -> dict:
        nodes = sorted(self.nodes.values(),
                       key=lambda node: (-node.games, node.ply))
        kept = [node for node in nodes if node.games >= min_games][:max_nodes]
        return {
            "color": self.color,
            "maxPly": self.max_ply,
            "games": self.games,
            "tally": self.tally.to_json(),
            "nodes": [node.to_json() for node in kept],
        }


# ------------------------------------------------------------------ building


def build_trees(games, username: str, *, max_ply: int = DEFAULT_MAX_PLY) -> dict:
    """``{"white": Tree, "black": Tree}`` for one player's games."""
    lowered = (username or "").strip().lstrip("@").lower()
    trees = {"white": Tree("white", max_ply), "black": Tree("black", max_ply)}

    for game in games or []:
        if game.get("white", "").lower() == lowered:
            color = "white"
        elif game.get("black", "").lower() == lowered:
            color = "black"
        else:
            continue
        moves = (game.get("moves") or "").split()
        if not moves:
            continue
        trees[color].add_game(
            moves, result=game.get("result", "*"),
            date=game.get("date", ""), url=game.get("url", ""))

    return trees


# ------------------------------------------------------------------ rankings


def top_moves(tree: Tree, *, min_games: int = 1, limit: int = 30) -> list:
    """Their most-played choices, busiest position first.

    One row per (position, move), which is the unit you actually prepare
    against: "in this position they play this, this often, and score this".
    """
    rows = []
    for node in tree.their_nodes(min_games=1):
        for stat in node.moves.values():
            if stat.games < min_games:
                continue
            rows.append({
                "epd": node.epd,
                "fen": node.fen,
                "ply": node.ply,
                "line": list(node.line),
                "lineUci": list(node.line_uci),
                "reached": node.games,
                "share": round(stat.games / node.games, 4) if node.games else 0.0,
                **stat.to_json(),
            })
    rows.sort(key=lambda row: (-row["games"], row["ply"]))
    return rows[:limit]


def weak_spots(tree: Tree, *, min_games: int = 6, limit: int = 15) -> list:
    """The moves of theirs that cost them the most points.

    Ranked by ``games * (0.5 - score)`` -- the number of points they have
    dropped below an even score in this line, which is the honest way to
    combine "how badly it goes for them" with "how often it happens".  A move
    they lose with once is not a plan; a move they score 35% with over thirty
    games is.

    This is a fact about their results, not a verdict on the move.  Both the
    raw record and the sample size travel with every row so you can see the
    difference.
    """
    rows = []
    for node in tree.their_nodes(min_games=1):
        for stat in node.moves.values():
            if stat.games < min_games:
                continue
            leak = stat.games * (0.5 - stat.score)
            if leak <= 0:
                continue
            rows.append({
                "epd": node.epd,
                "fen": node.fen,
                "ply": node.ply,
                "line": list(node.line),
                "lineUci": list(node.line_uci),
                "reached": node.games,
                "share": round(stat.games / node.games, 4) if node.games else 0.0,
                "leak": round(leak, 2),
                **stat.to_json(),
            })
    rows.sort(key=lambda row: (-row["leak"], -row["games"]))
    return rows[:limit]


def walk_to(tree: Tree, line_uci) -> dict:
    """The node a line of UCI moves arrives at, for browsing the tree.

    Returns the node, the moves they played from it, and -- because the tree
    is keyed by position -- every reply their opponents made, so the caller
    can step forward through either side.
    """
    board = chess.Board()
    for uci in line_uci or []:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return {"found": False, "reason": f"{uci} is not a move"}
        if move not in board.legal_moves:
            return {"found": False, "reason": f"{uci} is not legal here"}
        board.push(move)

    node = tree.nodes.get(board.epd())
    if node is None:
        return {"found": False, "fen": board.fen(),
                "reason": "they never reached this position"}

    replies = []
    if not node.moves:                      # their opponent to move here
        for move in board.legal_moves:
            after = board.copy(stack=False)
            after.push(move)
            child = tree.nodes.get(after.epd())
            if child is not None:
                replies.append({"uci": move.uci(), "san": board.san(move),
                                **child.to_json(with_moves=False)})
        replies.sort(key=lambda row: -row["games"])

    return {"found": True, "node": node.to_json(), "replies": replies}


__all__ = [
    "DEFAULT_MAX_PLY",
    "MoveStat",
    "Node",
    "Tally",
    "Tree",
    "build_trees",
    "outcome_for",
    "score_of",
    "top_moves",
    "walk_to",
    "weak_spots",
]
