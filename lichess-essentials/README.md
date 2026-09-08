# Lichess Essentials

All five apps in one install:

```
pip install lichess-essentials
```

## Try them live first

All five are running on a free Oracle Cloud box if you want to look before you
install. Login `test` / `testpassword1234@` where asked:

| App | Link |
|---|---|
| Lichess Study to PDF | https://study.lichess-essentials.duckdns.org *(no login)* |
| Chess Analyzer | https://analyzer.lichess-essentials.duckdns.org |
| Player Prepper | https://prepper.lichess-essentials.duckdns.org |
| Repertoire Creator | https://repertoire.lichess-essentials.duckdns.org |
| Weakness Report | https://weakness.lichess-essentials.duckdns.org |

Shared demo credentials and shared data, so treat anything you put in as
public, and do not paste a Lichess token you care about.


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
