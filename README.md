# Lichess Essentials

Tools that fix the things I keep running into as a long-time Lichess user.
Built for my own use, open source in case they are useful to anyone else.

## Apps

| App | What it does |
|---|---|
| [Lichess-Study-to-PDF](Lichess-Study-to-PDF/) | Turns a study into a typeset chess book or a step-through PDF — every sideline, comment and annotation included — plus a browser interface with a live engine eval bar and a board you can play your own moves on. Imports private studies without a token. |

## Setup, once

All apps share one virtualenv at the repository root:

```powershell
# Windows PowerShell
python -m venv .lichess
.\.lichess\Scripts\python.exe -m pip install -r Lichess-Study-to-PDF\requirements.txt
```

```bash
# Git Bash on Windows
python -m venv .lichess
./.lichess/Scripts/python.exe -m pip install -r Lichess-Study-to-PDF/requirements.txt

# macOS / Linux
python -m venv .lichess
./.lichess/bin/python -m pip install -r Lichess-Study-to-PDF/requirements.txt
```

## Start Lichess-Study-to-PDF

Run it **from inside the app folder**:

```powershell
# Windows PowerShell
cd Lichess-Study-to-PDF
& "..\.lichess\Scripts\python.exe" -m lichess_study_pdf.cli serve
```

```bash
# Git Bash on Windows
cd Lichess-Study-to-PDF
../.lichess/Scripts/python.exe -m lichess_study_pdf.cli serve

# macOS / Linux
cd Lichess-Study-to-PDF
../.lichess/bin/python -m lichess_study_pdf.cli serve
```

Then open <http://127.0.0.1:8777>. `Ctrl+C` stops it.

Optional but worth it: a [Stockfish](https://stockfishchess.org/download/)
binary in `Lichess-Study-to-PDF/engine/` gives you evaluation bars, and a LaTeX
install (MiKTeX or TeX Live) unlocks the typeset chess-book export. The startup
banner tells you whether it found each one.

Full instructions, the CLI, and troubleshooting live in
[the app's README](Lichess-Study-to-PDF/README.md).

## Licence

MIT — see [LICENSE](LICENSE).
