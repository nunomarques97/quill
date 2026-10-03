"""quill.streaming tests with a fake model and a fake clock.

The fake model "hears" invented words: each word is a burst of a constant
level (word N has level 1000 + 200 * N) and silence is zeros. No GPU, model
file, microphone or sound is used.
"""

import math
import threading
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace

from quill import whisper
from quill.streaming import (
    BYTES_PER_SECOND,
    FRAME_BYTES,
    MODEL_OPTIONS,
    SpeechDetector,
    Stabilizer,
    StreamingTranscriber,
    StreamOptions,
    drop_overlap,
    options_for,
)
from quill.whisper import Decode, Transcript, Word

RATE = 16_000
FRAME = 320


def silence(seconds):
    return bytes(2 * int(round(seconds * RATE)))


def word(index, seconds=0.3):
    level = 1000 + 200 * index
    return array("h", [level if i % 2 else -level for i in range(int(round(seconds * RATE)))]).tobytes()


def speech(indexes, gap=0.2, lead=0.2, trail=0.1, seconds=0.3):
    pcm = silence(lead)
    for position, index in enumerate(indexes):
        if position:
            pcm += silence(gap)
        pcm += word(index, seconds)
    return pcm + silence(trail)


def text(indexes):
    return " ".join(f"w{i}" for i in indexes)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


class FakeModel:
    """Decodes bursts into words with times; records every call."""

    def __init__(self, clock=None, cost_s=0.0):
        self.calls = []
        self.loads = self.closes = 0
        self.clock = clock
        self.cost_s = cost_s
        self.fail_load = None
        self.fail = lambda call: None  # returns an exception to raise, or None
        self.gate = None  # threading.Event the call waits on
        self.gate_when = lambda call: False
        self.entered = threading.Event()
        self.suffix = lambda call: ""

    def load(self):
        self.loads += 1
        if self.fail_load:
            raise self.fail_load

    def close(self):
        self.closes += 1

    def transcribe(self, pcm, options):
        call = SimpleNamespace(index=len(self.calls), seconds=len(pcm) / BYTES_PER_SECOND, options=options)
        self.calls.append(call)
        if self.gate is not None and self.gate_when(call):
            self.entered.set()
            self.gate.wait(10)
        if self.clock is not None:
            self.clock.advance(self.cost_s)
        error = self.fail(call)
        if error is not None:
            raise error
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
        words, current, start = [], None, 0
        frames = [samples[i : i + FRAME] for i in range(0, len(samples), FRAME)]
        for position, frame in enumerate(frames + [array("h")]):
            level = max((abs(v) for v in frame), default=0) if len(frame) else 0
            index = (level - 1000) // 200 if level >= 1000 else None
            if index != current:
                if current is not None:
                    words.append(Word(start * 0.02, position * 0.02, f"w{current}{self.suffix(call)}"))
                current, start = index, position
        return Transcript(" ".join(w.text for w in words), tuple(words) if options.word_timestamps else ())


def started(model, clock=None, **options):
    transcriber = StreamingTranscriber(model, StreamOptions(**options), clock=clock or FakeClock())
    transcriber.start()
    assert transcriber.ready.wait(5)
    return transcriber


def replay(transcriber, pcm, on_partial=None, chunk_s=0.05):
    session = transcriber.open(on_partial)
    size = int(chunk_s * BYTES_PER_SECOND)
    for start in range(0, len(pcm), size):
        session.feed(pcm[start : start + size])
        assert transcriber.drain(5)
    result = session.release().wait(5)
    assert result is not None
    return session, result


class Case(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.model = FakeModel(self.clock)
        self.transcribers = []

    def tearDown(self):
        if self.model.gate is not None:
            self.model.gate.set()
        for transcriber in self.transcribers:
            transcriber.stop(timeout=5)

    def start(self, **options):
        transcriber = started(self.model, self.clock, **options)
        self.transcribers.append(transcriber)
        return transcriber


class PartialOrderTest(Case):
    def test_partials_in_order_with_growing_committed_prefix(self):
        transcriber = self.start(agreement=True)
        partials = []
        indexes = list(range(1, 13))
        _, result = replay(transcriber, speech(indexes), partials.append)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, text(indexes))
        self.assertGreater(len(partials), 5)
        self.assertEqual([p.seq for p in partials], list(range(1, len(partials) + 1)))
        for before, after in zip(partials, partials[1:]):
            self.assertTrue(after.committed.startswith(before.committed))
            self.assertGreaterEqual(after.audio_s, before.audio_s)
        self.assertGreater(result.committed_words, 0)
        self.assertEqual(result.partials, len([p for p in partials if not p.speculative]))
        # Partials use the fast beam with word times; the final the final beam without.
        partial_calls = [c for c in self.model.calls if c.options.word_timestamps]
        final_calls = [c for c in self.model.calls if not c.options.word_timestamps]
        self.assertTrue(all(c.options.beam_size == 1 for c in partial_calls))
        self.assertEqual([c.options.beam_size for c in final_calls], [5])
        self.assertTrue(final_calls[0].options.without_timestamps)

    def test_final_transcribes_only_the_uncommitted_tail(self):
        transcriber = self.start(agreement=True)
        pcm = speech(list(range(1, 16)))
        _, result = replay(transcriber, pcm)
        final = [c for c in self.model.calls if not c.options.word_timestamps][-1]
        self.assertLess(final.seconds, 3.0)
        self.assertAlmostEqual(final.seconds, result.tail_s, places=3)
        self.assertLess(result.tail_s, len(pcm) / BYTES_PER_SECOND / 2)
        # Committed audio is never transcribed again: later windows start later.
        self.assertLess(max(c.seconds for c in self.model.calls), 4.0)

    def test_committed_text_goes_back_as_prompt_after_the_vocabulary(self):
        transcriber = StreamingTranscriber(self.model, StreamOptions(agreement=True), ["Zorblax", "deploy"], clock=self.clock)
        transcriber.start()
        self.transcribers.append(transcriber)
        _, result = replay(transcriber, speech(list(range(1, 10))))
        self.assertTrue(result.ok)
        first, last = self.model.calls[0].options, self.model.calls[-1].options
        self.assertEqual(first.initial_prompt, "Vocabulário: Zorblax, deploy.")
        self.assertEqual(first.hotwords, "Zorblax deploy")
        self.assertTrue(last.initial_prompt.startswith("Vocabulário: Zorblax, deploy. w1 w2"))
        call = self.model.calls[-1]
        self.assertEqual(last.max_new_tokens, math.ceil(call.seconds * 10.0) + 24)

    def test_context_can_be_disabled(self):
        transcriber = self.start(context_chars=0)
        replay(transcriber, speech(list(range(1, 10))))
        self.assertTrue(all(c.options.initial_prompt is None for c in self.model.calls))


class StaleTest(Case):
    def test_newer_partial_replaces_pending_one_and_release_drops_in_flight(self):
        self.model.gate = threading.Event()
        self.model.gate_when = lambda call: call.index == 0
        transcriber = self.start(speculate=False)
        shown = []
        session = transcriber.open(shown.append)
        pcm = speech(list(range(1, 8)), gap=0.1)
        chunk = int(0.05 * BYTES_PER_SECOND)
        position = 0

        def feed_until(seconds):
            nonlocal position
            end = int(seconds * BYTES_PER_SECOND)
            while position < end:
                session.feed(pcm[position : position + chunk])
                position += chunk

        feed_until(0.6)  # the first partial starts and blocks inside the model
        self.assertTrue(self.model.entered.wait(5))
        feed_until(1.1)  # a second partial waits
        self.assertEqual(session.dropped, 0)
        feed_until(1.6)  # a newer one replaces it
        self.assertEqual(session.dropped, 1)
        feed_until(len(pcm) / BYTES_PER_SECOND)
        handle = session.release()  # the pending partial is dropped; the in-flight one will be
        self.assertFalse(handle.done)
        self.model.gate.set()
        result = handle.wait(5)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, text(range(1, 8)))
        self.assertEqual(shown, [])  # the in-flight partial finished after release: not shown
        # Requests at 0.5 (ran), 1.0, 1.5, 2.0, 2.5 and 3.0 s: four replaced while
        # waiting, one dropped at release, and the in-flight one after it.
        self.assertEqual(result.dropped, 6)
        self.assertEqual(len(self.model.calls), 2)  # first partial and the final only
        self.assertEqual(result.committed_words, 0)  # nor committed

    def test_release_is_idempotent_and_cancel_forgets(self):
        transcriber = self.start()
        session = transcriber.open()
        session.feed(speech([3]))
        handle = session.release()
        self.assertIs(session.release(), handle)
        self.assertEqual(handle.wait(5).text, "w3")
        other = transcriber.open()
        other.feed(speech([4, 5]))
        other.cancel()
        other.feed(speech([6]))
        self.assertTrue(transcriber.drain(5))
        self.assertEqual(other.audio_s, len(speech([4, 5])) / BYTES_PER_SECOND)


class NoCommitTest(Case):
    def test_without_commits_partials_are_shown_and_the_final_covers_everything(self):
        transcriber = self.start(commit=False)
        partials = []
        pcm = speech(list(range(1, 11)), trail=0.6) + speech([11, 12], lead=0.0)
        _, result = replay(transcriber, pcm, partials.append)
        self.assertEqual(result.text, text(range(1, 13)))
        self.assertEqual(result.committed_words, 0)
        self.assertTrue(all(p.committed == "" for p in partials))
        self.assertIn("w5", partials[-1].text)
        final = [c for c in self.model.calls if not c.options.word_timestamps][-1]
        self.assertGreater(final.seconds, len(pcm) / BYTES_PER_SECOND - 0.6)

    def test_without_agreement_only_pauses_commit_and_partials_skip_word_times(self):
        transcriber = self.start(agreement=False, pause_commit_min_s=1.5)
        partials = []
        first, second = speech(list(range(1, 7)), trail=0.6), speech([7, 8], lead=0.0)
        _, result = replay(transcriber, first + second, partials.append)
        self.assertEqual(result.text, text(range(1, 9)))
        self.assertEqual(result.committed_words, 6)  # the pause, never agreement
        self.assertFalse(any(c.options.word_timestamps for c in self.model.calls))
        shown = [p for p in partials if not p.speculative]
        self.assertTrue(shown and all(p.committed in ("", text(range(1, 7))) for p in shown))
        self.assertIn("w7", partials[-1].text)
        self.assertLess(self.model.calls[-1].seconds, len(second) / BYTES_PER_SECOND + 0.3)

    def test_without_agreement_a_long_window_still_commits(self):
        transcriber = self.start(agreement=False, pause_commit=False, max_window_s=5.0)
        _, result = replay(transcriber, speech(list(range(1, 21)), gap=0.1))
        self.assertEqual(result.text, text(range(1, 21)))
        self.assertGreater(result.committed_words, 0)
        self.assertTrue(any(c.options.word_timestamps for c in self.model.calls))


class EdgeAudioTest(Case):
    def test_empty_audio(self):
        transcriber = self.start()
        _, result = replay(transcriber, b"")
        self.assertEqual((result.ok, result.text, result.speech, len(self.model.calls)), (True, "", False, 0))

    def test_silence_only_is_never_decoded(self):
        transcriber = self.start()
        _, result = replay(transcriber, silence(2.0))
        self.assertEqual((result.text, result.speech, len(self.model.calls)), ("", False, 0))

    def test_sub_second_audio(self):
        transcriber = self.start()
        _, result = replay(transcriber, speech([3], lead=0.05, trail=0.05))
        self.assertEqual((result.text, result.partials, len(self.model.calls)), ("w3", 0, 1))

    def test_leading_silence_is_trimmed_before_decoding(self):
        transcriber = self.start(speculate=False)
        pcm = speech([2, 3], lead=2.0)
        _, result = replay(transcriber, pcm)
        self.assertEqual(result.text, text([2, 3]))
        # Every window starts lead_s (0.2 s) before the first word at 2.0 s.
        self.assertTrue(all(c.seconds <= len(pcm) / BYTES_PER_SECOND - 1.8 + 0.02 for c in self.model.calls))

    def test_noise_heard_before_the_floor_is_known_is_not_decoded(self):
        transcriber = self.start()
        noise = array("h", [60 if i % 2 else -60 for i in range(2 * RATE)]).tobytes()
        _, result = replay(transcriber, noise)
        self.assertEqual((result.ok, result.text, len(self.model.calls)), (True, "", 0))

    def test_punctuation_only_output_is_not_text(self):
        self.model.suffix = lambda call: ""
        original = self.model.transcribe

        def dotted(pcm, options):
            out = original(pcm, options)
            dots = tuple(Word(w.end, w.end + 0.01, ".") for w in out.words)
            return Transcript(". " + out.text + " . …", tuple(sorted(out.words + dots, key=lambda w: w.start)))

        self.model.transcribe = dotted
        transcriber = self.start()
        _, result = replay(transcriber, speech(list(range(1, 9))))
        self.assertEqual(result.text, text(range(1, 9)))

    def test_odd_chunk_sizes(self):
        transcriber = self.start()
        _, result = replay(transcriber, speech([1, 2, 3, 4]), chunk_s=0.0371)
        self.assertEqual(result.text, text([1, 2, 3, 4]))

    def test_audio_longer_than_30_s_keeps_windows_bounded(self):
        indexes = [1 + i % 30 for i in range(80)]
        pcm = speech(indexes)
        self.assertGreater(len(pcm) / BYTES_PER_SECOND, 30)
        # Without pauses, the default commits only past max_window_s; agreement commits early.
        for options, bound in (({}, StreamOptions().max_window_s + 0.5 + 0.05), ({"agreement": True}, 5.0)):
            model = FakeModel(self.clock)
            transcriber = started(model, self.clock, speculate=False, **options)
            self.transcribers.append(transcriber)
            _, result = replay(transcriber, pcm)
            self.assertEqual(result.text, text(indexes))
            self.assertGreater(result.committed_words, 0)
            self.assertLess(max(c.seconds for c in model.calls), bound)

    def test_audio_longer_than_30_s_with_the_default_engine_tuning(self):
        indexes = [1 + i % 30 for i in range(80)]
        pcm = speech(indexes)
        self.assertGreater(len(pcm) / BYTES_PER_SECOND, 30)
        transcriber = StreamingTranscriber(self.model, options_for(whisper.DEFAULT_MODEL), clock=self.clock)
        transcriber.start()
        self.transcribers.append(transcriber)
        partials = []
        _, result = replay(transcriber, pcm, partials.append)
        self.assertEqual((result.ok, result.text, result.committed_words), (True, text(indexes), 0))
        self.assertTrue(partials and all(p.committed == "" for p in partials))
        # The final decodes the whole utterance once; its token bound stays per 30 s window.
        final = self.model.calls[-1]
        self.assertGreater(final.seconds, 30)
        self.assertLessEqual(final.options.max_new_tokens, int(30 * 10.0) + 24)

    def test_without_agreement_long_windows_are_force_committed(self):
        self.model.suffix = lambda call: "ab"[call.index % 2]  # consecutive partials never agree
        transcriber = self.start(agreement=True, speculate=False, max_window_s=8.0)
        pcm = speech([1 + i % 30 for i in range(70)])
        self.assertGreater(len(pcm) / BYTES_PER_SECOND, 30)
        _, result = replay(transcriber, pcm)
        self.assertTrue(result.ok)
        self.assertGreater(result.committed_words, 0)
        self.assertLess(max(c.seconds for c in self.model.calls), 8.0 + 0.5 + 0.05)
        self.assertEqual(len(result.text.split()), 70)


class FailureTest(Case):
    def test_final_failure_is_an_error_result_and_the_next_session_works(self):
        self.model.fail = lambda call: RuntimeError("cuda") if not call.options.word_timestamps and call.index < 99 else None
        transcriber = self.start(speculate=False)
        _, result = replay(transcriber, speech([1, 2, 3]))
        self.assertEqual((result.ok, result.text, result.error), (False, "", "transcription failed: RuntimeError"))
        self.model.fail = lambda call: None
        _, result = replay(transcriber, speech([4, 5]))
        self.assertEqual(result.text, "w4 w5")

    def test_partial_failure_is_counted_and_skipped(self):
        self.model.fail = lambda call: ValueError("x") if call.index == 0 else None
        transcriber = self.start()
        _, result = replay(transcriber, speech(list(range(1, 8))))
        self.assertEqual(transcriber.partial_errors, 1)
        self.assertEqual(result.text, text(range(1, 8)))

    def test_load_failure_gives_error_results_without_decoding(self):
        self.model.fail_load = RuntimeError("no device")
        transcriber = self.start()
        self.assertEqual(transcriber.load_error, "model unavailable: no device")
        _, result = replay(transcriber, speech([1, 2]))
        self.assertEqual((result.ok, result.error, len(self.model.calls)), (False, "model unavailable: no device", 0))

    def test_display_callback_failure_does_not_stop_transcription(self):
        def broken(partial):
            raise RuntimeError("display")

        transcriber = self.start()
        _, result = replay(transcriber, speech(list(range(1, 8))), broken)
        self.assertEqual(result.text, text(range(1, 8)))


class LifecycleTest(Case):
    def test_release_when_not_running_fails_at_once(self):
        transcriber = StreamingTranscriber(self.model, clock=self.clock)
        session = transcriber.open()
        session.feed(speech([1]))
        result = session.release().wait(0)
        self.assertEqual((result.ok, result.error), (False, "streaming not running"))

    def test_stop_resolves_pending_final_then_restart_works(self):
        self.model.gate = threading.Event()
        self.model.gate_when = lambda call: call.index == 0
        transcriber = self.start(speculate=False)
        session = transcriber.open()
        session.feed(speech(list(range(1, 6))))
        self.assertTrue(self.model.entered.wait(5))
        handle = session.release()
        transcriber.stop(timeout=0.05)  # the worker is still inside the model call
        result = handle.wait(1)
        self.assertEqual((result.ok, result.error), (False, "streaming stopped"))
        self.assertEqual(self.model.closes, 0)  # never closed under a running call
        self.model.gate.set()
        transcriber.start()
        self.assertTrue(transcriber.ready.wait(5))
        _, result = replay(transcriber, speech([7, 8]))
        self.assertEqual(result.text, "w7 w8")
        transcriber.stop()
        self.assertEqual((self.model.loads, self.model.closes), (2, 1))
        transcriber.start()
        _, result = replay(transcriber, speech([9]))
        self.assertEqual(result.text, "w9")
        transcriber.stop()
        self.assertEqual((self.model.loads, self.model.closes), (3, 2))
        for thread in threading.enumerate():
            if thread.name == "quill-asr":
                thread.join(5)
        self.assertFalse(any(t.name == "quill-asr" and t.is_alive() for t in threading.enumerate()))
        with self.assertRaises(RuntimeError):
            transcriber.start()
            transcriber.start()

    def test_capturing_session_is_closed_by_stop(self):
        transcriber = self.start()
        session = transcriber.open()
        session.feed(speech([1]))
        transcriber.stop()
        session.feed(speech([2]))
        result = session.release().wait(0)
        self.assertEqual(result.error, "streaming not running")


class SpeculationTest(Case):
    def test_pause_before_release_reuses_the_speculative_final(self):
        transcriber = self.start()
        partials = []
        session, result = replay(transcriber, speech([1, 2, 3, 4], trail=0.6), partials.append)
        self.assertTrue(result.speculative_hit)
        self.assertEqual(result.text, text([1, 2, 3, 4]))
        self.assertEqual(result.compute_s, 0.0)
        self.assertTrue(partials[-1].speculative)
        self.assertEqual(partials[-1].text, text([1, 2, 3, 4]))

    def test_speech_after_the_pause_invalidates_it(self):
        transcriber = self.start()
        pcm = speech([1, 2], trail=0.6) + speech([3, 4], lead=0.0)
        _, result = replay(transcriber, pcm)
        self.assertFalse(result.speculative_hit)
        self.assertEqual(result.text, text([1, 2, 3, 4]))

    def test_long_pause_commits_the_window_and_the_final_decodes_only_what_follows(self):
        transcriber = self.start(pause_commit_min_s=1.5)
        first, second = speech(list(range(1, 7)), trail=0.6), speech([7, 8], lead=0.0)
        _, result = replay(transcriber, first + second)
        self.assertEqual(result.text, text(range(1, 9)))
        self.assertGreaterEqual(result.committed_words, 6)
        final = [c for c in self.model.calls if not c.options.word_timestamps][-1]
        self.assertLess(final.seconds, len(second) / BYTES_PER_SECOND + 0.3)

    def test_pause_commit_off_or_too_short_keeps_it_speculative(self):
        pcm = speech(list(range(1, 7)), trail=0.6) + speech([7, 8], lead=0.0)
        for options in ({"pause_commit": False}, {"pause_commit_min_s": 5.0}):
            model = FakeModel(self.clock)
            # Partials never agree, so only a pause could commit the first words.
            model.suffix = lambda call, model=model: (
                "ab"[sum(c.options.word_timestamps for c in model.calls) % 2] if call.options.word_timestamps else "")
            transcriber = started(model, self.clock, **options)
            self.transcribers.append(transcriber)
            _, result = replay(transcriber, pcm)
            self.assertEqual(result.text, text(range(1, 9)))
            finals = [c for c in model.calls if not c.options.word_timestamps]
            self.assertGreater(finals[-1].seconds, 2.0)  # the final still covers words before the pause
            self.assertEqual(result.committed_words, 0)

    def test_release_while_the_speculative_final_runs_reuses_it(self):
        self.model.gate = threading.Event()
        self.model.gate_when = lambda call: not call.options.word_timestamps
        transcriber = self.start()
        session = transcriber.open()
        pcm = speech([1, 2, 3], trail=0.6)
        chunk = int(0.05 * BYTES_PER_SECOND)
        for start in range(0, len(pcm), chunk):
            session.feed(pcm[start : start + chunk])
        self.assertTrue(self.model.entered.wait(5))
        handle = session.release()
        self.assertFalse(handle.done)
        self.model.gate.set()
        result = handle.wait(5)
        self.assertEqual((result.ok, result.text, result.speculative_hit), (True, text([1, 2, 3]), True))
        self.assertEqual(len([c for c in self.model.calls if not c.options.word_timestamps]), 1)

    def test_same_final_text_with_speculation_off(self):
        pcm = speech([5, 6, 7, 8, 9], trail=0.8)
        _, with_spec = replay(self.start(), pcm)
        _, without = replay(self.start(speculate=False), pcm)
        self.assertEqual(with_spec.text, without.text)


class ClockTest(Case):
    def test_timings_come_from_the_injected_clock(self):
        self.model.cost_s = 0.25
        transcriber = self.start(speculate=False)
        _, result = replay(transcriber, speech([1, 2, 3]))
        self.assertAlmostEqual(result.compute_s, 0.25)
        self.assertAlmostEqual(result.waited_s, 0.0)
        self.assertAlmostEqual(result.latency_s, 0.25)


class DetectorTest(unittest.TestCase):
    def test_result_does_not_depend_on_chunking(self):
        pcm = speech([2, 5, 9], gap=0.37)
        results = set()
        for size in (FRAME_BYTES, 1234, 1600, len(pcm)):
            detector = SpeechDetector(0.001, 4.0)
            for start in range(0, len(pcm), size):
                detector.feed(pcm[start : start + size])
            results.add((detector.frames, detector.speech_frames, detector.speech_end))
        self.assertEqual(len(results), 1)
        frames, speech_frames, end = results.pop()
        self.assertEqual(speech_frames, 46)  # 45 word frames plus one straddling a word start
        self.assertEqual(end, len(pcm) - len(silence(0.1)))

    def test_floor_raises_the_threshold_on_a_noisy_input(self):
        noise = array("h", [200 if i % 2 else -200 for i in range(RATE)]).tobytes()  # rms 0.006
        detector = SpeechDetector(0.001, 4.0)
        detector.feed(noise)
        self.assertLessEqual(detector.speech_frames, 25)  # only until the floor is known
        before = detector.speech_frames
        detector.feed(noise + word(1))
        self.assertEqual(detector.speech_frames - before, 15)


class StabilizerTest(unittest.TestCase):
    @staticmethod
    def words(*items):
        return [Word(start, end, name) for name, start, end in items]

    def test_agreement_margin_and_cut(self):
        stable = Stabilizer(margin_s=1.0)
        end = 4 * BYTES_PER_SECOND
        first = self.words(("Alfa", 0.2, 0.6), ("beta", 0.8, 1.2), ("gama", 2.5, 3.5))
        self.assertEqual([w.text for w in stable.update(0, first, end)], ["Alfa", "beta", "gama"])
        self.assertEqual(stable.committed, [])
        second = self.words(("alfa,", 0.2, 0.6), ("beta", 0.8, 1.2), ("gama", 2.5, 3.5))
        tentative = stable.update(0, second, end)
        self.assertEqual(stable.committed, ["alfa,", "beta"])  # normalized agreement; gama ends inside the margin
        self.assertEqual([w.text for w in tentative], ["gama"])
        self.assertEqual(stable.offset, int(1.85 * 16_000) * 2)  # halfway to the next word

    def test_results_for_an_old_window_are_ignored(self):
        stable = Stabilizer(1.0)
        stable.offset = 3200
        self.assertEqual(stable.update(0, self.words(("x", 0, 0.1)), 64000), [])

    def test_repeated_words_across_the_cut_are_dropped(self):
        stable = Stabilizer(0.5)
        stable.committed = ["abre", "o", "ficheiro"]
        stable.offset = 32000
        tentative = stable.update(32000, self.words(("ficheiro", 0.0, 0.3), ("novo", 0.5, 0.8)), 32000 * 4)
        self.assertEqual([w.text for w in tentative], ["novo"])
        self.assertEqual(drop_overlap(["a", "b", "c"], ["b", "c", "d"]), 2)
        self.assertEqual(drop_overlap(["a"], ["b"]), 0)

    def test_force_commits_without_agreement_and_skips_empty_windows(self):
        stable = Stabilizer(1.0)
        stable.update(0, self.words(("um", 0.1, 0.4), ("dois", 3.0, 3.4)), 4 * 32000, force=True)
        self.assertEqual(stable.committed, ["um"])
        stable = Stabilizer(1.0)
        stable.update(0, [], 10 * 32000, force=True)
        self.assertEqual((stable.committed, stable.offset), ([], 9 * 32000))


class OptionsTest(unittest.TestCase):
    def test_out_of_range_options_are_refused(self):
        for bad in ({"step_s": 0}, {"final_beam": 0}, {"max_window_s": 29.5}, {"context_chars": 601}, {"floor_ratio": 0.5}):
            with self.assertRaises(ValueError):
                StreamOptions(**bad)

    def test_every_engine_model_has_its_tuning(self):
        self.assertEqual(set(MODEL_OPTIONS), set(whisper.MODELS))
        self.assertEqual(whisper.DEFAULT_MODEL, "large-v3-turbo")
        self.assertEqual(whisper.Whisper().model, whisper.DEFAULT_MODEL)
        self.assertFalse(options_for("large-v3-turbo").commit)
        self.assertEqual(options_for(whisper.PRECISE_MODEL), StreamOptions())
        with self.assertRaises(ValueError):
            options_for("tiny")


class WhisperTest(unittest.TestCase):
    def test_vocabulary_options(self):
        self.assertEqual(whisper.hint_options([]), (None, None))
        prompt, hot = whisper.hint_options(["Zorblax", "pull request", "deploy"], max_chars=21)
        self.assertEqual((prompt, hot), ("Vocabulário: Zorblax, pull request.", "Zorblax pull request deploy"[:20]))

    def test_generated_token_bound_fits_the_context(self):
        model = whisper.Whisper()
        encoder = SimpleNamespace(encode=lambda text, add_special_tokens=False: SimpleNamespace(ids=text.split()))
        model._whisper = SimpleNamespace(hf_tokenizer=encoder)
        short = Decode(initial_prompt="um dois", hotwords="tres", max_new_tokens=50, without_timestamps=True)
        self.assertEqual(model._prompt_length(short), 1 + 1 + 2 + 3 + 1)
        self.assertEqual(model._max_new_tokens(short), 50)
        long_prompt = " ".join(["x"] * 300)
        full = Decode(initial_prompt=long_prompt, hotwords=long_prompt, max_new_tokens=50)
        self.assertIsNone(model._max_new_tokens(full))  # 1 + 223 + 223 + 3 leaves no room
        some = Decode(initial_prompt=long_prompt, hotwords="a b", max_new_tokens=500)
        self.assertEqual(model._max_new_tokens(some), 448 - (1 + 2 + 223 + 3))
        self.assertIsNone(model._max_new_tokens(Decode()))
        model._whisper = SimpleNamespace()
        self.assertIsNone(model._max_new_tokens(short))

    def test_the_decode_language_overrides_the_model_language(self):
        class Audio:
            def astype(self, _type):
                return self

            def __truediv__(self, _value):
                return self

        seen = []
        model = whisper.Whisper(language="en")
        model._numpy = SimpleNamespace(frombuffer=lambda data, dtype: Audio(), int16="int16", float32="float32")
        model._whisper = SimpleNamespace(transcribe=lambda audio, **kwargs: (seen.append(kwargs) or ([], None)))
        model.transcribe(bytes(2), Decode(language="pt"))
        model.transcribe(bytes(2), Decode())
        self.assertEqual([kwargs["language"] for kwargs in seen], ["pt", "en"])
        self.assertEqual(whisper.Whisper().language, "pt")

    def test_token_counter_uses_the_tokenizer_or_counts_bytes(self):
        import tempfile

        encoder = SimpleNamespace(encode=lambda text, add_special_tokens=False: SimpleNamespace(ids=text.split()))
        loads = []
        counter = whisper.TokenCounter(loader=lambda path: loads.append(path.name) or encoder)
        self.assertEqual(counter("  um dois tres "), 3)
        self.assertEqual(counter("quatro"), 1)
        self.assertTrue(counter.exact)
        self.assertEqual(loads, ["tokenizer.json"])  # read once

        def broken(path):
            raise OSError("invented")

        fallback = whisper.TokenCounter(loader=broken)
        self.assertEqual(fallback("ção"), len(" ção".encode("utf-8")))
        self.assertFalse(fallback.exact)
        with tempfile.TemporaryDirectory() as folder:
            missing = whisper.TokenCounter("large-v3", Path(folder))
            self.assertEqual(missing("ab cd"), 6)
            self.assertFalse(missing.exact)

    def test_missing_model_is_unavailable_without_importing(self):
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(whisper.WhisperUnavailable):
                whisper.Whisper("large-v3", Path(folder)).load()
        self.assertNotIn("faster_whisper", sys.modules)
        with self.assertRaises(ValueError):
            whisper.Whisper("tiny")


class SessionHintsTest(Case):
    """A session opened with hints (a voice command) decodes with them; no other session does."""

    HINTS = whisper.SessionHints(prompt="Abre o VS Code no projeto. Projetos: zorblax, alfa-beta.",
                                 hotwords="zorblax alfa-beta alfa beta", language="pt")

    def transcriber(self, **options):
        transcriber = StreamingTranscriber(self.model, StreamOptions(**options), ["Zorblax", "deploy"], clock=self.clock)
        transcriber.start()
        self.transcribers.append(transcriber)
        return transcriber

    def replay_with(self, transcriber, pcm, hints):
        session = transcriber.open(hints=hints)
        size = int(0.05 * BYTES_PER_SECOND)
        for start in range(0, len(pcm), size):
            session.feed(pcm[start : start + size])
            self.assertTrue(transcriber.drain(5))
        result = session.release().wait(5)
        self.assertTrue(result.ok)
        return result

    def assert_voice(self, calls):
        for call in calls:
            self.assertEqual(call.options.initial_prompt, self.HINTS.prompt)
            self.assertEqual(call.options.hotwords, self.HINTS.hotwords)
            self.assertEqual(call.options.language, "pt")

    def assert_dictation(self, calls):
        for call in calls:
            self.assertEqual(call.options.initial_prompt, "Vocabulário: Zorblax, deploy.")
            self.assertEqual(call.options.hotwords, "Zorblax deploy")
            self.assertIsNone(call.options.language)

    def test_partial_speculative_and_final_decodes_use_the_session_hints_only(self):
        transcriber = self.transcriber(pause_commit=False)
        # Pauses longer than tail_pad_s ask for speculative finals; the short trail leaves a real final.
        result = self.replay_with(transcriber, speech([1, 2, 3], gap=0.6, trail=0.1), self.HINTS)
        self.assertEqual(result.text, text([1, 2, 3]))
        voice = list(self.model.calls)
        partials = [c for c in voice if c.options.beam_size == 1]
        finals = [c for c in voice if c.options.beam_size == 5]
        self.assertGreater(len(partials), 0)
        self.assertGreaterEqual(len(finals), 2)  # at least one speculative final and the release's final
        self.assert_voice(voice)
        # A later dictation session on the same transcriber gets the vocabulary hints again.
        _, again = replay(transcriber, speech([4, 5], gap=0.6, trail=0.1))
        self.assertEqual(again.text, text([4, 5]))
        dictation = self.model.calls[len(voice):]
        self.assertTrue(any(c.options.beam_size == 1 for c in dictation))
        self.assertTrue(any(c.options.beam_size == 5 for c in dictation))
        self.assert_dictation(dictation)

    def test_a_dictation_before_and_a_voice_session_after(self):
        transcriber = self.transcriber()
        replay(transcriber, speech([1, 2]))
        before = len(self.model.calls)
        self.assert_dictation(self.model.calls)
        self.replay_with(transcriber, speech([3]), self.HINTS)
        self.assert_voice(self.model.calls[before:])

    def test_empty_hints_decode_with_no_prompt_and_the_language(self):
        transcriber = self.transcriber()
        self.replay_with(transcriber, speech([1]), whisper.SessionHints(language="pt"))
        for call in self.model.calls:
            self.assertEqual((call.options.initial_prompt, call.options.hotwords, call.options.language),
                             (None, None, "pt"))

    def test_committed_context_follows_the_hints_within_the_prompt_bound(self):
        long_prompt = "Abre o VS Code no projeto. Projetos: " + ", ".join(["zorblax"] * 62) + "."
        self.assertLess(len(long_prompt), whisper.PROMPT_MAX_CHARS)
        hints = whisper.SessionHints(prompt=long_prompt, hotwords="zorblax", language="pt")
        transcriber = self.transcriber(agreement=True)
        self.replay_with(transcriber, speech(list(range(1, 10))), hints)
        prompts = [c.options.initial_prompt for c in self.model.calls]
        self.assertTrue(all(prompt.startswith(long_prompt) for prompt in prompts))
        self.assertTrue(any(len(prompt) > len(long_prompt) for prompt in prompts))  # committed words follow
        self.assertTrue(all(len(prompt) <= whisper.PROMPT_MAX_CHARS for prompt in prompts))


class SetHintsTest(Case):
    """``Session.set_hints``: mouse 5 gets its project's hints once its window is known."""

    HINTS = SessionHintsTest.HINTS
    transcriber = SessionHintsTest.transcriber
    assert_voice = SessionHintsTest.assert_voice
    assert_dictation = SessionHintsTest.assert_dictation

    def feed(self, transcriber, session, pcm, drain=True):
        size = int(0.05 * BYTES_PER_SECOND)
        for start in range(0, len(pcm), size):
            session.feed(pcm[start : start + size])
            if drain:
                self.assertTrue(transcriber.drain(5))

    def test_later_decodes_use_the_new_hints_and_an_earlier_speculation_is_decoded_again(self):
        transcriber = self.transcriber()
        session = transcriber.open()
        self.feed(transcriber, session, speech([1, 2], trail=0.6))  # a pause: a speculative final, vocabulary hints
        before = len(self.model.calls)
        self.assert_dictation(self.model.calls)
        self.assertTrue(any(c.options.beam_size == 5 for c in self.model.calls))
        self.assertTrue(session.set_hints(self.HINTS))
        self.feed(transcriber, session, speech([3], lead=0.0, trail=0.1))
        result = session.release().wait(5)
        self.assertEqual((result.ok, result.text, result.speculative_hit), (True, text([1, 2, 3]), False))
        after = self.model.calls[before:]
        self.assertTrue(any(c.options.beam_size == 5 for c in after))  # the final decoded with the new hints
        self.assert_voice(after)

    def test_a_pause_reused_only_when_decoded_with_the_current_hints(self):
        transcriber = self.transcriber()
        session = transcriber.open()
        self.feed(transcriber, session, speech([1, 2], trail=0.6))
        self.assertTrue(session.set_hints(self.HINTS))
        result = session.release().wait(5)  # no new audio: the old speculation is not reused
        self.assertEqual((result.text, result.speculative_hit), (text([1, 2]), False))
        self.assert_voice(self.model.calls[-1:])

    def test_a_speculation_running_when_the_hints_change_is_dropped(self):
        self.model.gate = threading.Event()
        self.model.gate_when = lambda call: not call.options.word_timestamps and call.options.beam_size == 5
        transcriber = self.transcriber()
        session = transcriber.open()
        partials = []
        session.on_partial = partials.append
        self.feed(transcriber, session, speech([1, 2], trail=0.6), drain=False)
        self.assertTrue(self.model.entered.wait(5))
        self.assertTrue(session.set_hints(self.HINTS))
        shown = len(partials)
        handle = session.release()
        self.model.gate_when = lambda call: False
        self.model.gate.set()
        result = handle.wait(5)
        self.assertEqual((result.ok, result.text, result.speculative_hit), (True, text([1, 2]), False))
        finals = [c for c in self.model.calls if c.options.beam_size == 5]
        self.assertEqual(len(finals), 2)  # the dropped speculation and the final decoded again
        self.assert_dictation(finals[:1])
        self.assert_voice(finals[1:])
        self.assertEqual(len(partials), shown)  # the stale speculation was never shown

    def test_refused_after_release_or_cancel_and_other_sessions_keep_the_vocabulary(self):
        transcriber = self.transcriber()
        session = transcriber.open()
        self.feed(transcriber, session, speech([1]))
        handle = session.release()
        self.assertFalse(session.set_hints(self.HINTS))
        self.assertTrue(handle.wait(5).ok)
        self.assert_dictation(self.model.calls)
        cancelled = transcriber.open()
        cancelled.cancel()
        self.assertFalse(cancelled.set_hints(self.HINTS))
        hinted = transcriber.open()
        self.assertTrue(hinted.set_hints(self.HINTS))
        self.assertTrue(hinted.set_hints(None))  # back to the vocabulary hints
        before = len(self.model.calls)
        self.feed(transcriber, hinted, speech([2]))
        self.assertTrue(hinted.release().wait(5).ok)
        _, other = replay(transcriber, speech([3]))
        self.assertTrue(other.ok)
        self.assert_dictation(self.model.calls[before:])


if __name__ == "__main__":
    unittest.main()
