# Lichess Essentials

Tools that fix the things I keep running into as a long-time Lichess user.
Built for my own use, open source in case they are useful to anyone else.

## Apps

| App | What it does |
|---|---|
| [Lichess-Study-to-PDF](Lichess-Study-to-PDF/) | Turns a study into a typeset chess book or a step-through PDF — every sideline, comment and annotation included — plus a browser interface with a live engine eval bar and a board you can play your own moves on. Imports private studies without a token. |
| [Repertoire-Creator](Repertoire-Creator/) | Build an opening repertoire locally — play or type the lines, annotate them, live eval bar and ranked engine suggestions — then publish it to Lichess as a study, drill yourself on it, or export it as a PDF. Knows which side you play, so it finds the positions you have no answer for. Its universal mode drops the chapters entirely: record sequences, and everything you have written down becomes one book keyed by position that tells you your own move as you play, or says *gap*. Saves to disk as you type and can commit and push itself. |

## Setup, once

All apps share one virtualenv at the repository root:

```powershell
# Windows PowerShell
python -m venv .lichess
.\.lichess\Scripts\python.exe -m pip install -r Lichess-Study-to-PDF\requirements.txt
.\.lichess\Scripts\python.exe -m pip install -r Repertoire-Creator\requirements.txt
.\.lichess\Scripts\python.exe -m pip install -e Lichess-Study-to-PDF
```

```bash
# Git Bash on Windows
python -m venv .lichess
./.lichess/Scripts/python.exe -m pip install -r Lichess-Study-to-PDF/requirements.txt
./.lichess/Scripts/python.exe -m pip install -r Repertoire-Creator/requirements.txt
./.lichess/Scripts/python.exe -m pip install -e Lichess-Study-to-PDF

# macOS / Linux
python -m venv .lichess
./.lichess/bin/python -m pip install -r Lichess-Study-to-PDF/requirements.txt
./.lichess/bin/python -m pip install -r Repertoire-Creator/requirements.txt
./.lichess/bin/python -m pip install -e Lichess-Study-to-PDF
```

That last line installs the study exporter as a library, which is how
Repertoire-Creator gets its engine ladder and its PDF layouts instead of
carrying a second copy of them.

## Start an app

Run each **from inside its own folder**:

```powershell
# Windows PowerShell
cd Lichess-Study-to-PDF
& "..\.lichess\Scripts\python.exe" -m lichess_study_pdf.cli serve      # port 8777

cd ..\Repertoire-Creator
& "..\.lichess\Scripts\python.exe" -m repertoire_creator.cli serve     # port 8778
```

```bash
# Git Bash on Windows
cd Lichess-Study-to-PDF   && ../.lichess/Scripts/python.exe -m lichess_study_pdf.cli serve
cd ../Repertoire-Creator  && ../.lichess/Scripts/python.exe -m repertoire_creator.cli serve

# macOS / Linux
cd Lichess-Study-to-PDF   && ../.lichess/bin/python -m lichess_study_pdf.cli serve
cd ../Repertoire-Creator  && ../.lichess/bin/python -m repertoire_creator.cli serve
```

Then open <http://127.0.0.1:8777> or <http://127.0.0.1:8778>. `Ctrl+C` stops
either one.

Optional but worth it: a [Stockfish](https://stockfishchess.org/download/)
binary in `Lichess-Study-to-PDF/engine/` gives both apps evaluation bars, and a
LaTeX install (MiKTeX or TeX Live) unlocks the typeset chess-book export. Each
app's startup banner tells you whether it found them.

Publishing a repertoire to Lichess additionally needs an API token with the
`study:write` scope — see
[the Repertoire-Creator README](Repertoire-Creator/README.md#publishing-to-lichess).

Repertoire-Creator writes into `Repertoire-Creator/repertoires/`, which is
inside this repository, and it commits and pushes that folder for you by
default. Only that folder is ever committed. If this repository is public, that
publishes your opening preparation too — turn pushing off with the `git` pill in
the app, or point `REPERTOIRE_DIR` somewhere private.

Full instructions, the CLI reference and troubleshooting live in each app's own
README: [Lichess-Study-to-PDF](Lichess-Study-to-PDF/README.md),
[Repertoire-Creator](Repertoire-Creator/README.md).

## Licence

MIT — see [LICENSE](LICENSE).
