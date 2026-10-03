"""Final pass of mouse 5 into Claude Code: one whole-audio decode after the release.

The streaming final (``quill.streaming.FinalResult``) is the live text of a
hold. ``run`` decodes the released session's audio once more, as one pass on
a transcriber's worker (``StreamingTranscriber.decode_once``, queued with the
finals), and returns the text to use with a reason code:

- ``ok``: the pass text (non-empty);
- anything else: the streaming text, unchanged. ``off`` (settings), no speech
  (``no_speech``), a failed streaming final (``streaming_error``), the pass
  model still loading or not running (``not_ready``), failed to load
  (``load_error``), raising (``model_error``), stopped meanwhile
  (``stopped``), no result within ``timeout_s`` (``timeout``; a pass that
  has not started is dropped) or an empty pass text (``empty``).

The pass decodes the trimmed speech range of the final (``trim``) or the
whole capture, with the session's hints: when the session has a hint source
(``quill.heard.HeardHints``) it is asked again with the streaming final text,
so terms heard anywhere in the hold lead the hints; a source that raises or
returns anything else leaves the session's last hints. With ``hints`` off
the pass decodes without a prompt or hotwords.

Outcomes and their ``log_fields`` hold reason codes and timings only; the
text never leaves the outcome.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from quill.streaming import (
    PASS_CANCELLED,
    PASS_LOAD_ERROR,
    PASS_NOT_RUNNING,
    PASS_STOPPED,
    FinalResult,
    HintSource,
    PassOptions,
    ReleasedAudio,
    Session,
    StreamingTranscriber,
    _choose,
)
from quill.whisper import MODELS, PRECISE_MODEL, SessionHints

OK = "ok"
OFF = "off"
NO_SPEECH = "no_speech"
STREAMING_ERROR = "streaming_error"
NOT_READY = "not_ready"
LOAD_ERROR = "load_error"
MODEL_ERROR = "model_error"
STOPPED = "stopped"
TIMEOUT = "timeout"
EMPTY = "empty"

# Where the hints of a pass came from.
HINTS_SOURCE = "source"  # the session's hint source, asked with the streaming text
HINTS_SESSION = "session"  # the session's hints (no source)
HINTS_SOURCE_FAILED = "source_failed"  # the source raised: the session's hints
HINTS_NONE = "none"  # hints off

MAX_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class FinalPassSettings:
    """How the final pass decodes; ``model`` names the transcriber the caller runs it on."""

    enabled: bool = True
    model: str = PRECISE_MODEL
    beam_size: int = 5
    temperature_fallback: bool = False
    condition_on_previous_text: bool = False
    vad_filter: bool = False
    # True: only the trimmed speech range of the final; False: the whole capture.
    trim: bool = True
    # True: the session's hints (or its hint source's); False: no prompt or hotwords.
    hints: bool = True
    timeout_s: float = 4.0

    def __post_init__(self) -> None:
        for name in ("enabled", "trim", "hints"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"final pass setting is not a boolean: {name}")
        if self.model not in MODELS:
            raise ValueError(f"unknown faster-whisper model {self.model!r}")
        timeout = self.timeout_s
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_S:
            raise ValueError("final pass setting out of range: timeout_s")
        self.pass_options()  # beam and decode switches are checked there

    def pass_options(self) -> PassOptions:
        return PassOptions(
            beam_size=self.beam_size,
            temperature_fallback=self.temperature_fallback,
            condition_on_previous_text=self.condition_on_previous_text,
            vad_filter=self.vad_filter,
        )


@dataclass(frozen=True)
class FinalPassOutcome:
    """The text to use and why; times are clock readings in seconds."""

    text: str = field(repr=False)
    reason: str
    hints: str | None = None  # where the pass hints came from; None when no pass was asked
    audio_s: float = 0.0  # audio decoded
    waited_s: float = 0.0  # request -> pass start
    compute_s: float = 0.0  # model time
    elapsed_s: float = 0.0  # call -> outcome

    @property
    def used_pass(self) -> bool:
        return self.reason == OK

    def log_fields(self) -> str:
        """Reason and timings for a log line; never the text."""
        return (f"final pass {self.reason}, hints {self.hints or '-'}, audio {self.audio_s:.1f} s, "
                f"wait {self.waited_s * 1000:.0f} ms, compute {self.compute_s * 1000:.0f} ms, "
                f"total {self.elapsed_s * 1000:.0f} ms")


def _pass_hints(settings: FinalPassSettings, hints: SessionHints | None, source: HintSource | None,
                heard: str) -> tuple[SessionHints | None, str]:
    if not settings.hints:
        return SessionHints(language=hints.language if hints is not None else None), HINTS_NONE
    if source is None:
        return hints, HINTS_SESSION
    ok, chosen = _choose(source, heard)
    if not ok:
        return hints, HINTS_SOURCE_FAILED
    return chosen, HINTS_SOURCE


def run(transcriber: StreamingTranscriber, final: FinalResult, audio: ReleasedAudio | None,
        settings: FinalPassSettings, *, hints: SessionHints | None = None, source: HintSource | None = None,
        clock: Callable[[], float] = time.perf_counter) -> FinalPassOutcome:
    """The text to type for a released hold: the pass text, or the streaming text with the reason.

    ``hints`` are the session's hints and ``source`` its hint source (None:
    none). Blocks for at most about ``settings.timeout_s``.
    """
    began = clock()
    streaming = final.text

    def fallback(reason: str, hint_from: str | None = None, result=None) -> FinalPassOutcome:
        times = {}
        if result is not None:
            times = {"audio_s": result.audio_s, "waited_s": result.waited_s, "compute_s": result.compute_s}
        return FinalPassOutcome(streaming, reason, hint_from, elapsed_s=max(0.0, clock() - began), **times)

    if not settings.enabled:
        return fallback(OFF)
    if not final.ok:
        return fallback(STREAMING_ERROR)
    if not final.speech or audio is None or not audio.speech:
        return fallback(NO_SPEECH)
    if transcriber.load_error:
        return fallback(LOAD_ERROR)
    if not transcriber.running or not transcriber.ready.is_set():
        return fallback(NOT_READY)
    chosen, hint_from = _pass_hints(settings, hints, source, streaming)
    span = (audio.start, audio.end) if settings.trim else None
    handle = transcriber.decode_once(audio.pcm, chosen, settings.pass_options(), span=span)
    result = handle.wait(settings.timeout_s)
    if result is None:
        handle.cancel()
        result = handle.wait(0)  # it may have finished just before the cancel
    if result is None or result.reason == PASS_CANCELLED:
        return fallback(TIMEOUT, hint_from, result)
    if not result.ok:
        reason = {PASS_LOAD_ERROR: LOAD_ERROR, PASS_STOPPED: STOPPED, PASS_NOT_RUNNING: NOT_READY}.get(result.reason, MODEL_ERROR)
        return fallback(reason, hint_from, result)
    if not result.text.strip():
        return fallback(EMPTY, hint_from, result)
    return FinalPassOutcome(result.text, OK, hint_from, result.audio_s, result.waited_s, result.compute_s,
                            max(0.0, clock() - began))


def run_session(transcriber: StreamingTranscriber, session: Session, final: FinalResult,
                settings: FinalPassSettings, *, clock: Callable[[], float] = time.perf_counter) -> FinalPassOutcome:
    """``run`` with a released streaming session's audio, hints and hint source."""
    return run(transcriber, final, session.released_audio(), settings, hints=session.hints,
               source=session.hint_source, clock=clock)
