"""Publishing a local repertoire to Lichess, and starting one from a study.

Local is the source of truth.  A push makes the Lichess study look like the
folder on disk; it never merges the other way.  That is stated plainly here
and in the UI, because the alternative -- silently discovering that a push
threw away an hour of browser editing -- is much worse than a warning.

How a chapter gets there depends on whether it has been pushed before:

* never pushed  -> ``import-pgn`` creates it, and the response tells us the
  Lichess chapter id, which we store so the *next* push updates in place
* pushed before -> ``/moves`` replaces its move tree, so the chapter keeps
  its id, its position in the study, and any link you have shared

A chapter whose content hash still matches the last push is skipped, so
pushing a 40-chapter repertoire after fixing one line costs one request.
"""

from __future__ import annotations

import chess
import chess.pgn

from .lichess import LichessClient, LichessError, PushedChapter
from .model import game_to_pgn, move_hash, pgn_to_game
from .storage import MAX_CHAPTERS, Repertoire, StorageError, now_iso, slugify


def ensure_study(repertoire: Repertoire, client: LichessClient,
                 *, visibility: str | None = None) -> tuple:
    """Return ``(study_id, created)``, creating the study if it has none.

    When Lichess creates a study it also creates one empty chapter.  We note
    which chapters existed beforehand so the caller can clear that placeholder
    away once real ones have landed.
    """
    if repertoire.meta.lichess_study_id:
        return repertoire.meta.lichess_study_id, False

    study_id = client.create_study(
        repertoire.meta.name,
        visibility=visibility or repertoire.meta.lichess_visibility,
    )
    repertoire.meta.lichess_study_id = study_id
    if visibility:
        repertoire.meta.lichess_visibility = visibility
    repertoire.save_manifest()
    return study_id, True


def _chapter_tags(repertoire: Repertoire, meta) -> dict:
    return {
        "Event": f"{repertoire.meta.name}: {meta.name}",
        "StudyName": repertoire.meta.name,
        "ChapterName": meta.name,
        "Orientation": meta.orientation,
    }


def push(
    repertoire: Repertoire,
    client: LichessClient,
    *,
    chapter_ids=None,
    force: bool = False,
    visibility: str | None = None,
    update_tags: bool = True,
    progress=None,
) -> dict:
    """Push chapters to Lichess. Returns a per-chapter report."""
    wanted = list(chapter_ids) if chapter_ids else [c.id for c in repertoire.meta.chapters]
    if not wanted:
        raise StorageError("This repertoire has no chapters to push.")
    if len(repertoire.meta.chapters) > MAX_CHAPTERS:
        raise StorageError(
            f"A Lichess study holds at most {MAX_CHAPTERS} chapters and this "
            f"repertoire has {len(repertoire.meta.chapters)}."
        )

    study_id, created = ensure_study(repertoire, client, visibility=visibility)

    placeholder_ids = set()
    if created:
        # Whatever is in the study now is the auto-created empty chapter.
        try:
            placeholder_ids = {c["id"] for c in client.study_chapters(study_id)}
        except LichessError:
            # Not fatal: worst case an "Empty chapter" is left behind and the
            # report says so.
            placeholder_ids = set()

    results = []
    for position, chapter_id in enumerate(wanted):
        meta = repertoire.meta.chapter(chapter_id)
        if meta is None:
            continue
        if progress:
            progress(position, len(wanted), meta.name)

        game = repertoire.game(chapter_id)
        content_hash = move_hash(game)

        if (not force and meta.lichess_chapter_id
                and meta.pushed_hash == content_hash):
            results.append(PushedChapter(
                chapter_id, meta.name, meta.lichess_chapter_id, "skipped",
                "unchanged since the last push",
            ))
            continue

        try:
            if meta.lichess_chapter_id:
                # Moves only -- the endpoint ignores tags, and sending the
                # headers as well would just be wasted bytes.
                client.update_moves(
                    study_id, meta.lichess_chapter_id,
                    game_to_pgn(game, headers=False),
                )
                if update_tags:
                    client.update_tags(
                        study_id, meta.lichess_chapter_id,
                        _chapter_tags(repertoire, meta),
                    )
                action = "updated"
            else:
                chapters = client.import_pgn(
                    study_id,
                    repertoire.study_pgn([chapter_id]),
                    name=meta.name,
                    orientation=meta.orientation,
                )
                if not chapters:
                    raise LichessError("Lichess accepted the import but created nothing.")
                meta.lichess_chapter_id = chapters[0].get("id")
                action = "created"

            meta.pushed_hash = content_hash
            meta.pushed_at = now_iso()
            results.append(PushedChapter(
                chapter_id, meta.name, meta.lichess_chapter_id, action,
            ))
        except LichessError as exc:
            results.append(PushedChapter(
                chapter_id, meta.name, meta.lichess_chapter_id, "failed", str(exc),
            ))
            # Keep going: one bad chapter should not strand the other thirty.
            continue

    repertoire.save_manifest()

    removed_placeholder = None
    if placeholder_ids and any(r.action == "created" for r in results):
        # Only now that real chapters exist is it safe to remove the empty
        # one Lichess made; deleting the last chapter would just recreate it.
        for empty_id in placeholder_ids:
            if empty_id in {r.chapter_id for r in results}:
                continue
            try:
                client.delete_chapter(study_id, empty_id)
                removed_placeholder = empty_id
            except LichessError:
                pass

    if progress:
        progress(len(wanted), len(wanted), "")

    return {
        "studyId": study_id,
        "studyUrl": repertoire.meta.lichess_url,
        "studyCreated": created,
        "removedPlaceholder": removed_placeholder,
        "chapters": [
            {
                "chapterId": r.local_id,
                "name": r.name,
                "lichessChapterId": r.chapter_id,
                "action": r.action,
                "detail": r.detail,
            }
            for r in results
        ],
        "created": sum(1 for r in results if r.action == "created"),
        "updated": sum(1 for r in results if r.action == "updated"),
        "skipped": sum(1 for r in results if r.action == "skipped"),
        "failed": sum(1 for r in results if r.action == "failed"),
    }


# ------------------------------------------------------------------- import


def _chapter_name(headers) -> str:
    name = headers.get("ChapterName") or ""
    if not name:
        event = headers.get("Event", "")
        name = event.split(":")[-1].strip() if ":" in event else event
    return name.strip()


def _chapter_id_from_site(headers, study_id: str) -> str | None:
    for tag in ("ChapterURL", "Site"):
        url = headers.get(tag, "")
        parts = [p for p in url.rstrip("/").split("/") if p]
        if len(parts) >= 2 and parts[-2] == study_id:
            return parts[-1]
    return None


def import_study(
    data_dir,
    client: LichessClient,
    study_id: str,
    *,
    color: str = "white",
    name: str | None = None,
) -> Repertoire:
    """Start a local repertoire from an existing Lichess study.

    The Lichess chapter ids are recorded as we go, so the very first push
    back updates those same chapters instead of duplicating the study.
    """
    import io

    pgn_text = client.export_study(study_id)
    handle = io.StringIO(pgn_text)

    games = []
    while True:
        game = chess.pgn.read_game(handle)
        if game is None:
            break
        games.append(game)
    if not games:
        raise StorageError(f"Study {study_id} has no chapters to import.")

    study_name = name or games[0].headers.get("StudyName") or ""
    if not study_name:
        event = games[0].headers.get("Event", "")
        study_name = event.split(":")[0].strip() or f"Study {study_id}"

    repertoire = Repertoire.create(data_dir, study_name, color=color)
    repertoire.meta.lichess_study_id = study_id

    for index, game in enumerate(games, start=1):
        chapter_name = _chapter_name(game.headers) or f"Chapter {index}"
        orientation = (game.headers.get("Orientation") or color).lower()
        if orientation not in ("white", "black"):
            orientation = color
        # Read the Lichess chapter id before adding: saving rewrites Site to
        # the study URL, and the chapter id only lives in the tag we came in
        # with.
        lichess_chapter_id = _chapter_id_from_site(game.headers, study_id)
        meta = repertoire.add_chapter(
            chapter_name, orientation=orientation, game=game
        )
        meta.lichess_chapter_id = lichess_chapter_id
        if meta.lichess_chapter_id:
            # It came from Lichess unchanged, so it does not need pushing.
            meta.pushed_hash = move_hash(game)
            meta.pushed_at = now_iso()

    repertoire.save_manifest()
    return repertoire


def import_pgn_text(
    data_dir,
    pgn_text: str,
    *,
    name: str,
    color: str = "white",
) -> Repertoire:
    """Start a local repertoire from a PGN file, one chapter per game."""
    import io

    handle = io.StringIO(pgn_text)
    games = []
    while True:
        game = chess.pgn.read_game(handle)
        if game is None:
            break
        games.append(game)
    if not games:
        raise StorageError("That PGN contains no games.")

    repertoire = Repertoire.create(data_dir, name, color=color)
    for index, game in enumerate(games, start=1):
        chapter_name = _chapter_name(game.headers) or f"Chapter {index}"
        repertoire.add_chapter(chapter_name, game=game)
    return repertoire


__all__ = [
    "ensure_study",
    "import_pgn_text",
    "import_study",
    "push",
    "pgn_to_game",
    "slugify",
]
