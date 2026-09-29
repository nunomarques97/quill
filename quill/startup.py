"""Start Quill with Windows: one value in the current user's Run key.

``python -m quill --install-startup`` writes the value ``Quill`` under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``:

    "<repo>\\.venv\\Scripts\\pythonw.exe" "<repo>\\quill\\__main__.py"

The repository path is resolved at runtime from this file, so the entry
follows the folder the command was run from; ``pythonw`` opens no console
window. ``--remove-startup`` deletes the value. These are manual commands:
nothing else writes the registry, and nothing else in it is read or written.
Only the current user's key is used, so no administrator rights are needed.

``WinRegistry`` is the only code that touches the registry (``winreg``);
tests pass a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from quill.config import REPO_ROOT

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Quill"
VENV_DIR = REPO_ROOT / ".venv"
ENTRY_SCRIPT = REPO_ROOT / "quill" / "__main__.py"

INSTALLED = "installed"
ABSENT = "absent"
OTHER = "other"
STATUS_TEXT = {
    INSTALLED: "starts with Windows",
    ABSENT: "does not start with Windows (--install-startup adds it)",
    OTHER: "a Run entry named Quill points elsewhere (--install-startup replaces it)",
}


class StartupError(OSError):
    """The Run entry cannot be written for this repository."""


def venv_pythonw(venv: Path = VENV_DIR) -> Path:
    return Path(venv) / "Scripts" / "pythonw.exe"


def launch_command(repo_root: Path = REPO_ROOT) -> str:
    """The command line of the Run entry, both paths quoted."""
    root = Path(repo_root).resolve()
    pythonw = venv_pythonw(root / ".venv")
    script = root / "quill" / "__main__.py"
    if '"' in str(root):
        raise StartupError("the repository path contains a double quote")
    if not pythonw.is_file():
        raise StartupError(".venv\\Scripts\\pythonw.exe not found: create the .venv first")
    if not script.is_file():
        raise StartupError("quill\\__main__.py not found in the repository")
    return f'"{pythonw}" "{script}"'


class WinRegistry:
    """The ``Quill`` value of the current user's Run key, through ``winreg``."""

    def __init__(self) -> None:
        import winreg

        self._winreg = winreg

    def read(self, name: str) -> str | None:
        winreg = self._winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
                value, kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) and kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else ""

    def write(self, name: str, value: str) -> None:
        winreg = self._winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> bool:
        winreg = self._winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return False
        return True


def _registry(registry: object | None) -> object:
    return WinRegistry() if registry is None else registry


def status(registry: object | None = None, repo_root: Path = REPO_ROOT) -> str:
    current = _registry(registry).read(VALUE_NAME)
    if current is None:
        return ABSENT
    try:
        expected = launch_command(repo_root)
    except StartupError:
        return OTHER
    return INSTALLED if current == expected else OTHER


def install(registry: object | None = None, repo_root: Path = REPO_ROOT) -> str:
    """Write the Run entry; returns the previous status (``installed``, ``absent`` or ``other``)."""
    registry = _registry(registry)
    command = launch_command(repo_root)
    previous = status(registry, repo_root)
    if previous != INSTALLED:
        registry.write(VALUE_NAME, command)
    return previous


def remove(registry: object | None = None) -> bool:
    """Delete the Run entry; False when there was none."""
    return _registry(registry).delete(VALUE_NAME)


def install_command(registry: object | None = None, repo_root: Path = REPO_ROOT,
                    out: Callable[[str], None] = print) -> int:
    try:
        previous = install(registry, repo_root)
    except OSError as exc:
        out(f"Start with Windows not set: {exc}")
        return 1
    if previous == INSTALLED:
        out("Quill already starts with Windows (nothing changed).")
    elif previous == OTHER:
        out("Quill will start with Windows from this folder (the previous entry was replaced).")
    else:
        out("Quill will start with Windows (HKCU Run entry 'Quill'). Remove it with --remove-startup.")
    return 0


def remove_command(registry: object | None = None, out: Callable[[str], None] = print) -> int:
    try:
        removed = remove(registry)
    except OSError as exc:
        out(f"Start with Windows not removed ({type(exc).__name__}).")
        return 1
    out("Quill no longer starts with Windows." if removed else "Quill was not set to start with Windows.")
    return 0
