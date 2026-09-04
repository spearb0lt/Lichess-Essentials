"""Drive a real headless browser over the DevTools protocol.

The rest of the suite covers Python and the pure parts of the browser code.
This covers the part neither can reach: whether the page actually lays out and
responds. It was written after a layout bug shipped that no amount of reading
the stylesheet would have caught -- the board and the scrollbar were resizing
each other several times a second -- and it found that in one measurement.

Nothing here is an extra dependency. Edge is already on the machine and
`websockets` arrived with uvicorn[standard]; if either is missing the tests
that use it skip.

Point it at a running server:

    python -m chess_analyzer.cli serve            # in another terminal
    python ChessAnalyzer/tests/test_layout.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import time
from pathlib import Path

import requests
import websockets

#: Whichever Chromium is on the machine; both speak the same protocol.
CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium", "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser():
    for path in CANDIDATES:
        if Path(path).exists():
            return path
    return None
PORT = 9333
PROFILE = Path(__file__).resolve().parent / "edge-profile"


class Browser:
    def __init__(self, width=1440, height=900):
        self.width, self.height = width, height
        self.process = None
        self.socket = None
        self.next_id = 0

    def launch(self):
        binary = find_browser()
        if binary is None:
            raise RuntimeError("no Chromium-based browser found")
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen([
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            f"--window-size={self.width},{self.height}",
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(60):
            try:
                targets = requests.get(f"http://127.0.0.1:{PORT}/json", timeout=2).json()
                page = next((t for t in targets if t["type"] == "page"), None)
                if page:
                    return page["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("Edge did not come up")

    async def connect(self, url):
        self.socket = await websockets.connect(url, max_size=64 * 1024 * 1024)

    async def send(self, method, **params):
        self.next_id += 1
        message_id = self.next_id
        await self.socket.send(json.dumps(
            {"id": message_id, "method": method, "params": params}))
        while True:
            raw = json.loads(await self.socket.recv())
            if raw.get("id") == message_id:
                if "error" in raw:
                    raise RuntimeError(f"{method}: {raw['error']}")
                return raw.get("result", {})

    async def js(self, expression, wait=False):
        result = await self.send(
            "Runtime.evaluate", expression=expression,
            returnByValue=True, awaitPromise=wait, userGesture=True)
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise RuntimeError(f"JS error: {text}")
        return result.get("result", {}).get("value")

    async def goto(self, url, settle=2.5):
        await self.send("Page.enable")
        await self.send("Page.navigate", url=url)
        for _ in range(60):
            await asyncio.sleep(0.25)
            try:
                if await self.js("document.readyState") == "complete":
                    break
            except Exception:
                pass
        await asyncio.sleep(settle)

    async def resize(self, width, height):
        self.width, self.height = width, height
        await self.send("Emulation.setDeviceMetricsOverride",
                        width=width, height=height,
                        deviceScaleFactor=1, mobile=False)
        await asyncio.sleep(0.4)

    async def shot(self, path, full=False):
        result = await self.send("Page.captureScreenshot",
                                 format="png", captureBeyondViewport=full)
        Path(path).write_bytes(base64.b64decode(result["data"]))
        return path

    async def click_at(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                            button="left", clickCount=1)
        await asyncio.sleep(0.35)

    async def wheel(self, x, y, delta):
        await self.send("Input.dispatchMouseEvent", type="mouseWheel",
                        x=x, y=y, deltaX=0, deltaY=delta)
        await asyncio.sleep(0.25)

    def close(self):
        if self.process:
            self.process.terminate()


async def open_page(url, width=1440, height=900, settle=3.0):
    browser = Browser(width, height)
    socket_url = browser.launch()
    await browser.connect(socket_url)
    await browser.resize(width, height)
    await browser.goto(url, settle=settle)
    return browser
