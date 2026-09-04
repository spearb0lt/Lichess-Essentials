"""Tests for the parts that would fail silently.

Same bias as the three sibling suites: cover what breaks *quietly*. A weakness
report is a page of confident numbers about somebody's chess, and there is no
way to eyeball whether a bucket was computed from the right moves or whether a
claim rests on eleven of them. So the arithmetic gets exact assertions on
hand-built moves where the answer is countable by hand.

Three of these exist because of a specific failure this app is designed to
avoid:

* ``test_a_review_at_other_settings_is_not_reused`` -- reusing a depth-10
  review inside a depth-18 batch produces a report that looks completely
  normal and whose every figure is wrong.
* ``test_a_weakness_and_a_near_duplicate_strength_cannot_both_appear`` -- two
  dimensions covering nearly the same moves once landed one in each list, so
  the report called the same thing a weakness and a strength.
* ``test_acpl_excludes_book_moves`` -- ChessAnalyzer excludes them, and a
  report that did not would quietly flatter anyone with preparation.

No network, no engine and no Stockfish. The one test that touches
ChessAnalyzer skips when it cannot be found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import chess
import pytest

# This app is not installed as a package, so the tests find it themselves --
# exactly as all three sibling suites do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weakness_report import aggregate, batch, buckets, exportcsv, features, \
    findings, report as report_module
from weakness_report.bridge import analyzer
from weakness_report.sources import _months_since, from_pgn_text
from weakness_report.store import Store, safe_name


# ------------------------------------------------------------------ helpers


def move(*, cp=50, phase="middlegame", fen=None, accuracy=80.0, label="good",
         judgment=None, in_book=False, colour="white", game_id="g1",
         clock=None, san="e4", move_number=10, is_best=False, forced=False,
         speed="blitz", you_elo="1500", them_elo="1500", opening=None):
    """One of your moves, in the shape the aggregation expects."""
    fen = fen or chess.STARTING_FEN
    return {
        "ply": 1, "moveNumber": move_number, "san": san, "phase": phase,
        "clock": clock, "cpLoss": cp, "winLoss": None, "accuracy": accuracy,
        "label": label, "judgment": judgment, "inBook": in_book,
        "forced": forced, "isBest": is_best, "bestSan": "Nf3",
        "fenBefore": fen,
        "features": features.describe(fen, colour == "white"),
        "game": {"id": game_id, "url": "", "you": colour, "them": "Foe",
                 "youElo": you_elo, "themElo": them_elo, "speed": speed,
                 "result": "1-0", "date": "2026-01-01",
                 "opening": opening or {"name": "Sicilian Defense: Alapin",
                                        "eco": "B22"}},
    }


QUEENLESS = "6k1/5ppp/8/8/8/8/5PPP/R2Q2K1 w - - 0 1"      # one queen, actually
NO_QUEENS = "r5k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
BOTH_QUEENS = "r2q2k1/5ppp/8/8/8/8/5PPP/R2Q2K1 w - - 0 1"


# ---------------------------------------------------------------- features


def test_queens_are_counted_by_who_still_has_one():
    assert features.queens(chess.Board(BOTH_QUEENS)) == "queens on"
    assert features.queens(chess.Board(QUEENLESS)) == "one queen"
    assert features.queens(chess.Board(NO_QUEENS)) == "queens off"


def test_material_is_from_your_side_not_whites():
    # White is a rook up.
    up = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
    assert features.describe(up, chess.WHITE)["material"] == "material ahead"
    assert features.describe(up, chess.BLACK)["material"] == "material behind"


def test_a_jammed_centre_is_closed_and_an_empty_one_is_open():
    start = chess.Board()
    assert features.locked_pawns(start) == 0
    assert features.centre(start) == "open centre"

    locked = chess.Board(
        "rnbqkbnr/pp3ppp/8/2ppp3/2PPP3/8/PP3PPP/RNBQKBNR w KQkq - 0 1")
    assert features.locked_pawns(locked) == 3
    assert features.centre(locked) == "closed centre"


def test_opposite_castling_needs_two_wings():
    opposite = "2kr3r/ppp2ppp/8/8/8/8/PPP2PPP/R4RK1 w - - 0 1"
    same = "r4rk1/ppp2ppp/8/8/8/8/PPP2PPP/R4RK1 w - - 0 1"
    assert features.opposite_castling(chess.Board(opposite), chess.WHITE)
    assert not features.opposite_castling(chess.Board(same), chess.WHITE)
    assert not features.opposite_castling(chess.Board(), chess.WHITE)


def test_endings_are_named_only_when_they_are_endings():
    assert features.ending(chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")) \
        == "rook ending"
    assert features.ending(chess.Board("6k1/5ppp/8/8/8/8/5PPP/6K1 w - - 0 1")) \
        == "pawn ending"
    assert features.ending(chess.Board("6k1/5pp1/8/3b4/8/8/5PP1/4B1K1 w - - 0 1")) \
        == "opposite bishops"
    # A full board is not an ending, and saying so is the point.
    assert features.ending(chess.Board()) == ""


# ----------------------------------------------------------------- buckets


def test_a_move_can_be_in_several_situations_at_once():
    # Queens off, and White a whole rook up: two situations, one move.
    a_rook_up = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
    row = move(phase="middlegame", fen=a_rook_up, colour="white")
    names = buckets.BY_KEY["situation"].buckets_of(row)
    assert "queenless middlegames" in names
    assert "when you are ahead" in names

    # The same position from the other side is the other situation.
    theirs = move(phase="middlegame", fen=a_rook_up, colour="black")
    assert "when you are behind" in buckets.BY_KEY["situation"].buckets_of(theirs)


def test_time_pressure_declines_when_there_is_no_clock():
    assert buckets.clock_band(move(clock=None)) == ()
    assert buckets.clock_band(move(clock=5)) == ("under 10 seconds",)
    assert buckets.clock_band(move(clock=45)) == ("30 to 60 seconds",)
    assert buckets.clock_band(move(clock=9999)) == ("over 3 minutes",)


def test_opponent_band_needs_both_ratings():
    assert buckets.opponent_band(move(you_elo="1500", them_elo="1700")) \
        == ("against stronger players",)
    assert buckets.opponent_band(move(you_elo="1500", them_elo="1300")) \
        == ("against weaker players",)
    assert buckets.opponent_band(move(you_elo="1500", them_elo="1520")) \
        == ("against similar players",)
    assert buckets.opponent_band(move(you_elo="", them_elo="1500")) == ()


def test_openings_are_grouped_by_family_not_by_variation():
    row = move(opening={"name": "Sicilian Defense: Najdorf, English Attack",
                        "eco": "B90"})
    assert buckets.opening_family(row) == ("Sicilian Defense",)
    assert buckets.opening_family(move(opening={"name": "", "eco": ""})) == ()


def test_the_endgame_dimension_ignores_moves_before_the_endgame():
    dimension = buckets.BY_KEY["ending"]
    assert dimension.buckets_of(move(phase="middlegame", fen=NO_QUEENS)) == ()
    assert dimension.buckets_of(move(phase="endgame", fen=NO_QUEENS)) \
        == ("rook ending",)


# --------------------------------------------------------------- aggregate


def test_acpl_excludes_book_moves():
    # ChessAnalyzer excludes them; a report that did not would flatter anyone
    # with preparation and say nothing about their play.
    rows = [move(cp=0, in_book=True), move(cp=0, in_book=True),
            move(cp=100), move(cp=200)]
    tally = aggregate.Tally()
    for row in rows:
        tally.add(row)
    assert tally.moves == 4
    assert tally.scored == 2
    assert tally.acpl == 150


def test_a_move_the_engine_could_not_score_is_not_counted_as_zero():
    rows = [move(cp=None), move(cp=100)]
    tally = aggregate.Tally()
    for row in rows:
        tally.add(row)
    assert tally.scored == 1 and tally.acpl == 100


def test_excess_loss_is_moves_times_the_gap_to_your_average():
    rows = [move(cp=100, game_id=f"g{i}") for i in range(10)]
    data = aggregate.slice_by(
        rows, buckets.BY_KEY["phase"], baseline=60.0, total_games=10)
    row = data[0]
    assert row["acpl"] == 100
    assert row["excessCp"] == pytest.approx(10 * (100 - 60))
    assert row["excessPawnsPerGame"] == pytest.approx(400 / 10 / 100)


def test_a_bucket_counts_distinct_games_not_moves():
    rows = [move(game_id="a"), move(game_id="a"), move(game_id="b")]
    data = aggregate.slice_by(rows, buckets.BY_KEY["phase"], baseline=50.0,
                              total_games=2)
    assert data[0]["moves"] == 3 and data[0]["games"] == 2


def test_accuracy_uses_the_analyzers_own_definition():
    # Mean of the arithmetic and harmonic means, which is what phase_accuracy
    # does. The harmonic half is what stops one perfect move hiding two awful
    # ones, so a plain mean here would be a different -- and kinder -- number.
    values = [100.0, 100.0, 10.0]
    mean = sum(values) / 3
    harmonic = 3 / sum(1 / v for v in values)
    assert aggregate.bucket_accuracy(values) == pytest.approx(
        round((mean + harmonic) / 2.0, 1))
    assert aggregate.bucket_accuracy(values) < mean
    assert aggregate.bucket_accuracy([]) is None


def test_move_rows_keeps_only_your_moves():
    games = [{"id": "g1", "you": "white", "them": "Foe", "speed": "blitz",
              "result": "1-0", "date": "2026-01-01", "url": "",
              "youElo": "1500", "themElo": "1500"}]
    reviews = {"g1": {"opening": {"name": "x", "eco": "A00"}, "moves": [
        {"color": "white", "san": "e4", "cpLoss": 10, "phase": "opening",
         "fenBefore": chess.STARTING_FEN, "accuracy": 99, "label": "best"},
        {"color": "black", "san": "c5", "cpLoss": 400, "phase": "opening",
         "fenBefore": chess.STARTING_FEN, "accuracy": 20, "label": "blunder"},
    ]}}
    rows = aggregate.move_rows(games, reviews)
    assert len(rows) == 1 and rows[0]["san"] == "e4"
    assert rows[0]["features"]["queens"] == "queens on"


def test_a_game_with_no_review_is_left_out_rather_than_guessed_at():
    games = [{"id": "g1", "you": "white"}, {"id": "g2", "you": "white"}]
    reviews = {"g1": {"moves": []}}
    assert aggregate.move_rows(games, reviews) == []


# ---------------------------------------------------------------- findings


def _spread(count, *, cp, phase="middlegame", fen=None, prefix="g"):
    return [move(cp=cp, phase=phase, fen=fen, game_id=f"{prefix}{i}")
            for i in range(count)]


def test_findings_need_both_enough_moves_and_enough_games():
    # Fifty moves, but all from one game: one game's worth of noise.
    rows = [move(cp=300, game_id="only") for _ in range(50)]
    found = findings.build(rows, total_games=1, baseline=50.0,
                           min_moves=10, min_games=5)
    assert found["weaknesses"] == []
    assert any(row["bucket"] == "middlegame" for row in found["skipped"])


def test_excess_is_how_bad_times_how_often_not_either_one_alone():
    # The metric itself, on the slice where no deduplication is in the way.
    rare_disaster = _spread(12, cp=400, phase="endgame", prefix="e")
    common_leak = _spread(120, cp=90, phase="middlegame", prefix="m")
    rows = aggregate.slice_by(rare_disaster + common_leak,
                              buckets.BY_KEY["phase"], baseline=80.0,
                              total_games=132)
    by_bucket = {row["bucket"]: row for row in rows}

    # Ranking by rate alone would put the endgame far ahead; by move count
    # alone, the middlegame. Excess says the endgame, and by how much.
    assert by_bucket["endgame"]["excessCp"] == pytest.approx(12 * (400 - 80))
    assert by_bucket["middlegame"]["excessCp"] == pytest.approx(120 * (90 - 80))

    # Turn the rate down and the common leak takes over, at the crossing point
    # the arithmetic predicts rather than one somebody chose.
    milder = _spread(12, cp=200, phase="endgame", prefix="e")
    bigger = _spread(300, cp=100, phase="middlegame", prefix="m")
    rows = aggregate.slice_by(milder + bigger, buckets.BY_KEY["phase"],
                              baseline=80.0, total_games=312)
    by_bucket = {row["bucket"]: row for row in rows}
    assert by_bucket["middlegame"]["excessCp"] > by_bucket["endgame"]["excessCp"]


def test_findings_come_out_in_excess_order():
    rows = (_spread(12, cp=400, phase="endgame", prefix="e")
            + _spread(120, cp=90, phase="middlegame", prefix="m"))
    found = findings.build(rows, total_games=132, baseline=80.0,
                           min_moves=10, min_games=10)
    excesses = [row["excessCp"] for row in found["weaknesses"]]
    assert excesses == sorted(excesses, reverse=True)
    assert all(value > 0 for value in excesses)

    # Strengths are the same list the other way up, and never positive.
    savings = [row["excessCp"] for row in found["strengths"]]
    assert savings == sorted(savings)
    assert all(value < 0 for value in savings)


def test_a_specific_bucket_survives_being_inside_a_vague_one():
    # Containment is not duplication. "queens on" holds every one of these
    # moves; the endgame inside it is the finding worth keeping, and an
    # overlap test built on containment would have thrown it away.
    rows = (_spread(40, cp=400, phase="endgame", fen=BOTH_QUEENS, prefix="e")
            + _spread(120, cp=90, phase="middlegame", fen=BOTH_QUEENS, prefix="m"))
    found = findings.build(rows, total_games=160, baseline=80.0,
                           min_moves=10, min_games=10)
    names = [row["bucket"] for row in found["weaknesses"]]
    assert "endgame" in names


def test_two_names_for_the_same_moves_are_reported_once():
    # "middlegame" and "middlegames with queens on" are the same 60 moves here,
    # so the report must pick one. Which one is settled by RANKABLE listing
    # `situation` first: on a tie, the more specific name wins.
    rows = _spread(60, cp=200, phase="middlegame", fen=BOTH_QUEENS)
    found = findings.build(rows, total_games=60, baseline=100.0,
                           min_moves=10, min_games=10)
    names = ([row["bucket"] for row in found["weaknesses"]]
             + [row["bucket"] for row in found["strengths"]])
    twins = [name for name in names
             if name in ("middlegame", "middlegames with queens on")]
    assert twins == ["middlegames with queens on"]


def test_a_near_duplicate_cannot_be_a_weakness_and_a_strength_at_once():
    # The failure this guards: two dimensions over nearly the same moves once
    # landed one in each list, and the report called the same thing both.
    rows = (_spread(50, cp=200, phase="middlegame", fen=BOTH_QUEENS, prefix="a")
            + _spread(10, cp=10, phase="middlegame", fen=BOTH_QUEENS, prefix="b"))
    found = findings.build(rows, total_games=60, baseline=150.0,
                           min_moves=10, min_games=10)
    weak = {row["bucket"] for row in found["weaknesses"]}
    strong = {row["bucket"] for row in found["strengths"]}
    assert not (weak & strong)


def test_a_finding_reads_as_a_sentence_whatever_the_bucket_is_called():
    for bucket in ("queenless middlegames", "opening", "as White",
                   "when you are ahead"):
        text = findings.sentence({
            "bucket": bucket, "excessPawnsPerGame": 0.42, "excessCp": 100.0,
            "scored": 187, "games": 41, "acpl": 71.0, "baseline": 54.0})
        assert text.startswith(bucket[0].upper())
        assert "0.42 pawns a game worse" in text
        assert "187 moves in 41 games" in text


def test_a_single_game_cannot_fill_the_worst_moments():
    rows = [move(cp=900 - index, game_id="one", san=f"a{index}")
            for index in range(10)]
    rows += [move(cp=100, game_id="two", san="b1")]
    moments = findings.worst_moments(rows, limit=6)
    assert sum(1 for row in moments if row["san"].startswith("a")) == 2


def test_forced_moves_are_not_blamed_on_you():
    rows = [move(cp=900, forced=True, game_id="g1"),
            move(cp=100, game_id="g1", san="Nf3")]
    moments = findings.worst_moments(rows)
    assert [row["san"] for row in moments] == ["Nf3"]


# ------------------------------------------------------------------- batch


def test_a_review_at_other_settings_is_not_reused():
    # The quiet failure this app exists to avoid: a depth-10 review inside a
    # depth-18 batch produces a normal-looking report with every figure wrong.
    old = {"engine": {"settingsKey": "Stockfish 18|t1-h256-pv2-d10"}}
    assert batch.review_signature(old) == "t1-h256-pv2-d10"
    assert batch.acceptable(old, "t1-h256-pv2-d10", "matching") is True
    assert batch.acceptable(old, "t1-h256-pv3-d18", "matching") is False
    assert batch.acceptable(old, "t1-h256-pv3-d18", "any") is True
    assert batch.acceptable(None, "t1-h256-pv3-d18", "any") is False


def test_batch_presets_are_fixed_depth_not_movetime():
    # A movetime budget makes every number depend on how busy the machine was,
    # which is fatal for a figure averaged over hundreds of games.
    for preset in batch.PRESETS.values():
        assert preset.get("depth")
        assert "movetime" not in preset
    assert batch.DEFAULT_THREADS == 1        # reproducibility over speed


def test_outstanding_counts_only_reviews_at_these_settings(tmp_path):
    store = Store(tmp_path)
    store.save_review("g1", {"engine": {"settingsKey": "SF|t1-h256-pv2-d10"}})
    games = [{"id": "g1"}, {"id": "g2"}]

    matching = batch.outstanding(store, games, preset="sweep", adopt="matching")
    assert matching["ready"] + matching["outstanding"] == 2
    anything = batch.outstanding(store, games, preset="deep", adopt="any")
    assert anything["ready"] == 1


# ------------------------------------------------------------------ sources


def test_a_pgn_is_read_with_your_side_worked_out():
    pgn = ('[Event "Rated blitz"]\n[Site "https://lichess.org/abc12345"]\n'
           '[Date "2026.01.02"]\n[White "Me"]\n[Black "Foe"]\n'
           '[WhiteElo "1600"]\n[BlackElo "1650"]\n[Result "1-0"]\n\n'
           '1. e4 e5 2. Nf3 1-0\n')
    rows = from_pgn_text(pgn, me="Me")
    assert len(rows) == 1
    assert rows[0]["you"] == "white"
    assert rows[0]["youElo"] == "1600" and rows[0]["themElo"] == "1650"
    assert rows[0]["date"] == "2026-01-02"

    # Somebody else's game is not yours.
    assert from_pgn_text(pgn, me="Nobody") == []


def test_a_pgn_with_no_moves_is_skipped():
    assert from_pgn_text('[Event "x"]\n[White "Me"]\n[Black "Foe"]\n\n*\n',
                         me="Me") == []


def test_months_since_keeps_the_cutoff_month():
    import datetime

    months = [f"https://api.chess.com/pub/player/x/games/{y}/{m:02d}"
              for y, m in ((2025, 11), (2026, 1), (2026, 8))]
    cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    assert [url[-7:] for url in
            _months_since(months, cutoff.timestamp() * 1000)] \
        == ["2026/01", "2026/08"]


# ------------------------------------------------------------------- store


def test_a_game_id_cannot_escape_its_folder(tmp_path):
    store = Store(tmp_path)
    assert safe_name("../../etc/passwd") == "etc-passwd"
    path = store.review_path("../../oops")
    assert tmp_path in path.parents


def test_reviews_survive_forgetting_a_report(tmp_path):
    store = Store(tmp_path)
    store.save_review("g1", {"moves": []})
    store.save_report("k", {"label": "x", "summary": {"games": 1}})
    store.save_games("k", {"games": [{"id": "g1"}]})

    store.delete_report("k")
    assert store.load_report("k") is None
    assert store.load_games("k") is None
    assert store.load_review("g1") is not None      # the expensive part stays


def test_a_corrupt_file_reads_as_missing_rather_than_raising(tmp_path):
    store = Store(tmp_path)
    path = store.review_path("g1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert store.load_review("g1") is None


# ------------------------------------------------------------------ report


def _small_report():
    games = [{"id": f"g{i}", "you": "white", "them": "Foe", "speed": "blitz",
              "result": "1-0" if i % 2 else "0-1", "date": f"2026-01-0{i+1}",
              "url": "", "youElo": "1500", "themElo": "1500"}
             for i in range(6)]
    reviews = {}
    for i in range(6):
        reviews[f"g{i}"] = {
            "opening": {"name": "Sicilian Defense", "eco": "B20"},
            "moves": [
                {"color": "white", "san": "e4", "cpLoss": 10, "phase": "opening",
                 "fenBefore": chess.STARTING_FEN, "accuracy": 95, "label": "best",
                 "moveNumber": 1, "inBook": False},
                {"color": "white", "san": "Rd1", "cpLoss": 250,
                 "phase": "middlegame", "fenBefore": NO_QUEENS, "accuracy": 30,
                 "label": "blunder", "judgment": "blunder", "moveNumber": 20,
                 "inBook": False},
            ]}
    return report_module.build(key="k", label="me", source="pgn", games=games,
                               reviews=reviews, min_moves=1, min_games=1)


def test_a_report_carries_what_it_was_built_from():
    built = _small_report()
    summary = built["summary"]
    assert summary["reviewed"] == 6 and summary["unreviewed"] == 0
    assert summary["record"] == {"win": 3, "draw": 0, "loss": 3}
    assert summary["from"] == "2026-01-01" and summary["to"] == "2026-01-06"
    assert summary["acpl"] == pytest.approx(130.0)
    assert built["thresholds"] == {"minMoves": 1, "minGames": 1}


def test_a_report_slices_every_dimension_it_can():
    built = _small_report()
    assert "phase" in built["slices"] and "situation" in built["slices"]
    phases = {row["bucket"] for row in built["slices"]["phase"]["buckets"]}
    assert phases == {"opening", "middlegame"}


def test_the_csv_has_one_row_per_bucket_and_names_its_dimension():
    built = _small_report()
    text = exportcsv.slices_csv(built)
    lines = [line for line in text.splitlines() if line.strip()]
    total = sum(len(data["buckets"])
                for data in built["slices"].values())
    assert len(lines) == total + 1                 # plus the header
    assert lines[0].startswith("dimension,dimension label,bucket,")


# ------------------------------------------------------- the sibling bridge


@pytest.mark.skipif(analyzer("accuracy") is None,
                    reason="ChessAnalyzer not found")
def test_our_accuracy_matches_the_analyzers_exactly():
    """One algorithm, not two. This fails the moment they diverge."""
    accuracy_module = analyzer("accuracy")
    values = [92.3, 41.0, 100.0, 12.5, 78.9]
    mean = sum(values) / len(values)
    expected = round((mean + accuracy_module.harmonic_mean(values)) / 2.0, 1)
    assert aggregate.bucket_accuracy(values) == expected


@pytest.mark.skipif(analyzer("accuracy") is None,
                    reason="ChessAnalyzer not found")
def test_phase_names_are_the_analyzers_phase_names():
    accuracy_module = analyzer("accuracy")
    names = {accuracy_module.phase_of(ply, 20, 60) for ply in (0, 30, 70)}
    assert names == {"opening", "middlegame", "endgame"}
    assert set(buckets.BY_KEY["phase"].order) == names


# ------------------------------------------------------------------- the css
#
# One stylesheet assertion, and it earns its place. The PDF dialog shipped with
# three checkboxes stretched to the full width of the dialog, each one shoving
# its own label out through the right-hand edge -- because `.dialog input` and
# `.check input` have identical specificity, so the one written later silently
# won. Nothing in Python could have caught that and nothing in the stylesheet
# looks wrong when you read it, which is the definition of a quiet failure.


WEB = Path(__file__).resolve().parent.parent / "weakness_report" / "web"


#: ``width: 100%`` and not ``max-width``/``min-width``, which are harmless on
#: a tickbox -- one caps it at a size it will never reach, the other at zero.
FULL_WIDTH = re.compile(r"(?<!-)\bwidth:\s*100%")


def _selectors_setting_full_width(css: str) -> list:
    """Every individual selector in a block that sets ``width: 100%``."""
    out = []
    for block in css.split("}"):
        if "{" not in block or not FULL_WIDTH.search(block.split("{", 1)[1]):
            continue
        selector = block.split("{")[0]
        out += [part.strip() for part in selector.split(",") if part.strip()]
    return out


def test_no_rule_stretches_a_checkbox_across_its_container():
    """A width:100% rule must exempt tickboxes by selector, not by luck.

    Written as "does any selector reach a bare ``input``" rather than "is the
    :not() still spelled correctly", so it keeps holding for rules nobody has
    written yet.
    """
    css = (WEB / "style.css").read_text(encoding="utf-8")
    offenders = [part for part in _selectors_setting_full_width(css)
                 if part.endswith("input") and ":not(" not in part]
    assert not offenders, f"these would stretch a checkbox: {offenders}"
    assert 'input[type="checkbox"], input[type="radio"]' in css, (
        "the belt-and-braces reset that pins a tickbox to its own size is gone")


def test_every_dialog_checkbox_still_has_a_label_beside_it():
    """A .check row is a box and a span. The span is what got pushed out."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    rows = re.findall(r'<label class="check">(.*?)</label>', html, re.S)
    assert len(rows) >= 3
    for row in rows:
        assert 'type="checkbox"' in row
        assert row.split("<span>")[1].split("</span>")[0].strip()
