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
  session is queued for finalization; the hook thread is free again, so a new
  press starts capturing immediately.
- ``cancel`` (short tap, combo, hooks stopping): the capture and the
  transcription are released; nothing is typed.

One finalizer thread takes the released sessions strictly in release order:
it waits for the final text, runs the text pipeline (cleanup, vocabulary,
learned corrections, the target's profile), types the text once into the
session's own target and, for a send trigger (``send_polished``,
``send_raw``, the older ``send_claude``), presses one plain Enter only after
a successful injection into a Claude Code window; anywhere else the text
stays typed without Enter, with the ``not_claude`` notice. A command session hands
the final text, the spoken instruction, to ``quill.command.CommandMode``,
which copies the selection, rewrites it with the local model and types the
rewrite over it; the indicator shows ``command`` while it listens. A voice
session (the ``voice`` trigger) never clicks, captures no target, types
nothing and presses no Enter: its final text goes to ``quill.voice``
(``voice.run``), and the indicator shows ``voice`` with the live words while
it listens, then the command's outcome ("A abrir <nome>", the closest names
or the reason). Its transcription is opened with the ``voice_hints()`` of
that press (``quill.voice.VoiceHints``: Portuguese, the command prompt and
the project names); every other session keeps the transcriber's vocabulary
hints. Hints that cannot be built never stop the capture.

A long dictation (over the ``[autorewrite]`` audio or word threshold) goes,
after the text pipeline, to the ``rewriter`` (``quill.autorewrite``) while
the indicator shows ``reviewing`` ("A rever o texto"); a short one never
calls it, so its path is unchanged. The ``send_polished`` trigger sends
every dictation to the rewriter whatever its length (``force``) and
``send_raw`` never does. A failure, a timeout or a refusal by the
content guard types the original text at once and shows a short notice. The
text is typed with the target's newline policy (Shift+Enter in the Claude
Code panel, a space elsewhere), and a send trigger presses Enter only
after all of it is typed. A rewrite typed without Enter is reported to
``on_typed`` with its original, for the undo key. ``submit`` runs other
work (the undo key) on the finalizer thread, in order with the sessions.

``alert(kind)`` shows a Claude Code alert (``quill.notify``: Claude Code
finished its reply or asks for a permission) with a short sound from the
``player``. While a hold is recording, a session is being finalized, the
model is loading or an outcome is on screen, the alert waits: it is kept,
once per kind, and shown when all of them are over (the finalizer checks
about every ``poll_s``). The sound never plays into the microphone: a
starting hold stops it under the same lock that decides whether an alert may
play. Several alerts waiting together are shown once (a permission request
first, since it needs an answer) and ring once; an alert within
``alert_repeat_s`` of the last sound is shown without sound.

A failure (microphone,
engine, target gone, foreground changed, ...) ends that session with the
``error`` state and a European Portuguese message; the next session is not
affected. The capture, the transcription job and the indicator state are
released on every exit path.

The indicator shows the newest live session (capturing or finalizing): an
older session finishing while a newer one is live is logged, not shown, so
the live words stay on screen.

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
from typing import Protocol

from quill import autorewrite
from quill import command as commands
from quill import inject
from quill import focus as focus_reasons
from quill import sound
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
    CLEANUP_FALLBACK: "Ollama indisponível: texto limpo pelas regras",
    FOCUS_FAILED: "Nenhum campo de texto sob o ponteiro",
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
}
INTERRUPTED = " (escrita interrompida)"
# Endings where the whole text was typed but something is worth showing.
TYPED_NOTICES = frozenset({NOT_CLAUDE, ENTER_FAILED, CLEANUP_FALLBACK, *autorewrite.MESSAGES})
# PCM16 mono at 16 kHz: bytes per second of audio.
AUDIO_BYTES_PER_S = 32_000

ERROR_SHOW_S = 4.0
SENT_SHOW_S = 1.5
VOICE_OPEN_SHOW_S = 2.5
VOICE_NONE_SHOW_S = 6.0
ALERT_SHOW_S = 6.0
ALERT_REPEAT_S = 5.0
# Claude Code alert kind -> indicator state; the text when a permission request also covers a finished reply.
ALERT_STATES = {sound.DONE: CLAUDE_DONE, sound.PERMISSION: CLAUDE_PERMISSION}
ALSO_DONE = "Aprove ou recuse o pedido; outra resposta também terminou"
FINAL_TIMEOUT_S = 30.0
POLL_S = 1.0
STOP_TIMEOUT_S = 10.0


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
    # Context of the automatic rewrite: words kept verbatim, the project read from
    # the window title, and the profile its layout follows (None: ``profile``).
    keep: tuple[str, ...] = field(default=(), repr=False)
    project: str = field(default="", repr=False)
    rewrite_profile: str | None = None
    # How line breaks are typed (``quill.inject`` newline policy).
    newline: str = NEWLINE_SPACE


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


@dataclass(eq=False)
class _Hold:
    number: int
    action: str
    trigger: str
    loading: bool = False
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


_STOP = object()


class _SafeIndicator:
    """The indicator behind a guard: a failing overlay never breaks a session."""

    def __init__(self, indicator: object) -> None:
        self.indicator = indicator
        self.errors = 0

    def __getattr__(self, name: str) -> Callable[..., None]:
        method = getattr(self.indicator, name)

        def call(*args: object, **kwargs: object) -> None:
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
    ``on_session_start`` runs when a hold starts recording and
    ``on_typed(target, text, original, newline)`` after a successful
    injection (manual-edit detection, the correction key and the undo key):
    ``text`` as typed, and ``original`` the text a rewrite replaced, as it
    would be typed (None when there is no rewrite to undo); ``housekeeping``
    runs on the finalizer thread about every ``poll_s``. ``player`` plays the
    Claude Code alert sounds (``quill.sound``; None: the alerts are silent).
    """

    def __init__(self, *, transcriber: object, capture_factory: Callable[[Callable[[bytes], None]], object],
                 focus: object, injector: object, indicator: object, pipeline: TextPipeline,
                 command: object | None = None, rewriter: object | None = None, voice: object | None = None,
                 voice_hints: Callable[[], object] | None = None,
                 on_session_start: Callable[[], None] | None = None,
                 on_typed: Callable[[Target, str, str | None, str], None] | None = None,
                 housekeeping: Callable[[], None] | None = None,
                 player: object | None = None, alert_repeat_s: float = ALERT_REPEAT_S,
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
        self.on_session_start = on_session_start
        self.on_typed = on_typed
        self.housekeeping = housekeeping
        self.player = player
        self.alert_repeat_s = alert_repeat_s
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
        # Claude Code alerts: the kinds waiting (in arrival order), until when an
        # outcome stays on screen, and when the last alert sound played.
        self._alerts: list[str] = []
        self._quiet_until = 0.0
        self._last_ring: float | None = None

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
            if state is None:
                self.indicator.hide()
            else:
                self._show_timed(state, text, hide_after_s)
            return True

    def alert(self, kind: str) -> bool:
        """A Claude Code alert (``quill.sound`` kind): shown now, or kept until no session is live."""
        if kind not in ALERT_STATES:
            raise ValueError("unknown alert kind")
        with self._lock:
            if self._closed:
                return False
            if kind not in self._alerts:
                self._alerts.append(kind)
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
            # From here no alert sound may start, and one still playing stops before the microphone opens.
            self._silence()
            if not self._ready:
                hold.loading = True
                if self._load_error:
                    self._show_timed(ERROR, MESSAGES[MODEL_UNAVAILABLE], ERROR_SHOW_S)
                else:
                    self.indicator.show(LOADING)
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
        if hold.error is not None:
            self._show_error(hold, hold.error)
            self._record(hold, hold.error)
            return
        log.info("session %d: %s started (%s)", hold.number, hold.action, hold.trigger)
        if self.on_session_start is not None:
            try:
                self.on_session_start()
            except Exception as exc:  # noqa: BLE001
                log.error("session start hook failed (%s)", type(exc).__name__)
        hints = self._session_hints(hold)
        extra = {} if hints is None else {"hints": hints}
        try:
            hold.asr = self.transcriber.open(on_partial=lambda partial: self._on_partial(hold, partial), **extra)
            capture = self.capture_factory(lambda pcm: self._on_audio(hold, pcm))
            capture.start()
        except Exception as exc:  # noqa: BLE001 - microphone missing, busy or unplugged
            log.warning("session %d: microphone could not start (%s)", hold.number, type(exc).__name__)
            self._fail(hold, MIC_ERROR, keep_active=True)
            return
        with self._lock:
            hold.capture = capture
            hold.capturing = True

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
        if hold is None or hold.loading or hold.error is not None:
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

    def _cancel(self, reason: str) -> None:
        with self._lock:
            hold, self._active = self._active, None
        if hold is None:
            return
        log.info("session %d: cancelled (%s)", hold.number, reason or "unknown")
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
            if hold.action == COMMAND_ACTION:
                self._finalize_command(hold, result.text, engine_at)
                return
            if hold.action == VOICE_ACTION:
                self._finalize_voice(hold, result.text, engine_at)
                return
            if not result.text.strip():
                self._fail(hold, NO_SPEECH)
                return
            processed = self.pipeline(result.text, hold.target)
            if not processed.text.strip():
                self._fail(hold, NO_SPEECH)
                return
            text, original, rewrite = processed.text, None, None
            pipeline_at = self.clock()
            force = hold.action == SEND_POLISHED
            if self.rewriter is not None and (force or self._wants_rewrite(hold, processed)):
                rewrite = self._rewrite(hold, processed, force)
                if rewrite.rewritten:
                    text, original = rewrite.text, processed.text
            text_at = self.clock()
            typed = self.injector.inject(text, hold.target, InjectOptions(newline=processed.newline))
            if not typed.ok:
                self._fail(hold, typed.reason, typed=typed.typed)
                return
            typed_at = self.clock()
            latency = typed_at - hold.released_at
            log.info("session %d: typed (%s profile%s); release to typed %.0f ms (engine %.0f ms, text %.0f ms, "
                     "%styping %.0f ms)", hold.number, processed.profile,
                     f", rewrite {rewrite.reason}" if rewrite is not None else "", latency * 1000,
                     (engine_at - hold.released_at) * 1000, (pipeline_at - engine_at) * 1000,
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
            notice = rewrite.reason if rewrite is not None and rewrite.message else processed.notice
            reason = TYPED
            if hold.action in SEND_ACTIONS:
                if not processed.claude_code:
                    reason = NOT_CLAUDE
                elif self._press_enter(hold):
                    reason = SENT_ENTER
                else:
                    reason = ENTER_FAILED
            elif notice:
                reason = notice
            self._end(hold, reason, typed=typed.typed, latency=latency,
                      rewrite=rewrite.reason if rewrite is not None else None, notice=notice)
        except Exception as exc:  # noqa: BLE001 - the message may not carry text: log the type only
            log.error("session %d: finalization failed (%s)", hold.number, type(exc).__name__)
            self._fail(hold, INTERNAL_ERROR)

    def _wants_rewrite(self, hold: _Hold, processed: Processed) -> bool:
        if hold.action not in (DICTATION, SEND_CLAUDE):
            return False  # send_raw is never rewritten; send_polished always is (``force``)
        try:
            return bool(self.rewriter.wants(processed.text, hold.audio_bytes / AUDIO_BYTES_PER_S))
        except Exception as exc:  # noqa: BLE001 - a broken rewriter never holds up a dictation
            log.error("session %d: rewrite check failed (%s)", hold.number, type(exc).__name__)
            return False

    def _rewrite(self, hold: _Hold, processed: Processed, force: bool = False) -> autorewrite.AutoRewrite:
        """The rewrite of a long dictation (any dictation with ``force``); the original text on every failure."""
        with self._lock:
            hold.reviewing = True
            if self._visible(hold):
                self.indicator.show(REVIEWING, hold.live_text)
        try:
            result = self.rewriter.rewrite(processed.text, audio_s=hold.audio_bytes / AUDIO_BYTES_PER_S,
                                           profile=processed.rewrite_profile or processed.profile,
                                           keep=processed.keep, project=processed.project, force=force)
            if not isinstance(result.text, str) or not result.text.strip():
                raise ValueError("empty rewrite result")
            return result
        except Exception as exc:  # noqa: BLE001 - never lose the dictation: type the original
            log.error("session %d: rewrite failed (%s)", hold.number, type(exc).__name__)
            return autorewrite.AutoRewrite(processed.text, processed.text, autorewrite.FAILED, type(exc).__name__)
        finally:
            with self._lock:
                hold.reviewing = False

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

    def _press_enter(self, hold: _Hold) -> bool:
        try:
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
             shown: tuple[str, str, float] | None = None) -> None:
        """``shown`` (state, text, seconds) replaces the outcome the reason would show."""
        with self._lock:
            if hold.ended:
                return
            hold.ended = True
            hold.reviewing = False
            if hold in self._live:
                self._live.remove(hold)
            if self._visible(hold):
                if shown is not None:
                    self._show_timed(*shown)
                else:
                    self._show_outcome(hold, reason, typed, notice)
            self.outcomes.append(Outcome(hold.number, hold.action, reason, typed, latency, rewrite, notice))
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

    def _silence(self) -> None:
        if self.player is None:
            return
        try:
            self.player.stop()
        except Exception as exc:  # noqa: BLE001
            log.error("alert sound stop failed (%s)", type(exc).__name__)

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
        kinds, self._alerts = self._alerts, []
        kind = sound.PERMISSION if sound.PERMISSION in kinds else kinds[-1]
        text = ALSO_DONE if kind == sound.PERMISSION and sound.DONE in kinds else ""
        rang = False
        if self.player is not None and (self._last_ring is None or now - self._last_ring >= self.alert_repeat_s):
            self._last_ring = now
            try:
                self.player.play(kind)
                rang = True
            except Exception as exc:  # noqa: BLE001 - the indicator still shows the alert
                log.error("alert sound failed (%s)", type(exc).__name__)
        self.indicator.show(ALERT_STATES[kind], text, hide_after_s=ALERT_SHOW_S)
        log.info("claude alert %s shown (%d waiting, %s)", kind, len(kinds), "sound" if rang else "no sound")
        return True

    def _visible(self, hold: _Hold) -> bool:
        """No newer session is live (capturing or finalizing)."""
        return all(other.number <= hold.number for other in self._live)

    def _show_outcome(self, hold: _Hold, reason: str, typed: int, notice: str | None = None) -> None:
        if reason in (TYPED, CANCELLED, commands.REWRITTEN):
            older = self._live[-1] if self._live else None
            if older is not None:
                # An older session is still being finalized: show it again.
                self.indicator.show(REVIEWING if older.reviewing else TRANSCRIBING, older.live_text)
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
