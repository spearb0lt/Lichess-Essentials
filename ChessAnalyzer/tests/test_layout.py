"""Does the page actually work? Driven in a real headless browser.

The other suites cover the Python and the pure browser logic. Neither can see
a layout, and a layout is where this app's worst bug lived: the board and the
panel's scrollbar were resizing each other several times a second, so the
whole thing shook. Reading the stylesheet would never have found it. One
measurement here did.

So these are the checks that need pixels:

* the board, the controls and the graph all fit the window, at sizes from a
  laptop down to a small one;
* the board's size is *stable* -- repeated layout passes and an arriving
  engine evaluation must not move it;
* trying a move leaves the game's notation alone, which is the thing that was
  reported broken;
* the mouse wheel steps moves, including from a trackpad's small deltas.

Run it against a server you have already started:

    python -m chess_analyzer.cli serve
    python ChessAnalyzer/tests/test_layout.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = "http://127.0.0.1:8779"

#: Window sizes worth caring about, down to a small laptop.
SIZES = [(1600, 1000), (1440, 900), (1366, 768), (1280, 720),
         (1100, 650), (1000, 620), (900, 560)]

OPEN_A_REVIEWED_GAME = """(async () => {
  const rows = await (await fetch('/api/library')).json();
  const game = rows.games.find(r => r.reviewed);
  if (!game) return null;
  await openGame(game.id);
  return game.id;
})()"""

MEASURE = """(() => {
  const centre = document.querySelector('.panel.centre');
  const graph = document.querySelector('.graph-wrap');
  const controls = document.querySelector('.controls').getBoundingClientRect();
  const shown = !graph.classList.contains('hidden');
  const last = shown ? graph.getBoundingClientRect() : controls;
  return {
    board: Math.round(document.getElementById('board-box').getBoundingClientRect().width),
    overflow: Math.max(0, centre.scrollHeight - centre.clientHeight),
    bottom: Math.round(last.bottom),
    viewport: window.innerHeight,
    graph: shown,
    narrow: window.matchMedia('(max-width: 860px)').matches,
  };
})()"""

ANY_OTHER_MOVE = """(async () => {
  const url = '/api/legal?fen=' + encodeURIComponent(currentFen());
  const legal = await (await fetch(url)).json();
  const next = line()[state.ply + 1];
  const played = next ? next.uci.slice(0, 4) : '';
  for (const [from, tos] of Object.entries(legal.moves))
    for (const to of tos) if (from + to !== played) return from + to;
  return null;
})()"""

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}{('  ' + detail) if detail else ''}")


async def main() -> int:
    import browser as driver

    if driver.find_browser() is None:
        print("no Chromium-based browser found; skipping")
        return 0

    page = await driver.open_page(BASE, 1440, 900)
    try:
        game = await page.js(OPEN_A_REVIEWED_GAME, wait=True)
        if not game:
            print("no reviewed game in the library; skipping")
            return 0
        await asyncio.sleep(1.4)
        await page.js("setRightTab('moves'); goTo(10)")
        # Clear the "loaded" banner: it is a row in the page, so measuring
        # across the moment it fades would compare two different layouts.
        await page.js("say('')")
        await asyncio.sleep(0.8)

        # ------------------------------------------------- it fits, and holds still
        print("\nlayout")
        for width, height in SIZES:
            await page.resize(width, height)
            await asyncio.sleep(0.5)
            widths = []
            for _ in range(5):
                await page.js("fitBoard()")
                await asyncio.sleep(0.1)
                widths.append(await page.js(
                    "Math.round(document.getElementById('board-box')"
                    ".getBoundingClientRect().width)"))
            found = await page.js(MEASURE)
            label = f"{width}x{height}"
            check(f"{label} everything fits",
                  found["narrow"] or (found["overflow"] == 0
                                      and found["bottom"] <= found["viewport"]),
                  f"overflow {found['overflow']}px, bottom {found['bottom']}"
                  f" of {found['viewport']}")
            check(f"{label} board size settles",
                  len(set(widths)) == 1, f"saw {sorted(set(widths))}")

        # --------------------------------------------- an evaluation must not move it
        print("\nstability while the engine answers")
        await page.resize(1280, 720)
        await asyncio.sleep(0.6)
        uci = await page.js(ANY_OTHER_MOVE, wait=True)
        await page.js(f"playMove('{uci}')", wait=True)
        await asyncio.sleep(0.8)
        # Two moves deep, so there is an inter-move space to look for.
        second = await page.js(ANY_OTHER_MOVE, wait=True)
        if second:
            await page.js(f"playMove('{second}')", wait=True)
        await page.js("say('')")
        widths = []
        for _ in range(14):
            await asyncio.sleep(0.2)
            widths.append(await page.js(
                "Math.round(document.getElementById('board-box')"
                ".getBoundingClientRect().width)"))
        check("board holds still across a custom move and its evaluation",
              len(set(widths)) == 1, f"saw {sorted(set(widths))}")

        # ------------------------------------------------------ the reported bug
        print("\nvariations")
        found = await page.js("""(() => ({
          game: state.moves.length - 1,
          drawn: document.querySelectorAll('#move-list .mv').length,
          blocks: document.querySelectorAll('#move-list .variation').length,
          text: [...document.querySelectorAll('#move-list .variation')]
                  .map(e => e.textContent.trim()).join(' '),
        }))()""")
        check("the game's notation survives a variation",
              found["drawn"] == found["game"],
              f"{found['drawn']} of {found['game']} moves drawn")
        check("the variation is shown in brackets", found["blocks"] >= 1)
        check("its moves are separated by spaces",
              " " in found["text"].strip("() "), f"got {found['text']!r}")

        # -------------------------------------------------------------- the wheel
        print("\nmouse wheel")
        await page.js("state.active = null; goTo(20)")
        await asyncio.sleep(0.4)
        await page.js("""(() => {
          const box = document.getElementById('board-box');
          for (let i = 0; i < 3; i += 1)
            box.dispatchEvent(new WheelEvent('wheel',
              { deltaY: 120, bubbles: true, cancelable: true }));
        })()""")
        await asyncio.sleep(0.4)
        forward = await page.js("state.ply")
        await page.js("""(() => {
          const box = document.getElementById('board-box');
          for (let i = 0; i < 2; i += 1)
            box.dispatchEvent(new WheelEvent('wheel',
              { deltaY: -120, bubbles: true, cancelable: true }));
        })()""")
        await asyncio.sleep(0.4)
        back = await page.js("state.ply")
        check("a wheel notch is one move", forward == 23 and back == 21,
              f"20 -> {forward} -> {back}")

        await page.js("goTo(20)")
        await asyncio.sleep(0.3)
        await page.js("""(() => {
          const box = document.getElementById('board-box');
          for (let i = 0; i < 6; i += 1)
            box.dispatchEvent(new WheelEvent('wheel',
              { deltaY: 5, bubbles: true, cancelable: true }));
        })()""")
        await asyncio.sleep(0.4)
        drift = await page.js("state.ply") - 20
        check("a trackpad flick is one move, not ten", drift == 1,
              f"moved {drift}")
    finally:
        page.close()

    print(f"\n  {'all passed' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (ImportError, RuntimeError) as exc:
        print(f"skipping: {exc}")
        raise SystemExit(0) from exc
