#!/bin/sh
# Wires the app's own gitsync feature (repertoire_creator/gitsync.py) to a
# dedicated GitHub repo so repertoire edits survive a container restart on a
# free host, which otherwise wipes anything written to disk at runtime.
#
# Rule this script follows, deliberately conservative: the DATA REPO always
# wins once it has a commit. This container's disk is disposable by design,
# so on every boot after the first we throw away whatever is baked into the
# image and clone the data repo instead. Nothing here ever force-pushes or
# rewrites remote history.
set -e

DATA_DIR="/app/Repertoire-Creator/repertoires"
BRANCH="${REPERTOIRE_GIT_BRANCH:-main}"

git config --global user.name "${GIT_AUTHOR_NAME:-Repertoire Creator}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-repertoire-bot@localhost}"
git config --global --add safe.directory "$DATA_DIR"

if [ -n "$REPERTOIRE_GIT_REMOTE_URL" ]; then
    if [ -n "$(git ls-remote "$REPERTOIRE_GIT_REMOTE_URL" "refs/heads/$BRANCH" 2>/dev/null)" ]; then
        echo "repertoires data repo already has '$BRANCH' - cloning it (image's baked-in seed data is discarded in favour of it)."
        rm -rf "$DATA_DIR"
        git clone --branch "$BRANCH" --single-branch "$REPERTOIRE_GIT_REMOTE_URL" "$DATA_DIR"
    else
        echo "repertoires data repo has no '$BRANCH' yet - publishing the image's seed data as the first commit."
        cd "$DATA_DIR"
        git init
        git checkout -b "$BRANCH"
        git remote add origin "$REPERTOIRE_GIT_REMOTE_URL"
        git add -A
        git commit -m "Seed from image" --allow-empty
        git push -u origin "$BRANCH"
    fi
    cd "$DATA_DIR"
    git remote set-url origin "$REPERTOIRE_GIT_REMOTE_URL" 2>/dev/null \
        || git remote add origin "$REPERTOIRE_GIT_REMOTE_URL"
else
    echo "REPERTOIRE_GIT_REMOTE_URL is not set - running with ephemeral storage only."
    echo "Edits made here will NOT survive the next restart. See the deploy notes."
fi

cd /app/Repertoire-Creator
exec "$@"
