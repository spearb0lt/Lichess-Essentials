#!/bin/bash
# Housekeeping for the Lichess Essentials deployment.
#
#   ~/prune.sh            show what WOULD be deleted, delete nothing
#   ~/prune.sh --apply    actually delete
#
# Two rules, deliberately different, because the data is not all the same kind:
#
#   COUNT CAP  on stores that hold one file per subject (a scout per opponent,
#              a report per dataset). Keeping the newest handful is harmless --
#              anything dropped is a few minutes of network away from coming
#              back.
#
#   SIZE VALVE on the engine reviews and the eval caches. A review is hours of
#              CPU, so it is not pruned on a whim -- only if the store grows
#              past a threshold, and then oldest-first.
#
# NEVER TOUCHED, at any size:
#   * repertoires  -- hand-authored, nothing regenerates them. The one
#                     genuinely irreplaceable thing on this machine.
#   * settings.json anywhere.
set -u
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1
LOG=~/prune.log
say() { echo "$*"; [ "$APPLY" = 1 ] && echo "$(date -Is) $*" >> "$LOG"; }

vol() { docker volume inspect "lichess-essentials_$1" --format '{{.Mountpoint}}' 2>/dev/null; }

# keep newest $2 entries in dir $1
cap_count() {
  local dir="$1" keep="$2" label="$3"
  sudo test -d "$dir" || { say "  $label: not created yet - nothing stored here"; return 0; }
  local total; total=$(sudo find "$dir" -maxdepth 1 -type f | wc -l)
  [ "$total" -le "$keep" ] && { say "  $label: $total files (cap $keep) - nothing to do"; return 0; }
  local n=$((total - keep))
  say "  $label: $total files (cap $keep) -> removing $n oldest"
  sudo find "$dir" -maxdepth 1 -type f -printf '%T@ %p\n' \
    | sort -n | head -n "$n" | cut -d' ' -f2- \
    | while read -r f; do
        say "      $( [ "$APPLY" = 1 ] && echo deleted || echo "would delete") $(basename "$f")"
        [ "$APPLY" = 1 ] && sudo rm -f "$f"
      done
}

# delete oldest in $1 until the tree is under $2 megabytes
cap_size() {
  local dir="$1" limit_mb="$2" label="$3"
  sudo test -d "$dir" || { say "  $label: not created yet - nothing stored here"; return 0; }
  local mb; mb=$(sudo du -sm "$dir" 2>/dev/null | cut -f1)
  [ "${mb:-0}" -le "$limit_mb" ] && { say "  $label: ${mb}MB (limit ${limit_mb}MB) - nothing to do"; return 0; }
  say "  $label: ${mb}MB exceeds ${limit_mb}MB -> trimming oldest first"
  sudo find "$dir" -type f -printf '%T@ %p\n' | sort -n | cut -d' ' -f2- \
    | while read -r f; do
        mb=$(sudo du -sm "$dir" 2>/dev/null | cut -f1)
        [ "${mb:-0}" -le "$limit_mb" ] && break
        say "      $( [ "$APPLY" = 1 ] && echo deleted || echo "would delete") $(basename "$f")"
        [ "$APPLY" = 1 ] && sudo rm -f "$f" || break
      done
}

say "=== prune $( [ "$APPLY" = 1 ] && echo '(APPLYING)' || echo '(dry run - nothing will be deleted)') $(date -Is) ==="
say "disk: $(df -h / | awk 'NR==2{print $3" used of "$2", "$4" free"}')"

# 20 across the board, as asked. Safe everywhere it is applied: every one of
# these holds ONE FILE PER SUBJECT -- a scout per opponent, a report per
# dataset, a game per import -- so 20 is 20 subjects, and anything dropped is
# minutes of network away from coming back. Nothing crashes if a file is gone:
# store.py:_read_json returns None for a missing file and the API answers 404.
P=$(vol prepper-prep);      cap_count "$P/scouts"  20 "prepper/scouts"
                            cap_count "$P/games"   20 "prepper/games"
W=$(vol weakness-history);  cap_count "$W/reports" 20 "weakness/reports"
                            cap_count "$W/games"   20 "weakness/games"
A=$(vol analyzer-games);    cap_count "$A"         20 "analyzer/games"

# reviews is the ONE exception, deliberately left on a size valve.
# It is one file per GAME, not per subject. Weakness Report exists to read
# hundreds of them at once -- its own README makes the point with "four hundred
# of them". Capping it at 20 would not crash anything, it would just quietly
# reduce the app to answering from 20 games and calling it your history. So it
# only trims when the store passes 2GB, oldest first.
                            cap_size  "$W/reviews" 2000 "weakness/reviews"

for c in study prepper repertoire analyzer weakness; do
  d=$(vol "$c-cache"); [ -n "$d" ] && cap_size "$d" 200 "$c-cache"
done

say "  repertoires: SKIPPED on purpose - irreplaceable"
say "=== done ==="
exit 0
