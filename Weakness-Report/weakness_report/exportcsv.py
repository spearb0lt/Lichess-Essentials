"""The slices as a spreadsheet.

The PDF is the document you read; this is the file you argue with. Every
bucket of every dimension, one row each, with the same numbers the tables
show -- so anyone who would rather sort by their own column, or plot a
dimension the report does not plot, can.

One file rather than one per dimension, with the dimension as a column,
because a folder of fourteen CSVs is worse than a filter.
"""

from __future__ import annotations

import csv
import io

COLUMNS = (
    ("dimension", "dimension"),
    ("dimensionLabel", "dimension label"),
    ("bucket", "bucket"),
    ("moves", "moves"),
    ("scored", "moves counted"),
    ("games", "games"),
    ("acpl", "acpl"),
    ("accuracy", "accuracy"),
    ("cpLost", "centipawns lost"),
    ("pawnsLost", "pawns lost"),
    ("excessCp", "excess centipawns"),
    ("excessPawnsPerGame", "excess pawns per game"),
    ("bestShare", "engine's move %"),
    ("blunders", "blunders"),
    ("mistakes", "mistakes"),
    ("inaccuracies", "inaccuracies"),
)


def slices_csv(report: dict) -> str:
    """Every bucket of every slice, one row each."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    baseline = report.get("baselineAcpl")
    writer.writerow([label for _, label in COLUMNS] + ["your overall acpl"])

    for key, data in (report.get("slices") or {}).items():
        for row in data.get("buckets") or []:
            judgments = row.get("judgments") or {}
            values = {
                **row,
                "dimension": key,
                "dimensionLabel": data.get("label", key),
                "blunders": judgments.get("blunder", 0),
                "mistakes": judgments.get("mistake", 0),
                "inaccuracies": judgments.get("inaccuracy", 0),
            }
            writer.writerow(
                [values.get(name, "") for name, _ in COLUMNS] + [baseline])

    return buffer.getvalue()


__all__ = ["COLUMNS", "slices_csv"]
