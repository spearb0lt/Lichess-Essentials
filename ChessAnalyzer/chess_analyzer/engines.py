"""Which engine is doing the thinking, and how it got onto this machine.

Three things live here.

**Finding what you already have.**  A Stockfish sitting in the sibling app's
``engine/`` folder, on ``PATH``, or pointed at by ``STOCKFISH_PATH`` is used
as-is.  Nothing is downloaded unless you ask for it.

**Fetching what you do not.**  Engine builds come from the projects' own
GitHub releases, read through the releases API rather than pasted in as URLs,
so a new Stockfish appears in the picker the day it ships instead of the day
this file is next edited.

The awkward part is CPU builds.  Stockfish publishes one binary per
instruction set -- ``bmi2``, ``avx2``, ``avx512``, down to a plain
``x86-64`` -- and running one your processor cannot execute does not fail
politely, it dies on an illegal instruction.  There is no portable way to read
CPU feature flags from Python on Windows, so this does not try to guess: it
downloads the build you asked for, **runs it** and waits for it to answer
``uci``, and on failure walks down the ladder to the next-safest build
automatically.  A working engine at the end is worth one wasted download, and
what worked is remembered so it only ever happens once.

**Keeping it warm.**  Spawning a UCI engine costs about a second, which is
fine once and unbearable per position, so engines are started on first use and
kept.  A UCI engine is one conversation at a time -- two overlapping
``analyse`` calls corrupt both -- so every engine carries its own lock.  That
lock is also why a full-game review and the live eval bar can share one
process without stepping on each other.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.engine
import requests

USER_AGENT = "chess-analyzer/0.1"

#: Where downloaded engines land. Beside the app, not in a cache directory:
#: these are hundreds of megabytes and you should be able to find and delete
#: them without knowing where your platform hides its caches.
ENGINE_DIR = Path(__file__).resolve().parent.parent / "engines"

GITHUB_API = "https://api.github.com/repos"
STOCKFISH_REPO = "official-stockfish/Stockfish"
LC0_REPO = "LeelaChessZero/lc0"

#: Stockfish Windows builds, fastest first. Downloading walks this list
#: downwards whenever a build refuses to run. ``x86-64`` at the bottom runs on
#: anything 64-bit, so the walk always terminates somewhere useful.
WINDOWS_LADDER = ["avx512", "bmi2", "avx2", "sse41-popcnt", "x86-64"]
LINUX_LADDER = ["avx512", "bmi2", "avx2", "sse41-popcnt", "x86-64"]
MACOS_LADDER = ["bmi2", "avx2", "x86-64"]

#: Leela needs a network file as well as a binary, and which one you pick
#: changes what kind of opponent it is far more than any engine setting.
#: The Maia nets are the interesting ones: they are trained to predict what a
#: human of a given rating actually plays, not what is best, which is the
#: closest freely available thing to Chess.com's "human" review engine.
LC0_NETWORKS = {
    "t1-256x10": {
        "name": "T1 256x10 distilled",
        "url": "https://storage.lczero.org/files/networks-contrib/"
               "t1-256x10-distilled-swa-2432500.pb.gz",
        "size": 37_118_673,
        "note": "Strong and usable on CPU. The sensible default.",
    },
    "t1-512x15": {
        "name": "T1 512x15 distilled",
        "url": "https://storage.lczero.org/files/networks-contrib/"
               "t1-512x15x8h-distilled-swa-3395000.pb.gz",
        "size": 149_758_071,
        "note": "Considerably stronger and considerably slower without a GPU.",
    },
    "maia-1500": {
        "name": "Maia 1500 (human-like)",
        "url": "https://github.com/CSSLab/maia-chess/raw/master/"
               "maia_weights/maia-1500.pb.gz",
        "size": 1_258_199,
        "note": "Predicts what a 1500 actually plays, not what is best. "
                "Use it to ask whether a move was findable, never to judge one.",
    },
    "maia-1900": {
        "name": "Maia 1900 (human-like)",
        "url": "https://github.com/CSSLab/maia-chess/raw/master/"
               "maia_weights/maia-1900.pb.gz",
        "size": 1_300_000,
        "note": "The same idea at club-strong level.",
    },
}


class EngineError(RuntimeError):
    """Anything that went wrong finding, fetching or starting an engine."""


@dataclass
class EngineSpec:
    """One engine the app knows about, installed or not."""

    id: str
    name: str
    kind: str                       # "stockfish" | "lc0" | "found"
    version: str = ""
    path: str | None = None
    weights: str | None = None
    installed: bool = False
    download_size: int = 0
    note: str = ""
    build: str = ""

    def json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "version": self.version,
            "installed": self.installed,
            "path": self.path,
            "weights": self.weights,
            "downloadSize": self.download_size,
            "note": self.note,
            "build": self.build,
        }


# ------------------------------------------------------------- discovery


def _ladder() -> list[str]:
    system = platform.system().lower()
    if system == "windows":
        return WINDOWS_LADDER
    if system == "darwin":
        return MACOS_LADDER
    return LINUX_LADDER


def _asset_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "windows-armv8" if "arm" in machine else "windows-x86-64"
    if system == "darwin":
        return "macos-m1-apple-silicon" if machine in ("arm64", "aarch64") \
            else "macos-x86-64"
    return "ubuntu-armv8" if "arm" in machine else "ubuntu-x86-64"


def _executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if platform.system().lower() == "windows":
        return path.suffix.lower() == ".exe"
    return os.access(path, os.X_OK) or path.suffix == ""


def _search_dirs() -> list[Path]:
    """Everywhere an engine binary might already be sitting.

    The sibling app's folder is first on the list deliberately: this
    repository tells you to put Stockfish there, so someone following its
    README already has one and should never be asked to download a second.
    """
    root = Path(__file__).resolve().parent.parent.parent
    return [
        ENGINE_DIR,
        root / "Lichess-Study-to-PDF" / "engine",
        root / "engine",
    ]


def discover() -> list[EngineSpec]:
    """Engines already on this machine, in the order they were found."""
    found: list[EngineSpec] = []
    seen: set[str] = set()

    def add(path: Path, label: str, kind: str) -> None:
        resolved = str(path.resolve())
        if resolved in seen or not _executable(path):
            return
        seen.add(resolved)
        found.append(EngineSpec(
            id=f"found:{resolved}",
            name=label,
            kind=kind,
            path=resolved,
            installed=True,
            note=f"Found at {resolved}",
        ))

    for directory in _search_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            name = path.name.lower()
            if name.startswith("stockfish"):
                add(path, _pretty_name(path.name), "stockfish")
            elif name.startswith("lc0"):
                add(path, "Lc0 (local)", "lc0")

    explicit = os.environ.get("STOCKFISH_PATH") or os.environ.get("ENGINE_PATH")
    if explicit and Path(explicit).is_file():
        add(Path(explicit), "Engine from $STOCKFISH_PATH", "stockfish")

    for name in ("stockfish", "stockfish.exe", "lc0", "lc0.exe"):
        on_path = shutil.which(name)
        if on_path:
            add(Path(on_path), f"{name} (on PATH)",
                "lc0" if name.startswith("lc0") else "stockfish")

    return found


def _pretty_name(filename: str) -> str:
    """``stockfish-windows-x86-64-bmi2.exe`` -> ``Stockfish (bmi2)``."""
    stem = Path(filename).stem
    for build in ("avx512icl", "avx512", "avxvnni", "vnni512", "bmi2", "avx2",
                  "sse41-popcnt", "dotprod"):
        if stem.endswith(build):
            return f"Stockfish ({build})"
    return "Stockfish"


# --------------------------------------------------------------- catalog

_CATALOG_LOCK = threading.Lock()
_CATALOG: dict[str, tuple[float, list]] = {}
#: GitHub's unauthenticated rate limit is 60 requests an hour per address, so
#: the release list is cached rather than fetched per page load.
_CATALOG_TTL = 6 * 3600


def _releases(repo: str, limit: int = 6) -> list[dict]:
    with _CATALOG_LOCK:
        cached = _CATALOG.get(repo)
        if cached and time.time() - cached[0] < _CATALOG_TTL:
            return cached[1]

    try:
        response = requests.get(
            f"{GITHUB_API}/{repo}/releases",
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/vnd.github+json"},
            params={"per_page": limit},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise EngineError(
            f"Could not read the {repo} release list: {exc}. "
            "You can still use an engine you already have."
        ) from exc

    releases = [r for r in data if not r.get("prerelease")][:limit]
    with _CATALOG_LOCK:
        _CATALOG[repo] = (time.time(), releases)
    return releases


def catalog(*, offline: bool = False) -> dict:
    """Everything the picker offers: what is here, and what can be fetched."""
    local = discover()
    installed_ids = {spec.id for spec in local}

    downloads: list[EngineSpec] = []
    warning = ""

    if not offline:
        try:
            for release in _releases(STOCKFISH_REPO):
                tag = release.get("tag_name", "")
                version = tag.replace("sf_", "Stockfish ")
                prefix = f"stockfish-{_asset_platform()}-"
                builds = sorted({
                    asset["name"][len(prefix):].rsplit(".", 1)[0]
                    for asset in release.get("assets", [])
                    if asset["name"].startswith(prefix)
                })
                if not builds:
                    continue
                size = next(
                    (a["size"] for a in release["assets"]
                     if a["name"].startswith(prefix)), 0)
                spec_id = f"stockfish:{tag}"
                downloads.append(EngineSpec(
                    id=spec_id,
                    name=version,
                    kind="stockfish",
                    version=tag,
                    download_size=size,
                    installed=_installed_path(spec_id) is not None,
                    path=_installed_path(spec_id),
                    note=("Best build for this machine is picked automatically, "
                          "with a fallback if your CPU cannot run it. "
                          f"Available: {', '.join(builds)}."),
                ))

            for release in _releases(LC0_REPO, limit=3):
                tag = release.get("tag_name", "")
                asset = _lc0_asset(release)
                if asset is None:
                    continue
                spec_id = f"lc0:{tag}"
                downloads.append(EngineSpec(
                    id=spec_id,
                    name=f"Lc0 {tag}",
                    kind="lc0",
                    version=tag,
                    download_size=asset["size"],
                    installed=_installed_path(spec_id) is not None,
                    path=_installed_path(spec_id),
                    note="Neural-net engine. Needs a network file as well; "
                         "CPU-only play is far slower than Stockfish.",
                ))
        except EngineError as exc:
            warning = str(exc)

    return {
        "found": [spec.json() for spec in local],
        "downloads": [spec.json() for spec in downloads
                      if spec.id not in installed_ids],
        "networks": [
            {"id": key, "installed": (ENGINE_DIR / "networks" /
                                      f"{key}.pb.gz").is_file(), **value}
            for key, value in LC0_NETWORKS.items()
        ],
        "warning": warning,
        "engineDir": str(ENGINE_DIR),
        "platform": _asset_platform(),
    }


def _lc0_asset(release: dict) -> dict | None:
    """The smallest Lc0 build that will run without special hardware."""
    system = platform.system().lower()
    if system != "windows":
        # Lc0 ships no generic Linux/macOS binary; those platforms build it.
        return None
    preferred = ("windows-cpu-dnnl", "windows-cpu-openblas")
    for want in preferred:
        for asset in release.get("assets", []):
            if want in asset["name"] and asset["name"].endswith(".zip"):
                return asset
    return None


def _installed_path(spec_id: str) -> str | None:
    """The binary for an installed download, or None."""
    marker = ENGINE_DIR / _slug(spec_id) / ".binary"
    if marker.is_file():
        path = Path(marker.read_text(encoding="utf-8").strip())
        if path.is_file():
            return str(path)
    return None


def _slug(spec_id: str) -> str:
    return spec_id.replace(":", "-").replace("/", "-").replace(".", "_")


# -------------------------------------------------------------- installing


def _download(url: str, target: Path, progress=None) -> Path:
    """Stream a file to disk, reporting progress as it goes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=60,
                          headers={"User-Agent": USER_AGENT}) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            done = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=262_144):
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except requests.RequestException as exc:
        partial.unlink(missing_ok=True)
        raise EngineError(f"Download failed: {exc}") from exc

    partial.replace(target)
    return target


def _extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(into)
    else:
        # Stockfish ships plain .tar for the Unix builds.
        with tarfile.open(archive) as bundle:
            # filter="data" refuses absolute paths and symlinks escaping the
            # target, which is the documented safe way to unpack an archive
            # you did not create.
            try:
                bundle.extractall(into, filter="data")
            except TypeError:            # Python < 3.12 has no filter
                bundle.extractall(into)


def _find_binary(root: Path, stem: str) -> Path | None:
    windows = platform.system().lower() == "windows"
    best = None
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if not name.startswith(stem):
            continue
        if windows and path.suffix.lower() != ".exe":
            continue
        if not windows and path.suffix not in ("", ".bin"):
            continue
        best = path
        break
    if best is not None and not windows:
        best.chmod(best.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return best


def probe(path: str, *, timeout: float = 20.0) -> str:
    """Start the engine and read its name back, or raise.

    This is the whole point of the fallback ladder: a build compiled for
    instructions your CPU lacks starts and dies rather than reporting an
    error, so the only reliable test is a real UCI handshake.
    """
    try:
        engine = chess.engine.SimpleEngine.popen_uci(path, timeout=timeout)
    except Exception as exc:                       # noqa: BLE001 - any failure
        raise EngineError(
            f"{Path(path).name} would not start on this machine: {exc}"
        ) from exc
    try:
        return engine.id.get("name", Path(path).name)
    finally:
        try:
            engine.quit()
        except Exception:                          # noqa: BLE001
            pass


def install(spec_id: str, *, progress=None, build: str | None = None) -> EngineSpec:
    """Fetch, unpack and verify an engine. Returns the installed spec."""
    kind, _, tag = spec_id.partition(":")
    if kind == "stockfish":
        return _install_stockfish(tag, progress=progress, build=build)
    if kind == "lc0":
        return _install_lc0(tag, progress=progress)
    raise EngineError(f"Nothing to install for {spec_id!r}.")


def _install_stockfish(tag: str, *, progress=None,
                       build: str | None = None) -> EngineSpec:
    releases = _releases(STOCKFISH_REPO)
    release = next((r for r in releases if r.get("tag_name") == tag), None)
    if release is None:
        raise EngineError(f"No Stockfish release tagged {tag}.")

    prefix = f"stockfish-{_asset_platform()}-"
    assets = {a["name"][len(prefix):].rsplit(".", 1)[0]: a
              for a in release["assets"] if a["name"].startswith(prefix)}
    if not assets:
        raise EngineError(
            f"Stockfish {tag} ships no build for {_asset_platform()}.")

    order = [b for b in ([build] if build else []) + _ladder() if b in assets]
    order += [b for b in assets if b not in order]

    home = ENGINE_DIR / _slug(f"stockfish:{tag}")
    problems = []
    for candidate in order:
        asset = assets[candidate]
        archive = home / asset["name"]
        try:
            if progress:
                progress(0, asset["size"], f"Downloading {asset['name']}")
            _download(asset["browser_download_url"], archive,
                      progress=(lambda d, t: progress(d, t, None))
                      if progress else None)
            if progress:
                progress(asset["size"], asset["size"], "Unpacking")
            _extract(archive, home)
            archive.unlink(missing_ok=True)

            binary = _find_binary(home, "stockfish")
            if binary is None:
                problems.append(f"{candidate}: no binary inside the archive")
                continue
            if progress:
                progress(asset["size"], asset["size"],
                         f"Checking {candidate} runs here")
            name = probe(str(binary))
        except EngineError as exc:
            problems.append(f"{candidate}: {exc}")
            # A build this CPU cannot run is dead weight; clear it out before
            # trying the next one down the ladder.
            shutil.rmtree(home, ignore_errors=True)
            continue

        (home / ".binary").write_text(str(binary), encoding="utf-8")
        return EngineSpec(
            id=f"stockfish:{tag}", name=name, kind="stockfish", version=tag,
            path=str(binary), installed=True, build=candidate,
            note=f"{candidate} build, verified running on this machine.",
        )

    raise EngineError(
        "No Stockfish build for this release would run here.\n  "
        + "\n  ".join(problems))


def _install_lc0(tag: str, *, progress=None) -> EngineSpec:
    releases = _releases(LC0_REPO, limit=3)
    release = next((r for r in releases if r.get("tag_name") == tag), None)
    if release is None:
        raise EngineError(f"No Lc0 release tagged {tag}.")
    asset = _lc0_asset(release)
    if asset is None:
        raise EngineError(
            "Lc0 publishes ready-made binaries for Windows only; on macOS and "
            "Linux it has to be built from source. Stockfish covers both.")

    home = ENGINE_DIR / _slug(f"lc0:{tag}")
    archive = home / asset["name"]
    if progress:
        progress(0, asset["size"], f"Downloading {asset['name']}")
    _download(asset["browser_download_url"], archive,
              progress=(lambda d, t: progress(d, t, None)) if progress else None)
    if progress:
        progress(asset["size"], asset["size"], "Unpacking")
    _extract(archive, home)
    archive.unlink(missing_ok=True)

    binary = _find_binary(home, "lc0")
    if binary is None:
        raise EngineError("The Lc0 archive contained no lc0 binary.")

    (home / ".binary").write_text(str(binary), encoding="utf-8")
    return EngineSpec(
        id=f"lc0:{tag}", name=f"Lc0 {tag}", kind="lc0", version=tag,
        path=str(binary), installed=True,
        note="Installed. Pick a network before using it.",
    )


def install_network(key: str, *, progress=None) -> str:
    """Fetch one Lc0 network file. Returns its path."""
    spec = LC0_NETWORKS.get(key)
    if spec is None:
        raise EngineError(f"Unknown network {key!r}.")
    target = ENGINE_DIR / "networks" / f"{key}.pb.gz"
    if target.is_file():
        return str(target)
    if progress:
        progress(0, spec["size"], f"Downloading {spec['name']}")
    _download(spec["url"], target,
              progress=(lambda d, t: progress(d, t, None)) if progress else None)
    return str(target)


def uninstall(spec_id: str) -> bool:
    """Delete a downloaded engine. Found-on-disk engines are never touched."""
    if spec_id.startswith("found:"):
        raise EngineError(
            "That engine was already on your machine; the app will not delete "
            "files it did not download.")
    home = ENGINE_DIR / _slug(spec_id)
    if not home.is_dir():
        return False
    shutil.rmtree(home, ignore_errors=True)
    return True


# ------------------------------------------------------------------ running


@dataclass
class EngineOptions:
    """Everything that changes what the engine says, in one hashable place."""

    threads: int = 2
    hash_mb: int = 256
    multipv: int = 3
    movetime: float = 0.3
    depth: int | None = None
    nodes: int | None = None
    weights: str | None = None

    def key(self) -> str:
        parts = [f"t{self.threads}", f"h{self.hash_mb}", f"pv{self.multipv}"]
        if self.depth:
            parts.append(f"d{self.depth}")
        elif self.nodes:
            parts.append(f"n{self.nodes}")
        else:
            parts.append(f"ms{int(self.movetime * 1000)}")
        if self.weights:
            parts.append(Path(self.weights).stem)
        return "-".join(parts)

    def limit(self) -> chess.engine.Limit:
        if self.depth:
            return chess.engine.Limit(depth=self.depth)
        if self.nodes:
            return chess.engine.Limit(nodes=self.nodes)
        return chess.engine.Limit(time=self.movetime)


class Engine:
    """One warm UCI process, with the lock that makes it safe to share."""

    def __init__(self, path: str, kind: str = "stockfish",
                 weights: str | None = None):
        self.path = path
        self.kind = kind
        self.weights = weights
        self.name = Path(path).name
        self._engine: chess.engine.SimpleEngine | None = None
        self._lock = threading.Lock()
        self._configured: str = ""

    def _start(self, options: EngineOptions) -> chess.engine.SimpleEngine:
        if self._engine is not None and self._configured == options.key():
            return self._engine
        if self._engine is None:
            try:
                self._engine = chess.engine.SimpleEngine.popen_uci(
                    self.path, timeout=30)
            except Exception as exc:                # noqa: BLE001
                raise EngineError(
                    f"{Path(self.path).name} would not start: {exc}") from exc
            self.name = self._engine.id.get("name", self.name)

        settings: dict = {}
        available = self._engine.options
        if "Threads" in available:
            settings["Threads"] = options.threads
        if "Hash" in available:
            settings["Hash"] = options.hash_mb
        if self.kind == "lc0" and options.weights and "WeightsFile" in available:
            settings["WeightsFile"] = options.weights
        try:
            if settings:
                self._engine.configure(settings)
        except Exception as exc:                    # noqa: BLE001
            raise EngineError(f"Engine rejected its settings: {exc}") from exc

        self._configured = options.key()
        return self._engine

    def reset(self) -> None:
        engine, self._engine = self._engine, None
        self._configured = ""
        if engine is not None:
            try:
                engine.quit()
            except Exception:                       # noqa: BLE001
                pass

    def analyse(self, board: chess.Board, options: EngineOptions) -> list[dict]:
        """Analyse one position. Returns one entry per requested variation.

        Scores come back in **White's point of view** so that everything
        downstream -- the eval bar, the review, the accuracy maths -- shares
        one convention and never has to ask whose turn it was.
        """
        if board.is_game_over():
            return [_terminal(board)]

        multipv = max(1, min(options.multipv, board.legal_moves.count()))
        with self._lock:
            engine = self._start(options)
            try:
                infos = engine.analyse(board, options.limit(), multipv=multipv)
            except Exception as exc:                # noqa: BLE001
                # A crashed engine must not poison every later request.
                self.reset()
                raise EngineError(f"Analysis failed: {exc}") from exc

        if isinstance(infos, dict):
            infos = [infos]

        lines = []
        for rank, info in enumerate(infos, start=1):
            score = info.get("score")
            if score is None:
                continue
            white = score.white()
            lines.append({
                "rank": rank,
                "cp": white.score(),
                "mate": white.mate(),
                "depth": int(info.get("depth", 0) or 0),
                "nodes": int(info.get("nodes", 0) or 0),
                "pv": [m.uci() for m in (info.get("pv") or [])],
            })
        if not lines:
            raise EngineError("The engine returned no evaluation.")
        return lines

    def quit(self) -> None:
        self.reset()


def _terminal(board: chess.Board) -> dict:
    """A finished game has an exact score, and no engine is needed to say it."""
    outcome = board.outcome(claim_draw=True)
    if outcome is not None and outcome.winner is not None:
        # A delivered mate is signed, not zero: "mate in 0" cannot say *who*
        # was mated, and every consumer of this needs to know. +1/-1 reads as
        # a won position for White/Black, which is exactly what it is.
        return {"rank": 1, "cp": None,
                "mate": 1 if outcome.winner == chess.WHITE else -1,
                "depth": 0, "nodes": 0, "pv": [],
                "terminal": "checkmate" if board.is_checkmate() else "over",
                "winner": "white" if outcome.winner == chess.WHITE else "black"}
    return {"rank": 1, "cp": 0, "mate": None, "depth": 0, "nodes": 0, "pv": [],
            "terminal": "draw", "winner": None}


class EnginePool:
    """The app's engines, started once and shared."""

    def __init__(self):
        self._engines: dict[str, Engine] = {}
        self._lock = threading.Lock()

    def resolve(self, spec_id: str | None) -> str:
        """Turn an id into a binary path, defaulting to whatever is here."""
        if spec_id and spec_id.startswith("found:"):
            path = spec_id.split(":", 1)[1]
            if Path(path).is_file():
                return path
            raise EngineError(f"That engine is gone from {path}.")
        if spec_id:
            path = _installed_path(spec_id)
            if path:
                return path
            raise EngineError(
                f"{spec_id} is not installed yet. Install it from the engine "
                "picker, or pick one you already have.")

        local = discover()
        if not local:
            raise EngineError(
                "No engine found. Download one from the engine picker, or put "
                "a Stockfish binary in Lichess-Study-to-PDF/engine/.")
        return local[0].path

    def get(self, spec_id: str | None = None,
            weights: str | None = None) -> Engine:
        path = self.resolve(spec_id)
        kind = "lc0" if "lc0" in Path(path).name.lower() else "stockfish"
        key = f"{path}|{weights or ''}"
        with self._lock:
            engine = self._engines.get(key)
            if engine is None:
                engine = Engine(path, kind=kind, weights=weights)
                self._engines[key] = engine
            return engine

    def close(self) -> None:
        with self._lock:
            for engine in self._engines.values():
                engine.quit()
            self._engines.clear()


#: One pool for the process.
#:
#: **Every entry point must call ``POOL.close()`` before it exits.**  This is
#: not tidiness, it is a hang: python-chess runs each engine's event loop on a
#: *non-daemon* thread (``chess.engine.run_in_background``), and CPython joins
#: non-daemon threads before it runs ``atexit`` handlers.  A program that
#: leaves an engine open therefore prints its last line and then blocks for
#: ever, with no output and no traceback to explain it.
#:
#: An ``atexit`` hook was tried here and does not fix it, for exactly that
#: ordering reason. The server closes the pool in its shutdown handler and the
#: CLI closes it in a ``finally``; anything else that opens an engine has to do
#: the same.
POOL = EnginePool()


def close() -> None:
    """Shut every engine down. Call this before the process exits."""
    POOL.close()


__all__ = [
    "ENGINE_DIR",
    "close",
    "LC0_NETWORKS",
    "POOL",
    "Engine",
    "EngineError",
    "EngineOptions",
    "EnginePool",
    "EngineSpec",
    "catalog",
    "discover",
    "install",
    "install_network",
    "probe",
    "uninstall",
]
