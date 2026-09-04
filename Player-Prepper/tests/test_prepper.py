"""Tests for the parts that would fail silently.

The bias here is the same as the sibling apps': cover the things that break
*quietly*.  A scouting report is a page of confident numbers, and there is no
way to eyeball whether 62% was computed from the right point of view or
whether a game was correctly excluded -- so those get exact assertions on
hand-built games where the right answer is countable by hand.

Two of these exist because the bug happened.  ``test_exporter_state_does_not_leak``
is here because python-chess's ``StringExporter`` accumulates, so one exporter
reused across chapters silently emits each chapter with every earlier chapter
glued in front of it.  ``test_all_gaps_aliases_the_report`` is here because
returning copies instead of the report's own dictionaries meant engine
suggestions were computed, stored nowhere, and silently missing from the PDF.

No network, no engine and no Stockfish: a suite that needs any of them is a
suite nobody runs.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import chess
import chess.pgn
import pytest

# This app is not installed as a package -- only the study exporter is -- so
# the tests have to find it themselves, exactly as the sibling apps' do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from player_prepper import export, scout
from player_prepper.book import Book, BookError, build_book
from player_prepper.fetch import _months_since, pgn_to_uci, san_line_to_uci
from player_prepper.pipeline import _cache_covers
from player_prepper.store import Store, StoreError, player_key
from player_prepper.tree import build_trees, outcome_for, score_of, top_moves, \
    walk_to, weak_spots


# ------------------------------------------------------------------ helpers


def game(white, black, result, san, date="2026-01-01", url="", speed="blitz",
         white_elo="1500", black_elo="1500"):
    """One game in the app's cached shape, written as SAN for readability."""
    return {
        "white": white, "black": black, "result": result,
        "moves": " ".join(san_line_to_uci(san)),
        "date": date, "url": url or f"https://x/{san.replace(' ', '_')}",
        "speed": speed, "rated": True,
        "whiteElo": white_elo, "blackElo": black_elo,
    }


def book_from_pgn(pgn: str, color: str = "white") -> Book:
    book = Book()
    parsed = chess.pgn.read_game(io.StringIO(pgn))
    book.absorb_game(parsed, "test", color=color)
    book.sources.append({"kind": "test", "label": "test"})
    return book


def epd_after(san: str) -> str:
    board = chess.Board()
    for token in san.split():
        board.push_san(token)
    return board.epd()


# ------------------------------------------------------------- move parsing


def test_san_line_stops_at_the_first_move_that_will_not_play():
    # A truncated or slightly wrong game stays usable up to where it stops
    # making sense, rather than being thrown away whole.
    assert san_line_to_uci("e4 c5 Nf3") == ["e2e4", "c7c5", "g1f3"]
    assert san_line_to_uci("e4 c5 Qxz9 d4") == ["e2e4", "c7c5"]
    assert san_line_to_uci("e4 c5 Nf3", limit=2) == ["e2e4", "c7c5"]
    assert san_line_to_uci("") == []


def test_pgn_to_uci_ignores_clock_comments():
    pgn = '[Event "x"]\n\n1. e4 {[%clk 0:03:00]} e5 2. Nf3 1-0'
    assert pgn_to_uci(pgn) == ["e2e4", "e7e5", "g1f3"]


def test_months_since_keeps_the_cutoff_month():
    import datetime

    months = [f"https://api.chess.com/pub/player/x/games/{y}/{m:02d}"
              for y, m in ((2025, 11), (2026, 1), (2026, 8))]
    cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    kept = _months_since(months, cutoff.timestamp() * 1000)
    assert [url[-7:] for url in kept] == ["2026/01", "2026/08"]
    assert _months_since(months, None) == months


# ------------------------------------------------------------ point of view


def test_outcome_is_from_the_scouted_players_point_of_view():
    assert outcome_for("1-0", "white") == "w"
    assert outcome_for("1-0", "black") == "l"
    assert outcome_for("0-1", "black") == "w"
    assert outcome_for("1/2-1/2", "white") == "d"


def test_score_counts_a_draw_as_half():
    assert score_of(3, 0, 1) == 0.75
    assert score_of(0, 2, 0) == 0.5
    assert score_of(0, 0, 0) == 0.5          # no games is not a losing record


# -------------------------------------------------------------------- trees


def test_tree_counts_only_their_own_choices():
    games = [
        game("Me", "Foe", "0-1", "e4 c5 Nf3 d6"),
        game("Me", "Foe", "1-0", "e4 c5 Nf3 Nc6"),
    ]
    black = build_trees(games, "Foe", max_ply=8)["black"]

    # The start position is White's move, so it carries no statistics for a
    # player who had Black -- but it is still a node the games passed through.
    root = black.node(chess.Board().epd())
    assert root.games == 2
    assert root.moves == {}

    after_e4 = black.node(epd_after("e4"))
    assert [stat.san for stat in after_e4.moves.values()] == ["c5"]
    assert after_e4.moves["c7c5"].games == 2


def test_tree_scores_are_theirs_not_yours():
    games = [
        game("Me", "Foe", "0-1", "e4 c5"),      # Foe (black) won
        game("Me", "Foe", "0-1", "e4 c5"),
        game("Me", "Foe", "1-0", "e4 c5"),      # Foe lost
    ]
    black = build_trees(games, "Foe", max_ply=4)["black"]
    stat = black.node(epd_after("e4")).moves["c7c5"]
    assert (stat.w, stat.d, stat.l) == (2, 0, 1)
    assert stat.score == pytest.approx(2 / 3)


def test_transposition_lands_on_one_node():
    games = [
        game("Foe", "Me", "1-0", "e4 e5 Nf3 Nc6 Bb5"),
        game("Foe", "Me", "1-0", "Nf3 Nc6 e4 e5 Bb5"),
    ]
    white = build_trees(games, "Foe", max_ply=10)["white"]
    node = white.node(epd_after("e4 e5 Nf3 Nc6 Bb5"))
    assert node.games == 2                      # both move orders, one node
    # The shorter route is the one shown, and both here are five plies, so the
    # line simply has to be a real one that reaches this position.
    assert len(node.line) == 5


def test_weak_spots_rank_by_points_dropped_not_by_percentage():
    games = (
        # 1...e5 three times, lost every one: 1.5 points dropped.
        [game("Me", "Foe", "1-0", "e4 e5 Nf3") for _ in range(3)]
        # 1...c5 four times, one loss: 0.5 dropped, but a worse percentage
        # would rank it above e5 if percentage were the metric.
        + [game("Me", "Foe", "0-1", "e4 c5 Nf3") for _ in range(3)]
        + [game("Me", "Foe", "1-0", "e4 c5 Nf3")]
    )
    black = build_trees(games, "Foe", max_ply=6)["black"]
    rows = weak_spots(black, min_games=3, limit=5)
    assert rows[0]["san"] == "e5"
    assert rows[0]["leak"] == pytest.approx(1.5)
    assert rows[0]["games"] == 3


def test_weak_spots_ignore_a_sample_that_is_too_small():
    games = [game("Me", "Foe", "1-0", "e4 e5 Nf3")]
    black = build_trees(games, "Foe", max_ply=6)["black"]
    assert weak_spots(black, min_games=2) == []


def test_top_moves_carry_their_share_of_the_position():
    games = ([game("Me", "Foe", "1-0", "e4 c5") for _ in range(3)]
             + [game("Me", "Foe", "1-0", "e4 e5")])
    black = build_trees(games, "Foe", max_ply=4)["black"]
    rows = top_moves(black, limit=5)
    first = next(row for row in rows if row["san"] == "c5")
    assert first["games"] == 3 and first["reached"] == 4
    assert first["share"] == pytest.approx(0.75)


def test_walk_to_reports_a_position_they_never_reached():
    games = [game("Me", "Foe", "1-0", "e4 c5")]
    black = build_trees(games, "Foe", max_ply=4)["black"]
    assert walk_to(black, ["e2e4"])["found"] is True
    assert walk_to(black, ["d2d4"])["found"] is False
    assert walk_to(black, ["zzzz"])["found"] is False


# --------------------------------------------------------------------- book


def test_a_white_repertoire_records_only_white_moves():
    book = book_from_pgn('[Event "x"]\n\n1. e4 c5 2. Nf3 *', color="white")
    assert book.has(chess.Board().epd())            # 1.e4 is mine
    assert book.has(epd_after("e4 c5"))             # 2.Nf3 is mine
    assert not book.has(epd_after("e4"))            # 1...c5 is theirs


def test_a_book_records_sidelines_not_just_the_mainline():
    book = book_from_pgn('[Event "x"]\n\n1. e4 ( 1. d4 ) 1... c5 2. Nf3 *',
                         color="white")
    moves = book.at(chess.Board().epd())
    assert sorted(move.san for move in moves.values()) == ["d4", "e4"]


def test_own_games_record_only_your_own_moves(tmp_path):
    store = Store(tmp_path)
    store.save_games(player_key("lichess", "me"), {
        "site": "lichess", "username": "Me", "fetched": "", "filters": {},
        "games": [game("Me", "Foe", "1-0", "e4 c5 Nf3"),
                  game("Foe", "Me", "0-1", "d4 Nf6 c4")],
    })
    book = build_book([{"kind": "games", "site": "lichess", "username": "me"}],
                      store=store)

    assert book.has(chess.Board().epd())        # 1.e4, mine as White
    assert book.has(epd_after("d4"))            # 1...Nf6, mine as Black
    assert not book.has(epd_after("e4"))        # 1...c5 was the opponent's
    assert not book.has(epd_after("d4 Nf6"))    # 2.c4 was the opponent's


def test_book_counts_how_often_you_played_a_move(tmp_path):
    store = Store(tmp_path)
    store.save_games(player_key("lichess", "me"), {
        "site": "lichess", "username": "Me", "fetched": "", "filters": {},
        "games": [game("Me", "Foe", "1-0", "e4 c5"),
                  game("Me", "Foe", "1-0", "e4 e5"),
                  game("Me", "Foe", "1-0", "d4 d5")],
    })
    book = build_book([{"kind": "games", "site": "lichess", "username": "me"}],
                      store=store)
    start = book.at(chess.Board().epd())
    assert start["e2e4"].count == 2 and start["d2d4"].count == 1


def test_unknown_book_source_is_refused():
    with pytest.raises(BookError):
        build_book([{"kind": "tea-leaves"}])


def test_repertoire_folder_is_read_without_importing_the_sibling_app(tmp_path):
    root = tmp_path / "white-italian"
    (root / "chapters").mkdir(parents=True)
    (root / "repertoire.json").write_text(json.dumps({
        "slug": "white-italian", "name": "White Italian", "color": "white",
        "chapters": [{"id": "a", "file": "0001-main.pgn", "name": "Main line"}],
    }), encoding="utf-8")
    (root / "chapters" / "0001-main.pgn").write_text(
        '[Event "x"]\n[ChapterName "Main line"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bc4 *',
        encoding="utf-8")

    book = build_book([{"kind": "repertoire", "slug": "white-italian"}],
                      repertoire_dir=tmp_path)
    assert book.has(epd_after("e4 e5 Nf3 Nc6"))          # 3.Bc4 is mine
    assert not book.has(epd_after("e4 e5 Nf3"))          # 2...Nc6 is theirs
    assert book.sources[0]["color"] == "white"


def test_a_missing_repertoire_says_where_it_looked(tmp_path):
    with pytest.raises(BookError) as caught:
        build_book([{"kind": "repertoire", "slug": "nope"}],
                   repertoire_dir=tmp_path)
    assert "nope" in str(caught.value)


# ----------------------------------------------------------------- coverage


COVER_BOOK = '[Event "prep"]\n\n1. e4 c5 2. Nf3 d6 3. d4 *'


def test_coverage_splits_games_three_ways():
    """The rule the whole product rests on. See scout.py's module docstring."""
    book = book_from_pgn(COVER_BOOK, color="white")
    games = [
        # Follows the book the whole way and stops inside it: covered.
        game("Me", "Foe", "0-1", "e4 c5 Nf3 d6"),
        # Leaves the book at *their* choice, where I have nothing: a gap.
        game("Me", "Foe", "1-0", "e4 c5 Nf3 Nc6"),
        game("Me", "Foe", "1-0", "e4 c5 Nf3 Nc6"),
        # Their real opponent opened 1.d4, which I never play: not my problem.
        game("Me", "Foe", "1-0", "d4 Nf6 c4"),
    ]
    result = scout.measure_coverage(games, "Foe", "black", book, max_ply=8)

    assert result["games"] == 4
    assert result["offBook"] == 1
    assert result["inScope"] == 3
    assert result["covered"] == 1
    assert result["gapGames"] == 2
    assert result["percent"] == pytest.approx(33.3, abs=0.1)
    assert result["youPlay"] == "white"


def test_a_gap_is_weighted_by_how_many_of_their_games_reach_it():
    book = book_from_pgn(COVER_BOOK, color="white")
    games = [game("Me", "Foe", "1-0", "e4 c5 Nf3 Nc6") for _ in range(5)]
    games += [game("Me", "Foe", "0-1", "e4 e5")]
    result = scout.measure_coverage(games, "Foe", "black", book, max_ply=8)

    gaps = {gap["lineText"]: gap for gap in result["gaps"]}
    assert gaps["1.e4 c5 2.Nf3 Nc6"]["games"] == 5
    assert gaps["1.e4 e5"]["games"] == 1
    # Biggest first, because that is the order you would work through them.
    assert result["gaps"][0]["games"] == 5


def test_a_gap_carries_their_record_from_that_position():
    book = book_from_pgn(COVER_BOOK, color="white")
    games = [game("Me", "Foe", "0-1", "e4 e5"),        # they won
             game("Me", "Foe", "0-1", "e4 e5"),
             game("Me", "Foe", "1-0", "e4 e5")]        # they lost
    result = scout.measure_coverage(games, "Foe", "black", book, max_ply=8)
    gap = result["gaps"][0]
    assert gap["games"] == 3
    # Reported scores are rounded to four places on the way out, on purpose.
    assert gap["theirScore"] == pytest.approx(2 / 3, abs=5e-5)
    assert (gap["tally"]["w"], gap["tally"]["d"], gap["tally"]["l"]) == (2, 0, 1)


def test_a_gap_names_its_opening():
    book = book_from_pgn('[Event "prep"]\n\n1. e4 *', color="white")
    games = [game("Me", "Foe", "0-1", "e4 c5")]
    result = scout.measure_coverage(games, "Foe", "black", book, max_ply=6)
    # Naming needs the openings dataset, which may not be downloaded here.
    # What must always hold is the shape, not the name.
    gap = result["gaps"][0]
    assert set(gap["opening"]) == {"eco", "name", "known"}


def test_games_beyond_the_scouting_depth_do_not_create_gaps():
    book = book_from_pgn('[Event "prep"]\n\n1. e4 c5 2. Nf3 *', color="white")
    games = [game("Me", "Foe", "0-1", "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6")]

    shallow = scout.measure_coverage(games, "Foe", "black", book, max_ply=4)
    assert shallow["covered"] == 1 and shallow["gapPositions"] == 0

    deep = scout.measure_coverage(games, "Foe", "black", book, max_ply=10)
    assert deep["covered"] == 0 and deep["gapPositions"] == 1


def test_no_book_means_no_coverage_claim():
    report = scout.build_report({
        "site": "lichess", "username": "Foe", "fetched": "", "filters": {},
        "games": [game("Me", "Foe", "1-0", "e4 c5")],
    }, book=None, max_ply=6)
    coverage = report["colors"]["black"]["coverage"]
    assert coverage["noBook"] is True
    assert coverage["gaps"] == []
    assert report["book"] is None


def test_coverage_ignores_games_in_the_other_colour():
    book = book_from_pgn(COVER_BOOK, color="white")
    games = [game("Foe", "Me", "1-0", "e4 c5 Nf3")]      # they had White
    result = scout.measure_coverage(games, "Foe", "black", book, max_ply=8)
    assert result["games"] == 0 and result["inScope"] == 0


# ------------------------------------------------------------------ summary


def test_summary_counts_from_their_side_in_both_colours():
    games = [
        game("Foe", "Me", "1-0", "e4 e5", white_elo="1800"),   # they won as White
        game("Me", "Foe", "1-0", "e4 e5", black_elo="1810"),   # they lost as Black
        game("Me", "Foe", "1/2-1/2", "e4 e5", black_elo="1820"),
    ]
    summary = scout.summarise(games, "Foe")
    assert summary["games"] == 3
    assert summary["tally"]["w"] == 1 and summary["tally"]["l"] == 1
    assert summary["colors"]["white"]["games"] == 1
    assert summary["colors"]["black"]["games"] == 2
    assert summary["rating"]["median"] == 1810
    assert summary["opponents"] == 1


def test_pretty_line_numbers_moves_the_way_people_read_them():
    assert scout.pretty_line(["e4"]) == "1.e4"
    assert scout.pretty_line(["e4", "c5"]) == "1.e4 c5"
    assert scout.pretty_line(["e4", "c5", "Nf3"]) == "1.e4 c5 2.Nf3"
    assert scout.pretty_line([]) == ""


# ------------------------------------------------------------------- export


def _report_with_gaps() -> dict:
    book = book_from_pgn(COVER_BOOK, color="white")
    games = [game("Me", "Foe", "1-0", "e4 c5 Nf3 Nc6"),
             game("Me", "Foe", "0-1", "e4 e5"),
             game("Foe", "Me", "1-0", "d4 Nf6 c4 e6")]
    return scout.build_report({
        "site": "lichess", "username": "Foe", "fetched": "", "filters": {},
        "games": games,
    }, book=book, max_ply=8, min_games=1)


def test_all_gaps_aliases_the_report():
    # Not a copy: filling in an engine suggestion through this list has to
    # reach the saved report and therefore the PDF.
    report = _report_with_gaps()
    gaps = scout.all_gaps(report)
    assert gaps
    gaps[0]["engine"] = {"source": "probe", "lines": []}
    inside = report["colors"][gaps[0]["theyPlay"]]["coverage"]["gaps"]
    assert any(gap.get("engine", {}).get("source") == "probe" for gap in inside)


def test_exporter_state_does_not_leak_between_chapters():
    # python-chess's StringExporter accumulates. One shared exporter makes
    # every chapter carry the text of the ones before it, and the only visible
    # symptom is a PDF with far too many chapters in it.
    report = _report_with_gaps()
    text = export.build_pgn(report)

    handle = io.StringIO(text)
    names = []
    while True:
        parsed = chess.pgn.read_game(handle)
        if parsed is None:
            break
        names.append(parsed.headers.get("ChapterName", ""))

    assert names == sorted(set(names), key=names.index), \
        f"a chapter was emitted more than once: {names}"
    assert len(names) == export.chapter_count(report)


def test_export_merges_lines_into_a_tree_rather_than_repeating_them():
    report = _report_with_gaps()
    handle = io.StringIO(export.build_pgn(report))
    chapters = {}
    while True:
        parsed = chess.pgn.read_game(handle)
        if parsed is None:
            break
        chapters[parsed.headers.get("ChapterName", "")] = parsed

    gaps = chapters["Your gaps when you have White"]
    # Both of those gaps start 1.e4, so the tree must share that first move
    # rather than writing the line out twice.
    assert len(gaps.variations) == 1
    assert gaps.variations[0].move == chess.Move.from_uci("e2e4")
    assert "Your gaps when you have Black" in chapters


def test_export_refuses_an_empty_report():
    with pytest.raises(ValueError):
        export.build_pgn({"username": "nobody", "colors": {}})


# -------------------------------------------------------------------- store


def test_player_key_is_stable_across_spelling():
    assert player_key("lichess", "@DrNykterstein") == "lichess-drnykterstein"
    assert player_key("chesscom", "Hikaru") == "chesscom-hikaru"
    with pytest.raises(StoreError):
        player_key("fide", "somebody")
    with pytest.raises(StoreError):
        player_key("lichess", "")


def test_store_round_trips_and_forgets(tmp_path):
    store = Store(tmp_path)
    key = player_key("lichess", "foe")
    store.save_games(key, {"site": "lichess", "username": "Foe", "games": [1]})
    store.save_scout(key, {"site": "lichess", "username": "Foe",
                           "scoutedAt": "2026-01-01", "summary": {"games": 1}})

    assert store.load_games(key)["username"] == "Foe"
    assert [row["key"] for row in store.list_scouts()] == [key]

    store.delete_scout(key)
    assert store.load_scout(key) is None
    assert store.load_games(key) is None
    assert store.list_scouts() == []


def test_a_corrupt_cache_reads_as_missing_rather_than_raising(tmp_path):
    store = Store(tmp_path)
    key = player_key("lichess", "foe")
    path = store.games_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert store.load_games(key) is None


# ----------------------------------------------------------------- caching


def test_cache_is_reused_only_when_it_is_wide_enough():
    cached = {"filters": {"limit": 100, "speeds": ["blitz"], "ratedOnly": True,
                          "sinceMs": None},
              "games": [{} for _ in range(100)]}

    # Same shape: reuse.
    assert _cache_covers(cached, 100, ["blitz"], True, None)
    # Wants more games than the cache holds, and the cache was not short.
    assert not _cache_covers(cached, 500, ["blitz"], True, None)
    # Wants a speed the cache filtered out.
    assert not _cache_covers(cached, 100, ["blitz", "rapid"], True, None)
    # Wants casual games the cache never fetched.
    assert not _cache_covers(cached, 100, ["blitz"], False, None)


def test_a_short_cache_is_not_refetched_forever():
    # The site only had 12 games; asking for 300 again must not re-fetch.
    cached = {"filters": {"limit": 300, "speeds": [], "ratedOnly": True,
                          "sinceMs": None},
              "games": [{} for _ in range(12)]}
    assert _cache_covers(cached, 300, [], True, None)


# ------------------------------------------------------------------ exploit


def _engine(cp=None, mate=None, san="e4"):
    return {"source": "test", "lines": [
        {"rank": 1, "cp": cp, "mate": mate, "text": "", "line": "",
         "first": {"uci": "e2e4", "san": san}}]}


def test_winning_chances_are_symmetric_and_bounded():
    from player_prepper.exploit import winning_chances

    assert winning_chances(0) == 0.5
    assert winning_chances(300) == pytest.approx(1 - winning_chances(-300))
    assert winning_chances(300) > winning_chances(100) > 0.5
    assert winning_chances(None) == 0.5           # unknown is not "equal-ish"
    assert winning_chances(None, 3) == 1.0
    assert winning_chances(None, -3) == 0.0
    assert 0.0 <= winning_chances(100000) <= 1.0  # clamped, not exploding


def test_candidates_are_the_position_after_their_move():
    from player_prepper.exploit import candidates

    section = {"topMoves": [{
        "fen": chess.Board().fen(), "uci": "e2e4", "san": "e4", "games": 10,
        "line": [], "lineUci": [], "score": 0.4, "reached": 10,
    }], "weakSpots": []}
    rows = candidates(section, min_games=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["fen"] == epd_after("e4") + " 0 1"      # after their move
    assert row["youPlay"] == "black"                   # so it is your turn
    assert row["line"] == ["e4"] and row["lineUci"] == ["e2e4"]


def test_candidates_merge_the_two_source_lists():
    from player_prepper.exploit import candidates

    move = {"fen": chess.Board().fen(), "uci": "e2e4", "san": "e4", "games": 9,
            "line": [], "lineUci": [], "score": 0.3}
    section = {"topMoves": [move], "weakSpots": [dict(move)]}
    rows = candidates(section, min_games=1)
    assert len(rows) == 1                              # one position, not two
    assert rows[0]["from"] == ["played often", "goes badly for them"]


def test_candidates_drop_a_sample_that_is_too_small_and_a_move_that_will_not_play():
    from player_prepper.exploit import candidates

    section = {"topMoves": [
        {"fen": chess.Board().fen(), "uci": "e2e4", "games": 1,
         "line": [], "lineUci": [], "score": 0.5},
        {"fen": chess.Board().fen(), "uci": "e2e5", "games": 50,   # illegal
         "line": [], "lineUci": [], "score": 0.5},
    ]}
    assert candidates(section, min_games=3) == []


def test_edge_is_read_from_your_side_not_whites():
    from player_prepper.exploit import edge_of, winning_chances

    white = {"youPlay": "white", "engine": _engine(cp=200)}
    black = {"youPlay": "black", "engine": _engine(cp=200)}
    assert edge_of(white) == pytest.approx(winning_chances(200))
    assert edge_of(black) == pytest.approx(1 - winning_chances(200))
    assert edge_of({"youPlay": "white", "engine": None}) is None


def test_ranking_multiplies_only_the_factors_you_enable():
    from player_prepper.exploit import rank, winning_chances

    common_but_fine = {"games": 100, "score": 0.5, "youPlay": "white",
                       "engine": _engine(cp=0)}
    rare_but_awful = {"games": 10, "score": 0.0, "youPlay": "white",
                      "engine": _engine(cp=600)}

    rows = rank([dict(common_but_fine), dict(rare_but_awful)])
    by_games = {row["games"]: row for row in rows}
    assert by_games[100]["opportunity"] == pytest.approx(1.0 * 0.5 * 0.5, abs=1e-3)
    assert by_games[10]["opportunity"] == pytest.approx(
        0.1 * 1.0 * winning_chances(600), abs=1e-3)

    # And so the common line comes first. That is the point of multiplying by
    # frequency: a refutation of something they played ten times out of a
    # hundred is worth less than a plan for the line they always play, even
    # when the rare one looks far more dramatic.
    assert rows[0]["games"] == 100

    # Turn frequency off and the order flips, which is what the toggle is for.
    rows = rank([dict(common_but_fine), dict(rare_but_awful)],
                use_frequency=False)
    assert rows[0]["games"] == 10
    assert rows[0]["used"] == ["edge", "record"]

    # Frequency alone: the busiest line scores exactly 1.
    rows = rank([dict(common_but_fine), dict(rare_but_awful)],
                use_record=False, use_edge=False)
    assert rows[0]["games"] == 100
    assert rows[0]["opportunity"] == pytest.approx(1.0)
    assert rows[0]["used"] == ["frequency"]


def test_ranking_keeps_frequency_order_when_every_factor_is_off():
    from player_prepper.exploit import rank

    rows = rank([{"games": 3, "score": 0.1}, {"games": 30, "score": 0.9}],
                use_frequency=False, use_record=False, use_edge=False)
    assert [row["games"] for row in rows] == [30, 3]
    assert all(row["opportunity"] == 0 for row in rows)


def test_a_row_with_no_engine_is_still_ranked_on_what_is_known():
    from player_prepper.exploit import rank

    rows = rank([{"games": 10, "score": 0.2, "youPlay": "white", "engine": None}])
    assert rows[0]["factors"]["edge"] is None
    assert rows[0]["used"] == ["frequency", "record"]
    assert rows[0]["opportunity"] == pytest.approx(0.8)


def test_summarise_counts_what_the_engine_reached():
    from player_prepper.exploit import summarise

    rows = [{"games": 5, "engine": _engine(cp=10)}, {"games": 3, "engine": None}]
    assert summarise(rows) == {"positions": 2, "analysed": 1, "games": 8,
                               "pending": 1}


# ----------------------------------------------------------- playable board


def test_line_positions_gives_one_fen_per_ply():
    from player_prepper.board import line_positions

    result = line_positions(["e2e4", "c7c5", "g1f3"])
    assert result["sans"] == ["e4", "c5", "Nf3"]
    assert len(result["fens"]) == 4 and result["complete"] is True


def test_line_positions_stops_at_a_move_that_will_not_play():
    from player_prepper.board import line_positions

    result = line_positions(["e2e4", "e7e5", "e1e8"])
    assert result["sans"] == ["e4", "e5"] and result["complete"] is False


def test_playing_a_move_fills_in_a_bare_promotion():
    from player_prepper.board import play_move

    assert play_move("8/4P3/8/8/8/8/8/K6k w - - 0 1", "e7e8")["san"] == "e8=Q"
    with pytest.raises(ValueError):
        play_move(chess.Board().fen(), "e2e5")


def test_the_click_overlay_is_inset_by_the_real_svg_margin():
    from player_prepper.board import margin_fraction

    # Measured from a rendered SVG, not from chess.svg.MARGIN, which does not
    # match. A wrong value here misplaces every square near an edge.
    fraction = margin_fraction(True)
    assert 0 < fraction < 0.1
    assert margin_fraction(False) == 0
