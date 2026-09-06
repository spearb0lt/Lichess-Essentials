# Lichess Essentials

All five apps in one install:

```
pip install lichess-essentials
```

That gives you five commands:

| Command | What it does |
|---|---|
| `chess-analyzer serve` | Review any game -- Lichess, Chess.com or your own PGN -- with a local engine |
| `lichess-study-pdf serve` | Turn a Lichess study into a PDF you can step through move by move |
| `repertoire serve` | Build an opening repertoire locally, then publish it as a Lichess study |
| `prepper serve` | Scout an opponent from their own games and measure your prep against them |
| `weakness serve` | Review your whole history and find out what you are actually bad at |

Each opens a small web app in your browser; each also works from the command
line. The apps are separately installable if you only want one -- this
package is only a convenience that pulls in all of them, and installing the
full set is what switches on the features that one app borrows from another.

Full documentation, and the runbooks for hosting these yourself:
<https://github.com/spearb0lt/Lichess-Essentials>

MIT licensed.
