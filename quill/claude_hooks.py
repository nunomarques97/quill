"""Install, show or remove the Claude Code hooks of Quill's attention alert.

Usage (from the repository folder):
    py -3.12 -m quill.claude_hooks --show       print the exact change; writes nothing
    py -3.12 -m quill.claude_hooks --install    print the change, then write it after you type "yes"
    py -3.12 -m quill.claude_hooks --remove     print the removal of Quill's entries, then write it after "yes"

The change goes to the user-level Claude Code settings
(``%USERPROFILE%\\.claude\\settings.json``, or ``settings.json`` in
``CLAUDE_CONFIG_DIR`` when that is set; ``--settings PATH`` names another
file). It adds two hook groups, both in exec form (an executable and its
arguments, no shell), with the ``.venv`` pythonw and the notifier script as
absolute paths resolved when the command runs, and a short timeout:

- ``Stop``: Claude Code finished its reply and waits for you;
- ``Notification`` with the matcher ``permission_prompt``: Claude Code asks
  for a permission (other notifications are not matched).

Every other setting and hook is kept. Installing twice changes nothing; after
moving the repository folder, ``--install`` replaces the old Quill entries.
``--remove`` deletes only Quill's entries (hooks that run ``quill\\notify.py``
from any folder). Before a write the previous file is copied next to it
(``settings.json.bak-quill-<time>``) and the new file replaces it atomically;
a file changed meanwhile is not overwritten. A settings file that cannot be
read, is not valid JSON or has an unexpected ``hooks`` layout is left
untouched and the command exits 1 with an English error. Nothing else writes
the Claude Code settings; tests use temporary files only.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from quill.config import REPO_ROOT
from quill.startup import venv_pythonw

NOTIFY_SCRIPT = REPO_ROOT / "quill" / "notify.py"
HOOK_TIMEOUT_S = 5
PERMISSION_MATCHER = "permission_prompt"
# Claude Code hook event -> (matcher or None, notifier argument).
ENTRIES = {"Stop": (None, "stop"), "Notification": (PERMISSION_MATCHER, "permission")}
NOTIFY_ARGUMENTS = frozenset(argument for _, argument in ENTRIES.values())
CONFIRM_WORD = "yes"
RETRY_S = 0.5
RETRY_FIRST_S = 0.005
RETRY_MAX_S = 0.05

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


class HooksError(Exception):
    """The settings cannot be changed safely; the file is left untouched."""


# ---------------------------------------------------------------- the entries


def user_settings_path(environ: dict[str, str] | None = None) -> Path:
    """The user-level Claude Code settings file."""
    environ = os.environ if environ is None else environ
    folder = environ.get("CLAUDE_CONFIG_DIR")
    return (Path(folder) if folder else Path.home() / ".claude") / "settings.json"


def hook_command(repo_root: Path = REPO_ROOT) -> tuple[str, str]:
    """(pythonw, notifier script): absolute paths, checked to exist."""
    root = Path(repo_root).resolve()
    pythonw = venv_pythonw(root / ".venv")
    script = root / "quill" / "notify.py"
    if not pythonw.is_file():
        raise HooksError(".venv\\Scripts\\pythonw.exe not found: create the .venv first")
    if not script.is_file():
        raise HooksError("quill\\notify.py not found in the repository")
    return str(pythonw), str(script)


def quill_groups(pythonw: str, script: str) -> dict[str, dict]:
    """Claude Code hook event -> the matcher group Quill adds to it."""
    groups = {}
    for event, (matcher, argument) in ENTRIES.items():
        group: dict[str, object] = {} if matcher is None else {"matcher": matcher}
        group["hooks"] = [{"type": "command", "command": pythonw, "args": [script, argument],
                           "timeout": HOOK_TIMEOUT_S}]
        groups[event] = group
    return groups


def is_quill_hook(hook: object) -> bool:
    """A hook that runs Quill's notifier, from this or any other repository folder."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    args = hook.get("args")
    if not isinstance(args, list) or len(args) != 2 or not all(isinstance(item, str) for item in args):
        return False
    parts = [part.lower() for part in Path(args[0].replace("/", "\\")).parts[-2:]]
    return parts == ["quill", "notify.py"] and args[1] in NOTIFY_ARGUMENTS


def _hook_lists(settings: dict) -> dict[str, list]:
    """The ``hooks`` table, checked: event -> list of matcher groups, each with a ``hooks`` list."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise HooksError("the settings field 'hooks' is not an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise HooksError(f"the settings field 'hooks.{event}' is not a list")
        for index, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise HooksError(f"the settings field 'hooks.{event}[{index}]' has no 'hooks' list")
    return hooks


def without_quill(settings: dict) -> dict:
    """A copy of ``settings`` without Quill's hooks; groups and events left empty by that are dropped."""
    result = copy.deepcopy(settings)
    hooks = _hook_lists(result)
    for event in list(hooks):
        kept_groups = []
        for group in hooks[event]:
            kept = [hook for hook in group["hooks"] if not is_quill_hook(hook)]
            if len(kept) == len(group["hooks"]):
                kept_groups.append(group)
            elif kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        elif len(kept_groups) != len(hooks[event]):
            del hooks[event]
    if "hooks" in result and not hooks and settings.get("hooks"):
        del result["hooks"]
    return result


def installed(settings: dict, pythonw: str, script: str) -> bool:
    """Whether the settings hold exactly Quill's current hooks, each in its own group, and no other Quill hook."""
    wanted = quill_groups(pythonw, script)
    found: dict[str, list] = {}
    for event, groups in _hook_lists(settings).items():
        for group in groups:
            if any(is_quill_hook(hook) for hook in group["hooks"]):
                found.setdefault(event, []).append(group)
    return found == {event: [group] for event, group in wanted.items()}


def with_quill(settings: dict, pythonw: str, script: str) -> dict:
    """``settings`` with exactly Quill's current hooks (added after every other hook when missing)."""
    if installed(settings, pythonw, script):
        return copy.deepcopy(settings)
    result = without_quill(settings)
    hooks = result.setdefault("hooks", {})
    for event, group in quill_groups(pythonw, script).items():
        hooks.setdefault(event, []).append(group)
    return result


# ---------------------------------------------------------------- the file


def _retry(action: Callable[[], object], sleep: Callable[[float], None] = time.sleep,
           monotonic: Callable[[], float] = time.monotonic) -> object:
    """``action()``, retried while Windows reports a sharing violation, for up to ``RETRY_S``."""
    deadline = monotonic() + RETRY_S
    delay = RETRY_FIRST_S
    while True:
        try:
            return action()
        except PermissionError:
            if monotonic() + delay > deadline:
                raise
        sleep(delay)
        delay = min(delay * 2, RETRY_MAX_S)


def read_settings(path: Path) -> tuple[bytes | None, dict]:
    """(the file's bytes, its JSON object); (None, {}) when the file does not exist."""
    try:
        raw = _retry(path.read_bytes)
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise HooksError(f"the Claude Code settings file cannot be read ({type(exc).__name__})") from None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise HooksError(f"the Claude Code settings file is not valid JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise HooksError("the Claude Code settings file does not hold a JSON object")
    return raw, data


def render(settings: dict) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def describe_change(path: Path, raw: bytes | None, new_text: str) -> str:
    """A unified diff from the file as it is to the file as it would be written."""
    old_text = "" if raw is None else raw.decode("utf-8-sig")
    lines = difflib.unified_diff(old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                                 fromfile=f"{path} (now)" if raw is not None else f"{path} (does not exist)",
                                 tofile=f"{path} (after)")
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def backup(path: Path, raw: bytes, clock: Callable[[], float] = time.time) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    for attempt in range(100):
        target = path.with_name(path.name + f".bak-quill-{stamp}" + (f"-{attempt}" if attempt else ""))
        try:
            with target.open("xb") as stream:
                stream.write(raw)
            return target
        except FileExistsError:
            continue
    raise OSError("no free backup name")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".settings-quill-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _retry(partial(os.replace, temporary, path))
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def apply(path: Path, raw: bytes | None, new_text: str, clock: Callable[[], float] = time.time) -> Path | None:
    """Write ``new_text`` over the file read as ``raw``; returns the backup (None: there was no file).

    The file is read again first: when it changed since ``raw`` it is not
    overwritten (``HooksError``).
    """
    current, _ = read_settings(path)
    if current != raw:
        raise HooksError("the Claude Code settings file changed meanwhile; run the command again")
    saved = None
    try:
        if raw is not None:
            saved = backup(path, raw, clock)
        write_atomic(path, new_text.encode("utf-8"))
    except OSError as exc:
        raise HooksError(f"the Claude Code settings file was not written ({type(exc).__name__})") from None
    return saved


# ---------------------------------------------------------------- command line


def run(action: str, path: Path, *, repo_root: Path = REPO_ROOT, out: Callable[[str], None] = print,
        ask: Callable[[str], str] = input, clock: Callable[[], float] = time.time) -> int:
    """``show``, ``install`` or ``remove`` on the settings file ``path``."""
    try:
        raw, settings = read_settings(path)
        if action == "remove":
            new = without_quill(settings)
            if new == settings:
                out("No Quill hooks in the Claude Code settings; nothing to remove.")
                return EXIT_OK
            what = "remove Quill's Claude Code hooks"
        else:
            pythonw, script = hook_command(repo_root)
            new = with_quill(settings, pythonw, script)
            if new == settings:
                out("The Quill hooks are already installed in the Claude Code settings; nothing to change.")
                return EXIT_OK
            what = "install Quill's Claude Code hooks (Stop, and Notification for permission_prompt only)"
        text = render(new)
        out(f"This would {what} in {path}:")
        out(describe_change(path, raw, text).rstrip("\n"))
        if action == "show":
            out("Nothing was written (--show). To write it: py -3.12 -m quill.claude_hooks --install")
            return EXIT_OK
        try:
            answer = ask(f'Type "{CONFIRM_WORD}" to write this change: ')
        except EOFError:
            answer = ""
        if answer.strip().lower() != CONFIRM_WORD:
            out("Not confirmed; nothing was written.")
            return EXIT_FAILED
        saved = apply(path, raw, text, clock)
    except HooksError as exc:
        out(f"Claude Code settings unchanged: {exc}.")
        return EXIT_FAILED
    out(f"Written. Previous file saved as {saved.name}." if saved is not None else "Written (new settings file).")
    if action == "install":
        out("Restart Claude Code (or open /hooks once) so it reads the new hooks.")
    return EXIT_OK


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print, ask: Callable[[str], str] = input,
         environ: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.claude_hooks",
                                     description="Quill's Claude Code attention alert hooks.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--show", action="store_true", help="print the change to the settings; write nothing")
    group.add_argument("--install", action="store_true", help="print the change, then write it after confirmation")
    group.add_argument("--remove", action="store_true", help="remove only Quill's hooks, after confirmation")
    parser.add_argument("--settings", type=Path, default=None,
                        help="Claude Code settings file (default: the user-level settings.json)")
    args = parser.parse_args(argv)
    action = "show" if args.show else "install" if args.install else "remove"
    path = args.settings if args.settings is not None else user_settings_path(environ)
    return run(action, path, out=out, ask=ask)


if __name__ == "__main__":
    sys.exit(main())
