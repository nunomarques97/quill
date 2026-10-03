"""Streaming transcription while a push-to-talk trigger is held.

A ``Session`` receives the PCM of one hold as it is captured. At every
``step_s`` of audio in which speech advanced, it asks for a *partial*: the
uncommitted part of the audio is transcribed with the fast beam and the words
are shown live.

Committed words are final: the audio before them is never transcribed again,
and later windows get the committed text as prompt context, after the
vocabulary hints. When the speaker has been silent for ``tail_pad_s`` while
still holding, the uncommitted audio up to that pause is transcribed with the
final beam (*speculative final*). A speculative final over at least
``pause_commit_min_s`` of speech is committed (``pause_commit``), so speech
after the pause starts a new window; a shorter one is kept, and a release
with no speech after it reuses the result instead of waiting.

On release only the uncommitted tail is transcribed, with the final beam. The
tail ends ``tail_pad_s`` after the last speech frame, so trailing silence is
never decoded. By default only pauses commit, which keeps the final text as
good as transcribing the whole take. With ``agreement`` on, words that two
consecutive partials agree on and that end at least ``commit_margin_s``
before the end of the audio are committed too (partials then need word
timestamps): tails are shorter, the text worse. Without either, a window
longer than ``max_window_s`` is committed by agreement anyway. With
``commit`` off nothing is committed and the final transcribes the whole
utterance.

Whisper invents words for silence and noise, so every window starts
``lead_s`` before its first speech frame (judged with the current noise
floor) and a window with less than ``min_speech_s`` of speech is not decoded;
tokens made only of punctuation are never text.

One worker thread owns the model. It runs finals first (in release order),
then speculative finals, then partials. Each session has at most one pending
partial: a newer request replaces it, and a partial that completes after its
session was released is dropped (neither shown nor committed). A model
exception in a partial is counted and skipped; in a final it yields an error
result. ``stop`` resolves every pending final with an error, so a caller
waiting for a final never hangs; ``start`` after ``stop`` works again.

A session opened with ``SessionHints`` (a voice command) is decoded with
those hints instead of the vocabulary hints: every partial, speculative and
final decode of that session, and no other session. ``Session.set_hints``
replaces a session's hints until it is released (mouse 5 into Claude Code
gets its project's hints once its window is known): later windows use them,
speculative finals decoded with the earlier hints are forgotten, and a
partial or speculative final still running with them is dropped.

``open`` and ``set_hints`` also take a *hint source* instead of hints: a
callable from the text heard so far to ``SessionHints`` (None: the
vocabulary hints), such as ``quill.heard.HeardHints`` for mouse 5 into Claude
Code. The session is decoded with what it returns for the text heard when it
is given, and after each partial (on the worker thread, outside the lock) it
asks again with the committed and tentative words; the hints switch, as with
``set_hints``, only when the source chooses different ones
(``FinalResult.hint_switches`` counts these switches). A switch chosen from a
partial shown before the release still applies to the final; partials that
complete after the release are dropped and ask nothing. A source that raises
or returns anything else leaves the session's hints as they are.

All session state is protected by one lock; the model runs outside it.
Nothing here logs or stores text or audio.
"""

from __future__ import annotations

import bisect
import math
import threading
import time
import unicodedata
from array import array
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from quill.whisper import HINTS_PREFIX, PROMPT_MAX_CHARS, Decode, SessionHints, Transcript, Word, join_vocabulary

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH
FRAME_SAMPLES = 320  # 20 ms
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE
# The noise floor is estimated only after this many frames (0.5 s).
FLOOR_MIN_FRAMES = 25
FLOOR_PERCENTILE = 0.10
# Words repeated across a cut are looked for only this close to the window start.
OVERLAP_S = 1.0
OVERLAP_MAX_WORDS = 3
# faster-whisper decodes at most 30 s per window; the token bound is per window.
WINDOW_S = 30.0


class Model(Protocol):
    def transcribe(self, pcm: bytes, options: Decode) -> Transcript: ...


@dataclass(frozen=True)
class StreamOptions:
    """Tuning of the streaming transcription (seconds are audio time)."""

    step_s: float = 0.5
    # False: nothing is committed while held; the final transcribes the whole
    # utterance (best text, slowest release) and partials are only shown.
    commit: bool = True
    # False: partials never commit by local agreement (they are decoded without
    # word timestamps, which is cheaper); only pauses and max_window_s commit.
    agreement: bool = False
    partial_beam: int = 1
    final_beam: int = 5
    commit_margin_s: float = 1.0
    # Committed text passed back as prompt context; 0 disables it.
    context_chars: int = 200
    # Past this much uncommitted audio, words are committed without agreement.
    max_window_s: float = 20.0
    tail_pad_s: float = 0.3
    speculate: bool = True
    # A speculative final at a pause is committed when its window holds at least
    # pause_commit_min_s of speech: speech after it starts a new window.
    pause_commit: bool = True
    pause_commit_min_s: float = 1.5
    # A window is decoded only with this much speech; leading silence is trimmed to lead_s.
    min_speech_s: float = 0.2
    lead_s: float = 0.2
    # Energy detector: a frame is speech above max(min_speech_rms, floor * floor_ratio).
    min_speech_rms: float = 0.001
    floor_ratio: float = 4.0
    # Generated-token bound: tokens_per_s per second of audio plus min_new_tokens.
    tokens_per_s: float = 10.0
    min_new_tokens: int = 24

    def __post_init__(self) -> None:
        checks = (
            (0.1 <= self.step_s <= 5.0, "step_s"),
            (1 <= self.partial_beam <= 10, "partial_beam"),
            (1 <= self.final_beam <= 10, "final_beam"),
            (0.0 <= self.commit_margin_s <= 5.0, "commit_margin_s"),
            (0 <= self.context_chars <= PROMPT_MAX_CHARS, "context_chars"),
            (5.0 <= self.max_window_s <= 28.0, "max_window_s"),
            (0.0 <= self.tail_pad_s <= 2.0, "tail_pad_s"),
            (0.0 <= self.pause_commit_min_s <= 20.0, "pause_commit_min_s"),
            (0.0 <= self.min_speech_s <= 2.0, "min_speech_s"),
            (0.0 <= self.lead_s <= 1.0, "lead_s"),
            (0.0 < self.min_speech_rms < 1.0, "min_speech_rms"),
            (self.floor_ratio >= 1.0, "floor_ratio"),
            (self.tokens_per_s > 0 and self.min_new_tokens >= 1, "tokens_per_s"),
        )
        for ok, name in checks:
            if not ok:
                raise ValueError(f"streaming option out of range: {name}")


# Tuning per engine model, measured on the Sponsor's real recordings.
# large-v3-turbo (the default) decodes a whole utterance fast enough that
# nothing needs committing: the final has the best text within the latency
# target. large-v3 (the precise mode) commits at pauses, so a release decodes
# only the rest.
MODEL_OPTIONS = {
    "large-v3-turbo": StreamOptions(commit=False),
    "large-v3": StreamOptions(),
}


def options_for(model: str) -> StreamOptions:
    """The streaming tuning of an engine model."""
    try:
        return MODEL_OPTIONS[model]
    except KeyError:
        raise ValueError(f"unknown faster-whisper model {model!r}") from None


# ---------------------------------------------------------------- speech detection


class SpeechDetector:
    """Energy detector over 20 ms frames at absolute positions.

    The threshold is ``min_speech_rms`` or, once half a second was heard,
    ``floor_ratio`` times the 10th percentile of the frame levels so far,
    whichever is higher. Frame boundaries do not depend on how the audio is
    chunked, so the result is the same for any chunk size.
    """

    def __init__(self, min_rms: float, floor_ratio: float) -> None:
        self.min_rms = min_rms
        self.floor_ratio = floor_ratio
        self._pending = b""
        self._levels: list[float] = []  # sorted, for the floor
        self._frame_levels = array("f")  # in order, for windows
        self.frames = 0
        self.speech_frames = 0
        self.speech_end = 0  # bytes: end of the last speech frame

    def threshold(self) -> float:
        if len(self._levels) < FLOOR_MIN_FRAMES:
            return self.min_rms
        floor = self._levels[int(FLOOR_PERCENTILE * (len(self._levels) - 1))]
        return max(self.min_rms, floor * self.floor_ratio)

    def feed(self, pcm: bytes) -> None:
        data = self._pending + pcm
        usable = len(data) - len(data) % FRAME_BYTES
        self._pending = data[usable:]
        samples = array("h")
        samples.frombytes(data[:usable])
        for start in range(0, len(samples), FRAME_SAMPLES):
            frame = samples[start : start + FRAME_SAMPLES]
            level = math.sqrt(sum(s * s for s in frame) / FRAME_SAMPLES) / 32768.0
            threshold = self.threshold()
            self.frames += 1
            if level >= threshold:
                self.speech_frames += 1
                self.speech_end = self.frames * FRAME_BYTES
            bisect.insort(self._levels, level)
            self._frame_levels.append(level)

    @property
    def has_speech(self) -> bool:
        return self.speech_frames > 0

    def speech_in(self, start: int, end: int) -> tuple[int | None, float]:
        """(first speech byte, seconds of speech) in ``[start, end)`` at the current threshold.

        Frames are judged again with today's threshold, so noise heard before
        the floor was known does not count as speech.
        """
        threshold = self.threshold()
        first = None
        count = 0
        for index in range(start // FRAME_BYTES, min(len(self._frame_levels), -(-end // FRAME_BYTES))):
            if self._frame_levels[index] >= threshold:
                count += 1
                if first is None:
                    first = max(start, index * FRAME_BYTES)
        return first, count * FRAME_S


# ---------------------------------------------------------------- stabilizer


def _norm(text: str) -> str:
    kept = "".join(ch for ch in unicodedata.normalize("NFC", text).casefold() if not unicodedata.category(ch).startswith(("P", "S")))
    return " ".join(kept.split())


def spoken_words(text: str) -> list[str]:
    """Words of a decoded text, without tokens made only of punctuation or symbols."""
    return [w for w in text.split() if any(ch.isalnum() for ch in w)]


def _to_bytes(seconds: float) -> int:
    return max(0, int(round(seconds * SAMPLE_RATE))) * SAMPLE_WIDTH


def drop_overlap(committed: Sequence[str], words: Sequence[str]) -> int:
    """How many leading ``words`` repeat the last committed words (0 to 3)."""
    for n in range(min(OVERLAP_MAX_WORDS, len(committed), len(words)), 0, -1):
        if [_norm(w) for w in committed[-n:]] == [_norm(w) for w in words[:n]]:
            return n
    return 0


class Stabilizer:
    """Committed words and the byte offset where the uncommitted audio starts.

    Local agreement: a word is committed when the previous and the current
    hypothesis agree on it (same normalized text, in order from the start of
    the window) and it ends ``margin_s`` before the window end. The cut is
    placed halfway between the last committed word and the next word.
    """

    def __init__(self, margin_s: float) -> None:
        self.margin_s = margin_s
        self.committed: list[str] = []
        self.offset = 0
        self._previous: list[Word] = []

    @property
    def text(self) -> str:
        return " ".join(self.committed)

    def commit_all(self, words: Sequence[str], end: int) -> None:
        """Commit a whole window that ends in a pause; the next window starts at ``end``."""
        self.committed.extend(words)
        self.offset = max(self.offset, end)
        self._previous = []

    def update(self, window_start: int, words: Sequence[Word], window_end: int, force: bool = False, audio_start: int | None = None) -> list[Word]:
        """Apply one partial over ``[window_start, window_end)``; returns the tentative words.

        ``words`` have times relative to ``audio_start`` (the decoded audio,
        by default ``window_start``). A result for a window that no longer
        starts at the offset is ignored.
        """
        if window_start != self.offset:
            return []
        base = (window_start if audio_start is None else audio_start) / BYTES_PER_SECOND
        current = [Word(base + w.start, base + w.end, w.text) for w in words if any(ch.isalnum() for ch in w.text)]
        early = [w for w in current if w.start - base < OVERLAP_S]
        skip = drop_overlap(self.committed, [w.text for w in early])
        current = current[skip:]
        limit = window_end / BYTES_PER_SECOND - self.margin_s
        agreed = 0
        if force:
            while agreed < len(current) and current[agreed].end <= limit:
                agreed += 1
        else:
            previous = self._previous
            while (
                agreed < min(len(previous), len(current))
                and _norm(previous[agreed].text) == _norm(current[agreed].text)
                and current[agreed].end <= limit
            ):
                agreed += 1
        if agreed:
            last = current[agreed - 1]
            cut = last.end
            if agreed < len(current):
                cut = min((last.end + current[agreed].start) / 2, limit)
            self.committed.extend(w.text for w in current[:agreed])
            self.offset = max(self.offset, min(_to_bytes(cut), window_end))
            current = current[agreed:]
        elif force and not current:
            # A long window the model hears no words in: skip it, keeping the margin.
            self.offset = max(self.offset, min(_to_bytes(limit), window_end))
        self._previous = current
        return current


# ---------------------------------------------------------------- results


@dataclass(frozen=True)
class Partial:
    """Live words of one session; ``committed`` never changes once shown."""

    session: int
    seq: int
    committed: str = field(repr=False)
    tentative: str = field(repr=False)
    audio_s: float
    speculative: bool = False

    @property
    def text(self) -> str:
        return " ".join(part for part in (self.committed, self.tentative) if part)


@dataclass(frozen=True)
class FinalResult:
    session: int
    text: str = field(repr=False)
    error: str | None
    audio_s: float
    speech: bool
    partials: int
    dropped: int
    committed_words: int
    tail_s: float
    speculative_hit: bool
    # Clock readings: release -> final job start, model time, release -> result.
    waited_s: float
    compute_s: float
    latency_s: float
    hint_switches: int = 0  # hint changes chosen by the session's hint source from partials

    @property
    def ok(self) -> bool:
        return self.error is None


class FinalHandle:
    """Resolves exactly once with the session's ``FinalResult``."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: FinalResult | None = None

    def _resolve(self, result: FinalResult) -> None:
        if not self._event.is_set():
            self._result = result
            self._event.set()

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> FinalResult | None:
        self._event.wait(timeout)
        return self._result


# ---------------------------------------------------------------- hint sources

HintSource = Callable[[str], "SessionHints | None"]


def is_hint_source(hints: object) -> bool:
    """Whether ``hints`` is a hint source (heard text -> hints) rather than ``SessionHints`` or None."""
    return callable(hints) and not isinstance(hints, SessionHints)


def _choose(source: HintSource, heard: str) -> tuple[bool, SessionHints | None]:
    """(True, the source's hints for ``heard``), or (False, None) when it raises or returns anything else."""
    try:
        hints = source(heard)
    except Exception:  # noqa: BLE001 - the session keeps the hints it has
        return False, None
    if hints is not None and not isinstance(hints, SessionHints):
        return False, None
    return True, hints


# ---------------------------------------------------------------- jobs

PARTIAL, SPECULATIVE, FINAL = "partial", "speculative", "final"


@dataclass(eq=False)
class _Job:
    kind: str
    session: "Session"
    end: int  # bytes of session audio the job covers
    speech_end: int = 0
    hints_version: int = 0  # the session's hints when the job was planned


class Session:
    """The audio of one hold. Created by ``StreamingTranscriber.open``."""

    def __init__(self, owner: "StreamingTranscriber", number: int, on_partial: Callable[[Partial], None] | None,
                 hints: SessionHints | None = None, source: HintSource | None = None) -> None:
        self.owner = owner
        self.number = number
        self.on_partial = on_partial
        self.hints = hints
        self.hint_source = source
        self.hint_switches = 0
        self._heard = ""  # committed and tentative words of the last partial shown
        options = owner.options
        self._pcm = bytearray()
        self._vad = SpeechDetector(options.min_speech_rms, options.floor_ratio)
        self._stable = Stabilizer(options.commit_margin_s)
        self._next_partial = options.step_s
        self._partial_speech_end = 0
        self._spec_speech_end = -1
        self._spec_results: dict[tuple[int, int], str] = {}
        self._hints_version = 0
        self._pause_end = -1  # offset set by the last pause commit
        self._pending_partial: _Job | None = None
        self._pending_spec: _Job | None = None
        self._seq = 0
        self.partials = 0
        self.dropped = 0
        self.released = False
        self.closed = False
        self.handle: FinalHandle | None = None
        self._release_time = 0.0

    # --------------------------------------------------- called by the capture side

    @property
    def audio_s(self) -> float:
        return len(self._pcm) / BYTES_PER_SECOND

    def feed(self, pcm: bytes) -> None:
        """Append captured PCM; may schedule a partial or a speculative final."""
        owner = self.owner
        options = owner.options
        with owner._cond:
            if self.released or self.closed or not pcm:
                return
            self._pcm += pcm
            self._vad.feed(pcm)
            end = len(self._pcm)
            now_s = end / BYTES_PER_SECOND
            speech_end = self._vad.speech_end
            if now_s + 1e-9 >= self._next_partial and speech_end > self._partial_speech_end:
                self._partial_speech_end = speech_end
                self._next_partial = (math.floor(now_s / options.step_s + 1e-9) + 1) * options.step_s
                if self._pending_partial is not None:
                    self.dropped += 1
                self._pending_partial = _Job(PARTIAL, self, end, speech_end)
                owner._cond.notify_all()
            pad = _to_bytes(options.tail_pad_s)
            if options.speculate and self._vad.has_speech and end - speech_end >= pad and self._spec_speech_end != speech_end:
                self._spec_speech_end = speech_end
                if self._pending_partial is not None:
                    # The speculative final covers everything the pending partial would.
                    self._pending_partial = None
                    self.dropped += 1
                self._pending_spec = _Job(SPECULATIVE, self, end, speech_end)
                owner._cond.notify_all()

    def set_hints(self, hints: SessionHints | HintSource | None) -> bool:
        """Decode the later windows with ``hints`` (None: the vocabulary hints); False once released or closed.

        Speculative finals decoded with the earlier hints are forgotten, so the
        final decodes its audio again, and the next pause asks for a new one.
        A hint source is asked at once with the words heard so far (outside
        the lock) and again after each partial; the hints switch only when it
        chooses different ones, and stay as they are when it fails.
        """
        if is_hint_source(hints):
            with self.owner._cond:
                if self.released or self.closed:
                    return False
                heard = self._heard
            ok, chosen = _choose(hints, heard)
            with self.owner._cond:
                if self.released or self.closed:
                    return False
                self.hint_source = hints
                if ok:
                    self._switch(chosen)
                return True
        with self.owner._cond:
            if self.released or self.closed:
                return False
            self.hint_source = None
            self.hints = hints
            self._forget_hints()
            return True

    def _switch(self, hints: SessionHints | None) -> bool:
        """Decode with ``hints`` from now on when they differ from the current ones (lock held)."""
        if hints == self.hints:
            return False
        self.hints = hints
        self._forget_hints()
        return True

    def _forget_hints(self) -> None:
        """Jobs planned and speculative finals decoded with the earlier hints are not used (lock held)."""
        self._hints_version += 1
        self._spec_results.clear()
        self._spec_speech_end = -1

    def release(self) -> FinalHandle:
        """Stop feeding and ask for the final text; the handle resolves exactly once."""
        return self.owner._release(self)

    def cancel(self) -> None:
        """Forget this hold without a final (for example a cancelled trigger)."""
        with self.owner._cond:
            self.closed = True
            self._pending_partial = self._pending_spec = None
            self.owner._forget(self)

    # --------------------------------------------------- used by the worker (lock held)

    def _final_end(self) -> int:
        if not self._vad.has_speech:
            return 0
        return min(len(self._pcm), self._vad.speech_end + _to_bytes(self.owner.options.tail_pad_s))

    def _window(self, start: int, end: int) -> tuple[int | None, float]:
        """(decode start, seconds of speech) for ``[start, end)``; None without enough speech.

        Leading silence is trimmed to ``lead_s`` before the first speech frame:
        Whisper tends to invent words for silence and noise.
        """
        options = self.owner.options
        first, seconds = self._vad.speech_in(start, end)
        if first is None or seconds + 1e-9 < options.min_speech_s:
            return None, seconds
        return max(start, first - _to_bytes(options.lead_s)), seconds

    def _decode(self, beam: int, start: int, end: int, *, words: bool) -> Decode:
        options = self.owner.options
        context = ""
        if options.context_chars and self._stable.committed:
            context = self._stable.text[-options.context_chars :]
            if len(self._stable.text) > options.context_chars and " " in context:
                context = context.split(" ", 1)[1]
        prompt_parts = []
        hints = self.hints
        language = None
        if hints is not None:
            # The session's own hints; committed text follows them only as far as it fits.
            if hints.prompt:
                prompt_parts.append(hints.prompt)
                room = PROMPT_MAX_CHARS - len(hints.prompt) - 1
                if len(context) > room:
                    context = context[len(context) - room :].split(" ", 1)[-1] if room > 0 else ""
            hotwords = hints.hotwords or None
            language = hints.language
        else:
            vocabulary = self.owner.vocabulary
            if vocabulary:
                joined = join_vocabulary(vocabulary, max(0, PROMPT_MAX_CHARS - len(context)))
                if joined:
                    prompt_parts.append(HINTS_PREFIX + joined + ".")
            hotwords = join_vocabulary(vocabulary, PROMPT_MAX_CHARS, separator=" ") or None
        if context:
            prompt_parts.append(context)
        seconds = min((end - start) / BYTES_PER_SECOND, WINDOW_S)
        return Decode(
            beam_size=beam,
            initial_prompt=" ".join(prompt_parts) or None,
            hotwords=hotwords,
            word_timestamps=words,
            without_timestamps=not words,
            max_new_tokens=int(math.ceil(seconds * options.tokens_per_s)) + options.min_new_tokens,
            language=language,
        )

    def _emit(self, committed: str, tentative: Sequence[str], end: int, speculative: bool) -> Partial:
        self._seq += 1
        return Partial(self.number, self._seq, committed, " ".join(tentative), end / BYTES_PER_SECOND, speculative)


class StreamingTranscriber:
    """One model, one worker thread, any number of sequential sessions."""

    def __init__(
        self,
        model: Model,
        options: StreamOptions = StreamOptions(),
        vocabulary: Sequence[str] = (),
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.model = model
        self.options = options
        self.vocabulary = tuple(vocabulary)
        self.clock = clock
        self._cond = threading.Condition()
        self._sessions: list[Session] = []
        self._finals: deque[_Job] = deque()
        self._busy = False
        self._running = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._numbers = 0
        self.ready = threading.Event()
        self.load_error: str | None = None
        self.partial_errors = 0

    # --------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start the worker; it loads the model first (``ready`` is set when done)."""
        with self._cond:
            if self._running:
                raise RuntimeError("streaming transcriber already started")
            self._running = True
            self._stopping = False
            self.ready.clear()
            self.load_error = None
            self._generation += 1
            self._thread = threading.Thread(target=self._run, args=(self._generation,), name="quill-asr", daemon=True)
            self._thread.start()

    def stop(self, close_model: bool = True, timeout: float | None = None) -> None:
        """Stop the worker, resolve pending finals with an error and release the model.

        With a ``timeout`` the worker may still be inside a model call when
        this returns: it exits after that call without taking another job,
        and the model is then left loaded rather than closed under it.
        """
        with self._cond:
            if not self._running:
                return
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        exited = thread is None or not thread.is_alive()
        with self._cond:
            finals = list(self._finals)
            self._finals.clear()
            sessions = list(self._sessions)
            self._sessions.clear()
            self._busy = False
            self._running = False
            self._thread = None
            self._cond.notify_all()
        for job in finals:
            self._fail(job.session, "streaming stopped")
        for session in sessions:
            session.closed = True
            if session.handle is not None:
                self._fail(session, "streaming stopped")
        if close_model and exited and hasattr(self.model, "close"):
            self.model.close()

    def open(self, on_partial: Callable[[Partial], None] | None = None,
             hints: SessionHints | HintSource | None = None) -> Session:
        """A new session; with ``hints`` its decodes use them instead of the vocabulary hints.

        A hint source is asked at once with nothing heard (a failure: the
        vocabulary hints) and again after each partial.
        """
        source = hints if is_hint_source(hints) else None
        if source is not None:
            hints = _choose(source, "")[1]
        with self._cond:
            self._numbers += 1
            session = Session(self, self._numbers, on_partial, hints, source)
            self._sessions.append(session)
            return session

    def drain(self, timeout: float | None = None) -> bool:
        """Wait until no job is pending or running; False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._running and (self._busy or self._next_job(peek=True) is not None):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(remaining if remaining is not None else 0.05)
        return True

    # --------------------------------------------------- sessions

    def _forget(self, session: Session) -> None:
        if session in self._sessions:
            self._sessions.remove(session)

    def _release(self, session: Session) -> FinalHandle:
        with self._cond:
            if session.handle is not None:
                return session.handle
            session.handle = FinalHandle()
            session.released = True
            session._release_time = self.clock()
            if session._pending_partial is not None:
                session.dropped += 1
            session._pending_partial = None
            session._pending_spec = None
            if not self._running or self._stopping:
                self._forget(session)
                failed = True
            else:
                self._finals.append(_Job(FINAL, session, len(session._pcm)))
                self._cond.notify_all()
                failed = False
        if failed:
            self._fail(session, "streaming not running")
        return session.handle

    def _result(self, session: Session, text: str, error: str | None, *, speech: bool, tail: int, hit: bool, started: float, compute: float) -> FinalResult:
        now = self.clock()
        return FinalResult(
            session=session.number,
            text=text,
            error=error,
            audio_s=session.audio_s,
            speech=speech,
            partials=session.partials,
            dropped=session.dropped,
            committed_words=len(session._stable.committed),
            tail_s=tail / BYTES_PER_SECOND,
            speculative_hit=hit,
            waited_s=max(0.0, started - session._release_time),
            compute_s=compute,
            latency_s=max(0.0, now - session._release_time),
            hint_switches=session.hint_switches,
        )

    def _fail(self, session: Session, reason: str) -> None:
        if session.handle is None or session.handle.done:
            return
        now = self.clock()
        session.handle._resolve(self._result(session, "", reason, speech=session._vad.has_speech, tail=0, hit=False, started=now, compute=0.0))

    # --------------------------------------------------- worker

    def _next_job(self, peek: bool = False) -> _Job | None:
        if self._finals:
            return self._finals[0] if peek else self._finals.popleft()
        for attribute in ("_pending_spec", "_pending_partial"):
            for session in reversed(self._sessions):
                job = getattr(session, attribute)
                if job is not None:
                    if not peek:
                        setattr(session, attribute, None)
                    return job
        return None

    def _run(self, generation: int) -> None:
        try:
            load = getattr(self.model, "load", None)
            if load is not None:
                load()
        except Exception as exc:
            self.load_error = f"model unavailable: {' '.join(str(exc).split())[:160] or type(exc).__name__}"
        finally:
            self.ready.set()
        while True:
            with self._cond:
                while not self._stopping and generation == self._generation and self._next_job(peek=True) is None:
                    self._cond.wait()
                if self._stopping or generation != self._generation:
                    return
                job = self._next_job()
                self._busy = True
                plan = self._plan(job)
            try:
                if plan is not None:
                    self._execute(job, *plan)
            finally:
                with self._cond:
                    if generation == self._generation:
                        self._busy = False
                    self._cond.notify_all()

    def _plan(self, job: _Job) -> tuple | None:
        """Everything a job needs from the session, read under the lock."""
        session = job.session
        job.hints_version = session._hints_version
        if job.kind == FINAL:
            end = session._final_end()
            start = session._stable.offset
            key = (start, end)
            if end <= start:
                return ("empty", key, b"", None, start, 0.0)
            if key in session._spec_results:
                return ("cached", key, b"", None, start, 0.0)
            audio, seconds = session._window(start, end)
            if audio is None:
                return ("empty", key, b"", None, start, seconds)
            decode = session._decode(self.options.final_beam, audio, end, words=False)
            return ("decode", key, bytes(session._pcm[audio:end]), decode, audio, seconds)
        if session.released or session.closed or self.load_error:
            if job.kind == PARTIAL:
                session.dropped += 1
            return None
        if job.kind == SPECULATIVE:
            if session._vad.speech_end != job.speech_end:
                return None  # speech resumed: the next pause asks again
            end = session._final_end()
            start = session._stable.offset
            if end <= start or (start, end) in session._spec_results:
                return None
            audio, seconds = session._window(start, end)
            if audio is None:
                return None
            decode = session._decode(self.options.final_beam, audio, end, words=False)
            return ("decode", (start, end), bytes(session._pcm[audio:end]), decode, audio, seconds)
        start = session._stable.offset
        if job.end <= start:
            return None
        audio, seconds = session._window(start, job.end)
        if audio is None:
            return None
        words = self._partial_commits(job.end - audio)
        decode = session._decode(self.options.partial_beam, audio, job.end, words=words)
        return ("decode", (start, job.end), bytes(session._pcm[audio : job.end]), decode, audio, seconds)

    def _partial_commits(self, window: int) -> bool:
        """Whether a partial over ``window`` bytes may commit words (it then needs word timestamps)."""
        options = self.options
        if not options.commit:
            return False
        return options.agreement or window / BYTES_PER_SECOND > options.max_window_s

    def _execute(self, job: _Job, mode: str, key: tuple[int, int], pcm: bytes, decode: Decode | None, audio: int, speech_s: float) -> None:
        session = job.session
        started = self.clock()
        transcript: Transcript | None = None
        error: str | None = None
        if mode == "decode":
            if job.kind == FINAL and self.load_error:
                error = self.load_error
            else:
                try:
                    transcript = self.model.transcribe(pcm, decode)
                except Exception as exc:
                    error = f"transcription failed: {type(exc).__name__}"
        compute = self.clock() - started
        if job.kind == FINAL:
            with self._cond:
                committed = list(session._stable.committed)
                speech = session._vad.has_speech
                cached = session._spec_results.get(key)
                self._forget(session)
            if error is not None:
                result = self._result(session, "", error, speech=speech, tail=key[1] - key[0], hit=False, started=started, compute=compute)
            else:
                tail = cached if mode == "cached" else (transcript.text if transcript else "")
                words = spoken_words(tail)
                words = words[drop_overlap(committed, words) :]
                text = " ".join(committed + words)
                hit = mode == "cached" or (mode == "empty" and speech and key[0] == session._pause_end)
                result = self._result(session, text, None, speech=speech, tail=max(0, key[1] - key[0]), hit=hit, started=started, compute=compute)
            session.handle._resolve(result)
            return
        if error is not None:
            with self._cond:
                self.partial_errors += 1
            return
        partial: Partial | None = None
        with self._cond:
            if job.hints_version != session._hints_version:
                # Decoded with hints the session no longer has: neither shown, committed nor reused.
                if job.kind == PARTIAL:
                    session.dropped += 1
                return
            if session.released or session.closed:
                if job.kind == PARTIAL:
                    session.dropped += 1
                elif not session.closed:
                    # Released while it ran: the final job (queued behind it) reuses it.
                    words = spoken_words(transcript.text)
                    session._spec_results[key] = " ".join(words[drop_overlap(session._stable.committed, words) :])
                return
            if job.kind == SPECULATIVE:
                words = spoken_words(transcript.text)
                words = words[drop_overlap(session._stable.committed, words) :]
                pause = self.options.commit and self.options.pause_commit and speech_s + 1e-9 >= self.options.pause_commit_min_s
                if pause and key[0] == session._stable.offset:
                    session._stable.commit_all(words, key[1])
                    session._pause_end = key[1]
                    words = []
                else:
                    session._spec_results[key] = " ".join(words)
                partial = session._emit(session._stable.text, words, key[1], True)
            else:
                if self._partial_commits(key[1] - audio):
                    force = (key[1] - audio) / BYTES_PER_SECOND > self.options.max_window_s
                    tentative = [w.text for w in session._stable.update(key[0], transcript.words, key[1], force=force, audio_start=audio)]
                elif key[0] == session._stable.offset:
                    tentative = spoken_words(transcript.text)
                    tentative = tentative[drop_overlap(session._stable.committed, tentative) :]
                else:
                    tentative = None  # a pause commit moved the window while it ran
                if tentative is None:
                    session.dropped += 1
                else:
                    session.partials += 1
                    partial = session._emit(session._stable.text, tentative, key[1], False)
            callback = session.on_partial
            source = session.hint_source if partial is not None else None
            if partial is not None:
                session._heard = partial.text
        if callback is not None and partial is not None:
            try:
                callback(partial)
            except Exception:
                pass  # a display failure must not stop transcription
        if source is not None:
            self._follow_heard(session, source, partial.text)

    def _follow_heard(self, session: Session, source: HintSource, heard: str) -> None:
        """Hints chosen by the session's hint source for the words of a partial shown (worker thread).

        Also after a release that came while it ran: the final, queued behind
        this job, then decodes with them.
        """
        ok, chosen = _choose(source, heard)
        if not ok:
            return
        with self._cond:
            if session.closed or session.hint_source is not source:
                return
            if session._switch(chosen):
                session.hint_switches += 1
