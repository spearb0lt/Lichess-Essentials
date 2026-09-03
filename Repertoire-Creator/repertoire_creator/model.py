"""The repertoire data model and the move-tree helpers everything else uses.

A repertoire is a folder on disk.  Each chapter is one PGN file holding one
game whose variation tree *is* the repertoire line -- python-chess already
models variations, comments, NAGs and ``[%eval]`` annotations correctly, so a
chapter is simply a :class:`chess.pgn.Game` and no parallel tree type exists
to drift out of sync with it.

**Node addressing.**  The browser needs to name a node in that tree.  We use
the sequence of child indices from the root: ``()`` is the starting position,
``(0,)`` the first move, ``(0, 1)`` the second child of the first move.  Paths
travel over the wire as dotted strings (``"0.1"``, ``""`` for the root).  They
are valid only while the tree is unchanged, which is fine because every
mutation makes the client refetch the chapter.

**Colour awareness.**  A repertoire is built for one side.  Given a chapter's
starting position we can say, for any node, whether the move about to be
played is *yours* or the *opponent's*, and that single fact drives gap
detection, drill mode and how the move tree is drawn.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field

import chess
import chess.pgn

#: Lichess stores drawable shapes inside PGN comments; we keep them verbatim
#: on the way through but need them out of the way when showing prose.
_CSL_RE = re.compile(r"\[%csl\s+([^\]]*)\]")
_CAL_RE = re.compile(r"\[%cal\s+([^\]]*)\]")
_CMD_RE = re.compile(r"\[%\w+\s*[^\]]*\]")

_SHAPE_COLORS = {"G": "green", "R": "red", "Y": "yellow", "B": "blue"}
_COLOR_LETTERS = {v: k for k, v in _SHAPE_COLORS.items()}

#: The NAGs worth offering as buttons, in the order Lichess shows them.
NAG_CHOICES = [
    (1, "!"), (2, "?"), (3, "!!"), (4, "??"), (5, "!?"), (6, "?!"),
]
NAG_SYMBOLS = dict(NAG_CHOICES) | {
    7: "□", 10: "=", 13: "∞", 14: "+/=", 15: "=/+",
    16: "±", 17: "∓", 18: "+-", 19: "-+", 22: "⊙",
    36: "→", 40: "↑", 132: "⇆",
}


def nag_text(nags) -> str:
    return "".join(NAG_SYMBOLS[n] for n in sorted(nags) if n in NAG_SYMBOLS)


def split_comment(raw: str):
    """Return ``(prose, circles, arrows)`` from a Lichess-style PGN comment."""
    if not raw:
        return "", [], []
    circles, arrows = [], []
    for chunk in _CSL_RE.findall(raw):
        for item in (c.strip() for c in chunk.split(",")):
            if len(item) == 3 and item[0] in _SHAPE_COLORS:
                circles.append((_SHAPE_COLORS[item[0]], item[1:].lower()))
    for chunk in _CAL_RE.findall(raw):
        for item in (c.strip() for c in chunk.split(",")):
            if len(item) == 5 and item[0] in _SHAPE_COLORS:
                arrows.append(
                    (_SHAPE_COLORS[item[0]], item[1:3].lower(), item[3:5].lower())
                )
    text = _CMD_RE.sub("", raw)
    return re.sub(r"\s+", " ", text).strip(), circles, arrows


def build_comment(text: str, circles=(), arrows=(), eval_command: str = "") -> str:
    """Rebuild a PGN comment from prose plus shapes, in the order Lichess uses."""
    parts = []
    if eval_command:
        parts.append(eval_command)
    if circles:
        csl = ",".join(
            f"{_COLOR_LETTERS.get(color, 'G')}{square}" for color, square in circles
        )
        parts.append(f"[%csl {csl}]")
    if arrows:
        cal = ",".join(
            f"{_COLOR_LETTERS.get(color, 'G')}{src}{dst}" for color, src, dst in arrows
        )
        parts.append(f"[%cal {cal}]")
    prose = (text or "").strip()
    if prose:
        parts.append(prose)
    return " ".join(parts)


# ------------------------------------------------------------------ manifest


@dataclass
class ChapterMeta:
    """What we track about a chapter beyond the PGN itself."""

    id: str                            # stable local id, never reused
    file: str                          # filename inside chapters/
    name: str
    orientation: str = "white"         # board orientation on Lichess
    #: The Lichess chapter this was last pushed to, if any.  Its presence is
    #: what turns the next push into an in-place update instead of a second
    #: chapter appearing alongside the first.
    lichess_chapter_id: str | None = None
    #: Hash of the move text at the moment of the last successful push.  While
    #: it still matches, the chapter is unchanged and a push can skip it.
    pushed_hash: str | None = None
    pushed_at: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> "ChapterMeta":
        return cls(
            id=data["id"],
            file=data["file"],
            name=data.get("name", ""),
            orientation=data.get("orientation", "white"),
            lichess_chapter_id=data.get("lichessChapterId"),
            pushed_hash=data.get("pushedHash"),
            pushed_at=data.get("pushedAt"),
        )

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "file": self.file,
            "name": self.name,
            "orientation": self.orientation,
            "lichessChapterId": self.lichess_chapter_id,
            "pushedHash": self.pushed_hash,
            "pushedAt": self.pushed_at,
        }


@dataclass
class RepertoireMeta:
    slug: str
    name: str
    color: str = "white"               # the side this repertoire is played as
    description: str = ""
    created: str = ""
    updated: str = ""
    lichess_study_id: str | None = None
    lichess_visibility: str = "unlisted"
    chapters: list = field(default_factory=list)   # list[ChapterMeta]

    @property
    def is_white(self) -> bool:
        return self.color == "white"

    @property
    def lichess_url(self) -> str | None:
        if not self.lichess_study_id:
            return None
        return f"https://lichess.org/study/{self.lichess_study_id}"

    @classmethod
    def from_json(cls, data: dict) -> "RepertoireMeta":
        lichess = data.get("lichess") or {}
        return cls(
            slug=data["slug"],
            name=data.get("name", data["slug"]),
            color=data.get("color", "white"),
            description=data.get("description", ""),
            created=data.get("created", ""),
            updated=data.get("updated", ""),
            lichess_study_id=lichess.get("studyId"),
            lichess_visibility=lichess.get("visibility", "unlisted"),
            chapters=[ChapterMeta.from_json(c) for c in data.get("chapters", [])],
        )

    def to_json(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "color": self.color,
            "description": self.description,
            "created": self.created,
            "updated": self.updated,
            "lichess": {
                "studyId": self.lichess_study_id,
                "visibility": self.lichess_visibility,
            },
            "chapters": [c.to_json() for c in self.chapters],
        }

    def chapter(self, chapter_id: str) -> ChapterMeta | None:
        for meta in self.chapters:
            if meta.id == chapter_id:
                return meta
        return None


# --------------------------------------------------------------- tree access


class PathError(LookupError):
    """Raised when a node path does not name a node in the tree."""


def parse_path(text: str | None) -> tuple:
    """``"0.1"`` -> ``(0, 1)``; ``""`` or ``None`` -> ``()``, the root."""
    if not text:
        return ()
    try:
        return tuple(int(part) for part in text.split(".") if part != "")
    except ValueError as exc:
        raise PathError(f"Bad node path: {text!r}") from exc


def format_path(path) -> str:
    return ".".join(str(index) for index in path)


def resolve(game: chess.pgn.Game, path) -> chess.pgn.GameNode:
    node = game
    for index in path:
        if index < 0 or index >= len(node.variations):
            raise PathError(f"No node at path {format_path(path)!r}")
        node = node.variations[index]
    return node


def path_of(node: chess.pgn.GameNode) -> tuple:
    """Walk back up to the root, collecting the child index at each step."""
    path = []
    current = node
    while current.parent is not None:
        path.append(current.parent.variations.index(current))
        current = current.parent
    return tuple(reversed(path))


def is_my_turn(board: chess.Board, color: str | None) -> bool | None:
    """True when the side to move in ``board`` is the repertoire own side.

    ``None`` for the colour means the caller has no side -- universal mode
    records both -- and the answer is then ``None`` rather than a guess.
    """
    if color not in ("white", "black"):
        return None
    mine = chess.WHITE if color == "white" else chess.BLACK
    return board.turn == mine


def move_hash(game: chess.pgn.Game) -> str:
    """Fingerprint a chapter content -- moves, comments, NAGs, shapes.

    Used to decide whether a chapter needs pushing again.  Headers are left
    out on purpose: renaming a chapter is a tag update, not a move update.
    """
    exporter = chess.pgn.StringExporter(
        headers=False, variations=True, comments=True, columns=None
    )
    body = game.accept(exporter)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def game_to_pgn(game: chess.pgn.Game, *, headers: bool = True) -> str:
    exporter = chess.pgn.StringExporter(
        headers=headers, variations=True, comments=True, columns=None
    )
    return game.accept(exporter)


def pgn_to_game(text: str) -> chess.pgn.Game:
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None:
        raise ValueError("That PGN contains no game.")
    return game


# ------------------------------------------------------------ serialisation


def node_json(node: chess.pgn.GameNode, board_before: chess.Board,
              color: str | None) -> dict:
    """One node, described for the browser.

    ``board_before`` is the position the move was played from; the caller
    already holds it while walking, so we never recompute it here.
    """
    path = path_of(node)
    is_root = node.parent is None
    prose, circles, arrows = split_comment(node.comment)

    if is_root:
        board_after = board_before
        san = uci = ""
        last_move = None
    else:
        board_after = board_before.copy(stack=False)
        board_after.push(node.move)
        san = board_before.san(node.move)
        uci = node.move.uci()
        last_move = [
            chess.square_name(node.move.from_square),
            chess.square_name(node.move.to_square),
        ]

    score = node.eval()
    eval_payload = None
    if score is not None:
        white = score.white()
        eval_payload = {
            "cp": white.score(),
            "mate": white.mate(),
            "depth": node.eval_depth(),
        }

    return {
        "path": format_path(path),
        "parent": format_path(path[:-1]) if path else None,
        "san": san,
        "uci": uci,
        "fen": board_after.fen(),
        "fenBefore": board_before.fen(),
        "moveNumber": board_before.fullmove_number,
        "whiteMoved": board_before.turn == chess.WHITE,
        # Whose move *this* was. The root has no move, so it is null.
        "mine": None if is_root else is_my_turn(board_before, color),
        # Whose move comes next from here -- what drill mode and gap
        # detection actually key off.
        "myTurnNext": is_my_turn(board_after, color),
        "comment": prose,
        "nags": sorted(node.nags),
        "nagText": nag_text(node.nags),
        "circles": [list(c) for c in circles],
        "arrows": [list(a) for a in arrows],
        "lastMove": last_move,
        "eval": eval_payload,
        "children": [format_path(path + (i,)) for i in range(len(node.variations))],
        "isMainline": True if is_root else node.is_mainline(),
        "gameOver": board_after.is_game_over(),
        "check": board_after.is_check(),
    }


def tree_json(game: chess.pgn.Game, color: str | None) -> dict:
    """Flatten a whole chapter into ``{path: node}`` plus the root path."""
    nodes: dict[str, dict] = {}

    def walk(node, board_before):
        payload = node_json(node, board_before, color)
        nodes[payload["path"]] = payload
        if node.parent is None:
            board_after = board_before
        else:
            board_after = board_before.copy(stack=False)
            board_after.push(node.move)
        for child in node.variations:
            walk(child, board_after)

    walk(game, game.board())
    return {"root": "", "nodes": nodes}


def mainline_paths(game: chess.pgn.Game) -> list:
    """Every node on the main line, root first."""
    out, node = [], game
    while True:
        out.append(format_path(path_of(node)))
        if not node.variations:
            return out
        node = node.variations[0]


def iter_nodes(game: chess.pgn.Game):
    """Yield ``(node, board_before)`` for every node except the root."""
    stack = [(game, game.board())]
    while stack:
        node, board = stack.pop()
        for child in node.variations:
            after = board.copy(stack=False)
            after.push(child.move)
            yield child, board
            stack.append((child, after))
