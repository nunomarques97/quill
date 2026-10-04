"""Dictation sessions: one push-to-talk hold, from the press to the typed text.

``SessionManager`` receives the trigger signals (``quill.triggers``) on the
hook worker thread and runs each hold as a session:

- ``start``: a new session opens a streaming transcription and starts the
  microphone at once, so no word is lost; the indicator shows ``listening``
  with the live words and the voice level. While the model is still loading
  a press only shows the ``loading`` state: nothing is recorded, and its
  release does nothing.
- ``confirm``: the dictation and send triggers click at the pointer
  (``quill.focus``) and capture the target window; the command trigger never
  clicks and captures the foreground window, where the selection is.
- ``stop``: the capture stops, the final transcription is requested and the
  session is queued for finalization.
- ``cancel`` (short tap, combo, hooks stopping): the capture and the
  transcription are released; nothing is typed.

While a released session is still pending (transcribing, correcting,
enriching, typing or pressing Enter), a press of any trigger starts no
session: no click, no microphone, no capture. It is recorded as
``PREVIOUS_PENDING``, the indicator shows "Aguarde: o ditado anterior ainda
está a ser escrito" in the pending session's state and, ``BUSY_SHOW_S``
later, that session's own words again (unless something newer was shown
meanwhile); the ignored press's confirm, release and cancel do nothing. A
second click would move the focus or the caret under the pending typing. A
session pending for over ``pending_limit_s`` (a hang) no longer blocks the
next press. A hold still held keeps the one-hold rule of ``quill.triggers``.

One finalizer thread takes the released sessions strictly in release order:
it waits for the final text, runs the text pipeline (cleanup, vocabulary,
learned corrections, the target's profile), types the text once into the
session's own target and, for a send trigger (``send_polished``,
``send_raw``, the older ``send_claude``), presses one plain Enter (or
Ctrl+Enter when the pipeline's ``send_key`` says so) only after a successful
injection into a Claude Code window; anywhere else the text stays typed
without Enter, with the ``not_claude`` notice. Just before that Enter,
``enter_check(target, window)`` describes the target again (the ``window``
the pipeline saw): when it is no longer the foreground window, no longer
Claude Code (for a VS Code window recognised by its focused element: the
focus is no longer in the Claude Code message input), or its title gained a
dirty marker (the text went into a file), no Enter is pressed and the
``enter_withheld`` notice is shown. A
command session hands the final text, the spoken instruction, to
``quill.command.CommandMode``, which copies the selection, rewrites it with
the local model and types the rewrite over it; the indicator shows ``command`` while it listens. A voice
session (the ``voice`` trigger) never clicks, captures no target, types
nothing and presses no Enter: its final text goes to ``quill.voice``
(``voice.run``), and the indicator shows ``voice`` with the live words while
it listens, then the command's outcome ("A abrir <nome>", the closest names
or the reason). Its transcription is opened with the ``voice_hints()`` of
that press (``quill.voice.VoiceHints``: Portuguese, the command prompt and
the project names). A mouse 5 session starts with the vocabulary hints and,
once click-to-focus has named its window, gets ``send_hints(target)`` on
another thread (in Claude Code: a hint source, ``quill.heard.HeardHints``,
with the project name and its pack terms, those that sound like words
already heard first) for the audio decoded after that, until its release;
the number of hint switches is logged, never the hints. Every other session keeps
the transcriber's vocabulary hints. Hints that cannot be built never stop
the capture.

With ``last_reply(folder)`` (``quill.claude_reply.reply_context``: the last
reply of the folder's Claude Code session as a
``quill.reply_terms.ReplyContext``; None: no reply context), a mouse 5 hold
looks the reply up at most once: on the hint thread, through the lookup
``send_hints(target, lookup)`` is given (its terms are heard-only hint
candidates), else at the release, for a target in Claude Code with a project
folder. The release waits for a lookup the hint thread started at most
``REPLY_WAIT_S`` and goes on without the reply after that; it never looks up
again. The reply reaches the correction and the enrichment
(``rewriter.rewrite(..., reply=...)``) of that hold only. One line per hold
gives the lookup's reason code, source, counts and milliseconds; no other
trigger and no other window ever looks a reply up, and a failing lookup
leaves the dictation on its path without a reply.

A ``send_polished`` (mouse 5) session may get a final pass
(``final_pass(asr, final, target)``, ``quill.finalpass``): after its
streaming final it returns the text to use (the pass text, or the streaming
text with a reason code) and the target window it described, which the text
pipeline then receives as ``window`` so the window is described once. The
caller decides whether the target is Claude Code; no other trigger ever calls
it, and a failing or raising pass keeps the streaming text. The ``release to
typed`` log line adds the pass reason and milliseconds.

A long dictation (over the ``[autorewrite]`` audio or word threshold) goes,
after the text pipeline, to the ``rewriter`` (``quill.autorewrite``) while
the indicator shows ``reviewing`` ("A rever o texto"); a short one never
calls it, so its path is unchanged. The ``send_polished`` trigger sends
every dictation to the rewriter whatever its length (``force``) and
``send_raw`` never does. A failure, a timeout or a refusal by the
content guard types the original text at once and shows a short notice.
Into Claude Code, ``send_polished`` also gives the rewriter the context pack
of the window's project (``context_pack(folder)``, a
``quill.context_pack.ContextPacks.get``: cached, built on a miss within its
time bound, else None; no detected folder or a failing lookup gives None and
the correction goes on without it) and asks it to enrich the corrected text
into a structured prompt (``quill.enrich``); the indicator then shows
``ENRICHING`` in the ``reviewing`` state, and after the Enter "Prompt
enriquecido e enviado" when the prompt was enriched, or a short notice when
the corrected text was sent instead. The long automatic rewrite,
``send_claude``, ``send_raw`` and every other window keep the plain rewrite.
The text is typed with the target's newline policy (Shift+Enter in the
Claude Code panel, a space elsewhere; an enriched prompt goes into a terminal
as one paragraph with its labels, ``quill.enrich.one_paragraph``), and a
send trigger presses Enter only after all of it is typed. A rewrite typed without Enter is reported to
``on_typed`` with its original, for the undo key. ``submit`` runs other
work (the undo key) on the finalizer thread, in order with the sessions.

``alert(kind, project)`` shows a Claude Code alert (``quill.notify``: Claude
Code finished its reply or asks for a permission, in the project ``project``
or an unnamed one) with a short sound from the ``player``. While a hold is
recording, a session is being finalized, the model is loading or an outcome
is on screen, the alert waits: it is kept, once per (kind, project), and
shown when all of them are over (the finalizer checks about every
``poll_s``). The sound never plays into the microphone: a starting hold stops
it under the same lock that decides whether an alert may play. Several alerts
waiting together are shown once and ring once: the state is the permission
request when there is one, since it needs an answer, and the words line
names every waiting project (``alert_text``), the permission requests last so
the indicator's truncation, which keeps the end, never hides them. An alert
arriving while earlier alerts are still on screen (``ALERT_SHOW_S``) is shown
together with them, so several sessions finishing close together are all
named. An alert within ``alert_repeat_s`` of the last sound is shown without
sound.

A failure (microphone,
engine, target gone, foreground changed, ...) ends that session with the
``error`` state and a European Portuguese message; the next session is not
affected. The capture, the transcription job and the indicator state are
released on every exit path.

The indicator shows the newest live session (capturing or finalizing): an
older session finishing while a newer one is live (only after the pending
limit) is logged, not shown, so the live words stay on screen.

Logs hold session numbers, events, reason codes and timings only, never
spoken or typed text; ``outcomes`` keeps the same for tests.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from quill import autorewrite
from quill import command as commands
from quill import enrich
from quill import inject
from quill import focus as focus_reasons
from quill import sound
from quill import speech
from quill.indicator.render import (CLAUDE_DONE, CLAUDE_PERMISSION, COMMAND, ERROR, LISTENING, LOADING, REVIEWING,
                                    SENT, TRANSCRIBING, VOICE, VOICE_OPEN)
from quill.inject import NEWLINE_SPACE, InjectOptions, Target, normalize_text
from quill.triggers import CANCEL, CONFIRM, START, STOP, Signal

log = logging.getLogger("quill.session")

DICTATION = "dictation"
COMMAND_ACTION = "command"
SEND_CLAUDE = "send_claude"
SEND_POLISHED = "send_polished"
SEND_RAW = "send_raw"
VOICE_ACTION = "voice"
# Enter follows the text, in Claude Code only.
SEND_ACTIONS = (SEND_CLAUDE, SEND_POLISHED, SEND_RAW)
CLICK_ACTIONS = (DICTATION, *SEND_ACTIONS)

# Outcomes (reason codes of a finished session).
TYPED = "typed"
SENT_ENTER = "sent"
NOT_CLAUDE = "not_claude"
ENTER_FAILED = "enter_failed"
ENTER_WITHHELD = "enter_withheld"
CTRL_ENTER = "ctrl+enter"  # [claude_code] send_key for claudeCode.useCtrlEnterToSend
CANCELLED = "cancelled"
WHILE_LOADING = "while_loading"
MIC_ERROR = "mic_error"
ENGINE_ERROR = "engine_error"
ENGINE_TIMEOUT = "engine_timeout"
NO_SPEECH = "no_speech"
COMMAND_UNAVAILABLE = "command_unavailable"
VOICE_UNAVAILABLE = "voice_unavailable"
MODEL_UNAVAILABLE = "model_unavailable"
INTERNAL_ERROR = "internal_error"
CLEANUP_FALLBACK = "cleanup_fallback"
FOCUS_FAILED = "focus_failed"
PREVIOUS_PENDING = "previous_pending"  # a press while an earlier session is still pending: ignored
# Final pass reasons the session adds to those of ``quill.finalpass`` (the streaming text is typed).
PASS_FAILED = "pass_failed"  # the final pass raised or returned no outcome
PASS_EMPTY_TEXT = "pass_empty_text"  # the pass text left nothing to type after the text pipeline
# Reason code of a mouse 5 hold into Claude Code without a project folder: no reply is looked up.
REPLY_NO_PROJECT = "no_project"
# How long the release waits for the last reply lookup the hint thread started.
REPLY_WAIT_S = 0.3

# What the indicator says (European Portuguese); the reason codes stay in the logs.
MESSAGES = {
    MIC_ERROR: "Microfone indisponível; verifique o headset",
    ENGINE_ERROR: "O reconhecimento de voz falhou",
    ENGINE_TIMEOUT: "O reconhecimento de voz demorou demasiado",
    NO_SPEECH: "Não ouvi nada",
    COMMAND_UNAVAILABLE: "O modo comando não está disponível",
    VOICE_UNAVAILABLE: "Os comandos de voz não estão disponíveis",
    MODEL_UNAVAILABLE: "Modelo de voz indisponível",
    INTERNAL_ERROR: "Erro interno; o texto não foi escrito",
    NOT_CLAUDE: "Não é o Claude Code: escrito sem Enter",
    ENTER_FAILED: "Texto escrito, mas o Enter não foi enviado",
    ENTER_WITHHELD: "O destino deixou de ser o Claude Code: escrito sem Enter",
    CLEANUP_FALLBACK: "Ollama indisponível: texto limpo pelas regras",
    FOCUS_FAILED: "Nenhum campo de texto sob o ponteiro",
    PREVIOUS_PENDING: "Aguarde: o ditado anterior ainda está a ser escrito",
    inject.NO_TARGET: "Nenhum campo de texto sob o ponteiro",
    inject.TARGET_GONE: "A janela de destino fechou",
    inject.FOREGROUND_CHANGED: "A janela ativa mudou; o texto não foi escrito",
    inject.TARGET_ELEVATED: "Janela de administrador: não é possível escrever",
    inject.TARGET_ACCESS_DENIED: "Sem acesso à janela de destino",
    inject.TARGET_NOT_RESPONDING: "A janela de destino não responde",
    inject.MODIFIER_HELD: "Solte Ctrl, Alt ou Windows e tente de novo",
    inject.SENDINPUT_FAILED: "O Windows bloqueou a escrita",
    focus_reasons.OWN_WINDOW: "Nenhum campo de texto sob o ponteiro",
    focus_reasons.NO_WINDOW: "Nenhum campo de texto sob o ponteiro",
    focus_reasons.NO_POINTER: "Nenhum campo de texto sob o ponteiro",
    focus_reasons.BUTTON_HELD: "Solte o outro botão do rato e tente de novo",
    focus_reasons.FOCUS_NOT_MOVED: "O campo não recebeu o foco; tente de novo",
    **commands.MESSAGES,
    **autorewrite.MESSAGES,
    **enrich.MESSAGES,
    enrich.ENRICHED: "Prompt enriquecido e enviado",
}
INTERRUPTED = " (escrita interrompida)"
# Endings where the whole text was typed but something is worth showing.
TYPED_NOTICES = frozenset({NOT_CLAUDE, ENTER_FAILED, ENTER_WITHHELD, CLEANUP_FALLBACK, *autorewrite.MESSAGES,
                           *enrich.MESSAGES, enrich.ENRICHED})
# The words line of the ``reviewing`` state while the model enriches a mouse 5 prompt.
ENRICHING = "A enriquecer o prompt para o Claude Code…"
# PCM16 mono at 16 kHz: bytes per second of audio.
AUDIO_BYTES_PER_S = 32_000

ERROR_SHOW_S = 4.0
SENT_SHOW_S = 1.5
VOICE_OPEN_SHOW_S = 2.5
VOICE_NONE_SHOW_S = 6.0
ALERT_SHOW_S = 6.0
ALERT_REPEAT_S = 5.0
BUSY_SHOW_S = 2.0  # the "Aguarde" notice, then the pending session's words again
PENDING_LIMIT_S = 120.0  # a session pending longer than this (a hang) no longer blocks a press
# Claude Code alert kind -> indicator state; the text when a permission request also covers a finished reply.
ALERT_STATES = {sound.DONE: CLAUDE_DONE, sound.PERMISSION: CLAUDE_PERMISSION}
ALSO_DONE = "Aprove ou recuse o pedido; outra resposta também terminou"
# What each alert kind says after a project name.
ALERT_PHRASES = {sound.DONE: "Claude acabou", sound.PERMISSION: "Claude pede permissão"}
MAX_ALERTS = 24  # distinct (kind, project) kept waiting; beyond it the oldest finished reply is dropped
FINAL_TIMEOUT_S = 30.0
POLL_S = 1.0
STOP_TIMEOUT_S = 10.0


def alert_text(alerts: list[tuple[str, str | None]]) -> tuple[str, str]:
    """(kind shown, words line) of the waiting (kind, project) alerts, in arrival order.

    Named alerts read '<name>: Claude acabou' and '<name>: Claude pede
    permissão', several names of one kind sharing the phrase; the finished
    replies come first and the permission requests last. A nameless alert
    of a kind that has a named one adds nothing; alone among named ones it
    shows its phrase without a name. Nameless alerts only keep the plain
    texts (the state's placeholder, or ``ALSO_DONE``).
    """
    kinds = {kind for kind, _ in alerts}
    shown = sound.PERMISSION if sound.PERMISSION in kinds else sound.DONE
    names: dict[str, list[str]] = {kind: [] for kind in ALERT_PHRASES}
    for kind, project in alerts:
        if project and project not in names[kind]:
            names[kind].append(project)
    if not any(names.values()):
        return shown, ALSO_DONE if shown == sound.PERMISSION and sound.DONE in kinds else ""
    parts = [f"{', '.join(names[kind])}: {ALERT_PHRASES[kind]}" if names[kind] else ALERT_PHRASES[kind]
             for kind in (sound.DONE, sound.PERMISSION) if kind in kinds]
    return shown, "; ".join(parts)


def _keep(alerts: list[tuple[str, str | None]], entry: tuple[str, str | None]) -> None:
    """Add ``entry`` once to ``alerts``; beyond ``MAX_ALERTS`` the oldest finished reply is dropped."""
    if entry in alerts:
        return
    if len(alerts) >= MAX_ALERTS:
        kinds = [kind for kind, _ in alerts]
        alerts.pop(kinds.index(sound.DONE) if sound.DONE in kinds else 0)
    alerts.append(entry)


def message(reason: str, typed: int = 0) -> str:
    text = MESSAGES.get(reason, MESSAGES[INTERNAL_ERROR])
    return text + INTERRUPTED if typed else text


def voice_level(pcm: bytes) -> float:
    """Loudness of a PCM16 chunk from 0 (-50 dBFS or less) to 1 (-10 dBFS or more)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    if rms <= 0.0:
        return 0.0
    return max(0.0, min(1.0, (20.0 * math.log10(rms) + 50.0) / 40.0))


@dataclass(frozen=True)
class Processed:
    """The text pipeline's result for one session."""

    text: str = field(repr=False)
    profile: str
    claude_code: bool
    # A reason code worth showing although the text was typed (CLEANUP_FALLBACK).
    notice: str | None = None
    # Context of the automatic rewrite: words kept verbatim, the project of the
    # window (``quill.projects``, else the hint read from its title), that
    # project's local folder when known, and the profile its layout follows
    # (None: ``profile``).
    keep: tuple[str, ...] = field(default=(), repr=False)
    project: str = field(default="", repr=False)
    project_folder: Path | None = field(default=None, repr=False)
    rewrite_profile: str | None = None
    # How line breaks are typed (``quill.inject`` newline policy).
    newline: str = NEWLINE_SPACE
    # The target window as the pipeline described it (``quill.profiles.WindowInfo``; None: unknown),
    # compared again before a send trigger's Enter.
    window: object | None = field(default=None, repr=False)
    # The key a send trigger presses after the text: "enter", or "ctrl+enter" (``[claude_code] send_key``).
    send_key: str = "enter"


class TextPipeline(Protocol):
    def __call__(self, raw: str, target: Target) -> Processed: ...


@dataclass(frozen=True)
class Outcome:
    """How a session ended: numbers and reason codes only, never text."""

    session: int
    action: str
    reason: str
    typed: int = 0
    release_to_typed_s: float | None = None
    rewrite: str | None = None  # the automatic rewrite's reason code, when it was asked
    notice: str | None = None  # a notice shown with a text that was typed (for example after Enter)
    enrichment: str | None = None  # the enrichment's reason code, when it was asked
    final_pass: str | None = None  # the final pass's reason code, when it was asked (mouse 5)


@dataclass(eq=False)
class _Hold:
    number: int
    action: str
    trigger: str
    loading: bool = False
    ignored: bool = False  # pressed while an earlier session was pending: nothing is recorded
    error: str | None = None  # set when the session failed before its release
    capture: object | None = None
    asr: object | None = None
    capturing: bool = False
    target: Target | None = None
    focus_reason: str = ""
    live_text: str = field(default="", repr=False)
    handle: object | None = None
    released_at: float = 0.0
    ended: bool = False
    audio_bytes: int = 0
    reviewing: bool = False
    enriching: bool = False
    reply: _Reply = field(default_factory=lambda: _Reply())


@dataclass(eq=False)
class _Reply:
    """The last Claude Code reply of one mouse 5 hold: looked up at most once (hint thread or release)."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False  # a lookup started (or was decided against): never a second one
    folder: object | None = field(default=None, repr=False)
    context: object | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _Passed:
    """The final pass of a mouse 5 session: its reason code and how long it took (seconds)."""

    reason: str
    seconds: float


_STOP = object()


def _later(seconds: float, job: Callable[[], None]) -> None:
    timer = threading.Timer(seconds, job)
    timer.daemon = True
    timer.start()


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="quill-hints", daemon=True).start()


class _SafeIndicator:
    """The indicator behind a guard: a failing overlay never breaks a session.

    ``changes`` counts what replaced the words on screen (show, hide, set_text).
    """

    CHANGES = frozenset({"show", "hide", "set_text"})

    def __init__(self, indicator: object) -> None:
        self.indicator = indicator
        self.errors = 0
        self.changes = 0

    def __getattr__(self, name: str) -> Callable[..., None]:
        method = getattr(self.indicator, name)

        def call(*args: object, **kwargs: object) -> None:
            if name in self.CHANGES:
                self.changes += 1
            try:
                method(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the text shown may not be logged: the type only
                self.errors += 1
                log.error("indicator %s failed (%s)", name, type(exc).__name__)

        return call


class SessionManager:
    """Runs trigger signals as ordered dictation sessions.

    ``transcriber`` is a ``quill.streaming.StreamingTranscriber`` (``open``);
    ``capture_factory(on_data)`` returns an unstarted capture (``start``,
    ``stop``) that delivers PCM16 16 kHz chunks to ``on_data``; ``focus`` is
    a ``quill.focus.ClickToFocus``; ``injector`` a ``quill.inject.Injector``;
    ``indicator`` a ``quill.indicator.Indicator``; ``pipeline(raw, target)``
    returns a ``Processed``; ``command`` is a ``quill.command.CommandMode``
    (None: the command trigger only says it is unavailable); ``rewriter`` is a
    ``quill.autorewrite.AutoRewriter`` (None: no automatic rewrite); ``voice``
    is a ``quill.voice.VoiceCommands`` (None: the voice trigger only says it
    is unavailable) and ``voice_hints()`` returns the decoding hints of a
    voice session (``quill.whisper.SessionHints``; None: the vocabulary hints).
    ``voice_transcriber`` decodes the voice sessions with its own model once it
    is ready (None, still loading or failed: ``transcriber`` decodes them).
    ``on_session_start(action)`` runs when a hold starts recording (before the
    microphone opens; it must return at once) and
    ``on_typed(target, text, original, newline)`` after a successful
    injection (manual-edit detection, the correction key and the undo key):
    ``text`` as typed, and ``original`` the text a rewrite replaced, as it
    would be typed (None when there is no rewrite to undo); ``housekeeping``
    runs on the finalizer thread about every ``poll_s``. ``player`` plays the
    Claude Code alert sounds (``quill.sound``; None: the alerts are silent) and
    ``speaker`` says the project names after them (``quill.speech.Speaker``;
    None: only the chime). ``context_pack(folder)`` returns the context pack
    of a project folder or None (``quill.context_pack.ContextPacks.get``;
    None: mouse 5 corrects and enriches without a pack). ``enter_check(target,
    window)`` returns None when the send trigger may press Enter in ``target``
    now, else a reason code (None: no check beyond the injector's own).
    ``schedule(seconds, job)`` runs ``job`` once after ``seconds`` on another
    thread (None: a daemon ``threading.Timer``); it brings back the pending
    session's words after the "Aguarde" notice. ``send_hints(target)``
    returns the decoding hints of a mouse 5 session once click-to-focus has
    named its window (``quill.whisper.SessionHints`` or a hint source such as
    ``quill.heard.HeardHints``: the project's terms in Claude Code; None: the
    vocabulary hints); it runs through
    ``run_hints(job)`` (None: a daemon thread), never on the hook or session
    thread, and hints that arrive after the release are not used.
    ``final_pass(asr, final, target)`` (None: none) runs on the finalizer
    thread for ``send_polished`` sessions only, with the session's streaming
    session (``quill.streaming.Session``), its final result and its target; it
    returns ``(outcome, window)``: a ``quill.finalpass.FinalPassOutcome`` (text,
    reason, ``log_fields()``) and the target window it described (given to the
    pipeline as ``window``).
    ``last_reply(folder)`` (None: no reply context) returns the reply context
    of a project folder (``quill.claude_reply.ReplyFound``: ``context`` and
    ``log_fields()``); with it ``send_hints`` is called as
    ``send_hints(target, lookup)``, where ``lookup(folder)`` gives the hold's
    reply context (see the module docstring).
    """

    def __init__(self, *, transcriber: object, capture_factory: Callable[[Callable[[bytes], None]], object],
                 focus: object, injector: object, indicator: object, pipeline: TextPipeline,
                 command: object | None = None, rewriter: object | None = None, voice: object | None = None,
                 voice_hints: Callable[[], object] | None = None, voice_transcriber: object | None = None,
                 on_session_start: Callable[[str], None] | None = None,
                 on_typed: Callable[[Target, str, str | None, str], None] | None = None,
                 housekeeping: Callable[[], None] | None = None,
                 player: object | None = None, speaker: object | None = None,
                 context_pack: Callable[[Path], object | None] | None = None,
                 enter_check: Callable[[Target | None, object | None], str | None] | None = None,
                 alert_repeat_s: float = ALERT_REPEAT_S,
                 schedule: Callable[[float, Callable[[], None]], None] | None = None,
                 send_hints: Callable[[Target], object | None] | None = None,
                 run_hints: Callable[[Callable[[], None]], None] | None = None,
                 final_pass: Callable[[object, object, Target], tuple[object, object]] | None = None,
                 last_reply: Callable[[Path], object] | None = None, reply_wait_s: float = REPLY_WAIT_S,
                 pending_limit_s: float = PENDING_LIMIT_S,
                 clock: Callable[[], float] = time.perf_counter,
                 final_timeout_s: float = FINAL_TIMEOUT_S, poll_s: float = POLL_S) -> None:
        self.transcriber = transcriber
        self.capture_factory = capture_factory
        self.focus = focus
        self.injector = injector
        self.indicator = _SafeIndicator(indicator)
        self.pipeline = pipeline
        self.command = command
        self.rewriter = rewriter
        self.voice = voice
        self.voice_hints = voice_hints
        self.voice_transcriber = voice_transcriber
        self.on_session_start = on_session_start
        self.on_typed = on_typed
        self.housekeeping = housekeeping
        self.player = player
        self.speaker = speaker
        self.context_pack = context_pack
        self.enter_check = enter_check
        self.alert_repeat_s = alert_repeat_s
        self.schedule = schedule or _later
        self.send_hints = send_hints
        self.run_hints = run_hints or _in_thread
        self.final_pass = final_pass
        self.last_reply = last_reply
        self.reply_wait_s = reply_wait_s
        self.pending_limit_s = pending_limit_s
        self.clock = clock
        self.final_timeout_s = final_timeout_s
        self.poll_s = poll_s
        self.outcomes: deque[Outcome] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._ready = False
        self._load_error = False
        self._closed = True
        self._numbers = 0
        self._active: _Hold | None = None
        self._live: list[_Hold] = []
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        # Claude Code alerts: the (kind, project) waiting (in arrival order), until
        # when an outcome stays on screen, and when the last alert sound played.
        self._alerts: list[tuple[str, str | None]] = []
        self._quiet_until = 0.0
        self._last_ring: float | None = None
        # The alerts on screen and until when: a new alert is shown together with them.
        self._on_screen: list[tuple[str, str | None]] = []
        self._on_screen_until = 0.0

    # ------------------------------------------------------------ lifecycle

    @property
    def ready(self) -> bool:
        return self._ready

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("session manager already started")
            self._closed = False
            self._ready = self._load_error = False
            self._active = None
            self._live = []
            self._alerts = []
            self._quiet_until = 0.0
            self._last_ring = None
            self._on_screen = []
            self._jobs = queue.SimpleQueue()
            self._thread = threading.Thread(target=self._run, args=(self._jobs,), name="quill-sessions", daemon=True)
            self._thread.start()

    def loaded(self, error: bool = False) -> None:
        """The model finished loading (``error``: it could not be loaded)."""
        with self._lock:
            self._ready = not error
            self._load_error = error
            if self._live or self._active is not None and not self._active.loading:
                return
            self._on_screen = []
            if error:
                self._show_timed(ERROR, MESSAGES[MODEL_UNAVAILABLE], ERROR_SHOW_S)
            else:
                self.indicator.hide()
            self._deliver_alerts()

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """Cancel the hold in progress, finish the queued sessions and end the finalizer."""
        with self._lock:
            thread, jobs = self._thread, self._jobs
            self._closed = True
            hold, self._active = self._active, None
            self._alerts = []
            self._silence()
        if hold is not None:
            self._release(hold)
            self._end(hold, CANCELLED)
        if thread is None:
            return
        jobs.put(_STOP)
        thread.join(timeout_s)
        if thread.is_alive():
            log.error("session finalizer did not stop in time")
        with self._lock:
            self._thread = None

    def submit(self, job: Callable[[], None]) -> bool:
        """Run ``job`` on the finalizer thread after the sessions released before it; False when stopped."""
        with self._lock:
            if self._closed or self._thread is None:
                return False
            jobs = self._jobs
        jobs.put(job)
        return True

    def notify(self, state: str | None, text: str = "", hide_after_s: float = ERROR_SHOW_S) -> bool:
        """Show ``state`` with ``text`` (None: hide) unless a session is live: its words stay on screen."""
        with self._lock:
            if self._live or self._active is not None:
                return False
            self._on_screen = []
            if state is None:
                self.indicator.hide()
            else:
                self._show_timed(state, text, hide_after_s)
            return True

    def alert(self, kind: str, project: str | None = None) -> bool:
        """A Claude Code alert (``quill.sound`` kind) of ``project`` (None: unnamed): shown now,
        or kept until no session is live."""
        if kind not in ALERT_STATES:
            raise ValueError("unknown alert kind")
        if project is not None and not isinstance(project, str):
            raise ValueError("the alert project must be a string")
        entry = (kind, project or None)
        with self._lock:
            if self._closed:
                return False
            _keep(self._alerts, entry)
            waiting = not self._deliver_alerts()
        if waiting:
            log.info("claude alert %s waits for the current session", kind)
        return True

    def poll_alerts(self) -> None:
        """Show the alerts kept while a session was live, once none is (finalizer thread)."""
        with self._lock:
            self._deliver_alerts()

    # ------------------------------------------------------------ signals (hook worker thread)

    def handle(self, signal: Signal) -> None:
        with self._lock:
            current = self._active
        try:
            if signal.kind == START:
                self._start(signal)
            elif signal.kind == CONFIRM:
                self._confirm()
            elif signal.kind == STOP:
                self._stop()
            elif signal.kind == CANCEL:
                self._cancel(signal.reason)
        except Exception as exc:  # noqa: BLE001 - one broken session never stops the next
            log.error("session signal %s failed (%s)", signal.kind, type(exc).__name__)
            with self._lock:
                newest, self._active = self._active, None
            # The hold the signal was about (already taken off ``_active`` by
            # stop and cancel) and any hold a failed start left behind.
            for hold in (current, newest):
                if hold is not None and not hold.ended and hold.handle is None:
                    self._fail(hold, INTERNAL_ERROR)

    def _start(self, signal: Signal) -> None:
        with self._lock:
            if self._closed:
                return
            if self._active is not None:  # the machine allows one hold; a stale one is dropped
                stale, self._active = self._active, None
            else:
                stale = None
            self._numbers += 1
            hold = _Hold(self._numbers, signal.action, signal.trigger)
            self._active = hold
            pending = self._pending() if self._ready else None
            token = None
            # From here no alert sound or spoken name may start, and one still playing stops before the
            # microphone opens.
            self._silence()
            self._on_screen = []
            if not self._ready:
                hold.loading = True
                if self._load_error:
                    self._show_timed(ERROR, MESSAGES[MODEL_UNAVAILABLE], ERROR_SHOW_S)
                else:
                    self.indicator.show(LOADING)
            elif pending is not None:
                hold.ignored = True
                token = self._show_busy(pending)
            elif hold.action == COMMAND_ACTION and self.command is None:
                hold.error = COMMAND_UNAVAILABLE
            elif hold.action == VOICE_ACTION and self.voice is None:
                hold.error = VOICE_UNAVAILABLE
            else:
                self._live.append(hold)
                self.indicator.show({COMMAND_ACTION: COMMAND, VOICE_ACTION: VOICE}.get(hold.action, LISTENING))
        if stale is not None:
            self._fail(stale, INTERNAL_ERROR)
        if hold.loading:
            log.info("session %d: %s pressed while the model is %s; not recording", hold.number, hold.action,
                     "unavailable" if self._load_error else "loading")
            return
        if hold.ignored:
            log.info("session %d: %s pressed while session %d is still pending; ignored (%s)", hold.number,
                     hold.action, pending.number, PREVIOUS_PENDING)
            self._record(hold, PREVIOUS_PENDING)
            if token is not None:
                try:
                    self.schedule(BUSY_SHOW_S, lambda: self._restore(pending, token))
                except Exception as exc:  # noqa: BLE001 - the pending session's next state still shows
                    log.error("indicator restore could not be scheduled (%s)", type(exc).__name__)
            return
        if hold.error is not None:
            self._show_error(hold, hold.error)
            self._record(hold, hold.error)
            return
        log.info("session %d: %s started (%s)", hold.number, hold.action, hold.trigger)
        if self.on_session_start is not None:
            try:
                self.on_session_start(hold.action)
            except Exception as exc:  # noqa: BLE001
                log.error("session start hook failed (%s)", type(exc).__name__)
        hints = self._session_hints(hold)
        extra = {} if hints is None else {"hints": hints}
        try:
            transcriber = self._transcriber_for(hold)
            hold.asr = transcriber.open(on_partial=lambda partial: self._on_partial(hold, partial), **extra)
            capture = self.capture_factory(lambda pcm: self._on_audio(hold, pcm))
            capture.start()
        except Exception as exc:  # noqa: BLE001 - microphone missing, busy or unplugged
            log.warning("session %d: microphone could not start (%s)", hold.number, type(exc).__name__)
            self._fail(hold, MIC_ERROR, keep_active=True)
            return
        with self._lock:
            hold.capture = capture
            hold.capturing = True

    def _transcriber_for(self, hold: _Hold) -> object:
        """The voice model for a voice session when it is loaded; the engine model otherwise."""
        voice = self.voice_transcriber
        if hold.action != VOICE_ACTION or voice is None:
            return self.transcriber
        if voice.ready.is_set() and not voice.load_error:
            return voice
        log.info("session %d: voice model %s; decoding with the engine model", hold.number,
                 "not loaded" if voice.load_error else "still loading")
        return self.transcriber

    def _session_hints(self, hold: _Hold) -> object | None:
        """The decoding hints of a voice session; None (the vocabulary hints) for any other or on failure."""
        if hold.action != VOICE_ACTION or self.voice_hints is None:
            return None
        try:
            return self.voice_hints()
        except Exception as exc:  # noqa: BLE001 - never stop the capture for its hints
            log.error("session %d: voice hints failed (%s)", hold.number, type(exc).__name__)
            return None

    def _confirm(self) -> None:
        with self._lock:
            hold = self._active
        if hold is None or hold.loading or hold.ignored or hold.error is not None:
            return
        if hold.action not in CLICK_ACTIONS and hold.action != COMMAND_ACTION:
            return
        try:
            result = self.focus.on_confirm(hold.action, hold.trigger)
        except Exception as exc:  # noqa: BLE001
            log.error("session %d: click-to-focus failed (%s)", hold.number, type(exc).__name__)
            hold.target, hold.focus_reason = None, FOCUS_FAILED
            return
        hold.target, hold.focus_reason = result.target, result.reason
        if hold.action == SEND_POLISHED and self.send_hints is not None and result.target is not None:
            target = result.target
            try:
                self.run_hints(lambda: self._apply_hints(hold, target))
            except Exception as exc:  # noqa: BLE001 - the session decodes with the vocabulary hints
                log.error("session %d: decoding hints not started (%s)", hold.number, type(exc).__name__)

    def _apply_hints(self, hold: _Hold, target: Target) -> None:
        """The project hints of a mouse 5 session, given to its transcription unless it was released (hint thread)."""
        try:
            if self.last_reply is not None:
                hints = self.send_hints(target, lambda folder: self._reply_for(hold, folder))
            else:
                hints = self.send_hints(target)
        except Exception as exc:  # noqa: BLE001 - the vocabulary hints decode instead
            log.error("session %d: decoding hints failed (%s)", hold.number, type(exc).__name__)
            return
        if hints is None:
            return
        applied = False
        setter = getattr(hold.asr, "set_hints", None)
        if setter is not None:
            try:
                applied = bool(setter(hints))
            except Exception as exc:  # noqa: BLE001
                log.error("session %d: decoding hints not applied (%s)", hold.number, type(exc).__name__)
                return
        log.info("session %d: project hints %s", hold.number, "applied" if applied else "too late")

    def _reply_for(self, hold: _Hold, folder: Path | None) -> object | None:
        """The reply context of ``hold`` for project ``folder`` (hint thread or finalizer); None: no reply.

        The first call looks it up (``folder`` None: none, logged as
        ``REPLY_NO_PROJECT``); a later call waits for that lookup at most
        ``reply_wait_s`` and gets its context only for the same folder.
        """
        slot = hold.reply
        with slot.lock:
            first = not slot.claimed
            if first:
                slot.claimed, slot.folder = True, folder
        if not first:
            if not slot.done.wait(self.reply_wait_s):
                log.info("session %d: last reply not ready after %.0f ms; going on without it", hold.number,
                         self.reply_wait_s * 1000)
                return None
            if slot.context is not None and slot.folder != folder:
                log.info("session %d: last reply of another project folder; going on without it", hold.number)
                return None
            return slot.context
        context = None
        try:
            if folder is None:
                log.info("session %d: last reply %s", hold.number, REPLY_NO_PROJECT)
            else:
                found = self.last_reply(folder)
                log.info("session %d: %s", hold.number, found.log_fields())
                context = found.context or None
        except Exception as exc:  # noqa: BLE001 - never lose the dictation for its context
            log.error("session %d: last reply failed (%s)", hold.number, type(exc).__name__)
            context = None
        finally:
            slot.context = context
            slot.done.set()
        return context

    def _cancel(self, reason: str) -> None:
        with self._lock:
            hold, self._active = self._active, None
        if hold is None:
            return
        log.info("session %d: cancelled (%s)", hold.number, reason or "unknown")
        if hold.ignored:
            self._idle()
            return
        if hold.loading or hold.error is not None:
            self._record(hold, hold.error or WHILE_LOADING)
            return
        self._release(hold)
        self._end(hold, CANCELLED)

    def _stop(self) -> None:
        with self._lock:
            hold, self._active = self._active, None
        if hold is None:
            return
        if hold.loading:
            log.info("session %d: released while loading; nothing recorded", hold.number)
            self._record(hold, WHILE_LOADING)
            return
        if hold.ignored:
            log.info("session %d: released; it was ignored (%s)", hold.number, PREVIOUS_PENDING)
            self._idle()
            return
        if hold.error is not None:
            self._record(hold, hold.error)
            return
        capture, hold.capture = hold.capture, None
        with self._lock:
            hold.capturing = False
        try:
            if capture is not None:
                capture.stop()
        except Exception as exc:  # noqa: BLE001 - the device failed while recording
            log.warning("session %d: microphone failed while recording (%s)", hold.number, type(exc).__name__)
            self._fail(hold, MIC_ERROR)
            return
        if hold.target is None and hold.action != VOICE_ACTION:  # a voice command needs no target
            if hold.action == COMMAND_ACTION:
                self._fail(hold, commands.NO_TARGET)
            else:
                self._fail(hold, hold.focus_reason if hold.focus_reason in MESSAGES else FOCUS_FAILED)
            return
        hold.handle = hold.asr.release()
        hold.released_at = self.clock()
        with self._lock:
            if self._visible(hold):
                self.indicator.show(TRANSCRIBING, hold.live_text)
            jobs = self._jobs
        jobs.put(hold)

    # ------------------------------------------------------------ callbacks (capture and engine threads)

    def _on_audio(self, hold: _Hold, pcm: bytes) -> None:
        hold.audio_bytes += len(pcm)
        asr = hold.asr
        if asr is not None:
            asr.feed(pcm)
        level = voice_level(pcm)
        with self._lock:
            if hold.capturing and self._visible(hold):
                self.indicator.set_level(level)

    def _on_partial(self, hold: _Hold, partial: object) -> None:
        with self._lock:
            if not hold.capturing:
                return
            hold.live_text = partial.text
            if self._visible(hold):
                self.indicator.set_text(hold.live_text)

    # ------------------------------------------------------------ finalizer thread

    def _run(self, jobs: queue.SimpleQueue) -> None:
        while True:
            try:
                item = jobs.get(timeout=self.poll_s)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            if isinstance(item, _Hold):
                self._finalize(item)
            elif item is not None:
                self._run_job(item)
            self.poll_alerts()
            self._housekeeping()

    def _run_job(self, job: Callable[[], None]) -> None:
        if self._closed:
            log.info("queued job skipped: Quill is stopping")
            return
        try:
            job()
        except Exception as exc:  # noqa: BLE001
            log.error("queued job failed (%s)", type(exc).__name__)

    def _housekeeping(self) -> None:
        if self.housekeeping is None:
            return
        try:
            self.housekeeping()
        except Exception as exc:  # noqa: BLE001
            log.error("housekeeping failed (%s)", type(exc).__name__)

    def _finalize(self, hold: _Hold) -> None:
        try:
            result = hold.handle.wait(self.final_timeout_s)
            engine_at = self.clock()
            if result is None:
                self._fail(hold, ENGINE_TIMEOUT)
                return
            if not result.ok:
                log.warning("session %d: engine failed (%s)", hold.number, result.error)
                self._fail(hold, ENGINE_ERROR)
                return
            switches = getattr(result, "hint_switches", 0)
            if isinstance(switches, int) and switches > 0:
                log.info("session %d: heard-term hints switched %d times", hold.number, switches)
            if hold.action == COMMAND_ACTION:
                self._finalize_command(hold, result.text, engine_at)
                return
            if hold.action == VOICE_ACTION:
                self._finalize_voice(hold, result.text, engine_at)
                return
            if not result.text.strip():
                self._fail(hold, NO_SPEECH)
                return
            passed = None
            if hold.action == SEND_POLISHED and self.final_pass is not None:
                passed, processed = self._final_pass(hold, result)
            else:
                processed = self.pipeline(result.text, hold.target)
            pass_at = engine_at if passed is None else engine_at + passed.seconds
            if not processed.text.strip():
                self._fail(hold, NO_SPEECH)
                return
            text, original, rewrite = processed.text, None, None
            pipeline_at = self.clock()
            force = hold.action == SEND_POLISHED
            # Mouse 5 into Claude Code: the project's context pack, then the enrichment.
            polish = force and processed.claude_code
            reply = None
            if polish and self.rewriter is not None and self.last_reply is not None:
                reply = self._reply_for(hold, processed.project_folder)
            if self.rewriter is not None and (force or self._wants_rewrite(hold, processed)):
                rewrite = self._rewrite(hold, processed, force, polish, reply)
                if rewrite.rewritten:
                    # The original is always the dictation before correction and enrichment.
                    text, original = rewrite.text, processed.text
                    if rewrite.enriched and processed.newline == NEWLINE_SPACE:
                        text = enrich.one_paragraph(text)
            text_at = self.clock()
            typed = self.injector.inject(text, hold.target, InjectOptions(newline=processed.newline))
            if not typed.ok:
                self._fail(hold, typed.reason, typed=typed.typed)
                return
            typed_at = self.clock()
            latency = typed_at - hold.released_at
            enrichment = (rewrite.enrichment or None) if rewrite is not None else None
            log.info("session %d: typed (%s profile%s%s); release to typed %.0f ms (engine %.0f ms, %stext %.0f ms, "
                     "%styping %.0f ms)", hold.number, processed.profile,
                     f", rewrite {rewrite.reason}" if rewrite is not None else "",
                     f", {enrichment} in {rewrite.enrich_seconds * 1000:.0f} ms" if enrichment else "",
                     latency * 1000, (engine_at - hold.released_at) * 1000,
                     f"final pass {passed.reason} {passed.seconds * 1000:.0f} ms, " if passed is not None else "",
                     (pipeline_at - pass_at) * 1000,
                     f"rewrite {(text_at - pipeline_at) * 1000:.0f} ms, " if rewrite is not None else "",
                     (typed_at - text_at) * 1000)
            enter = hold.action in SEND_ACTIONS and processed.claude_code
            if self.on_typed is not None:
                # A rewrite can be undone only when no Enter follows it.
                undo = normalize_text(original, processed.newline) if original is not None and not enter else None
                try:
                    self.on_typed(hold.target, normalize_text(text, processed.newline), undo, processed.newline)
                except Exception as exc:  # noqa: BLE001
                    log.error("typed hook failed (%s)", type(exc).__name__)
            notice = processed.notice
            if rewrite is not None and rewrite.message:
                notice = rewrite.reason
            elif enrichment in MESSAGES:
                notice = enrichment  # enriched, or the corrected text sent instead
            reason = TYPED
            if hold.action in SEND_ACTIONS:
                if not processed.claude_code:
                    reason = NOT_CLAUDE
                elif not self._may_press_enter(hold, processed):
                    reason = ENTER_WITHHELD
                elif self._press_enter(hold, processed):
                    reason = SENT_ENTER
                else:
                    reason = ENTER_FAILED
            elif notice:
                reason = notice
            self._end(hold, reason, typed=typed.typed, latency=latency,
                      rewrite=rewrite.reason if rewrite is not None else None, notice=notice, enrichment=enrichment,
                      final_pass=passed.reason if passed is not None else None)
        except Exception as exc:  # noqa: BLE001 - the message may not carry text: log the type only
            log.error("session %d: finalization failed (%s)", hold.number, type(exc).__name__)
            self._fail(hold, INTERNAL_ERROR)

    def _final_pass(self, hold: _Hold, final: object) -> tuple[_Passed, Processed]:
        """(the pass's reason and time, the pipeline's result) of a mouse 5 session's final.

        The pipeline gets the pass text with the window the pass described; on
        any failure, or when the pass text leaves nothing to type, it gets the
        streaming text as it would without a pass.
        """
        began = self.clock()
        try:
            outcome, window = self.final_pass(hold.asr, final, hold.target)
            text, reason = outcome.text, outcome.reason
            if not isinstance(text, str) or not isinstance(reason, str):
                raise TypeError("final pass outcome without a text and a reason")
            log.info("session %d: %s", hold.number, outcome.log_fields())
        except Exception as exc:  # noqa: BLE001 - never lose the dictation: the streaming text is typed
            log.error("session %d: final pass failed (%s)", hold.number, type(exc).__name__)
            return _Passed(PASS_FAILED, self.clock() - began), self.pipeline(final.text, hold.target)
        passed = _Passed(reason, self.clock() - began)
        processed = self.pipeline(text, hold.target, window=window)
        if text == final.text or processed.text.strip():
            return passed, processed
        log.warning("session %d: final pass text left nothing to type; the streaming text is used", hold.number)
        return _Passed(PASS_EMPTY_TEXT, passed.seconds), self.pipeline(final.text, hold.target, window=window)

    def _wants_rewrite(self, hold: _Hold, processed: Processed) -> bool:
        if hold.action not in (DICTATION, SEND_CLAUDE):
            return False  # send_raw is never rewritten; send_polished always is (``force``)
        try:
            return bool(self.rewriter.wants(processed.text, hold.audio_bytes / AUDIO_BYTES_PER_S))
        except Exception as exc:  # noqa: BLE001 - a broken rewriter never holds up a dictation
            log.error("session %d: rewrite check failed (%s)", hold.number, type(exc).__name__)
            return False

    def _rewrite(self, hold: _Hold, processed: Processed, force: bool = False,
                 polish: bool = False, reply: object | None = None) -> autorewrite.AutoRewrite:
        """The rewrite of a long dictation (any dictation with ``force``); the original text on every failure.

        ``polish`` (mouse 5 into Claude Code) adds the project's context pack and the enrichment, and
        ``reply`` (the last Claude Code reply's context, when there is one) goes with them.
        """
        with self._lock:
            hold.reviewing = True
            if self._visible(hold):
                self.indicator.show(REVIEWING, hold.live_text)
        try:
            extra = {}
            if polish:
                extra = {"pack": self._pack(hold, processed), "enrich_prompt": True, "context": True,
                         "on_enrich": lambda: self._enriching(hold)}
                if reply:
                    extra["reply"] = reply
            result = self.rewriter.rewrite(processed.text, audio_s=hold.audio_bytes / AUDIO_BYTES_PER_S,
                                           profile=processed.rewrite_profile or processed.profile,
                                           keep=processed.keep, project=processed.project, force=force, **extra)
            if not isinstance(result.text, str) or not result.text.strip():
                raise ValueError("empty rewrite result")
            return result
        except Exception as exc:  # noqa: BLE001 - never lose the dictation: type the original
            log.error("session %d: rewrite failed (%s)", hold.number, type(exc).__name__)
            return autorewrite.AutoRewrite(processed.text, processed.text, autorewrite.FAILED, type(exc).__name__)
        finally:
            with self._lock:
                hold.reviewing = hold.enriching = False

    def _pack(self, hold: _Hold, processed: Processed) -> object | None:
        """The context pack of the window's project folder; None without a folder or on any failure."""
        folder = processed.project_folder
        if folder is None or self.context_pack is None:
            return None
        try:
            pack = self.context_pack(folder)
        except Exception as exc:  # noqa: BLE001 - a pack is optional: never lose the dictation for it
            log.error("session %d: context pack failed (%s)", hold.number, type(exc).__name__)
            return None
        log.info("session %d: context pack %s", hold.number, "found" if pack is not None else "none")
        return pack

    def _enriching(self, hold: _Hold) -> None:
        """The model starts enriching the corrected text (called by the rewriter on the finalizer thread)."""
        with self._lock:
            hold.enriching = True
            if self._visible(hold):
                self.indicator.show(REVIEWING, ENRICHING)

    def _finalize_command(self, hold: _Hold, instruction: str, engine_at: float) -> None:
        outcome = self.command.run(instruction, hold.target)
        latency = self.clock() - hold.released_at
        steps = ", ".join(f"{name[:-2]} {seconds * 1000:.0f} ms" for name, seconds in outcome.timings.items())
        log.info("session %d: command %s; release to done %.0f ms (engine %.0f ms%s)", hold.number, outcome.reason,
                 latency * 1000, (engine_at - hold.released_at) * 1000, f", {steps}" if steps else "")
        if outcome.ok:
            self._end(hold, outcome.reason, typed=outcome.typed, latency=latency)
        else:
            self._fail(hold, outcome.reason, typed=outcome.typed)

    def _finalize_voice(self, hold: _Hold, text: str, engine_at: float) -> None:
        """Run the spoken command; nothing is clicked, typed or entered here."""
        if not text.strip():
            self._fail(hold, NO_SPEECH)
            return
        outcome = self.voice.run(text)
        latency = self.clock() - hold.released_at
        log.info("session %d: voice %s; release to done %.0f ms (engine %.0f ms)", hold.number, outcome.reason,
                 latency * 1000, (engine_at - hold.released_at) * 1000)
        seconds = VOICE_OPEN_SHOW_S if outcome.state == VOICE_OPEN else VOICE_NONE_SHOW_S
        self._end(hold, outcome.reason, latency=latency, shown=(outcome.state, outcome.text, seconds))

    def _may_press_enter(self, hold: _Hold, processed: Processed) -> bool:
        """The target is still the Claude Code window the text was typed into; False on any doubt."""
        if self.enter_check is None:
            return True
        try:
            problem = self.enter_check(hold.target, processed.window)
        except Exception as exc:  # noqa: BLE001 - an unknown target never gets an Enter
            log.error("session %d: target check before Enter failed (%s)", hold.number, type(exc).__name__)
            return False
        if problem is not None:
            log.warning("session %d: Enter withheld (%s)", hold.number, problem)
            return False
        return True

    def _press_enter(self, hold: _Hold, processed: Processed) -> bool:
        try:
            if processed.send_key == CTRL_ENTER:
                result = self.injector.press_enter(hold.target, ctrl=True)
            else:
                result = self.injector.press_enter(hold.target)
        except Exception as exc:  # noqa: BLE001 - the text is typed already: report the Enter only
            log.error("session %d: Enter failed (%s)", hold.number, type(exc).__name__)
            return False
        if not result.ok:
            log.warning("session %d: Enter not sent (%s)", hold.number, result.reason)
        return result.ok

    # ------------------------------------------------------------ endings

    def _release(self, hold: _Hold) -> None:
        """Release the microphone and the transcription job of a hold; never raises."""
        with self._lock:
            hold.capturing = False
        capture, hold.capture = hold.capture, None
        if capture is not None:
            try:
                capture.stop()
            except Exception as exc:  # noqa: BLE001 - the device is released by stop on every path
                log.warning("session %d: capture stop failed (%s)", hold.number, type(exc).__name__)
        asr, hold.asr = hold.asr, None
        if asr is not None and (hold.handle is None or not hold.handle.done):
            try:
                asr.cancel()
            except Exception as exc:  # noqa: BLE001
                log.warning("session %d: transcription cancel failed (%s)", hold.number, type(exc).__name__)

    def _fail(self, hold: _Hold, reason: str, typed: int = 0, keep_active: bool = False) -> None:
        log.warning("session %d: %s failed (%s)", hold.number, hold.action, reason)
        self._release(hold)
        self._end(hold, reason, typed=typed)
        if keep_active:
            # The trigger is still held: its release must not start anything.
            with self._lock:
                if self._active is hold:
                    hold.error = reason

    def _end(self, hold: _Hold, reason: str, typed: int = 0, latency: float | None = None,
             rewrite: str | None = None, notice: str | None = None,
             shown: tuple[str, str, float] | None = None, enrichment: str | None = None,
             final_pass: str | None = None) -> None:
        """``shown`` (state, text, seconds) replaces the outcome the reason would show."""
        with self._lock:
            if hold.ended:
                return
            hold.ended = True
            hold.reviewing = hold.enriching = False
            if hold in self._live:
                self._live.remove(hold)
            if self._visible(hold):
                if shown is not None:
                    self._show_timed(*shown)
                else:
                    self._show_outcome(hold, reason, typed, notice)
            self.outcomes.append(Outcome(hold.number, hold.action, reason, typed, latency, rewrite, notice,
                                         enrichment, final_pass))
            self._deliver_alerts()

    def _idle(self) -> None:
        """An ignored press ended: the alerts kept meanwhile may show."""
        with self._lock:
            self._deliver_alerts()

    def _record(self, hold: _Hold, reason: str) -> None:
        with self._lock:
            if not hold.ended:
                hold.ended = True
                self.outcomes.append(Outcome(hold.number, hold.action, reason))
            self._deliver_alerts()

    def _show_error(self, hold: _Hold, reason: str) -> None:
        with self._lock:
            if self._visible(hold):
                self._show_timed(ERROR, message(reason), ERROR_SHOW_S)

    # Lock held from here on.

    def _show_timed(self, state: str, text: str, seconds: float) -> None:
        """Show an outcome for ``seconds``; alerts wait until it has been seen."""
        self.indicator.show(state, text, hide_after_s=seconds)
        self._quiet_until = max(self._quiet_until, self.clock() + seconds)
        self._on_screen = []

    def _silence(self) -> None:
        """Stop the chime and the spoken names (a pending one never starts), without waiting."""
        if self.player is not None:
            try:
                self.player.stop()
            except Exception as exc:  # noqa: BLE001
                log.error("alert sound stop failed (%s)", type(exc).__name__)
        if self.speaker is not None:
            try:
                self.speaker.stop()
            except Exception as exc:  # noqa: BLE001
                log.error("alert speech stop failed (%s)", type(exc).__name__)

    def _deliver_alerts(self) -> bool:
        """Show (and ring) the waiting alerts when nothing else needs the screen or the
        microphone; True when nothing is left waiting."""
        if not self._alerts:
            return True
        if self._closed or self._active is not None or self._live:
            return False
        if not self._ready and not self._load_error:
            return False  # the loading state is on screen and a press would record nothing yet
        now = self.clock()
        if now < self._quiet_until:
            return False
        alerts, self._alerts = self._alerts, []
        if self._on_screen and now < self._on_screen_until:
            shown = list(self._on_screen)
            for entry in alerts:
                _keep(shown, entry)
            alerts = shown
        self._on_screen, self._on_screen_until = list(alerts), now + ALERT_SHOW_S
        kind, text = alert_text(alerts)
        rang = False
        if self.player is not None and (self._last_ring is None or now - self._last_ring >= self.alert_repeat_s):
            self._last_ring = now
            try:
                self.player.play(kind)
                rang = True
            except Exception as exc:  # noqa: BLE001 - the indicator still shows the alert
                log.error("alert sound failed (%s)", type(exc).__name__)
            if rang and self.speaker is not None:
                try:
                    # Nameless alerts speak nothing (and end a speech still going, like the chime does).
                    self.speaker.say(speech.spoken_names(alerts))
                except Exception as exc:  # noqa: BLE001 - the chime played and the indicator shows the names
                    log.error("alert speech failed (%s)", type(exc).__name__)
        self.indicator.show(ALERT_STATES[kind], text, hide_after_s=ALERT_SHOW_S)
        log.info("claude alert %s shown (%d listed, %d named, %s)", kind, len(alerts),
                 sum(1 for _, project in alerts if project), "sound" if rang else "no sound")
        return True

    def _pending(self) -> _Hold | None:
        """The released session still being finalized; None when there is none (or it hangs past the limit)."""
        for hold in self._live:
            if hold.handle is None or hold.ended:
                continue  # still held, or already over
            waited = self.clock() - hold.released_at
            if waited < self.pending_limit_s:
                return hold
            log.warning("session %d still pending after %.0f s; a new press may start", hold.number, waited)
        return None

    def _show_pending(self, hold: _Hold, words: str | None = None) -> None:
        """Show the state of a session being finalized, with its own words or ``words``."""
        if hold.enriching:
            state, text = REVIEWING, ENRICHING
        else:
            state, text = REVIEWING if hold.reviewing else TRANSCRIBING, hold.live_text
        self.indicator.show(state, text if words is None else words)

    def _show_busy(self, pending: _Hold) -> int | None:
        """The "Aguarde" notice in the pending session's state; the token of what is now on screen, or None."""
        if not self._visible(pending):
            return None
        self._show_pending(pending, MESSAGES[PREVIOUS_PENDING])
        return self.indicator.changes

    def _restore(self, pending: _Hold, token: int) -> None:
        """After the notice, the pending session's words again, unless anything newer was shown meanwhile."""
        with self._lock:
            if (self._closed or pending.ended or pending not in self._live or not self._visible(pending)
                    or self.indicator.changes != token):
                return
            self._show_pending(pending)

    def _visible(self, hold: _Hold) -> bool:
        """No newer session is live (capturing or finalizing)."""
        return all(other.number <= hold.number for other in self._live)

    def _show_outcome(self, hold: _Hold, reason: str, typed: int, notice: str | None = None) -> None:
        if reason in (TYPED, CANCELLED, commands.REWRITTEN):
            older = self._live[-1] if self._live else None
            if older is not None:
                # An older session is still being finalized: show it again.
                self._show_pending(older)
            else:
                self.indicator.hide()
        elif reason == SENT_ENTER:
            if notice:
                self._show_timed(SENT, message(notice), ERROR_SHOW_S)
            else:
                self._show_timed(SENT, "", SENT_SHOW_S)
        else:
            interrupted = 0 if reason in TYPED_NOTICES else typed
            self._show_timed(ERROR, message(reason, interrupted), ERROR_SHOW_S)
