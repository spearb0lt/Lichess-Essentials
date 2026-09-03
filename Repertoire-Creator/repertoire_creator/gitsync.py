"""Committing and pushing the repertoire folder on its own.

Every edit already lands on disk immediately.  This adds the other half of
"it just saves": the disk state gets committed to git and pushed, so the
repertoire survives the machine, not just the process.

Three rules keep it from being dangerous.

*It only ever touches the repertoire folder.*  Commits are made with an
explicit pathspec (``git commit -- <data dir>``), so whatever else you have
staged or in flight elsewhere in the repository is left exactly as it was.
Automatic tooling that runs ``git add -A`` on someone else's working tree is
a menace.

*It never blocks an edit.*  Saves schedule a commit on a debounce timer and
return; the git work happens on a worker thread.  A burst of twenty moves is
one commit, not twenty.

*It never hangs waiting for a human.*  Git is run with terminal prompts
disabled and a hard timeout, so a missing credential fails fast and visibly
rather than wedging a thread forever.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Quiet period after the last save before a commit is made.
DEFAULT_DEBOUNCE = 20.0
#: Hard limit on any single git invocation.
COMMIT_TIMEOUT = 60
PUSH_TIMEOUT = 180


@dataclass
class GitSettings:
    enabled: bool = True
    push: bool = True
    remote: str = "origin"
    branch: str | None = None          # None = whatever branch is checked out
    debounce_seconds: float = DEFAULT_DEBOUNCE

    @classmethod
    def from_json(cls, data: dict | None) -> "GitSettings":
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            push=bool(data.get("push", True)),
            remote=str(data.get("remote") or "origin"),
            branch=data.get("branch") or None,
            debounce_seconds=float(data.get("debounceSeconds", DEFAULT_DEBOUNCE)),
        )

    def to_json(self) -> dict:
        return {
            "enabled": self.enabled,
            "push": self.push,
            "remote": self.remote,
            "branch": self.branch,
            "debounceSeconds": self.debounce_seconds,
        }


@dataclass
class GitState:
    """What happened last, for the status indicator."""

    running: bool = False
    pending: bool = False
    last_action: str = ""              # committed | pushed | nothing | failed
    last_message: str = ""
    last_error: str = ""
    last_at: float = 0.0
    commits: int = 0
    detail: dict = field(default_factory=dict)


def _git_env() -> dict:
    env = dict(os.environ)
    # Never let git stop to ask for a password: fail fast instead.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "echo")
    env.setdefault("SSH_ASKPASS", "echo")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def run_git(args, cwd: Path, timeout: int = COMMIT_TIMEOUT):
    """Run one git command. Returns ``(returncode, stdout, stderr)``."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "git is not installed, or not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {timeout}s"
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def repo_root(path: Path) -> Path | None:
    code, out, _ = run_git(["rev-parse", "--show-toplevel"], path, timeout=15)
    if code != 0 or not out:
        return None
    return Path(out)


class GitSync:
    """Debounced commit-and-push of one folder."""

    def __init__(self, data_dir: Path, settings: GitSettings | None = None):
        self.data_dir = Path(data_dir)
        self.settings = settings or GitSettings()
        self.state = GitState()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._messages: list[str] = []
        self._root: Path | None = None
        self._root_checked = False

    # ------------------------------------------------------------ discovery

    @property
    def root(self) -> Path | None:
        if not self._root_checked:
            base = self.data_dir if self.data_dir.exists() else self.data_dir.parent
            self._root = repo_root(base) if base.exists() else None
            self._root_checked = True
        return self._root

    def current_branch(self) -> str | None:
        if self.root is None:
            return None
        code, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], self.root, 15)
        return out if code == 0 and out and out != "HEAD" else None

    def has_remote(self) -> bool:
        if self.root is None:
            return False
        code, out, _ = run_git(["remote"], self.root, 15)
        return code == 0 and self.settings.remote in out.split()

    # -------------------------------------------------------------- trigger

    def note(self, message: str) -> None:
        """Record that something was saved, and arm the debounce timer."""
        if not self.settings.enabled or self.root is None:
            return
        with self._lock:
            if message and message not in self._messages:
                self._messages.append(message)
            self.state.pending = True
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                max(1.0, self.settings.debounce_seconds), self._fire
            )
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> dict:
        """Commit now rather than waiting for the timer. Blocking."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        return self._commit_and_push()

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _fire(self) -> None:
        thread = threading.Thread(target=self._commit_and_push, daemon=True)
        thread.start()

    # ---------------------------------------------------------------- work

    def _take_message(self) -> str:
        with self._lock:
            messages, self._messages = self._messages, []
            self.state.pending = False
        if not messages:
            return "repertoire: save"
        if len(messages) == 1:
            return f"repertoire: {messages[0]}"
        head = ", ".join(messages[:3])
        extra = f" and {len(messages) - 3} more" if len(messages) > 3 else ""
        return f"repertoire: {head}{extra}"

    def _commit_and_push(self) -> dict:
        root = self.root
        if root is None:
            return self._finish("failed", error=(
                f"{self.data_dir} is not inside a git repository, so there is "
                "nothing to commit to. Run git init, or turn auto-commit off."
            ))

        message = self._take_message()
        with self._lock:
            if self.state.running:
                # Another commit is in flight; the next save will pick this up.
                self.state.pending = True
                return {"action": "busy"}
            self.state.running = True

        try:
            relative = self._pathspec(root)
            code, _, err = run_git(["add", "--", relative], root)
            if code != 0:
                return self._finish("failed", error=f"git add: {err}")

            code, out, err = run_git(
                ["commit", "-m", message, "--", relative], root
            )
            if code != 0:
                blob = f"{out}\n{err}".lower()
                if "nothing to commit" in blob or "no changes added" in blob:
                    return self._finish("nothing", message=message)
                return self._finish("failed", error=f"git commit: {err or out}")

            self.state.commits += 1
            if not self.settings.push:
                return self._finish("committed", message=message)

            if not self.has_remote():
                return self._finish("committed", message=message, error=(
                    f"Committed locally. There is no remote called "
                    f"{self.settings.remote!r} to push to."
                ))

            branch = self.settings.branch or self.current_branch()
            if not branch:
                return self._finish("committed", message=message, error=(
                    "Committed locally, but HEAD is detached so there is no "
                    "branch to push."
                ))

            code, out, err = run_git(
                ["push", self.settings.remote, f"HEAD:{branch}"],
                root, timeout=PUSH_TIMEOUT,
            )
            if code != 0:
                return self._finish("committed", message=message, error=(
                    f"Committed locally, but the push failed: {err or out}"
                ))
            return self._finish("pushed", message=message)
        finally:
            with self._lock:
                self.state.running = False

    def _pathspec(self, root: Path) -> str:
        """The data folder, relative to the repository root when it is inside it."""
        try:
            return str(self.data_dir.resolve().relative_to(root.resolve()))
        except ValueError:
            # Data folder outside the repo: commit it by absolute path and let
            # git complain if that makes no sense.
            return str(self.data_dir.resolve())

    def _finish(self, action: str, *, message: str = "", error: str = "") -> dict:
        self.state.last_action = action
        self.state.last_message = message
        self.state.last_error = error
        self.state.last_at = time.time()
        return self.status()

    # -------------------------------------------------------------- status

    def status(self) -> dict:
        root = self.root
        return {
            "enabled": self.settings.enabled,
            "push": self.settings.push,
            "remote": self.settings.remote,
            "branch": self.settings.branch or self.current_branch(),
            "debounceSeconds": self.settings.debounce_seconds,
            "repo": str(root) if root else None,
            "inRepo": root is not None,
            "hasRemote": self.has_remote() if root else False,
            "pending": self.state.pending,
            "running": self.state.running,
            "lastAction": self.state.last_action,
            "lastMessage": self.state.last_message,
            "lastError": self.state.last_error,
            "lastAt": self.state.last_at,
            "commits": self.state.commits,
        }
