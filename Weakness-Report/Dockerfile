# Lives inside Weakness-Report/, but the build context must be the REPO ROOT
# (not this folder), because this app reads its review rules from the sibling
# ChessAnalyzer and its PDF diagrams from Lichess-Study-to-PDF. On Render that
# means: Root Directory left at repo root, Dockerfile Path set to
# "Weakness-Report/Dockerfile". See README.md "Hosting it for free".
#
# There is deliberately only one implementation of "was that a blunder" in this
# image: ChessAnalyzer is installed as a package rather than copied in pieces,
# so a game means the same thing here as it does there.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

ENV STOCKFISH_PATH=/usr/games/stockfish

WORKDIR /app

COPY Lichess-Study-to-PDF/requirements.txt Lichess-Study-to-PDF/requirements.txt
COPY ChessAnalyzer/requirements.txt ChessAnalyzer/requirements.txt
COPY Weakness-Report/requirements.txt Weakness-Report/requirements.txt
RUN pip install --no-cache-dir \
    -r Lichess-Study-to-PDF/requirements.txt \
    -r ChessAnalyzer/requirements.txt \
    -r Weakness-Report/requirements.txt

COPY Lichess-Study-to-PDF Lichess-Study-to-PDF
COPY ChessAnalyzer ChessAnalyzer
COPY Weakness-Report Weakness-Report

# Editable installs so every package imports correctly regardless of cwd.
RUN pip install --no-cache-dir \
    -e Lichess-Study-to-PDF -e ChessAnalyzer -e Weakness-Report

# Render sets $PORT; Hugging Face Spaces expects 7860 with nothing set, so
# ${PORT:-7860} satisfies both from the same image.
EXPOSE 7860
CMD ["sh", "-c", "weakness serve --host 0.0.0.0 --port ${PORT:-7860}"]
