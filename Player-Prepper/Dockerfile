# Lives inside Player-Prepper/, but the build context must still be the REPO
# ROOT (not this folder), because this app pip-installs its sibling package
# for the engine suggestions and the PDF export (see README.md "Setting up").
# On Render that means: Root Directory left at repo root, Dockerfile Path set
# to "Player-Prepper/Dockerfile". See README.md "Hosting it for free" for the
# exact steps. The COPY paths below are resolved against that repo root, which
# is why they are prefixed with the folder names rather than "./".
#
# LaTeX is deliberately not installed: a full texlive is several GB and only
# the "book" PDF layout needs it. The grid layout, which is the one worth
# printing a scouting report in, does not.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

# engine.py finds Stockfish through the sibling app's find_stockfish(), which
# checks $STOCKFISH_PATH first -- so nothing else needs configuring.
ENV STOCKFISH_PATH=/usr/games/stockfish

WORKDIR /app

COPY Lichess-Study-to-PDF/requirements.txt Lichess-Study-to-PDF/requirements.txt
COPY Player-Prepper/requirements.txt Player-Prepper/requirements.txt
RUN pip install --no-cache-dir \
    -r Lichess-Study-to-PDF/requirements.txt \
    -r Player-Prepper/requirements.txt

COPY Lichess-Study-to-PDF Lichess-Study-to-PDF
COPY Player-Prepper Player-Prepper

# Editable installs so both packages import correctly regardless of cwd.
RUN pip install --no-cache-dir -e Lichess-Study-to-PDF -e Player-Prepper

# Render sets $PORT; Hugging Face Spaces expects 7860 with nothing set, so
# ${PORT:-7860} satisfies both from the same image.
EXPOSE 7860
CMD ["sh", "-c", "prepper serve --host 0.0.0.0 --port ${PORT:-7860}"]
