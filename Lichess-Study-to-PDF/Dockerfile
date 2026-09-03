# Stockfish comes from Debian's apt repo instead of a manual binary download,
# so the eval bar works out of the box on any host that builds this image.
# LaTeX ("book mode") is deliberately left out: texlive alone is several GB
# and would blow past most free-tier build size/time limits.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

# Debian's stockfish package puts the binary in /usr/games, which is not on
# PATH for non-login shells (i.e. inside Docker) — point find_stockfish() at
# it directly instead of fighting PATH.
ENV STOCKFISH_PATH=/usr/games/stockfish

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT; Hugging Face Spaces expects 7860 with nothing set, so
# ${PORT:-7860} satisfies both from the same image.
EXPOSE 7860
CMD ["sh", "-c", "python -m lichess_study_pdf.cli serve --host 0.0.0.0 --port ${PORT:-7860}"]
