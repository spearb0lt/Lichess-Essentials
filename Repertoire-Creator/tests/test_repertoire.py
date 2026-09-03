"""Tests for the parts that would quietly corrupt a repertoire if they broke.

Nothing here touches the network or the engine.  The Lichess client is
exercised against a fake transport so the sync logic -- which chapter gets
created, which gets updated in place, which is skipped -- is covered without
a token or a study to write to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repertoire_creator import analysis, drill, editing, sync
from repertoire_creator.model import (
    build_comment,
    format_path,
    move_hash,
    parse_path,
    split_comment,
    tree_json,
)
from repertoire_creator.storage import Repertoire, StorageError


@pytest.fixture()
def white(tmp_path):
    repertoire = Repertoire.create(tmp_path, "White Italian", color="white")
    repertoire.add_chapter("Main line")
    return repertoire


def chapter_id(repertoire):
    return repertoire.meta.chapters[0].id


# ------------------------------------------------------------------ editing


def test_playing_the_same_move_twice_does_not_duplicate_it(white):
    game = white.game(chapter_id(white))
    first, created = editing.play_move(game, (), san="e4")
    again, created_again = editing.play_move(game, (), san="e4")

    assert created is True and created_again is False
    assert first == again == (0,)
    assert len(game.variations) == 1


def test_illegal_moves_are_refused(white):
    game = white.game(chapter_id(white))
    with pytest.raises(editing.EditError):
        editing.play_move(game, (), san="e5")
    with pytest.raises(editing.EditError):
        editing.play_move(game, (), uci="e2e5")


def test_add_line_merges_rather_than_appending(white):
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. e4 e5 2. Nf3")
    stats = editing.add_line(game, (), "1. e4 e5 2. Nf3 Nc6 3. Bc4")

    assert stats["existing"] == 3          # e4, e5, Nf3 were already there
    assert stats["added"] == 2             # Nc6 and Bc4 are new
    assert len(game.variations) == 1       # still one first move
    # And it leaves you standing at the end of what you just pasted.
    assert stats["path"] == "0.0.0.0.0"


def test_add_line_reports_a_bad_move_instead_of_importing_half(white):
    game = white.game(chapter_id(white))
    with pytest.raises(editing.EditError):
        editing.add_line(game, (), "1. e4 e5 2. Qxf7")
    assert len(game.variations) == 0


def test_promote_to_main_changes_the_move_you_play(white):
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. e4 e5 2. Nf3")
    editing.add_line(game, parse_path("0.0"), "2. Bc4")

    node = game.variations[0].variations[0]
    assert node.variations[0].san() == "Nf3"

    editing.promote(game, parse_path("0.0.1"), to_main=True)
    assert node.variations[0].san() == "Bc4"


def test_promote_to_main_lifts_a_move_out_of_a_nested_sideline(white):
    """A promoted move has to reach the main line, not just the top of its box."""
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. e4 e5 2. Nf3 Nc6 3. Bc4")
    editing.add_line(game, parse_path("0.0"), "2. Bc4 Nc6 3. d3")

    # The d3 line is currently two levels down inside a sideline.
    editing.promote(game, parse_path("0.0.1.0.0"), to_main=True)

    mainline = [node.san() for node in game.mainline()]
    assert mainline == ["e4", "e5", "Bc4", "Nc6", "d3"]


def test_annotation_survives_a_round_trip_through_disk(white):
    cid = chapter_id(white)
    game = white.game(cid)
    editing.add_line(game, (), "1. e4 e5")
    editing.set_comment(game, parse_path("0.0"), "The main reply")
    editing.toggle_nag(game, parse_path("0.0"), 5)
    editing.set_shapes(
        game, parse_path("0.0"),
        circles=[("green", "d4")], arrows=[("red", "f1", "c4")],
    )
    white.save_chapter(cid, game)

    reopened = Repertoire.load(white.root)
    tree = tree_json(reopened.game(cid), "white")
    node = tree["nodes"]["0.0"]

    assert node["comment"] == "The main reply"
    assert node["nags"] == [5]
    assert node["circles"] == [["green", "d4"]]
    assert node["arrows"] == [["red", "f1", "c4"]]


def test_setting_a_comment_keeps_the_eval(white):
    """Editing prose must not throw away a baked evaluation."""
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. e4")
    node = game.variations[0]
    node.set_eval(chess.engine.PovScore(chess.engine.Cp(31), chess.WHITE))

    editing.set_comment(game, parse_path("0"), "First move")
    assert node.eval().white().score() == 31
    assert node.comment.count("[%eval") == 1


def test_toggling_a_nag_replaces_the_previous_judgement(white):
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. e4")
    editing.toggle_nag(game, parse_path("0"), 1)
    nags = editing.toggle_nag(game, parse_path("0"), 3)
    assert nags == [3]


# ------------------------------------------------------------------ colour


def test_colour_decides_whose_move_is_whose(tmp_path):
    black = Repertoire.create(tmp_path, "Caro", color="black")
    black.add_chapter("Main")
    cid = black.meta.chapters[0].id
    game = black.game(cid)
    editing.add_line(game, (), "1. e4 c6")

    tree = tree_json(game, "black")
    assert tree["nodes"]["0"]["mine"] is False       # 1. e4 is White's
    assert tree["nodes"]["0.0"]["mine"] is True      # 1... c6 is ours
    assert tree["nodes"]["0"]["myTurnNext"] is True  # after e4 it is our move


# -------------------------------------------------------------------- gaps


def test_a_position_with_no_reply_of_yours_is_a_gap(white):
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6")

    gaps = analysis.chapter_gaps(game, "white")
    missing = [g for g in gaps if g["kind"] == "missing"]
    assert len(missing) == 1
    assert missing[0]["line"].endswith("3.Bc4 Nf6")


def test_two_moves_of_your_own_are_flagged_as_undecided(white):
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. e4 e5 2. Nf3")
    editing.add_line(game, parse_path("0.0"), "2. Bc4")

    undecided = [g for g in analysis.chapter_gaps(game, "white")
                 if g["kind"] == "undecided"]
    assert len(undecided) == 1
    assert set(undecided[0]["moves"]) == {"Nf3", "Bc4"}


def test_a_finished_game_is_not_a_gap(white):
    game = white.game(chapter_id(white))
    editing.add_line(game, (), "1. f3 e5 2. g4 Qh4#")
    missing = [g for g in analysis.chapter_gaps(game, "white") if g["kind"] == "missing"]
    assert missing == []


# ---------------------------------------------------------- transpositions


def test_conflicting_answers_to_one_position_are_reported(white):
    first = white.meta.chapters[0]
    game = white.game(first.id)
    editing.add_line(game, (), "1. e4 e5 2. Nf3")
    white.save_chapter(first.id, game)

    second = white.add_chapter("Other move order")
    other = white.game(second.id)
    editing.add_line(other, (), "1. e4 e5 2. Bc4")
    white.save_chapter(second.id, other)

    chapters = [(m, white.game(m.id)) for m in white.meta.chapters]
    conflicts = [t for t in analysis.transpositions(chapters, "white") if t["conflict"]]

    assert len(conflicts) == 1
    assert set(conflicts[0]["moves"]) == {"Nf3", "Bc4"}


def test_a_real_transposition_is_not_a_conflict(white):
    """Same position, same answer, different move order: nothing to fix."""
    first = white.meta.chapters[0]
    game = white.game(first.id)
    editing.add_line(game, (), "1. e4 e5 2. Nf3 Nc6 3. Bc4")
    white.save_chapter(first.id, game)

    second = white.add_chapter("Bishop first")
    other = white.game(second.id)
    editing.add_line(other, (), "1. e4 e5 2. Bc4 Nc6 3. Nf3")
    white.save_chapter(second.id, other)

    chapters = [(m, white.game(m.id)) for m in white.meta.chapters]
    found = analysis.transpositions(chapters, "white")
    same = [t for t in found if t["ply"] == 5]

    assert same, "the shared position after 3 moves each was not spotted"
    assert not any(t["conflict"] for t in same)


# ------------------------------------------------------------------- drill


def test_drill_asks_only_about_your_own_moves(tmp_path):
    black = Repertoire.create(tmp_path, "Caro", color="black")
    black.add_chapter("Main")
    cid = black.meta.chapters[0].id
    game = black.game(cid)
    editing.add_line(game, (), "1. e4 c6 2. d4 d5")
    black.save_chapter(cid, game)

    cards = drill.collect_cards([(black.meta.chapters[0], game)], "black")
    answers = {card["answerSan"] for card in cards}
    assert answers == {"c6", "d5"}


def test_a_wrong_answer_brings_the_card_back_sooner(white):
    state = {}
    drill.grade(state, "k", drill.quality_for(True))
    drill.grade(state, "k", drill.quality_for(True))
    good = state["k"]["interval"]

    drill.grade(state, "k", drill.quality_for(False))
    assert state["k"]["interval"] < good
    assert state["k"]["lapses"] == 1
    assert state["k"]["reps"] == 0


def test_drill_state_survives_a_save(white):
    state = {"a": {"ease": 2.5, "interval": 3.0, "reps": 2, "lapses": 0,
                   "due": None, "last": None}}
    white.save_drill(state)
    assert white.load_drill() == state


# ----------------------------------------------------------------- storage


def test_chapters_are_plain_pgn_on_disk(white):
    cid = chapter_id(white)
    game = white.game(cid)
    editing.add_line(game, (), "1. e4 e5")
    white.save_chapter(cid, game)

    path = white.chapter_path(white.meta.chapters[0])
    text = path.read_text(encoding="utf-8")
    assert '[ChapterName "Main line"]' in text
    assert "1. e4 e5" in text
    assert json.loads((white.root / "repertoire.json").read_text())["color"] == "white"


def test_a_slug_cannot_escape_the_data_folder(tmp_path):
    from repertoire_creator.storage import open_repertoire

    with pytest.raises(StorageError):
        open_repertoire(tmp_path, "../elsewhere")


def test_the_hash_ignores_the_chapter_name(white):
    cid = chapter_id(white)
    game = white.game(cid)
    editing.add_line(game, (), "1. e4")
    white.save_chapter(cid, game)
    before = white.chapter_hash(cid)

    white.rename_chapter(cid, "A different name")
    assert white.chapter_hash(cid) == before, "renaming should not force a push"


def test_comment_rebuilding_keeps_shapes_and_prose_apart():
    raw = "[%csl Gd4,Re5] [%cal Gf1c4] Watch the f7 square"
    prose, circles, arrows = split_comment(raw)
    assert prose == "Watch the f7 square"
    assert circles == [("green", "d4"), ("red", "e5")]
    assert arrows == [("green", "f1", "c4")]

    rebuilt = build_comment(prose, circles, arrows)
    assert split_comment(rebuilt) == (prose, circles, arrows)


# -------------------------------------------------------------------- sync


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """Stands in for LichessClient, recording what a push would send."""

    def __init__(self):
        self.token = "fake"
        self.calls = []
        self.next_chapter = 0

    def create_study(self, name, *, visibility="unlisted"):
        self.calls.append(("create_study", name, visibility))
        return "study00"

    def study_chapters(self, study_id):
        self.calls.append(("study_chapters", study_id))
        return [{"id": "placehol", "name": "Chapter 1"}]

    def import_pgn(self, study_id, pgn, *, name=None, orientation=None, mode=None):
        self.calls.append(("import_pgn", name, orientation))
        self.next_chapter += 1
        return [{"id": f"chapter{self.next_chapter}", "name": name}]

    def update_moves(self, study_id, chapter_id, pgn):
        self.calls.append(("update_moves", chapter_id, pgn))

    def update_tags(self, study_id, chapter_id, tags):
        self.calls.append(("update_tags", chapter_id, tags["ChapterName"]))

    def delete_chapter(self, study_id, chapter_id):
        self.calls.append(("delete_chapter", chapter_id))


def test_first_push_creates_the_study_and_clears_the_empty_chapter(white):
    cid = chapter_id(white)
    game = white.game(cid)
    editing.add_line(game, (), "1. e4 e5")
    white.save_chapter(cid, game)

    client = FakeClient()
    report = sync.push(white, client)

    assert report["created"] == 1 and report["failed"] == 0
    assert white.meta.lichess_study_id == "study00"
    assert white.meta.chapters[0].lichess_chapter_id == "chapter1"
    assert ("delete_chapter", "placehol") in client.calls


def test_second_push_updates_in_place_and_skips_unchanged(white):
    cid = chapter_id(white)
    game = white.game(cid)
    editing.add_line(game, (), "1. e4 e5")
    white.save_chapter(cid, game)

    client = FakeClient()
    sync.push(white, client)

    # Nothing changed: the next push must not send anything at all.
    client.calls.clear()
    report = sync.push(white, client)
    assert report["skipped"] == 1 and report["updated"] == 0
    assert not [c for c in client.calls if c[0] in ("import_pgn", "update_moves")]

    # Change one move: now it updates the same chapter rather than making one.
    game = white.game(cid)
    editing.add_line(game, parse_path("0.0"), "2. Nf3")
    white.save_chapter(cid, game)

    client.calls.clear()
    report = sync.push(white, client)
    assert report["updated"] == 1 and report["created"] == 0
    sent = [c for c in client.calls if c[0] == "update_moves"]
    assert sent and sent[0][1] == "chapter1"
    assert "[Event" not in sent[0][2], "the moves endpoint should get moves only"


def test_a_failing_chapter_does_not_stop_the_others(white):
    from repertoire_creator.lichess import LichessError

    first = white.meta.chapters[0]
    editing.add_line(white.game(first.id), (), "1. e4")
    white.save_chapter(first.id)
    second = white.add_chapter("Second")
    editing.add_line(white.game(second.id), (), "1. d4")
    white.save_chapter(second.id)

    class Flaky(FakeClient):
        def import_pgn(self, study_id, pgn, *, name=None, orientation=None, mode=None):
            if name == "Main line":
                raise LichessError("nope")
            return super().import_pgn(study_id, pgn, name=name,
                                      orientation=orientation, mode=mode)

    report = sync.push(white, Flaky())
    assert report["failed"] == 1 and report["created"] == 1
    assert white.meta.chapters[1].lichess_chapter_id is not None
    assert white.meta.chapters[0].lichess_chapter_id is None


def test_importing_a_study_records_its_chapter_ids(tmp_path):
    pgn = (
        '[Event "My Study: First"]\n'
        '[Site "https://lichess.org/study/study00/chapter1"]\n'
        '[StudyName "My Study"]\n'
        '[ChapterName "First"]\n'
        '[Orientation "black"]\n\n'
        "1. e4 c6 *\n\n\n"
        '[Event "My Study: Second"]\n'
        '[Site "https://lichess.org/study/study00/chapter2"]\n'
        '[ChapterName "Second"]\n\n'
        "1. d4 d5 *\n"
    )

    class Exporter(FakeClient):
        def export_study(self, study_id, **kwargs):
            return pgn

    repertoire = sync.import_study(tmp_path, Exporter(), "study00", color="black")

    assert [c.name for c in repertoire.meta.chapters] == ["First", "Second"]
    assert [c.lichess_chapter_id for c in repertoire.meta.chapters] == \
        ["chapter1", "chapter2"]
    # Freshly imported chapters match Lichess, so nothing needs pushing back.
    assert not any(repertoire.is_dirty(c.id) for c in repertoire.meta.chapters)
