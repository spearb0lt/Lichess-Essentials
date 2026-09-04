# Lives inside Repertoire-Creator/, but the build context must still be the
# REPO ROOT (not this folder) because this app pip-installs its sibling
# package for the engine ladder and PDF export (see README.md "Setup").
# On Render that means: Root Directory left at repo root, Dockerfile Path
# set to "Repertoire-Creator/Dockerfile". See README.md "Hosting it for
# free" for exact steps. COPY paths below are resolved against that repo
# root, which is why they're prefixed with the folder names rather than "./".
#
# git is installed because the app's own gitsync feature
# (repertoire_creator/gitsync.py) shells out to it to autosave your
# repertoires -- see the persistence note in the README before you deploy.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish git \
    && rm -rf /var/lib/apt/lists/*

ENV STOCKFISH_PATH=/usr/games/stockfish

WORKDIR /app

COPY Lichess-Study-to-PDF/requirements.txt Lichess-Study-to-PDF/requirements.txt
COPY Repertoire-Creator/requirements.txt Repertoire-Creator/requirements.txt
RUN pip install --no-cache-dir \
    -r Lichess-Study-to-PDF/requirements.txt \
    -r Repertoire-Creator/requirements.txt

COPY Lichess-Study-to-PDF Lichess-Study-to-PDF
COPY Repertoire-Creator Repertoire-Creator

# Editable installs so both packages import correctly regardless of cwd.
RUN pip install --no-cache-dir -e Lichess-Study-to-PDF -e Repertoire-Creator
RUN chmod +x Repertoire-Creator/entrypoint.sh

# Render sets $PORT; Hugging Face Spaces expects 7860 with nothing set, so
# ${PORT:-7860} satisfies both from the same image.
EXPOSE 7860
ENTRYPOINT ["Repertoire-Creator/entrypoint.sh"]
CMD ["sh", "-c", "repertoire serve --host 0.0.0.0 --port ${PORT:-7860}"]
