"""Position evaluation: Lichess cloud first, local Stockfish as the fallback.

The cloud endpoint only answers for positions already in Lichess's analysis
cache, which in practice means popular openings.  Anything deeper than a few
moves into a personal repertoire returns 404, so a local engine is what
actually gives a study full coverage.

It is also firmly rate limited, and bulk-querying a few hundred positions will
get you a 429 that persists for a while.  So we only ask the cloud about
positions where it plausibly has an answer (early moves, see
``cloud_max_fullmove``), pace those requests about a second apart, and hand
everything else straight to the engine.

Sign convention: every score in this module is **from White's point of view**,
matching the cloud endpoint (verified against known positions) and
``PovScore.white()`` from python-chess.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path

import chess
import chess.engine
import requests

CLOUD_URL = "https://lichess.org/api/cloud-eval"

#: Lichess's own centipawn -> winning-chances curve.
_WIN_K = -0.00368208


@dataclass
class Eval:
    """A White-POV evaluation of a single position."""

    cp: int | None = None
    mate: int | None = None
    depth: int = 0
    source: str = ""          # "cloud", "local" or "cache"
    best_move: str | None = None

    @property
    def known(self) -> bool:
        return self.cp is not None or self.mate is not None

    def text(self) -> str:
        """Short label such as ``+1.24``, ``-0.30`` or ``M5``."""
        if self.mate is not None:
            sign = "+" if self.mate > 0 else "-"
            return f"{sign}M{abs(self.mate)}"
        if self.cp is None:
            return ""
        return f"{self.cp / 100:+.2f}"

    def white_fraction(self) -> float:
        """How much of the eval bar White fills, in ``[0, 1]``."""
        if self.mate is not None:
            return 1.0 if self.mate > 0 else 0.0
        if self.cp is None:
            return 0.5
        capped = max(-1500, min(1500, self.cp))
        chances = 2.0 / (1.0 + math.exp(_WIN_K * capped)) - 1.0
        return max(0.0, min(1.0, (chances + 1.0) / 2.0))


def find_stockfish(explicit: str | None = None) -> str | None:
    """Locate a Stockfish binary: explicit path, env var, bundled dir, PATH."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("STOCKFISH_PATH")
    if env:
        candidates.append(env)

    engine_dir = Path(__file__).resolve().parent.parent / "engine"
    if engine_dir.is_dir():
        for path in sorted(engine_dir.rglob("stockfish*")):
            if path.is_file() and path.suffix.lower() in (".exe", ""):
                candidates.append(str(path))

    for name in ("stockfish", "stockfish.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


class EvalProvider:
    """Evaluates positions with an on-disk cache in front of both backends."""

    def __init__(
        self,
        cache_path: Path | str | None = None,
        *,
        use_cloud: bool = True,
        stockfish_path: str | None = None,
        movetime: float = 0.25,
        depth: int | None = None,
        threads: int = 2,
        hash_mb: int = 128,
        cloud_workers: int = 1,
        cloud_interval: float = 0.9,
        cloud_max_fullmove: int = 20,
    ):
        self.cache_path = Path(cache_path) if cache_path else None
        self.use_cloud = use_cloud
        self.stockfish_path = stockfish_path
        self.movetime = movetime
        self.depth = depth
        self.threads = threads
        self.hash_mb = hash_mb
        self.cloud_workers = max(1, cloud_workers)

        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._engine: chess.engine.SimpleEngine | None = None
        self._cloud_disabled = False
        # Lichess rate limits the cloud endpoint; pace ourselves rather than
        # firing a few hundred requests as fast as the pool allows.
        self._cloud_interval = max(0.0, cloud_interval)
        #: Past this full-move number the cloud almost never has an entry, so
        #: asking is just burning the rate limit.
        self.cloud_max_fullmove = cloud_max_fullmove
        self._cloud_last = 0.0
        self._cloud_429 = 0
        #: Wall-clock point after which the cloud may be tried again. We never
        #: sleep on a 429 -- a blocking back-off would freeze the live eval bar
        #: for a minute -- we just stop asking until this passes.
        self._cloud_blocked_until = 0.0
        self._rate_lock = threading.Lock()
        # A UCI engine is a single conversation: only one analyse() at a time.
        self._engine_lock = threading.Lock()
        self.stats = {"cache": 0, "cloud": 0, "local": 0, "missing": 0}
        #: Set when Lichess rate limited us, so callers can say so out loud.
        self.cloud_rate_limited = False

        self._load_cache()

    # ------------------------------------------------------------------ cache

    def _cloud_key(self, fen: str) -> str:
        # A cloud answer does not depend on our engine settings, so it is
        # cached under a settings-free key and reused by every run.
        return f"{fen}|cloud"

    def _engine_key(self, fen: str) -> str:
        # Engine settings do change the answer, so they belong in the key.
        if self.depth:
            return f"{fen}|d{self.depth}"
        return f"{fen}|t{self.movetime}"

    def _lookup(self, fen: str):
        """Return a cached Eval for this position, cloud result preferred."""
        with self._lock:
            for key in (self._cloud_key(fen), self._engine_key(fen)):
                hit = self._cache.get(key)
                if hit is not None:
                    return Eval(**hit)
        return None

    def _store(self, fen: str, value: "Eval") -> None:
        key = (self._cloud_key(fen) if value.source == "cloud"
               else self._engine_key(fen))
        with self._lock:
            self._cache[key] = asdict(value)

    def _throttle(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            wait = self._cloud_last + self._cloud_interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._cloud_last = now

    def _load_cache(self) -> None:
        if self.cache_path and self.cache_path.is_file():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}

    def save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, separators=(",", ":")), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------- backends

    def _cloud_worth_asking(self, fen: str) -> bool:
        """Cheap filter: only early positions are plausibly in the cloud."""
        if self.cloud_max_fullmove <= 0:
            return True
        try:
            fullmove = int(fen.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            return True
        return fullmove <= self.cloud_max_fullmove

    def _from_cloud(self, fen: str) -> Eval | None:
        """One cloud lookup, paced and with a bounded back-off on 429."""
        if not self._cloud_worth_asking(fen):
            return None
        for attempt in range(2):
            if not self.use_cloud or self._cloud_disabled:
                return None
            if time.monotonic() < self._cloud_blocked_until:
                return None
            self._throttle()
            try:
                response = requests.get(
                    CLOUD_URL, params={"fen": fen, "multiPv": 1}, timeout=12
                )
            except requests.RequestException:
                return None

            if response.status_code == 429:
                # Lichess wants a minute of quiet. Record that and bail out
                # immediately -- the caller falls through to the engine, which
                # is both faster and always available.
                try:
                    delay = float(response.headers.get("Retry-After", 60))
                except (TypeError, ValueError):
                    delay = 60.0
                with self._rate_lock:
                    self._cloud_429 += 1
                    self._cloud_blocked_until = (
                        time.monotonic() + min(120.0, max(5.0, delay))
                    )
                    if self._cloud_429 > 8:
                        self._cloud_disabled = True
                self.cloud_rate_limited = True
                return None

            if response.status_code != 200:
                return None

            try:
                data = response.json()
                pv = data["pvs"][0]
            except (ValueError, KeyError, IndexError):
                return None

            moves = pv.get("moves", "")
            return Eval(
                cp=pv.get("cp"),
                mate=pv.get("mate"),
                depth=int(data.get("depth", 0) or 0),
                source="cloud",
                best_move=moves.split()[0] if moves else None,
            )
        return None

    def _reset_engine(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass

    def _ensure_engine(self) -> chess.engine.SimpleEngine | None:
        if self._engine is not None:
            return self._engine
        path = find_stockfish(self.stockfish_path)
        if not path:
            return None
        try:
            engine = chess.engine.SimpleEngine.popen_uci(path)
            engine.configure({"Threads": self.threads, "Hash": self.hash_mb})
        except Exception:
            return None
        self._engine = engine
        return engine

    def _from_engine(self, fen: str, movetime: float | None = None,
                     depth: int | None = None) -> Eval | None:
        engine = self._ensure_engine()
        if engine is None:
            return None
        try:
            board = chess.Board(fen)
        except ValueError:
            return None
        if board.is_game_over():
            outcome = board.outcome()
            if outcome and outcome.winner is not None:
                return Eval(mate=0 if outcome.winner else 0, cp=None, depth=0,
                            source="local")
            return Eval(cp=0, depth=0, source="local")

        use_depth = depth if depth is not None else self.depth
        use_time = movetime if movetime is not None else self.movetime
        limit = (chess.engine.Limit(depth=use_depth) if use_depth
                 else chess.engine.Limit(time=use_time))
        try:
            with self._engine_lock:
                info = engine.analyse(board, limit)
        except Exception:
            # A crashed engine should not poison every later request.
            self._reset_engine()
            return None

        score = info.get("score")
        if score is None:
            return None
        white = score.white()
        pv = info.get("pv") or []
        return Eval(
            cp=white.score(),
            mate=white.mate(),
            depth=int(info.get("depth", 0) or 0),
            source="local",
            best_move=pv[0].uci() if pv else None,
        )

    # -------------------------------------------------------------- public

    def evaluate(self, fen: str) -> Eval:
        hit = self._lookup(fen)
        if hit is not None:
            self.stats["cache"] += 1
            return hit

        result = self._from_cloud(fen) or self._from_engine(fen)
        if result is None:
            self.stats["missing"] += 1
            return Eval()

        self.stats[result.source] = self.stats.get(result.source, 0) + 1
        self._store(fen, result)
        return result

    def evaluate_many(self, fens, progress=None) -> dict:
        """Evaluate a list of FENs, reusing the cache and parallelising cloud hits."""
        unique = list(dict.fromkeys(fens))
        results: dict[str, Eval] = {}
        pending = []

        for fen in unique:
            hit = self._lookup(fen)
            if hit is not None:
                self.stats["cache"] += 1
                results[fen] = hit
            else:
                pending.append(fen)

        done = len(results)
        total = len(unique)
        if progress:
            progress(done, total)

        # Cloud lookups are network-bound, so a small pool helps a lot.
        if pending and self.use_cloud and not self._cloud_disabled:
            with ThreadPoolExecutor(max_workers=self.cloud_workers) as pool:
                for fen, value in zip(pending, pool.map(self._from_cloud, pending)):
                    if value is not None and value.known:
                        results[fen] = value
                        self.stats["cloud"] += 1
                        self._store(fen, value)

        # Whatever the cloud did not know, the engine answers one at a time.
        for fen in pending:
            if fen in results:
                continue
            value = self._from_engine(fen)
            if value is not None and value.known:
                results[fen] = value
                self.stats["local"] += 1
                self._store(fen, value)
            else:
                results[fen] = Eval()
                self.stats["missing"] += 1
            done += 1
            if progress and done % 5 == 0:
                progress(done, total)

        if progress:
            progress(total, total)
        self.save_cache()
        return results

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None
        self.save_cache()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
