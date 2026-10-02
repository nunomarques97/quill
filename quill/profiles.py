"""Active-window profiles: which profile a window gets and how it shapes the text.

The foreground window is described by ``WindowInfo`` (process image name,
window class, title). ``Profiles.select`` checks the configured matchers
(``[profiles.*]`` in ``quill.example.toml`` and ``local/quill.toml``) in
order; the first match wins and a window that matches none gets ``default``.
Within a matcher every non-empty field must match; a field matches when any
entry does. Processes and classes compare whole names, titles compare
substrings, all ignoring case. A title entry in square brackets (a marker
such as "[Claude Code]") matches only at the end of the title, after a space
(or as the whole title), never the same text inside a file or folder name.
In a VS Code window (``Code.exe``) whose title has the app name, the marker
is VS Code's " [${focusedView}]" after the last "Visual Studio Code": it
matches when it directly follows the app name and is followed by nothing or
by the " - <editor state>" suffix that VS Code appends when its screen
reader optimization is on (``editor.accessibilitySupport`` "on" with
``accessibility.windowTitleOptimized``, the default): the decoration of the
active editor, such as "Modified" or its problems (``split_vscode_title``).
A process that cannot be read (an elevated or
protected process, or a window that is gone) never matches a process list:
the profile is not guessed from the title alone.

A profile may have several matchers (``[[profiles.<name>]]``); it applies
when any of them matches.

Claude Code is the ``claude-code`` profile: a terminal or editor process plus
a title (by default Windows Terminal with "Claude Code" in the title, or VS
Code with the " [Claude Code]" marker that its ``${focusedView}`` title
variable shows while the Claude Code sidebar view has the focus).
``Profiles.is_claude_code`` is what the send triggers ask before they press
Enter. Any other focused view ("Text Editor" or its translation, "Terminal",
"Explorer") or an empty one ("[]": the Claude Code editor tab, whose title
alone cannot tell it from another tab) keeps the ``vscode`` profile: no Enter.

``py -3.12 -m quill.profiles --probe`` describes the foreground window (after
``--delay`` seconds, so another window can be brought forward first), or
with ``--all`` every visible VS Code and terminal window: process, class,
profile and why, the project-detection reason code and whether mouse 5 would
press Enter. It prints no title, project name or path, and only reads.

Profile rules (``apply_profile``) run last, on the cleaned, matched and
corrected text. They are deterministic, change only punctuation, spacing and
sentence-start capitals, never a word, and applying them twice gives the
same text:

- ``technical`` (claude-code, vscode): sentence starts capitalized; the text
  ends with a sentence mark, and a trailing ellipsis or ``,;:`` becomes a
  period (a prompt or commit line never trails off).
- ``informal`` (whatsapp): sentence starts capitalized; a single final
  period and a trailing ``,;:`` are dropped, as in a chat message; ``?``,
  ``!`` and an ellipsis stay.
- ``full`` (email): sentence completion: sentence starts capitalized, every
  sentence ends with a mark (a trailing ellipsis or ``,;:`` becomes a
  period) and an opening greeting ("Bom dia", "Olá a todos", ...) followed
  directly by text gets its comma.
- ``default`` (any other window): sentence starts capitalized and a final
  sentence mark, the same as the cleanup rules.

Writing style: example texts in the ignored ``local/style/`` folder
(``<profile>.txt``, or ``default.txt`` for every profile without its own
file; samples separated by a blank line) are added to the prompt of the
local LLM cleanup, with the profile's instruction, only when
``[cleanup] mode = "llm"``. With the default rules cleanup the samples are
not read: the style is the profile rules above. Samples are personal: they
are never logged, and messages name the file only.

Usage: py -3.12 -m quill.profiles --check [STYLE_DIR]
       py -3.12 -m quill.profiles --probe [--delay SECONDS] [--all]
"""

from __future__ import annotations

import argparse
import ntpath
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from quill.cleanup import Token, _keep_set, _set_marks, capitalize, tokenize, tidy_punctuation
from quill.config import PROFILE_NAMES, ProfileMatcher

DEFAULT = "default"
CLAUDE_CODE = "claude-code"
VSCODE_PROCESS = "code.exe"
APP_NAME = "Visual Studio Code"
STATE_SEPARATOR = " - "  # VS Code's default window.titleSeparator
MAX_TITLE_CHARS = 1024  # a longer title is not parsed
DIRTY_MARK = "●"  # VS Code's ${dirty}: an editor has unsaved changes
# Why Enter is withheld after typing (reason codes; logged).
GONE = "target_gone"
NO_LONGER_CLAUDE = "not_claude_code"
BECAME_DIRTY = "editor_dirty"
PROFILES = (*PROFILE_NAMES, DEFAULT)

TECHNICAL = "technical"
INFORMAL = "informal"
FULL = "full"
STYLES = {"claude-code": TECHNICAL, "vscode": TECHNICAL, "whatsapp": INFORMAL, "email": FULL, DEFAULT: DEFAULT}

# Opening greetings of an email, longest first; matched on the first words.
GREETINGS = (
    ("bom", "dia", "a", "todos"), ("boa", "tarde", "a", "todos"), ("boa", "noite", "a", "todos"),
    ("olá", "a", "todos"), ("bom", "dia"), ("boa", "tarde"), ("boa", "noite"), ("olá",),
)
# A greeting followed by one of these goes on ("Boa tarde a equipa"): no comma is added.
GREETING_CONTINUES = frozenset({"a", "à", "ao", "aos", "às", "e"})
# Instruction added to the LLM cleanup prompt for each style.
STYLE_INSTRUCTIONS = {
    TECHNICAL: "The text is a prompt, comment or commit message for developer tools: keep it direct and "
               "keep every technical term, file name and command exactly as written.",
    INFORMAL: "The text is an informal chat message: keep its casual tone.",
    FULL: "The text is an email: write complete sentences with a greeting and closing only if they were dictated.",
    DEFAULT: "",
}
STYLE_SUFFIX = ".txt"
MAX_SAMPLES = 5
MAX_SAMPLE_CHARS = 400
MAX_STYLE_CHARS = 1200


class StyleError(ValueError):
    """A style file cannot be used; the message names the file, never its text."""


# ---------------------------------------------------------------- windows


@dataclass(frozen=True)
class WindowInfo:
    """What a profile is chosen from. ``process`` is the image file name, or '' when unreadable."""

    process: str = ""
    window_class: str = ""
    title: str = ""


def _read(read: object, *args: object) -> str:
    try:
        value = read(*args)
    except (OSError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def window_info(api: object, hwnd: int) -> WindowInfo | None:
    """The window's process name, class and title through the Win32 layer; None when it is gone.

    ``api`` is ``quill.win32.User32`` or a fake. Nothing here activates,
    focuses or changes the window.
    """
    try:
        if not hwnd or not api.is_window(hwnd):
            return None
        pid = api.window_process_id(hwnd)
    except OSError:
        return None
    image = _read(api.process_image, pid) if pid else ""
    return WindowInfo(
        process=ntpath.basename(image),
        window_class=_read(api.window_class, hwnd),
        title=_read(api.window_text, hwnd),
    )


def _fold(value: str) -> str:
    return " ".join(value.split()).casefold()


@dataclass(frozen=True)
class VscodeTitle:
    """A VS Code title cut at its last app name: ``head`` before it, the focused-view ``marker``
    after it (None: no marker; '': an empty "[]") and the editor ``state`` suffix (None: none)."""

    head: str = field(repr=False)
    marker: str | None = field(repr=False)
    state: str | None = field(repr=False)


def split_vscode_title(title: object) -> VscodeTitle | None:
    """``title`` (whitespace folded) cut around its last "Visual Studio Code"; None when it has no
    app name or the text after it is neither " [<view>]", " - <state>" nor both, in that order.

    VS Code builds the end of the title from " ${separator}${appName} [${focusedView}]" and,
    with its screen reader optimization, "${separator}${activeEditorState}" (the default
    separator " - "); a file, folder or tab name always comes before the app name.
    """
    if not isinstance(title, str) or len(title) > MAX_TITLE_CHARS:
        return None
    text = " ".join(title.split())
    at = text.casefold().rfind(APP_NAME.casefold())
    if at < 0:
        return None
    head, rest = text[:at], text[at + len(APP_NAME):]
    marker = None
    if rest.startswith(" ["):
        end = rest.find("]")
        if end < 0:
            return None
        marker, rest = rest[2:end].strip(), rest[end + 1:]
    if not rest:
        return VscodeTitle(head, marker, None)
    if rest.startswith(STATE_SEPARATOR) and rest[len(STATE_SEPARATOR):].strip():
        return VscodeTitle(head, marker, rest[len(STATE_SEPARATOR):].strip())
    return None


def matches(matcher: ProfileMatcher, info: WindowInfo) -> bool:
    """True when every field the matcher lists matches the window."""
    if not (matcher.processes or matcher.classes or matcher.titles):
        return False
    if matcher.processes and _fold(info.process) not in {_fold(p) for p in matcher.processes}:
        return False
    if matcher.classes and _fold(info.window_class) not in {_fold(c) for c in matcher.classes}:
        return False
    title = _fold(info.title)
    vscode = _fold(info.process) == VSCODE_PROCESS
    return not matcher.titles or any(_title_matches(_fold(t), title, vscode) for t in matcher.titles)


def _title_matches(entry: str, title: str, vscode: bool = False) -> bool:
    """A substring, or for a bracketed marker the end of the title after a space.

    In a VS Code title (``vscode``) with the app name, a marker is only the
    focused view after the last app name, optionally followed by the editor
    state suffix.
    """
    if len(entry) > 2 and entry.startswith("[") and entry.endswith("]"):
        split = split_vscode_title(title) if vscode else None
        if split is not None:
            return split.marker is not None and _fold(split.marker) == entry[1:-1].strip()
        return title == entry or title.endswith(" " + entry)
    return entry in title


class Profiles:
    """The configured matchers, in precedence order."""

    def __init__(self, matchers: Sequence[ProfileMatcher]) -> None:
        unknown = [m.name for m in matchers if m.name not in PROFILE_NAMES]
        if unknown:
            raise ValueError(f"unknown profile: {unknown[0]}")
        self.matchers = tuple(matchers)

    def select(self, info: WindowInfo | None) -> str:
        """The first matching profile, or ``default`` (also for an unknown window)."""
        if info is None:
            return DEFAULT
        for matcher in self.matchers:
            if matches(matcher, info):
                return matcher.name
        return DEFAULT

    def is_claude_code(self, info: WindowInfo | None) -> bool:
        """Whether the send trigger may press Enter in this window."""
        return self.select(info) == CLAUDE_CODE

    def enter_refusal(self, before: WindowInfo | None, now: WindowInfo | None) -> str | None:
        """Why the send trigger's Enter must not follow the typed text (a reason code), or None.

        ``before`` is the target as it was described before typing and ``now``
        the same window described again just before Enter: it must still be
        Claude Code, and its title must not have gained VS Code's dirty marker
        (the text went into a file, not into the Claude Code input).
        """
        if now is None:
            return GONE
        if not self.is_claude_code(now):
            return NO_LONGER_CLAUDE
        if DIRTY_MARK in now.title and (before is None or DIRTY_MARK not in before.title):
            return BECAME_DIRTY
        return None


# ---------------------------------------------------------------- text rules


def style_of(profile: str) -> str:
    if profile not in STYLES:
        raise ValueError(f"unknown profile: {profile}")
    return STYLES[profile]


def _greeting_comma(tokens: list[Token]) -> None:
    words = [token.key for token in tokens]
    for greeting in GREETINGS:
        size = len(greeting)
        if len(words) > size and tuple(words[:size]) == greeting:
            goes_on = greeting[-1] != "todos" and words[size] in GREETING_CONTINUES
            if not any(token.marks for token in tokens[:size]) and not goes_on:
                _set_marks(tokens[size - 1], ",")
            return


def apply_profile(text: str, profile: str, keep: Iterable[str] = ()) -> str:
    """``text`` shaped by the profile's rules; words are never added, removed or respelled."""
    style = style_of(profile)
    tokens = tokenize(text)
    if not tokens:
        return ""
    tokens = tidy_punctuation(tokens, final_mark=style != INFORMAL)
    last = tokens[-1]
    if style in (TECHNICAL, FULL) and last.marks == "...":
        _set_marks(last, ".")
    if style == INFORMAL and last.marks in (".", ",", ";", ":"):
        _set_marks(last, "")
    if style == FULL:
        _greeting_comma(tokens)
    tokens = capitalize(tokens, _keep_set(keep))
    return " ".join(token.text() for token in tokens)


# ---------------------------------------------------------------- writing style


def _label(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


def load_style_samples(style_dir: Path, profile: str) -> tuple[str, ...]:
    """The profile's samples (``<profile>.txt``, else ``default.txt``); none when missing.

    Samples are separated by a blank line; at most ``MAX_SAMPLES`` are kept,
    each cut to ``MAX_SAMPLE_CHARS`` characters and all within
    ``MAX_STYLE_CHARS``.
    """
    style_of(profile)
    for name in dict.fromkeys((profile, DEFAULT)):
        path = Path(style_dir) / f"{name}{STYLE_SUFFIX}"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            raise StyleError(f"style: {_label(path)} cannot be read ({type(exc).__name__})") from None
        samples: list[str] = []
        used = 0
        for block in text.replace("\r\n", "\n").split("\n\n"):
            sample = " ".join(block.split())[:MAX_SAMPLE_CHARS]
            if not sample:
                continue
            if len(samples) == MAX_SAMPLES or used + len(sample) > MAX_STYLE_CHARS:
                break
            samples.append(sample)
            used += len(sample)
        return tuple(samples)
    return ()


def style_prompt(profile: str, samples: Sequence[str] = ()) -> str:
    """Text added to the LLM cleanup prompt for ``profile``; '' for the default profile without samples."""
    parts = [STYLE_INSTRUCTIONS[style_of(profile)]] if STYLE_INSTRUCTIONS[style_of(profile)] else []
    if samples:
        parts.append("Match the tone and punctuation of these examples of the user's writing, "
                     "never their content:\n" + "\n".join(f"- {sample}" for sample in samples))
    return "\n".join(parts)


# ---------------------------------------------------------------- probe


MAX_PROBE_DELAY_S = 60.0
MAX_PROBED_WINDOWS = 64
# Kinds of focused-view marker the probe reports (never the marker text itself).
MARKER_CLAUDE = "claude-code"
MARKER_OTHER = "other"
MARKER_EMPTY = "empty"
MARKER_NONE = "none"


def _safe_name(value: str) -> str:
    """A process or class name for the probe's output: printable, bounded, never empty."""
    text = "".join(ch if ch.isprintable() and not ch.isspace() else "?" for ch in value)[:64]
    return text or "?"


def _claude_markers(matchers: Sequence[ProfileMatcher]) -> set[str]:
    """The bracketed title entries of the claude-code matchers, folded and without brackets."""
    return {_fold(title)[1:-1].strip() for matcher in matchers if matcher.name == CLAUDE_CODE
            for title in matcher.titles if len(title) > 2 and title.startswith("[") and title.endswith("]")}


def marker_kind(info: WindowInfo, claude_markers: set[str]) -> tuple[str, bool]:
    """(kind of the focused-view marker of a VS Code title, whether the editor state suffix is there)."""
    split = split_vscode_title(info.title) if _fold(info.process) == VSCODE_PROCESS else None
    if split is None or split.marker is None:
        return MARKER_NONE, split is not None and split.state is not None
    if not split.marker:
        kind = MARKER_EMPTY
    elif _fold(split.marker) in claude_markers:
        kind = MARKER_CLAUDE
    else:
        kind = MARKER_OTHER
    return kind, split.state is not None


def _rule(profiles: Profiles, info: WindowInfo) -> str:
    """Which configured matcher chose the profile ('<profile> #<n> (<fields>)'), or 'none'."""
    count: dict[str, int] = {}
    for matcher in profiles.matchers:
        count[matcher.name] = count.get(matcher.name, 0) + 1
        if matches(matcher, info):
            fields = [name for name, value in (("process", matcher.processes), ("class", matcher.classes),
                                               ("title", matcher.titles)) if value]
            return f"{matcher.name} #{count[matcher.name]} ({'+'.join(fields)})"
    return "none"


def probe_line(api: object, profiles: Profiles, detector: object | None, hwnd: int, *,
               claude_markers: set[str] | None = None) -> str:
    """One window described for the probe: no title text, project name or path."""
    from quill.command import is_terminal

    info = window_info(api, hwnd)
    if info is None:
        return "window gone"
    markers = _claude_markers(profiles.matchers) if claude_markers is None else claude_markers
    profile = profiles.select(info)
    kind, state = marker_kind(info, markers)
    project = "n/a"
    if detector is not None and (profile in (CLAUDE_CODE, "vscode") or is_terminal(info)):
        try:
            pid = int(api.window_process_id(hwnd))
        except (OSError, ValueError):
            pid = 0
        project = detector.detect_reason(info, pid, claude_code=profile == CLAUDE_CODE)[1]
    return (f"process {_safe_name(info.process or '(unreadable)')}; class {_safe_name(info.window_class)}; "
            f"profile {profile}; rule {_rule(profiles, info)}; marker {kind}; "
            f"state suffix {'yes' if state else 'no'}; project {project}; "
            f"mouse 5 Enter {'yes' if profile == CLAUDE_CODE else 'no'}")


def probe(api: object, profiles: Profiles, detector: object | None = None, *, all_windows: bool = False) -> list[str]:
    """The probe's lines: the foreground window, or every visible VS Code and terminal window.

    Only reads (window, class, process image and title queries): nothing is
    sent, clicked, focused or changed, and no title, name or path is printed.
    """
    from quill.command import is_terminal

    if not all_windows:
        hwnd = int(api.foreground_window() or 0)
        return ["foreground: " + (probe_line(api, profiles, detector, hwnd) if hwnd else "no window")]
    markers = _claude_markers(profiles.matchers)
    lines: list[str] = []
    for hwnd in api.top_level_windows():
        try:
            if not api.is_visible(hwnd):
                continue
        except OSError:
            continue
        info = window_info(api, hwnd)
        if info is None or not info.title or not (_fold(info.process) == VSCODE_PROCESS or is_terminal(info)):
            continue
        if len(lines) == MAX_PROBED_WINDOWS:
            lines.append(f"(more than {MAX_PROBED_WINDOWS} windows: the rest is not listed)")
            break
        lines.append(f"window {len(lines) + 1}: "
                     + probe_line(api, profiles, detector, hwnd, claude_markers=markers))
    return lines or ["no visible VS Code or terminal window"]


def _real_probe_parts(config: object) -> tuple[object, object]:
    """The read-only Win32 layer and the project detector of the probe (Windows only)."""
    from quill.projects import ProjectDetector, ProjectFolders
    from quill.win32 import Processes, User32

    api = User32()
    try:
        processes = Processes()
    except OSError:
        processes = None
    folders = ProjectFolders(config.project_context.folders, config.voice.shortcut_dirs)
    return api, ProjectDetector(folders, processes)


def main(argv: list[str] | None = None, *, parts: Callable[[object], tuple[object, object]] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    from quill.config import ConfigError, load_config

    parser = argparse.ArgumentParser(prog="python -m quill.profiles", description=__doc__.splitlines()[0])
    parser.add_argument("--check", nargs="?", const="", metavar="STYLE_DIR",
                        help="count the style samples per profile (default: paths.style of the config)")
    parser.add_argument("--probe", action="store_true",
                        help="describe the foreground window: profile and why, project reason code, mouse 5 Enter "
                             "(no title, name or path is printed; nothing is sent, clicked or focused)")
    parser.add_argument("--delay", type=float, default=0.0, metavar="SECONDS",
                        help=f"with --probe: wait first (0 to {MAX_PROBE_DELAY_S:.0f} s) to bring a window forward")
    parser.add_argument("--all", action="store_true", help="with --probe: every visible VS Code and terminal window")
    args = parser.parse_args(argv)
    if (args.check is None) == (not args.probe):
        parser.print_usage(sys.stderr)
        return 2
    if not args.probe and (args.all or args.delay):
        parser.print_usage(sys.stderr)
        return 2
    if not 0.0 <= args.delay <= MAX_PROBE_DELAY_S:  # also refuses nan
        print(f"quill profiles: --delay must be between 0 and {MAX_PROBE_DELAY_S:.0f} seconds", file=sys.stderr)
        return 2
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.probe:
        try:
            api, detector = (parts or _real_probe_parts)(config)
        except OSError as exc:
            print(f"quill profiles: window probe unavailable ({type(exc).__name__})", file=sys.stderr)
            return 1
        if args.delay:
            print(f"quill profiles: probing in {args.delay:g} s; bring the window forward", flush=True)
            sleep(args.delay)
        for line in probe(api, Profiles(config.profiles), detector, all_windows=args.all):
            print(line)
        return 0
    style_dir = Path(args.check) if args.check else config.style_dir
    counts = []
    try:
        for profile in PROFILES:
            counts.append(f"{profile} {len(load_style_samples(style_dir, profile))}")
    except StyleError as exc:
        print(exc, file=sys.stderr)
        return 1
    used = "used" if config.cleanup_mode == "llm" else "not used (cleanup mode is rules)"
    print(f"quill profiles: {len(config.profiles)} matchers; style samples: {', '.join(counts)}; {used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
