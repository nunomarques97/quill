"""Voice commands: what is said while the voice trigger (F9 by default) is held.

The transcribed text is normalized (case, accents and punctuation removed,
spaces collapsed) and offered to the commands of a ``Parser``, a registry: a
command has a ``name``, ``parse(text)`` that returns an ``Intent`` or None,
and ``run(intent)`` that returns a ``VoiceOutcome``. The first command that
recognizes the text runs; a new command is added with ``register`` without
changing the parser. Text that no command recognizes shows "Comando não
reconhecido" and does nothing.

The first command, ``OpenProject`` ("abre VS Code no <projeto>", also
"abre o VS Code na <projeto>", "abrir VS Code em <projeto>" and common
transcriptions of "VS Code" such as "vê esse code" or "Visual Studio Code"),
lists the shortcuts of ``[voice_commands] shortcut_dirs``, matches the spoken
name with ``quill.shortcuts.match`` and opens the one clear match with
``quill.shortcuts.launch``. The indicator shows "A abrir <nome>", or the
closest names, or the reason nothing was opened.

While the voice trigger is held, the audio is decoded in European Portuguese
with its own hints (``voice_hints``, built at every press by ``VoiceHints``):
a command-shaped Portuguese prompt listing the shortcut names, and hotwords
made of the shortcut names (a hyphenated name also as separate words) and the
personal-vocabulary names with their spoken variants. Shortcut names come
first when the hints are trimmed. Dictation keeps the vocabulary hints.

A voice command never clicks, never types and never presses Enter. Spoken
text is only compared with the names of the enumerated shortcuts; it never
reaches a shell, a command line or a file path. Logs and outcomes carry the
command name, reason codes, counts and timings only, never the spoken text,
project names or paths.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from quill import shortcuts
from quill.indicator.render import ERROR, VOICE_NONE, VOICE_OPEN
from quill.vocabulary import Vocabulary
from quill.whisper import MAX_LENGTH, PROMPT_MAX_CHARS, SOT_SEQUENCE_TOKENS, SessionHints

log = logging.getLogger("quill.voice")

# Reason codes of the parser (the commands add their own).
UNRECOGNIZED = "unrecognized"
FAILED = "command_failed"

MESSAGES = {
    UNRECOGNIZED: "Comando não reconhecido",
    FAILED: "O comando falhou; nada foi aberto",
    shortcuts.NO_SHORTCUTS: "Nenhum atalho de projeto: veja shortcut_dirs em [voice_commands]",
    shortcuts.NO_MATCH: "Nenhum projeto com esse nome",
    shortcuts.AMBIGUOUS: "Não sei qual abrir",
    shortcuts.REFUSED: "Atalho recusado: não abre o VS Code nem uma pasta",
    shortcuts.UNREADABLE: "Não consegui ler o atalho",
    shortcuts.VSCODE_MISSING: "VS Code não encontrado neste PC",
    shortcuts.LAUNCH_FAILED: "O Windows não abriu o atalho",
}
OPENING = "A abrir {name}"
SIMILAR = "Parecidos: {names}"
# Endings that are errors of the launch rather than of what was said.
ERROR_REASONS = frozenset({FAILED, shortcuts.UNREADABLE, shortcuts.VSCODE_MISSING, shortcuts.LAUNCH_FAILED})


def normalize(text: str) -> str:
    """Lowercase, no accents, letters and digits separated by single spaces."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w]|_", " ", plain).split())


@dataclass(frozen=True)
class Intent:
    """A recognized command and its arguments (spoken words: never logged)."""

    command: str
    args: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class VoiceOutcome:
    """How a voice command ended. ``state`` and ``text`` are for the indicator only."""

    ok: bool
    reason: str
    state: str
    text: str = field(default="", repr=False)
    command: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)
    timings: Mapping[str, float] = field(default_factory=dict)


class Command(Protocol):
    name: str

    def parse(self, text: str) -> Intent | None:
        """``text`` is ``normalize``d; an ``Intent`` when this command recognizes it."""

    def run(self, intent: Intent) -> VoiceOutcome: ...


def failure(reason: str, command: str = "", options: Sequence[str] = (), **extra: object) -> VoiceOutcome:
    """A command that did nothing: the reason's message, with the closest names when there are some."""
    text = MESSAGES.get(reason, MESSAGES[FAILED])
    if options:
        text = f"{text}. {SIMILAR.format(names=', '.join(options))}"
    state = ERROR if reason in ERROR_REASONS else VOICE_NONE
    return VoiceOutcome(False, reason, state, text, command, **extra)  # type: ignore[arg-type]


class Parser:
    """The registry of voice commands, tried in registration order."""

    def __init__(self, commands: Sequence[Command] = ()) -> None:
        self._commands: list[Command] = []
        for command in commands:
            self.register(command)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(command.name for command in self._commands)

    def register(self, command: Command) -> None:
        if command.name in self.names:
            raise ValueError("a voice command with this name is already registered")
        self._commands.append(command)

    def parse(self, text: str) -> tuple[Command, Intent] | None:
        normalized = normalize(text)
        if not normalized:
            return None
        for command in self._commands:
            intent = command.parse(normalized)
            if intent is not None:
                return command, intent
        return None


class VoiceCommands:
    """Runs one transcribed voice command through the parser and its command."""

    def __init__(self, parser: Parser, clock: Callable[[], float] = time.perf_counter) -> None:
        self.parser = parser
        self.clock = clock

    def run(self, text: str) -> VoiceOutcome:
        started = self.clock()
        found = self.parser.parse(text)
        parsed = self.clock()
        if found is None:
            outcome = failure(UNRECOGNIZED)
        else:
            command, intent = found
            try:
                outcome = command.run(intent)
            except Exception as exc:  # noqa: BLE001 - the message may carry spoken text: the type only
                log.error("voice command %s failed (%s)", command.name, type(exc).__name__)
                outcome = failure(FAILED, command.name)
        timings = {"parse_s": parsed - started, **outcome.timings, "total_s": self.clock() - started}
        outcome = VoiceOutcome(outcome.ok, outcome.reason, outcome.state, outcome.text, outcome.command,
                               outcome.counts, timings)
        counts = ", ".join(f"{name} {value}" for name, value in outcome.counts.items())
        steps = ", ".join(f"{name[:-2]} {seconds * 1000:.0f} ms" for name, seconds in timings.items())
        log.info("voice command %s: %s (%s%s)", outcome.command or "none", outcome.reason,
                 f"{counts}; " if counts else "", steps)
        return outcome


# ---------------------------------------------------------------- open-project


# Spoken forms of "VS Code" after ``normalize``: "vs code", "vscode", "v s code",
# "ve esse code", "vi es code", "bs code", "visual studio code", "vs cod", ...
_V = r"(?:v|ve|vi|b)"
_S = r"(?:s|es|esse|ess)"
_CODE = r"(?:code|codes|cod|codi|coude|coda|cold)"
VSCODE = rf"(?:{_V}\s?{_S}\s?{_CODE}|visual\s+studio(?:\s+{_CODE})?)"
OPEN_PROJECT = re.compile(
    r"^(?:(?:por favor|podes|pode|quero|consegues)\s+)*"
    r"(?:abre|abrir|abra|abri|abre me)\s+"
    rf"(?:o\s+|a\s+)?{VSCODE}\s+"
    r"(?:no|na|nos|nas|num|numa|em|com|para|pra|ao|do|da)\s+"
    r"(?:(?:o|a)\s+)?(?:(?:projeto|repositorio|repo|pasta)\s+)?"
    r"(?P<name>.+?)"
    r"(?:\s+por favor|\s+obrigado|\s+obrigada)?$"
)


class OpenProject:
    """"abre VS Code no <projeto>": open the project-hub shortcut the spoken name clearly means.

    ``folders`` are the configured shortcut folders; ``vocabulary()`` gives the
    current personal vocabulary; ``launcher`` opens for real (a fake in tests).
    """

    name = "open_project"

    def __init__(self, folders: Sequence[Path], vocabulary: Callable[[], Vocabulary], launcher: object, *,
                 lister: Callable[[Sequence[Path]], shortcuts.Listing] = shortcuts.list_shortcuts,
                 matcher: Callable[..., shortcuts.Match] = shortcuts.match,
                 launch: Callable[..., shortcuts.Launch] = shortcuts.launch,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.folders = tuple(folders)
        self.vocabulary = vocabulary
        self.launcher = launcher
        self.lister = lister
        self.matcher = matcher
        self.launch = launch
        self.clock = clock

    def parse(self, text: str) -> Intent | None:
        found = OPEN_PROJECT.match(text)
        if found is None:
            return None
        return Intent(self.name, {"project": found.group("name")})

    def run(self, intent: Intent) -> VoiceOutcome:
        started = self.clock()
        listing = self.lister(self.folders)
        counts = {"folders": len(self.folders), "folders_failed": listing.folders_failed,
                  "shortcuts": len(listing.shortcuts)}
        found = self.matcher(intent.args["project"], listing.shortcuts, self.vocabulary())
        matched = self.clock()
        counts["candidates"] = found.candidates
        if found.distance is not None:
            counts["distance"] = found.distance
        timings = {"match_s": matched - started}
        if not found.ok:
            return failure(found.reason, self.name, found.options, counts=counts, timings=timings)
        result = self.launch(found.shortcut, self.launcher)
        timings["launch_s"] = self.clock() - matched
        if not result.ok:
            return failure(result.reason, self.name, counts=counts, timings=timings)
        return VoiceOutcome(True, result.reason, VOICE_OPEN, OPENING.format(name=found.shortcut.name), self.name,
                            counts, timings)


# ---------------------------------------------------------------- decoding hints


# Voice commands are always decoded in Portuguese, never auto-detected.
VOICE_LANGUAGE = "pt"
# Whisper imitates the prompt's language and style: a Portuguese command, then the project names.
VOICE_PROMPT = "Abre o VS Code no projeto."
PROJECTS_PREFIX = " Projetos: "
# Tokens of the prompt and of the hotwords (as faster-whisper encodes each
# part). Both stay under its cut of PROMPT_PART_MAX tokens per part (which
# would drop the start of the prompt or the end of the hotwords), and together
# they leave VOICE_ROOM_TOKENS of the 448-token context to the command.
VOICE_PART_MAX_TOKENS = 150
# The hotwords' and the prompt's start tokens, the start sequence and "no timestamps".
VOICE_ROOM_TOKENS = MAX_LENGTH - 2 * VOICE_PART_MAX_TOKENS - (1 + SOT_SEQUENCE_TOKENS + 1)
# Reason code of a listing that failed while building the hints.
HINTS_UNLISTED = "hints_shortcuts_unlisted"
_NAME_SEPARATORS = re.compile(r"[-_.]+")


def utf8_tokens(text: str) -> int:
    """An upper bound of a prompt part's tokens without the tokenizer: its UTF-8 bytes."""
    return len((" " + text.strip()).encode("utf-8"))


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def _dedupe(words: Sequence[str], key: Callable[[str], str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for word in words:
        folded = key(word)
        if word and folded and folded not in seen:
            seen.add(folded)
            out.append(word)
    return out


def _with_parts(name: str) -> list[str]:
    """A name, and a hyphenated (or underscored, dotted) name also as separate words."""
    split = _clean(_NAME_SEPARATORS.sub(" ", name))
    return [name, split] if split and split != name else [name]


def _fits(words: Sequence[str], separator: str, fits: Callable[[str], bool]) -> str:
    """Words joined in order while ``fits``; the first word that does not fit ends the list."""
    out = ""
    for word in words:
        candidate = word if not out else out + separator + word
        if not fits(candidate):
            break
        out = candidate
    return out


def voice_hints(shortcut_names: Sequence[str], vocabulary: Vocabulary = Vocabulary(), *,
                tokens: Callable[[str], int] = utf8_tokens) -> SessionHints:
    """The decoding hints of a voice session: the one builder of the app and the benchmark.

    The prompt is ``VOICE_PROMPT`` and a list of project names: the shortcut
    names, then the personal-vocabulary names. The hotwords are the shortcut
    names, then each vocabulary name and its declared spoken variants; a
    hyphenated name is also offered as separate words. Each part stays within
    ``PROMPT_MAX_CHARS`` and ``VOICE_PART_MAX_TOKENS`` as counted by
    ``tokens`` (``quill.whisper.TokenCounter``; by default the UTF-8 bytes,
    never fewer than the tokens); each list keeps its order and stops at the
    first name that does not fit, so shortcut names are the last dropped.
    """

    def fits(part: str) -> bool:
        return len(part) <= PROMPT_MAX_CHARS and tokens(part) <= VOICE_PART_MAX_TOKENS

    shortcut = [_clean(name) for name in shortcut_names]
    personal = [_clean(entry.text) for entry in vocabulary.names]
    listed = _dedupe(shortcut + personal, shortcuts.name_key)
    names = _fits(listed, ", ", lambda joined: fits(f"{VOICE_PROMPT}{PROJECTS_PREFIX}{joined}."))
    prompt = VOICE_PROMPT + (f"{PROJECTS_PREFIX}{names}." if names else "")
    spoken: list[str] = []
    for name in shortcut:
        spoken.extend(_with_parts(name))
    for entry in vocabulary.names:
        spoken.extend(_with_parts(_clean(entry.text)))
        spoken.extend(_clean(text) for text in entry.variants)
    hotwords = _fits(_dedupe(spoken, str.casefold), " ", fits)
    return SessionHints(prompt=prompt, hotwords=hotwords or None, language=VOICE_LANGUAGE)


class VoiceHints:
    """Builds ``voice_hints`` at each press of the voice trigger.

    The shortcut folders are listed again every time, so a new shortcut is
    hinted at once. A listing that fails yields the prompt without shortcut
    names (the folder reasons are logged by ``quill.shortcuts``) and a token
    counter that fails counts bytes; neither stops the capture. Logs carry
    counts only.
    """

    def __init__(self, folders: Sequence[Path], vocabulary: Callable[[], Vocabulary], *,
                 tokens: Callable[[str], int] = utf8_tokens,
                 lister: Callable[[Sequence[Path]], shortcuts.Listing] = shortcuts.list_shortcuts) -> None:
        self.folders = tuple(folders)
        self.vocabulary = vocabulary
        self.tokens = tokens
        self.lister = lister

    def _count(self, text: str) -> int:
        try:
            return self.tokens(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("voice hints: token count failed (%s); counting bytes", type(exc).__name__)
            return utf8_tokens(text)

    def __call__(self) -> SessionHints:
        names: list[str] = []
        try:
            names = [shortcut.name for shortcut in self.lister(self.folders).shortcuts]
        except Exception as exc:  # noqa: BLE001 - the message may carry a path: the type only
            log.warning("voice hints: %s (%s)", HINTS_UNLISTED, type(exc).__name__)
        try:
            vocabulary = self.vocabulary()
        except Exception as exc:  # noqa: BLE001
            log.warning("voice hints: vocabulary unavailable (%s)", type(exc).__name__)
            vocabulary = Vocabulary()
        hints = voice_hints(names, vocabulary, tokens=self._count)
        log.info("voice hints: %d shortcut names, %d vocabulary names; prompt %d chars, hotwords %d chars",
                 len(names), len(vocabulary.names), len(hints.prompt or ""), len(hints.hotwords or ""))
        return hints


def default_parser(folders: Sequence[Path], vocabulary: Callable[[], Vocabulary], launcher: object) -> Parser:
    """The voice commands Quill knows today."""
    return Parser([OpenProject(folders, vocabulary, launcher)])
