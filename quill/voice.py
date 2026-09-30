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


def default_parser(folders: Sequence[Path], vocabulary: Callable[[], Vocabulary], launcher: object) -> Parser:
    """The voice commands Quill knows today."""
    return Parser([OpenProject(folders, vocabulary, launcher)])
