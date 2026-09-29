"""Dictation sessions: one push-to-talk hold, from the press to the typed text.

``SessionManager`` receives the trigger signals (``quill.triggers``) on the
hook worker thread and runs each hold as a session:

- ``start``: a new session opens a streaming transcription and starts the
  microphone at once, so no word is lost; the indicator shows ``listening``
  with the live words and the voice level. While the model is still loading
  a press only shows the ``loading`` state: nothing is recorded, and its
  release does nothing.
- ``confirm``: the dictation and send-to-Claude triggers click at the pointer
  (``quill.focus``) and capture the target window.
- ``stop``: the capture stops, the final transcription is requested and the
  session is queued for finalization; the hook thread is free again, so a new
  press starts capturing immediately.
- ``cancel`` (short tap, combo, hooks stopping): the capture and the
  transcription are released; nothing is typed.

One finalizer thread takes the released sessions strictly in release order:
it waits for the final text, runs the text pipeline (cleanup, vocabulary,
learned corrections, the target's profile), types the text once into the
session's own target and, for the send trigger, presses Enter only after a
successful injection into a Claude Code window. A failure (microphone,
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

from quill import inject
from quill import focus as focus_reasons
from quill.indicator.render import COMMAND, ERROR, LISTENING, LOADING, SENT, TRANSCRIBING
from quill.inject import Target
from quill.triggers import CANCEL, CONFIRM, START, STOP, Signal

log = logging.getLogger("quill.session")

DICTATION = "dictation"
COMMAND_ACTION = "command"
SEND_CLAUDE = "send_claude"
CLICK_ACTIONS = (DICTATION, SEND_CLAUDE)

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
    COMMAND_UNAVAILABLE: "O modo comando ainda não está disponível",
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
}
INTERRUPTED = " (escrita interrompida)"
# Endings where the whole text was typed but something is worth showing.
TYPED_NOTICES = frozenset({NOT_CLAUDE, ENTER_FAILED, CLEANUP_FALLBACK})

ERROR_SHOW_S = 4.0
SENT_SHOW_S = 1.5
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
    returns a ``Processed``. ``on_session_start`` runs when a hold starts
    recording and ``on_typed(target, text)`` after a successful injection
    (manual-edit detection and the correction key); ``housekeeping`` runs
    on the finalizer thread about every ``poll_s``.
    """

    def __init__(self, *, transcriber: object, capture_factory: Callable[[Callable[[bytes], None]], object],
                 focus: object, injector: object, indicator: object, pipeline: TextPipeline,
                 on_session_start: Callable[[], None] | None = None,
                 on_typed: Callable[[Target, str], None] | None = None,
                 housekeeping: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.perf_counter,
                 final_timeout_s: float = FINAL_TIMEOUT_S, poll_s: float = POLL_S) -> None:
        self.transcriber = transcriber
        self.capture_factory = capture_factory
        self.focus = focus
        self.injector = injector
        self.indicator = _SafeIndicator(indicator)
        self.pipeline = pipeline
        self.on_session_start = on_session_start
        self.on_typed = on_typed
        self.housekeeping = housekeeping
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
                self.indicator.show(ERROR, MESSAGES[MODEL_UNAVAILABLE], hide_after_s=ERROR_SHOW_S)
            else:
                self.indicator.hide()

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """Cancel the hold in progress, finish the queued sessions and end the finalizer."""
        with self._lock:
            thread, jobs = self._thread, self._jobs
            self._closed = True
            hold, self._active = self._active, None
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
            if not self._ready:
                hold.loading = True
                if self._load_error:
                    self.indicator.show(ERROR, MESSAGES[MODEL_UNAVAILABLE], hide_after_s=ERROR_SHOW_S)
                else:
                    self.indicator.show(LOADING)
            elif hold.action == COMMAND_ACTION:
                hold.error = COMMAND_UNAVAILABLE
            else:
                self._live.append(hold)
                self.indicator.show(LISTENING)
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
        try:
            hold.asr = self.transcriber.open(on_partial=lambda partial: self._on_partial(hold, partial))
            capture = self.capture_factory(lambda pcm: self._on_audio(hold, pcm))
            capture.start()
        except Exception as exc:  # noqa: BLE001 - microphone missing, busy or unplugged
            log.warning("session %d: microphone could not start (%s)", hold.number, type(exc).__name__)
            self._fail(hold, MIC_ERROR, keep_active=True)
            return
        with self._lock:
            hold.capture = capture
            hold.capturing = True

    def _confirm(self) -> None:
        with self._lock:
            hold = self._active
        if hold is None or hold.loading or hold.error is not None or hold.action not in CLICK_ACTIONS:
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
        if hold.target is None:
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
            if item is not None:
                self._finalize(item)
            self._housekeeping()

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
            if not result.text.strip():
                self._fail(hold, NO_SPEECH)
                return
            processed = self.pipeline(result.text, hold.target)
            if not processed.text.strip():
                self._fail(hold, NO_SPEECH)
                return
            text_at = self.clock()
            typed = self.injector.inject(processed.text, hold.target)
            if not typed.ok:
                self._fail(hold, typed.reason, typed=typed.typed)
                return
            typed_at = self.clock()
            latency = typed_at - hold.released_at
            log.info("session %d: typed (%s profile); release to typed %.0f ms (engine %.0f ms, text %.0f ms, "
                     "typing %.0f ms)", hold.number, processed.profile, latency * 1000,
                     (engine_at - hold.released_at) * 1000, (text_at - engine_at) * 1000, (typed_at - text_at) * 1000)
            if self.on_typed is not None:
                try:
                    self.on_typed(hold.target, processed.text)
                except Exception as exc:  # noqa: BLE001
                    log.error("typed hook failed (%s)", type(exc).__name__)
            reason = TYPED
            if hold.action == SEND_CLAUDE:
                if not processed.claude_code:
                    reason = NOT_CLAUDE
                elif self._press_enter(hold):
                    reason = SENT_ENTER
                else:
                    reason = ENTER_FAILED
            elif processed.notice:
                reason = processed.notice
            self._end(hold, reason, typed=typed.typed, latency=latency)
        except Exception as exc:  # noqa: BLE001 - the message may not carry text: log the type only
            log.error("session %d: finalization failed (%s)", hold.number, type(exc).__name__)
            self._fail(hold, INTERNAL_ERROR)

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

    def _end(self, hold: _Hold, reason: str, typed: int = 0, latency: float | None = None) -> None:
        with self._lock:
            if hold.ended:
                return
            hold.ended = True
            if hold in self._live:
                self._live.remove(hold)
            if self._visible(hold):
                self._show_outcome(hold, reason, typed)
            self.outcomes.append(Outcome(hold.number, hold.action, reason, typed, latency))

    def _record(self, hold: _Hold, reason: str) -> None:
        with self._lock:
            if not hold.ended:
                hold.ended = True
                self.outcomes.append(Outcome(hold.number, hold.action, reason))

    def _show_error(self, hold: _Hold, reason: str) -> None:
        with self._lock:
            if self._visible(hold):
                self.indicator.show(ERROR, message(reason), hide_after_s=ERROR_SHOW_S)

    # Lock held from here on.

    def _visible(self, hold: _Hold) -> bool:
        """No newer session is live (capturing or finalizing)."""
        return all(other.number <= hold.number for other in self._live)

    def _show_outcome(self, hold: _Hold, reason: str, typed: int) -> None:
        if reason in (TYPED, CANCELLED):
            older = self._live[-1] if self._live else None
            if older is not None:
                # An older session is still being finalized: show it again.
                self.indicator.show(TRANSCRIBING, older.live_text)
            else:
                self.indicator.hide()
        elif reason == SENT_ENTER:
            self.indicator.show(SENT, hide_after_s=SENT_SHOW_S)
        else:
            interrupted = 0 if reason in TYPED_NOTICES else typed
            self.indicator.show(ERROR, message(reason, interrupted), hide_after_s=ERROR_SHOW_S)
