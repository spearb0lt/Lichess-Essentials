"""Tests for the parts that would fail silently.

The bias here is towards things that break *quietly*: a TCN decoder that gets
one move wrong produces a legal-looking game that is not the game you played,
and an accuracy formula that drifts produces a number nobody can check by
eye. Those get real fixtures and exact assertions.

The engine is deliberately not required. Every test here runs with no network
and no Stockfish, because a test suite that needs both is a test suite nobody
runs.
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
# the tests have to find it themselves, exactly as the sibling app's tests do.
# Without this, `pytest ChessAnalyzer/tests` from the repository root fails to
# import anything, which is precisely how it is run.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chess_analyzer import accuracy, classify, openings, review, tcn
from chess_analyzer.library import EvalCache, Library
from chess_analyzer.sources import (
    common,
    from_fen,
    from_pgn,
    looks_like_fen,
    looks_like_pgn,
)
from chess_analyzer.sources.chesscom import parse_reference as cc_reference
from chess_analyzer.sources.common import SourceError
from chess_analyzer.sources.lichess import parse_reference as li_reference

FIXTURES = Path(__file__).parent / "fixtures"


def load_games() -> list[dict]:
    path = FIXTURES / "chesscom_games.json"
    if not path.is_file():
        pytest.skip("fixture games not built")
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- TCN


def test_tcn_decodes_every_fixture_game_exactly():
    """The decoder against 40 real games chess.com shipped both ways.

    Each fixture carries chess.com's own TCN *and* its own PGN for the same
    game, so this compares our decoding against their notation move for move.
    A single wrong square anywhere shows up as a SAN mismatch.
    """
    games = load_games()
    assert len(games) >= 20

    promotions = en_passants = 0
    for entry in games:
        truth = chess.pgn.read_game(io.StringIO(entry["pgn"]))
        expected = [node.san() for node in truth.mainline()]

        board = chess.Board()
        decoded = []
        for uci in tcn.to_uci(entry["tcn"]):
            move = board.parse_uci(uci)
            if board.is_en_passant(move):
                en_passants += 1
            if move.promotion:
                promotions += 1
            decoded.append(board.san(move))
            board.push(move)

        assert decoded == expected, entry["url"]
        assert board.board_fen() == entry["fen"].split()[0], entry["url"]

    # If the fixture ever stops covering these, the interesting half of the
    # decoder is untested and the assertion above proves much less.
    assert promotions >= 5
    assert en_passants >= 0


def test_tcn_rejects_rubbish():
    with pytest.raises(tcn.TCNError):
        tcn.decode_moves("<<")
    with pytest.raises(tcn.TCNError):
        # Syntactically fine, not legal from the start position.
        tcn.to_uci("zzzz")


def test_tcn_tolerates_a_torn_read():
    """A live game read mid-write can end half a move short."""
    full = "mCYIgv5QfH2Uks"
    assert len(tcn.decode_moves(full + "m")) == len(tcn.decode_moves(full))


# -------------------------------------------------------------- accuracy


def test_win_percent_is_symmetric_and_bounded():
    assert accuracy.win_percent(0) == pytest.approx(50.0)
    assert accuracy.win_percent(300) + accuracy.win_percent(-300) \
        == pytest.approx(100.0)
    assert accuracy.win_percent(None, 5) == 100.0
    assert accuracy.win_percent(None, -5) == 0.0
    # Enormous scores saturate rather than running off the end.
    assert accuracy.win_percent(100_000) <= 100.0
    assert accuracy.win_percent(-100_000) >= 0.0


def test_move_accuracy_rewards_holding_and_punishes_dropping():
    # Lichess's fitted constants land a hair under 100 for a perfect move
    # (103.1668 - 3.1669), which is theirs, not a rounding slip of ours.
    assert accuracy.move_accuracy(60, 60) == pytest.approx(100.0, abs=1e-3)
    assert accuracy.move_accuracy(60, 70) == pytest.approx(100.0, abs=1e-3)
    assert accuracy.move_accuracy(60, 50) < 100.0
    assert accuracy.move_accuracy(60, 30) < accuracy.move_accuracy(60, 50)
    assert accuracy.move_accuracy(60, 0) >= 0.0


def test_game_accuracy_of_perfect_play_is_100():
    flat = [50.0] * 21
    assert accuracy.game_accuracy(flat, chess.WHITE) == 100.0
    assert accuracy.game_accuracy(flat, chess.BLACK) == 100.0


def test_game_accuracy_blames_the_side_that_moved():
    """White drops 40 points on their first move; Black must not be scored."""
    percents = [50.0, 10.0, 10.0, 10.0, 10.0]
    white = accuracy.game_accuracy(percents, chess.WHITE)
    black = accuracy.game_accuracy(percents, chess.BLACK)
    assert white < 60.0
    assert black == 100.0


def test_one_catastrophe_drags_the_whole_game_down():
    """The harmonic half of the mean is what makes this true, and it is the
    reason a 40-move game with one lost mate does not score in the nineties."""
    steady = [50.0 + (index % 2) for index in range(41)]
    disaster = list(steady)
    # Position 21 is reached by White's 11th move, so collapsing it is White's
    # doing. Collapsing an even-numbered position would charge Black instead,
    # which is the whole point of the sign convention.
    disaster[21] = 0.0
    assert accuracy.game_accuracy(disaster, chess.WHITE) \
        < accuracy.game_accuracy(steady, chess.WHITE) - 20


# ----------------------------------------------------------------- phases


def test_phase_boundaries_follow_the_position_not_the_move_number():
    game = chess.pgn.read_game(io.StringIO(
        "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Bxc6 dxc6 5. Nxe5 Qd4 "
        "6. Nf3 Qxe4+ 7. Qe2 Qxe2+ 8. Kxe2"))
    boards, _ = common.positions(game)
    middle, end = accuracy.phase_boundaries(boards)
    assert 0 < middle <= len(boards)
    assert end >= middle
    assert accuracy.phase_of(0, middle, end) == "opening"


def test_a_bare_kings_endgame_is_an_endgame():
    boards = [chess.Board("8/8/4k3/8/8/4K3/8/8 w - - 0 1")]
    middle, end = accuracy.phase_boundaries(boards)
    assert (middle, end) == (0, 0)
    assert accuracy.phase_of(0, middle, end) == "endgame"


# --------------------------------------------------------- classification


def _lines(cp=None, mate=None, pv=()):
    return {"cp": cp, "mate": mate, "pv": list(pv), "depth": 20}


def test_playing_the_engines_move_is_best():
    board = chess.Board()
    judged = classify.classify(
        board_before=board,
        move=chess.Move.from_uci("e2e4"),
        best_before=_lines(cp=30, pv=["e2e4", "e7e5"]),
        second_before=_lines(cp=25, pv=["d2d4"]),
        eval_after=_lines(cp=30),
    )
    assert judged["isBest"] is True
    assert judged["label"] == "best"
    assert judged["winLoss"] == 0.0
    assert judged["accuracy"] == 100.0


def test_a_recapture_is_never_great():
    """Taking back beats the alternatives because they hang a piece, which is
    not to the player's credit. Without this rule half of every game is
    'great'."""
    board = chess.Board(
        "rnbqkb1r/ppp1pppp/5n2/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 3")
    move = chess.Move.from_uci("f6d5")           # recapturing on d5
    previous = chess.Move.from_uci("e4d5")       # ...the pawn that took there

    # Black to move, so a *positive* score is the bad alternative for them.
    without = classify.classify(
        board_before=board, move=move,
        best_before=_lines(cp=20, pv=["f6d5"]),
        second_before=_lines(cp=400, pv=["e7e6"]),
        eval_after=_lines(cp=20))
    assert without["label"] == "great"

    with_context = classify.classify(
        board_before=board, move=move,
        best_before=_lines(cp=20, pv=["f6d5"]),
        second_before=_lines(cp=400, pv=["e7e6"]),
        eval_after=_lines(cp=20), previous_move=previous)
    assert with_context["label"] == "best"


def test_nothing_is_brilliant_once_the_game_is_decided():
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    judged = classify.classify(
        board_before=board, move=chess.Move.from_uci("a1a8"),
        best_before=_lines(mate=4, pv=["a1a8"]),
        second_before=_lines(cp=-500, pv=["g1h1"]),
        eval_after=_lines(mate=3))
    assert judged["label"] == "best"


def test_missing_a_forced_mate_is_a_miss():
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    judged = classify.classify(
        board_before=board, move=chess.Move.from_uci("g1h1"),
        best_before=_lines(mate=2, pv=["a1a8"]),
        second_before=_lines(cp=300, pv=["g1h1"]),
        eval_after=_lines(cp=100))
    assert judged["missedMate"] is True
    assert judged["label"] == "miss"


def test_a_blunder_is_judged_from_the_movers_side():
    """A 40-point drop in White's winning chances is a *gain* for Black, and
    the sign has to survive the round trip."""
    board = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1")
    judged = classify.classify(
        board_before=board, move=chess.Move.from_uci("g7g5"),
        best_before=_lines(cp=-20, pv=["e7e5"]),
        second_before=_lines(cp=-10, pv=["c7c5"]),
        eval_after=_lines(cp=600))
    assert judged["judgment"] == "blunder"
    assert judged["winLoss"] > accuracy.BLUNDER


def test_centipawn_loss_is_capped():
    board = chess.Board()
    judged = classify.classify(
        board_before=board, move=chess.Move.from_uci("a2a4"),
        best_before=_lines(cp=50_000, pv=["e2e4"]),
        second_before=None,
        eval_after=_lines(cp=-50_000))
    assert judged["cpLoss"] == classify.CP_LOSS_CAP


def test_a_position_with_one_legal_move_is_forced():
    board = chess.Board("7k/8/8/8/8/8/6q1/7K w - - 0 1")
    only = list(board.legal_moves)
    assert len(only) == 1
    judged = classify.classify(
        board_before=board, move=only[0],
        best_before=_lines(cp=-900, pv=[only[0].uci()]),
        second_before=None, eval_after=_lines(cp=-900))
    assert judged["label"] == "forced"
    assert judged["forced"] is True


def test_sacrifice_depth_tells_a_gift_from_a_trade():
    # A plain exchange: material dips and comes straight back.
    board = chess.Board(
        "rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3")
    after = board.copy()
    after.push_uci("f3e5")
    trade = classify.sacrifice_depth(after, ["b8c6", "e5c6", "d7c6"], chess.WHITE)
    assert trade >= -400          # the knight comes back

    # Nothing given up at all.
    quiet = chess.Board()
    assert classify.sacrifice_depth(quiet, ["e2e4", "e7e5"], chess.WHITE) == 0


# ---------------------------------------------------------------- sources


def test_the_import_box_tells_the_four_formats_apart():
    assert looks_like_pgn('[Event "x"]\n\n1. e4 e5')
    assert looks_like_pgn("1. e4 e5 2. Nf3")
    assert not looks_like_pgn("https://lichess.org/abcd1234")
    assert looks_like_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert not looks_like_fen("1. e4 e5")


def test_a_pgn_whose_site_tag_says_lichess_is_still_a_pgn():
    """Structure beats hostname, or a pasted game gets fetched instead of read."""
    text = ('[Event "T"]\n[Site "https://lichess.org/abcd1234"]\n'
            '[White "a"]\n[Black "b"]\n[Result "1-0"]\n\n1. e4 e5 1-0')
    assert looks_like_pgn(text)
    record = from_pgn(text)
    assert record.source == "pgn"
    assert record.ply_count == 2


def test_game_references_are_pulled_out_of_urls():
    assert li_reference("https://lichess.org/Wi8IPxc3") == "Wi8IPxc3"
    assert li_reference("https://lichess.org/Wi8IPxc3/black#42") == "Wi8IPxc3"
    assert li_reference("Wi8IPxc3") == "Wi8IPxc3"
    assert cc_reference("https://www.chess.com/game/live/1234567") == ("live", "1234567")
    assert cc_reference(
        "https://www.chess.com/analysis/game/live/1234567?tab=review") \
        == ("live", "1234567")
    assert cc_reference("https://www.chess.com/game/daily/99") == ("daily", "99")


def test_a_variant_game_is_refused_with_a_reason():
    text = ('[Event "T"]\n[Variant "Crazyhouse"]\n[White "a"]\n[Black "b"]\n'
            '[Result "*"]\n\n1. e4 e5 *')
    with pytest.raises(SourceError, match="Crazyhouse"):
        from_pgn(text)


def test_an_illegal_move_is_refused_rather_than_truncated():
    """python-chess collects errors instead of raising, which would otherwise
    leave a game silently missing its second half."""
    with pytest.raises(SourceError):
        from_pgn('[Event "T"]\n[Result "*"]\n\n1. e4 e5 2. Ke2 Ke7 3. Kxe7 *')


def test_a_bare_fen_becomes_a_zero_move_game():
    record = from_fen("r3q1k1/5p1p/pQ1p2p1/8/R7/1P3N1P/2B2PP1/6K1 b - - 0 26")
    assert record.ply_count == 0
    assert "SetUp" in record.pgn


def test_building_a_pgn_round_trips():
    pgn = common.build_pgn(
        {"Event": "T", "White": "a", "Black": "b", "Result": "1-0"},
        ["e2e4", "e7e5", "g1f3"])
    game = common.parse_game(pgn)
    assert [node.san() for node in game.mainline()] == ["e4", "e5", "Nf3"]


# --------------------------------------------------------------- openings


def test_openings_are_matched_by_position_so_transpositions_work():
    if not openings.available():
        pytest.skip("openings dataset not downloaded")
    direct = chess.Board()
    for san in ("e4", "c5", "c3"):
        direct.push_san(san)
    transposed = chess.Board()
    for san in ("c3", "c5", "e4"):
        transposed.push_san(san)
    assert direct.epd() == transposed.epd()
    assert openings.lookup(direct) == openings.lookup(transposed)
    assert "Alapin" in openings.lookup(direct)["name"]


def test_a_middlegame_position_is_not_book():
    if not openings.available():
        pytest.skip("openings dataset not downloaded")
    board = chess.Board("r3q1k1/5p1p/pQ1p2p1/8/R7/1P3N1P/2B2PP1/6K1 b - - 0 26")
    assert not openings.in_book(board)


# ---------------------------------------------------------------- library


def test_the_library_round_trips_a_game(tmp_path):
    lib = Library(tmp_path)
    record = from_pgn('[Event "T"]\n[White "a"]\n[Black "b"]\n[Result "1-0"]\n\n'
                      '1. e4 e5 1-0')
    lib.save(record)

    loaded = lib.load(record.id)
    assert loaded["record"]["white"] == "a"
    assert loaded["review"] is None

    lib.save_review(record.id, {"summary": {"white": {"accuracy": 91.0}}})
    assert lib.load(record.id)["review"]["summary"]["white"]["accuracy"] == 91.0

    rows = lib.listing()
    assert len(rows) == 1 and rows[0]["reviewed"] is True
    assert lib.delete(record.id) is True
    assert lib.load(record.id) is None


def test_a_game_id_cannot_escape_the_library(tmp_path):
    lib = Library(tmp_path)
    path = lib.path_for("../../etc/passwd")
    assert path.parent == lib.dir


def test_the_cache_separates_positions_by_engine_settings(tmp_path):
    cache = EvalCache(tmp_path / "positions.json")
    fen = chess.STARTING_FEN
    cache.put(fen, "sf|ms100", [{"cp": 30}])
    assert cache.get(fen, "sf|ms100")[0]["cp"] == 30
    # A different think time is a different answer and must not be reused.
    assert cache.get(fen, "sf|ms2000") is None

    cache.save()
    assert EvalCache(tmp_path / "positions.json").get(fen, "sf|ms100") is not None


# ----------------------------------------------------------------- review


def test_presets_all_describe_themselves():
    for name, preset in review.PRESETS.items():
        assert preset["label"] and preset["detail"]
        assert preset.get("movetime") or preset.get("depth"), name
        settings = review.Settings.from_preset(name)
        assert settings.preset == name
        assert settings.options().limit() is not None


def test_an_unknown_preset_falls_back_rather_than_exploding():
    assert review.Settings.from_preset("nonsense").preset == "standard"


def test_estimated_rating_moves_the_right_way():
    assert review.estimated_rating(None) is None
    assert review.estimated_rating(10) > review.estimated_rating(100)
    assert 400 <= review.estimated_rating(10_000) <= 3000


def test_eval_text_reads_from_whites_side():
    assert review.eval_text(124, None) == "+1.24"
    assert review.eval_text(-30, None) == "-0.30"
    assert review.eval_text(None, 5) == "+M5"
    assert review.eval_text(None, -3) == "-M3"


def test_describe_pv_numbers_a_black_first_move_correctly():
    board = chess.Board()
    board.push_san("e4")
    described = review.describe_pv(board, ["e7e5", "g1f3"])
    assert described["line"].startswith("1...e5")
    assert described["first"]["san"] == "e5"


def test_describe_pv_stops_at_an_illegal_continuation():
    described = review.describe_pv(chess.Board(), ["e2e4", "e2e4"])
    assert described["line"] == "1.e4"


def test_a_game_with_no_moves_is_refused_clearly():
    record = from_fen(chess.STARTING_FEN)
    with pytest.raises(SourceError, match="no moves"):
        review.review(record)


def test_a_sentence_is_not_mistaken_for_a_game_id():
    """`parse_reference` searches, so "total nonsense" ends in eight
    alphanumerics and used to be fetched from Lichess as game `nonsense`."""
    from chess_analyzer.sources import resolve

    with pytest.raises(SourceError, match="Could not tell"):
        resolve("total nonsense")
    with pytest.raises(SourceError, match="Could not tell"):
        resolve("hello world not a game")
    with pytest.raises(SourceError, match="Paste a game"):
        resolve("   ")


def test_the_job_listing_leaves_out_the_results():
    """One finished review is ~150 KB, and the listing is polled to draw
    progress bars."""
    from chess_analyzer.jobs import JobRunner

    runner = JobRunner()
    job = runner.start("test", lambda _: {"huge": "x" * 1000})
    for _ in range(100):
        if job.state in ("done", "failed"):
            break
        import time as _time
        _time.sleep(0.02)

    assert job.json()["result"] == {"huge": "x" * 1000}
    rows = runner.listing()
    assert len(rows) == 1
    assert "result" not in rows[0]
    assert rows[0]["state"] == "done"


def test_the_position_cache_is_not_listed_as_a_game(tmp_path):
    """The cache lives in the games directory, and `listing` globs *.json."""
    lib = Library(tmp_path)
    cache = lib.load_cache()
    cache.put(chess.STARTING_FEN, "sf|ms100", [{"cp": 30}])
    cache.save()
    assert lib.cache_path.is_file()

    record = from_pgn('[Event "T"]\n[White "a"]\n[Black "b"]\n[Result "*"]\n\n1. e4 *')
    lib.save(record)

    rows = lib.listing()
    assert len(rows) == 1
    assert rows[0]["id"] == record.id

    # And no game id can ever be written to the cache's own filename.
    assert not lib.path_for("positions").name.startswith(".")
    assert lib.path_for(".positions") != lib.cache_path


# ------------------------------------------------------- arranging a board


def test_a_click_map_becomes_a_fen_board_field():
    from chess_analyzer import position

    assert position.placement_from_map({}) == "8/8/8/8/8/8/8/8"
    assert position.placement_from_map({"e1": "K", "e8": "k"}) \
        == "4k3/8/8/8/8/8/8/4K3"
    assert position.placement_from_map({"a1": "R", "h1": "R", "e1": "K",
                                        "e8": "k"}) == "4k3/8/8/8/8/8/8/R3K2R"


def test_a_bad_square_or_piece_is_refused():
    from chess_analyzer import position

    with pytest.raises(position.PositionError):
        position.placement_from_map({"j9": "K"})
    with pytest.raises(position.PositionError):
        position.placement_from_map({"e1": "X"})


def test_every_illegal_arrangement_gets_a_sentence():
    """The point of the editor is that it explains itself. A flag with no
    message would show as a bare status number."""
    from chess_analyzer import position

    cases = {
        "empty": ({}, "empty"),
        "no black king": ({"e1": "K"}, "black king"),
        "two white kings": ({"e1": "K", "e2": "K", "e8": "k"}, "more than one king"),
        "pawn on the back rank": ({"e1": "K", "e8": "k", "a1": "P"}, "first or last rank"),
        "other side in check": ({"e1": "K", "e8": "k", "a8": "R"}, "not to move is in check"),
    }
    for label, (pieces, fragment) in cases.items():
        described = position.describe(position.assemble(pieces, turn="w"))
        assert not described["valid"], label
        assert any(fragment in problem for problem in described["problems"]), \
            f"{label}: {described['problems']}"


def test_a_legal_arrangement_is_accepted_with_its_details():
    from chess_analyzer import position

    described = position.describe(
        position.assemble({"e1": "K", "e8": "k", "d4": "P", "h5": "q"}, turn="w"))
    assert described["valid"]
    assert described["problems"] == []
    assert described["turn"] == "white"
    assert described["material"]["diff"] == 1 - 9


def test_castling_is_only_offered_where_it_could_exist():
    from chess_analyzer import position

    home = position.placement_from_map(
        {"e1": "K", "h1": "R", "a1": "R", "e8": "k", "h8": "r", "a8": "r"})
    assert position.castling_options(home) == {"K": True, "Q": True,
                                               "k": True, "q": True}

    moved = position.placement_from_map({"g1": "K", "h1": "R", "e8": "k", "a8": "r"})
    options = position.castling_options(moved)
    assert options["K"] is False and options["Q"] is False
    assert options["q"] is True


def test_stale_castling_ticks_are_dropped_not_rejected():
    """They are almost always left over from the previous arrangement, and
    failing the whole position over one would be obtuse."""
    from chess_analyzer import position

    fen = position.assemble({"g1": "K", "h1": "R", "e8": "k", "a8": "r"},
                            turn="w", castling="KQkq")
    assert fen.split()[2] == "q"
    assert position.describe(fen)["valid"]


def test_en_passant_is_only_offered_when_the_capture_is_real():
    """`has_legal_en_passant` alone says yes when there is no pawn to take --
    it never looks for the victim. Offering such a square would hand the user
    a position the validator then rejects."""
    from chess_analyzer import position

    ready = position.placement_from_map(
        {"e1": "K", "e8": "k", "e5": "P", "d5": "p"})
    assert position.en_passant_options(ready, "w") == ["d6"]

    both = position.placement_from_map(
        {"e1": "K", "e8": "k", "e5": "P", "d5": "p", "f5": "p"})
    assert position.en_passant_options(both, "w") == ["d6", "f6"]

    lonely = position.placement_from_map({"e1": "K", "e8": "k", "a5": "p"})
    assert position.en_passant_options(lonely, "w") == []

    # And every square that is offered survives the validator.
    for square in position.en_passant_options(both, "w"):
        fen = position.assemble(
            {"e1": "K", "e8": "k", "e5": "P", "d5": "p", "f5": "p"},
            turn="w", en_passant=square)
        assert position.describe(fen)["valid"], square
        assert fen.split()[3] == square


def test_an_impossible_en_passant_square_is_dropped():
    from chess_analyzer import position

    fen = position.assemble({"e1": "K", "e8": "k", "e5": "P", "d5": "p"},
                            turn="w", en_passant="h6")
    assert fen.split()[3] == "-"
    assert position.describe(fen)["valid"]


def test_an_arranged_checkmate_says_there_is_nothing_to_evaluate():
    from chess_analyzer import position

    described = position.describe(
        position.assemble({"h8": "k", "g7": "Q", "g6": "K"}, turn="b"))
    assert described["valid"]
    assert described["gameOver"]
    assert "Checkmate" in described["outcome"]


def test_a_session_started_from_an_arranged_position_replays_from_it():
    from chess_analyzer import position
    from chess_analyzer.live import LiveSession

    fen = position.assemble({"e1": "K", "e8": "k", "d4": "P", "a7": "r"}, turn="w")
    session = LiveSession("manual", start_fen=fen)
    try:
        assert session.json()["arranged"] is True
        session.push_move("d4d5")
        session.push_move("a7a1")

        rows = session.positions()
        assert len(rows) == 3
        assert rows[0]["fen"] == fen
        assert [row["san"] for row in rows[1:]] == ["d5", "Ra1+"]

        assert len(session.undo()["positions"]) == 2
    finally:
        session.stop()


def test_an_arranged_session_refuses_a_pgn_from_a_different_start():
    """Replaying it would either fail on move one or, worse, succeed and build
    a game nobody played."""
    from chess_analyzer import position
    from chess_analyzer.live import LiveError, LiveSession

    fen = position.assemble({"e1": "K", "e8": "k", "d4": "P"}, turn="w")
    session = LiveSession("pgn", start_fen=fen)
    try:
        with pytest.raises(LiveError, match="different position"):
            session.feed_pgn('[Event "T"]\n[Result "*"]\n\n1. e4 e5 *')
    finally:
        session.stop()
