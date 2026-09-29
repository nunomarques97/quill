"""Active-window profiles: which profile a window gets and how it shapes the text.

The foreground window is described by ``WindowInfo`` (process image name,
window class, title). ``Profiles.select`` checks the configured matchers
(``[profiles.*]`` in ``quill.example.toml`` and ``local/quill.toml``) in
order; the first match wins and a window that matches none gets ``default``.
Within a matcher every non-empty field must match; a field matches when any
entry does. Processes and classes compare whole names, titles compare
substrings, all ignoring case. A process that cannot be read (an elevated or
protected process, or a window that is gone) never matches a process list:
the profile is not guessed from the title alone.

Claude Code is the ``claude-code`` profile: a terminal or editor process plus
a title (by default Windows Terminal or VS Code with "Claude Code" in the
title). ``Profiles.is_claude_code`` is what the send trigger asks before it
presses Enter.

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
"""

from __future__ import annotations

import argparse
import ntpath
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from quill.cleanup import Token, _keep_set, _set_marks, capitalize, tokenize, tidy_punctuation
from quill.config import PROFILE_NAMES, ProfileMatcher

DEFAULT = "default"
CLAUDE_CODE = "claude-code"
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


def matches(matcher: ProfileMatcher, info: WindowInfo) -> bool:
    """True when every field the matcher lists matches the window."""
    if not (matcher.processes or matcher.classes or matcher.titles):
        return False
    if matcher.processes and _fold(info.process) not in {_fold(p) for p in matcher.processes}:
        return False
    if matcher.classes and _fold(info.window_class) not in {_fold(c) for c in matcher.classes}:
        return False
    title = _fold(info.title)
    return not matcher.titles or any(_fold(t) in title for t in matcher.titles)


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


def main(argv: list[str] | None = None) -> int:
    from quill.config import ConfigError, load_config

    parser = argparse.ArgumentParser(prog="python -m quill.profiles", description=__doc__.splitlines()[0])
    parser.add_argument("--check", nargs="?", const="", metavar="STYLE_DIR",
                        help="count the style samples per profile (default: paths.style of the config)")
    args = parser.parse_args(argv)
    if args.check is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
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
