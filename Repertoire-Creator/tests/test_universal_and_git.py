"""Tests for the universal book, git auto-commit, and the board overlay.

The git tests build a throwaway repository with a local bare remote, so a
real commit and a real push are exercised without touching anything of yours
and without a network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repertoire_creator import editing, universal
from repertoire_creator.board import board_svg, margin_fraction
from repertoire_creator.gitsync import GitSettings, GitSync
from repertoire_creator.storage import Repertoire


# ------------------------------------------------------------------- board


def test_the_overlay_margin_matches_the_svg_the_server_draws():
    """The click overlay is inset by this, so a wrong value misplaces every square."""
    import re

    svg = board_svg("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    total = float(re.search(r'viewBox="[\d.]+ [\d.]+ ([\d.]+) ', svg).group(1))
    first = re.search(
        r'<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"[^>]*class="square', svg
    )
    offset, square = float(first.group(1)), float(first.group(2))

    assert margin_fraction(True) == pytest.approx(offset / total)
    assert margin_fraction(True) == pytest.approx((total - square * 8) / 2 / total)
    assert margin_fraction(True) > 0, "a zero margin would mean the bug is back"


def test_a_board_without_coordinates_has_no_margin():
    assert margin_fraction(False) == 0.0


# --------------------------------------------------------------- universal


@pytest.fixture()
def book_fixture(tmp_path):
    """Two recordings and one repertoire chapter, folded into a book."""
    store = universal.UniversalStore(tmp_path)

    first = store.add("Italian")
    game = store.game(first.id)
    editing.add_line(game, (), "1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5")
    store.save(first.id, game)

    second = store.add("Bishop first")
    other = store.game(second.id)
    editing.add_line(other, (), "1. e4 e5 2. Bc4 Nc6 3. Nf3")
    store.save(second.id, other)

    repertoire = Repertoire.create(tmp_path, "White d4", color="white")
    chapter_id = repertoire.add_chapter("Queens Gambit").id
    chapter = repertoire.game(chapter_id)
    editing.add_line(chapter, (), "1. d4 d5 2. c4 e6")
    repertoire.save_chapter(chapter_id, chapter)

    return tmp_path, store


def at(moves):
    """``(fen, previous_fen)`` after playing a list of SAN moves."""
    board = chess.Board()
    previous = None
    for san in moves:
        previous = board.fen()
        board.push_san(san)
    return board.fen(), previous


def test_the_book_answers_from_recordings_and_from_chapters(book_fixture):
    data_dir, _ = book_fixture
    book = universal.build_book(data_dir)

    fen, previous = at(["e4", "e5"])
    found = universal.lookup(book, fen, previous)
    assert found["status"] == "known"
    assert {m["san"] for m in found["moves"]} == {"Nf3", "Bc4"}

    # A chapter of a repertoire is part of the same book.
    fen, previous = at(["d4", "d5"])
    assert universal.lookup(book, fen, previous)["moves"][0]["san"] == "c4"


def test_the_book_is_keyed_by_position_so_move_orders_merge(book_fixture):
    """Both recordings reach the same position; the book must answer once."""
    data_dir, _ = book_fixture
    book = universal.build_book(data_dir)

    # 1.e4 e5 2.Nf3 Nc6 3.Bc4 and 1.e4 e5 2.Bc4 Nc6 3.Nf3 are the same position.
    one, _ = at(["e4", "e5", "Nf3", "Nc6", "Bc4"])
    two, _ = at(["e4", "e5", "Bc4", "Nc6", "Nf3"])
    assert chess.Board(one).epd() == chess.Board(two).epd()

    found = universal.lookup(book, two, None)
    assert found["status"] == "known"
    assert [m["san"] for m in found["moves"]] == ["Bc5"], (
        "the answer recorded by one move order should be found by the other"
    )


def test_a_position_your_lines_reach_with_no_continuation_is_a_gap(book_fixture):
    data_dir, _ = book_fixture
    book = universal.build_book(data_dir)

    fen, previous = at(["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"])
    assert universal.lookup(book, fen, previous)["status"] == "gap"


def test_a_position_no_line_of_yours_reaches_is_not_a_gap(book_fixture):
    data_dir, _ = book_fixture
    book = universal.build_book(data_dir)

    fen, previous = at(["h4", "h5"])
    found = universal.lookup(book, fen, previous)
    assert found["status"] == "outside"
    assert found["cameFromBook"] is False


def test_book_moves_name_where_they_came_from(book_fixture):
    data_dir, _ = book_fixture
    book = universal.build_book(data_dir)

    fen, _ = at(["e4", "e5"])
    kinds = {
        source["kind"]
        for move in universal.lookup(book, fen, None)["moves"]
        for source in move["sources"]
    }
    assert kinds == {"recording"}

    fen, _ = at(["d4"])
    sources = universal.lookup(book, fen, None)["moves"][0]["sources"]
    assert sources[0]["kind"] == "chapter"
    assert "Queens Gambit" in sources[0]["name"]


def test_chapters_can_be_left_out_of_the_book(book_fixture):
    data_dir, _ = book_fixture
    book = universal.build_book(data_dir, include_chapters=False)
    fen, previous = at(["d4"])
    assert universal.lookup(book, fen, previous)["moves"] == []


def test_export_merges_recordings_into_one_chapter_per_opening_move(book_fixture):
    _, store = book_fixture
    groups = universal.export_games(store)

    assert [name for name, _ in groups] == ["e4"]
    game = groups[0][1]
    # Both recordings are in there, as two branches of one tree.
    replies = {game.variations[0].variations[0].variations[i].san()
               for i in range(len(game.variations[0].variations[0].variations))}
    assert replies == {"Nf3", "Bc4"}


def test_export_refuses_to_exceed_the_chapter_limit(tmp_path):
    store = universal.UniversalStore(tmp_path)
    recording = store.add("Everything")
    game = store.game(recording.id)
    # Sixteen distinct first moves, against a limit of four.
    for file in "abcdefgh":
        for rank in ("3", "4"):
            editing.play_move(game, (), san=f"{file}{rank}")
    store.save(recording.id, game)

    with pytest.raises(ValueError, match="chapters"):
        universal.export_games(store, max_chapters=4)


def test_recordings_are_plain_pgn_on_disk(tmp_path):
    store = universal.UniversalStore(tmp_path)
    recording = store.add("A session")
    game = store.game(recording.id)
    editing.add_line(game, (), "1. e4 c5")
    store.save(recording.id, game)

    text = store.path_for(recording).read_text(encoding="utf-8")
    assert "1. e4 c5" in text
    assert '[ChapterName "A session"]' in text

    # And they survive a reload from disk.
    again = universal.UniversalStore(tmp_path)
    assert [r.name for r in again.recordings] == ["A session"]
    assert again.game(recording.id).variations[0].san() == "e4"


# --------------------------------------------------------------------- git


def run_git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True, check=False)


@pytest.fixture()
def repo(tmp_path):
    """A repository with a bare remote, and a data folder inside it."""
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    work.mkdir()
    data = work / "repertoires"
    data.mkdir()

    run_git(["init", "-q", "-b", "main"], work)
    run_git(["config", "user.email", "test@example.com"], work)
    run_git(["config", "user.name", "Test"], work)
    run_git(["config", "commit.gpgsign", "false"], work)
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    run_git(["remote", "add", "origin", str(bare)], work)
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    run_git(["add", "README.md"], work)
    run_git(["commit", "-qm", "seed"], work)
    run_git(["push", "-q", "-u", "origin", "main"], work)
    return work, bare, data


def make_change(data: Path) -> Repertoire:
    repertoire = Repertoire.create(data, "White Italian", color="white")
    chapter_id = repertoire.add_chapter("Main").id
    game = repertoire.game(chapter_id)
    editing.add_line(game, (), "1. e4 e5")
    repertoire.save_chapter(chapter_id, game)
    return repertoire


def test_a_commit_takes_only_the_repertoire_folder(repo):
    """Anything else in the working tree, staged or not, must be left alone."""
    work, _, data = repo
    make_change(data)

    (work / "unrelated.txt").write_text("mine, not yours\n", encoding="utf-8")
    run_git(["add", "unrelated.txt"], work)     # staged, and still must not go

    sync = GitSync(data, GitSettings(enabled=True, push=False))
    result = sync.flush()
    assert result["lastAction"] == "committed", result

    committed = run_git(["show", "--name-only", "--format=", "HEAD"], work).stdout.split()
    assert committed, "nothing was committed"
    assert all(name.startswith("repertoires/") for name in committed), committed
    assert "unrelated.txt" in run_git(["status", "--short"], work).stdout


def test_pushing_reaches_the_remote(repo):
    work, bare, data = repo
    make_change(data)

    sync = GitSync(data, GitSettings(enabled=True, push=True))
    result = sync.flush()
    assert result["lastAction"] == "pushed", result["lastError"]

    # Name the branch: a bare repo's HEAD may point somewhere never pushed.
    remote = run_git(["log", "--oneline", "-1", "main"], bare).stdout
    assert "repertoire:" in remote


def test_nothing_to_commit_is_not_an_error(repo):
    work, _, data = repo
    make_change(data)
    sync = GitSync(data, GitSettings(enabled=True, push=False))
    sync.flush()

    result = sync.flush()
    assert result["lastAction"] == "nothing"
    assert not result["lastError"]


def test_the_commit_message_names_what_changed(repo):
    work, _, data = repo
    make_change(data)

    sync = GitSync(data, GitSettings(enabled=True, push=False))
    sync.note("White Italian / Main")
    sync.flush()

    subject = run_git(["log", "-1", "--format=%s"], work).stdout.strip()
    assert subject == "repertoire: White Italian / Main"


def test_a_folder_outside_any_repository_reports_rather_than_crashing(tmp_path):
    lonely = tmp_path / "nowhere"
    lonely.mkdir()
    sync = GitSync(lonely, GitSettings(enabled=True))
    status = sync.status()
    # tmp_path is not inside a repository, so there is nothing to commit to.
    if not status["inRepo"]:
        result = sync.flush()
        assert result["lastAction"] == "failed"
        assert "not inside a git repository" in result["lastError"]


def test_disabled_sync_does_not_arm_the_timer(repo):
    _, _, data = repo
    sync = GitSync(data, GitSettings(enabled=False))
    sync.note("something")
    assert sync.status()["pending"] is False
