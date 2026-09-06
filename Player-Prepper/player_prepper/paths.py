"""Where this app keeps its files, in a checkout and after ``pip install``.

There are two quite different situations, and the difference is not one the
rest of the app should have to think about.

**Running from a source tree** -- a git checkout, an editable install
(``pip install -e``), or the ``COPY`` inside this repo's Docker images -- the
package sits next to the project's own ``pyproject.toml``, and files belong
beside it: ``ChessAnalyzer/games``, ``Weakness-Report/history``, and so on.
That is what the READMEs describe, what the Docker volumes are mounted at,
and it stays exactly that way.

**Installed as a wheel from PyPI** the package lands in ``site-packages``,
where there is no project folder and nothing of the user's belongs. Writing
there would scatter games and reports through a Python installation and lose
them on the next upgrade, so files go to the normal per-user data directory
for the platform instead.

``pyproject.toml`` next to the package is what separates the two: it is
present in every source tree and absent from every wheel install. Nothing
here inspects how the app was launched, which is why the answer stays the
same whether it was started by the CLI, by uvicorn, or by a test.

An explicit environment variable always wins over both.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Name of the folder this app owns under the per-user data directory.
APP_NAME = "player-prepper"


def source_root(package_file: str | os.PathLike) -> Path | None:
    """The project folder above the package, or None in a wheel install.

    ``package_file`` is the ``__file__`` of any module in the package.
    """
    root = Path(package_file).resolve().parent.parent
    return root if (root / "pyproject.toml").is_file() else None


def data_home(app_name: str | None = None) -> Path:
    """A per-user data folder: this app's, or a named sibling app's.

    ``%LOCALAPPDATA%`` on Windows, ``~/Library/Application Support`` on macOS
    and ``$XDG_DATA_HOME`` (else ``~/.local/share``) elsewhere.

    Passing ``app_name`` is how one app finds another's files when both were
    installed from PyPI and there is no repository holding them side by side.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / (app_name or APP_NAME)


def resolve(package_file: str | os.PathLike, name: str,
            env_var: str | None = None) -> Path:
    """Where ``name`` lives: ``$env_var``, else beside the package, else per-user.

    The three cases in order of priority, so that pointing an environment
    variable at a folder moves it everywhere at once -- which is how the
    Docker Compose file gives Player Prepper the shared repertoires volume.
    """
    if env_var:
        override = os.environ.get(env_var)
        if override:
            return Path(override).expanduser().resolve()
    root = source_root(package_file)
    if root is not None:
        return (root / name).resolve()
    return (data_home() / name).resolve()


__all__ = ["APP_NAME", "source_root", "data_home", "resolve"]
