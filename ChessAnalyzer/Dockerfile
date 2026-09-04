# Stockfish comes from Debian's apt repo rather than the app's own
# GitHub-releases downloader (see chess_analyzer/engines.py) -- simpler, and
# guaranteed to match the runtime CPU since apt picks the right package for
# the machine it's installed on. Lc0/Maia are left for the in-app picker to
# fetch on demand if you want them; they're optional extras, not needed to
# review a game.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

# engines.py checks $STOCKFISH_PATH directly (chess_analyzer/engines.py),
# same convention as the sibling apps' find_stockfish().
ENV STOCKFISH_PATH=/usr/games/stockfish

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT; Hugging Face Spaces expects 7860 with nothing set, so
# ${PORT:-7860} satisfies both from the same image.
EXPOSE 7860
CMD ["sh", "-c", "python -m chess_analyzer.cli serve --host 0.0.0.0 --port ${PORT:-7860}"]
