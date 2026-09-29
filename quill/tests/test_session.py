"""quill.session tests: ordered dictation sessions with fake parts.

The transcriber, capture, click-to-focus, injector, pipeline and indicator
are fakes; no microphone, window, hook or input is touched. The texts are
invented.
"""

import logging
import threading
import time
import unittest
from types import SimpleNamespace

from quill import inject
from quill import session as S
from quill.focus import CLICKED, NO_WINDOW, FocusResult
from quill.indicator.render import ERROR, LISTENING, LOADING, SENT, TRANSCRIBING
from quill.inject import InjectResult, Target
from quill.session import Processed, SessionManager
from quill.tests.fakes import FakeCaptures, FakeClock, FakeIndicator
from quill.triggers import CANCEL, CONFIRM, START, STOP, Signal

TARGET = Target(hwnd=100, pid=7)
CLAUDE = Target(hwnd=500, pid=50)
OTHER = Target(hwnd=200, pid=8)
SPOKEN = "texto inventado de teste"
PCM = b"\x10\x27" * 1600  # 0.1 s of a constant level


def signal(kind, action="dictation", trigger="xbutton1", reason=""):
    return Signal(kind, action, trigger, 0.0, reason)


class FakeHandle:
    def __init__(self):
        self.event = threading.Event()
        self.result = None

    def resolve(self, text=SPOKEN, error=None):
        self.result = SimpleNamespace(text=text, error=error, ok=error is None)
        self.event.set()

    @property
    def done(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        self.event.wait(timeout)
        return self.result


class FakeAsr:
    def __init__(self, on_partial, fail_release=False):
        self.on_partial = on_partial
        self.fed = []
        self.cancelled = False
        self.handle = None
        self.fail_release = fail_release

    def feed(self, pcm):
        self.fed.append(pcm)

    def partial(self, text):
        self.on_partial(SimpleNamespace(text=text))

    def release(self):
        if self.fail_release:
            raise RuntimeError("fake release failure")
        self.handle = FakeHandle()
        return self.handle

    def cancel(self):
        self.cancelled = True

    @property
    def released_or_cancelled(self):
        return self.cancelled or (self.handle is not None and self.handle.done)


class FakeTranscriber:
    def __init__(self):
        self.sessions = []
        self.fail_release = False

    def open(self, on_partial=None):
        asr = FakeAsr(on_partial, self.fail_release)
        self.sessions.append(asr)
        return asr


class FakeFocus:
    def __init__(self):
        self.targets = [TARGET]
        self.calls = []
        self.error = None

    def on_confirm(self, action, trigger=""):
        self.calls.append((action, trigger))
        if self.error is not None:
            raise self.error
        target = self.targets.pop(0) if len(self.targets) > 1 else self.targets[0]
        return FocusResult(target, target is not None, CLICKED if target is not None else NO_WINDOW)


class FakeInjector:
    def __init__(self, clock):
        self.clock = clock
        self.typed = []
        self.enters = []
        self.results = []  # reasons to return, in order; empty: ok
        self.enter_reason = inject.OK
        self.enter_error = None

    def inject(self, text, target):
        self.clock.now += 0.1
        reason = self.results.pop(0) if self.results else inject.OK
        if reason == inject.OK:
            self.typed.append((text, target))
            return InjectResult(reason, len(text), len(text))
        return InjectResult(reason, 3 if reason == inject.FOREGROUND_CHANGED else 0, len(text))

    def press_enter(self, target):
        self.enters.append(target)
        if self.enter_error is not None:
            raise self.enter_error
        return InjectResult(self.enter_reason, 0, 1)


class FakePipeline:
    def __init__(self):
        self.calls = 0
        self.error = None
        self.notice = None

    def __call__(self, raw, target):
        self.calls += 1
        if self.error is not None:
            raise self.error
        claude = target == CLAUDE
        return Processed(raw.strip().capitalize() + ".", "claude-code" if claude else "default", claude, self.notice)


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.transcriber = FakeTranscriber()
        self.captures = FakeCaptures()
        self.focus = FakeFocus()
        self.injector = FakeInjector(self.clock)
        self.indicator = FakeIndicator()
        self.pipeline = FakePipeline()
        self.started = 0
        self.typed_hook = []
        self.manager = SessionManager(
            transcriber=self.transcriber, capture_factory=self.captures, focus=self.focus, injector=self.injector,
            indicator=self.indicator, pipeline=self.pipeline, on_session_start=self._started,
            on_typed=lambda target, text: self.typed_hook.append((target, text)), clock=self.clock,
            final_timeout_s=5.0, poll_s=0.05)
        self.manager.start()
        self.addCleanup(self.manager.stop)

    def _started(self):
        self.started += 1

    def ready(self):
        self.manager.loaded()

    def wait_outcomes(self, count):
        deadline = time.monotonic() + 5
        while len(self.manager.outcomes) < count:
            if time.monotonic() > deadline:
                self.fail(f"expected {count} outcomes, got {list(self.manager.outcomes)}")
            time.sleep(0.005)
        return list(self.manager.outcomes)

    def press(self, action="dictation", trigger="xbutton1"):
        self.manager.handle(signal(START, action, trigger))
        self.manager.handle(signal(CONFIRM, action, trigger))

    def release(self, action="dictation", trigger="xbutton1"):
        self.manager.handle(signal(STOP, action, trigger, "release"))

    def dictate(self, text=SPOKEN, action="dictation", error=None):
        """One full hold: press, audio, release, final result."""
        self.press(action)
        capture = self.captures.made[-1]
        capture.push(PCM)
        self.release(action)
        self.transcriber.sessions[-1].handle.resolve(text, error)

    def assert_released(self):
        self.assertEqual(self.captures.open, [])
        for asr in self.transcriber.sessions:
            self.assertTrue(asr.released_or_cancelled)
        self.assertEqual(self.manager._live, [])
        self.assertIsNone(self.manager._active)


class LoadingTest(SessionCase):
    def test_press_while_loading_shows_loading_and_records_nothing(self):
        self.press()
        self.release()
        self.assertEqual(self.captures.made, [])
        self.assertEqual(self.transcriber.sessions, [])
        self.assertEqual(self.focus.calls, [])
        self.assertEqual(self.indicator.states, [LOADING])
        self.assertEqual([o.reason for o in self.wait_outcomes(1)], [S.WHILE_LOADING])
        # Released while loading, then the model is ready: the next press records.
        self.ready()
        self.assertEqual(self.indicator.last, ("hide",))
        self.dictate()
        self.assertEqual(self.wait_outcomes(2)[-1].reason, S.TYPED)
        self.assertEqual(self.injector.typed, [("Texto inventado de teste.", TARGET)])

    def test_model_ready_while_a_loading_press_is_held_does_not_start_recording(self):
        self.press()
        self.ready()
        self.release()
        self.assertEqual(self.captures.made, [])
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.WHILE_LOADING)

    def test_load_error_shows_the_error_and_presses_record_nothing(self):
        self.manager.loaded(error=True)
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.MODEL_UNAVAILABLE]))
        self.press()
        self.release()
        self.assertEqual(self.captures.made, [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.MODEL_UNAVAILABLE]))


class FlowTest(SessionCase):
    def setUp(self):
        super().setUp()
        self.ready()

    def test_session_types_once_into_its_target_and_logs_no_text(self):
        with self.assertLogs("quill.session", level="DEBUG") as logs:
            self.press()
            capture = self.captures.made[0]
            self.assertTrue(capture.started)
            self.assertEqual(self.indicator.last, ("show", LISTENING, ""))
            capture.push(PCM)
            asr = self.transcriber.sessions[0]
            self.assertEqual(b"".join(asr.fed), PCM)
            asr.partial("texto inventado")
            self.assertIn(("text", "texto inventado"), self.indicator.calls)
            self.assertTrue(any(call[0] == "level" and call[1] > 0.5 for call in self.indicator.calls))
            self.clock.now = 10.0
            self.release()
            self.assertTrue(capture.stopped)
            self.assertEqual(self.indicator.last, ("show", TRANSCRIBING, "texto inventado"))
            asr.handle.resolve()
            outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.typed), (S.TYPED, len("Texto inventado de teste.")))
        self.assertAlmostEqual(outcome.release_to_typed_s, 0.1)
        self.assertEqual(self.injector.typed, [("Texto inventado de teste.", TARGET)])
        self.assertEqual(self.injector.enters, [])
        self.assertEqual(self.typed_hook, [(TARGET, "Texto inventado de teste.")])
        self.assertEqual(self.started, 1)
        self.assertEqual(self.indicator.last, ("hide",))
        text = "\n".join(logs.output)
        self.assertIn("release to typed 100 ms", text)
        for word in ("texto", "inventado", "teste"):
            self.assertNotIn(word, text.casefold())
        self.assert_released()

    def test_partials_after_release_do_not_change_the_indicator(self):
        self.press()
        asr = self.transcriber.sessions[0]
        self.release()
        asr.partial("tarde demais")
        self.assertNotIn(("text", "tarde demais"), self.indicator.calls)

    def test_short_tap_or_combo_cancel_releases_everything_and_types_nothing(self):
        for reason in ("short_hold", "combo"):
            self.manager.handle(signal(START))
            self.manager.handle(signal(CANCEL, reason=reason))
        outcomes = self.wait_outcomes(2)
        self.assertEqual([o.reason for o in outcomes], [S.CANCELLED, S.CANCELLED])
        self.assertTrue(all(asr.cancelled for asr in self.transcriber.sessions))
        self.assertEqual(self.injector.typed, [])
        self.assertEqual(self.focus.calls, [])
        self.assertEqual(self.indicator.last, ("hide",))
        self.assert_released()

    def test_new_press_during_finalization_captures_at_once_and_sessions_stay_ordered(self):
        self.focus.targets = [TARGET, OTHER]
        self.press()
        self.captures.made[0].push(PCM)
        self.release()
        first = self.transcriber.sessions[0]
        # The first session is still being finalized: the next press records immediately.
        self.press()
        second_capture = self.captures.made[1]
        self.assertTrue(second_capture.started)
        self.assertFalse(second_capture.stopped)
        self.assertEqual(self.indicator.last, ("show", LISTENING, ""))
        # The older session's result does not replace the live one on screen.
        self.transcriber.sessions[1].partial("segunda frase")
        second_capture.push(PCM)
        self.release()
        second = self.transcriber.sessions[1]
        second.handle.resolve("segunda frase")  # resolved first...
        time.sleep(0.05)
        self.assertEqual(self.injector.typed, [])  # ...but typed only after the first one
        first.handle.resolve("primeira frase")
        outcomes = self.wait_outcomes(2)
        self.assertEqual([o.reason for o in outcomes], [S.TYPED, S.TYPED])
        self.assertEqual(self.injector.typed, [("Primeira frase.", TARGET), ("Segunda frase.", OTHER)])
        self.assertEqual(self.indicator.last, ("hide",))
        self.assert_released()

    def test_older_session_ending_while_a_newer_one_listens_keeps_the_live_words(self):
        self.press()
        self.release()
        self.press()
        shown = len(self.indicator.states)
        self.transcriber.sessions[1].partial("ao vivo")
        self.transcriber.sessions[0].handle.resolve("", None)  # no speech: an error, but not shown
        self.wait_outcomes(1)
        self.assertEqual(self.manager.outcomes[0].reason, S.NO_SPEECH)
        self.assertEqual(self.indicator.states[shown:], [])
        self.assertEqual(self.indicator.last, ("show", LISTENING, ""))
        self.release()
        self.transcriber.sessions[1].handle.resolve()
        self.wait_outcomes(2)
        self.assertEqual(self.injector.typed, [("Texto inventado de teste.", TARGET)])

    def test_stop_cancels_the_hold_and_start_again_works(self):
        self.press()
        capture = self.captures.made[0]
        self.manager.stop()
        self.assertTrue(capture.stopped)
        self.assertTrue(self.transcriber.sessions[0].cancelled)
        self.assertEqual(self.manager.outcomes[-1].reason, S.CANCELLED)
        self.manager.handle(signal(START))  # after stop: ignored
        self.assertEqual(len(self.captures.made), 1)
        self.manager.start()
        self.manager.loaded()
        self.dictate()
        self.assertEqual(self.wait_outcomes(2)[-1].reason, S.TYPED)
        self.assertEqual(len(self.injector.typed), 1)

    def test_stop_waits_for_queued_sessions(self):
        self.dictate()
        self.manager.stop()
        self.assertEqual(self.manager.outcomes[-1].reason, S.TYPED)
        self.assertEqual(len(self.injector.typed), 1)

    def test_command_trigger_shows_it_is_unavailable_without_recording(self):
        self.press("command", "f14")
        self.release("command", "f14")
        self.assertEqual(self.captures.made, [])
        self.assertEqual(self.focus.calls, [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.COMMAND_UNAVAILABLE]))
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.COMMAND_UNAVAILABLE)

    def test_a_broken_indicator_never_breaks_a_session(self):
        self.indicator.fail = True
        with self.assertLogs("quill.session", level="ERROR"):
            self.dictate()
            self.wait_outcomes(1)
        self.assertEqual(self.manager.outcomes[0].reason, S.TYPED)
        self.assertEqual(len(self.injector.typed), 1)
        self.assert_released()


class FailureTest(SessionCase):
    """Every failure shows the error state, releases everything and leaves the next session unaffected."""

    def setUp(self):
        super().setUp()
        self.ready()

    def then_success(self, count):
        self.dictate("frase seguinte")
        outcomes = self.wait_outcomes(count + 1)
        self.assertEqual(outcomes[-1].reason, S.TYPED)
        self.assertEqual(self.injector.typed[-1], ("Frase seguinte.", TARGET))
        self.assert_released()

    def assert_error(self, reason, typed=0):
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual(outcome.reason, reason)
        self.assertEqual(self.indicator.last, ("show", ERROR, S.message(reason, typed)))
        self.assert_released()

    def test_microphone_missing_at_press(self):
        self.captures.fail_start = True
        self.press()
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.MIC_ERROR]))
        self.release()  # the release of the failed hold does nothing
        self.assert_error(S.MIC_ERROR)
        self.assertEqual(self.injector.typed, [])
        self.captures.fail_start = False
        self.then_success(1)

    def test_microphone_lost_while_recording(self):
        self.captures.fail_stop = True
        self.press()
        self.release()
        self.assert_error(S.MIC_ERROR)
        self.captures.fail_stop = False
        self.then_success(1)

    def test_no_field_under_the_pointer(self):
        self.focus.targets = [None]
        self.dictate_without_final()
        self.assert_error(NO_WINDOW)
        self.focus.targets = [TARGET]
        self.then_success(1)

    def dictate_without_final(self):
        self.press()
        self.captures.made[-1].push(PCM)
        self.release()

    def test_click_to_focus_raising(self):
        self.focus.error = OSError("fake")
        self.dictate_without_final()
        self.assert_error(S.FOCUS_FAILED)
        self.focus.error = None
        self.then_success(1)

    def test_engine_error(self):
        self.dictate(error="transcription failed: RuntimeError")
        self.assert_error(S.ENGINE_ERROR)
        self.then_success(1)

    def test_engine_timeout(self):
        self.manager.final_timeout_s = 0.05
        self.dictate_without_final()
        self.assert_error(S.ENGINE_TIMEOUT)
        self.manager.final_timeout_s = 5.0
        self.then_success(1)

    def test_no_speech(self):
        self.dictate(text="  ")
        self.assert_error(S.NO_SPEECH)
        self.then_success(1)

    def test_target_gone(self):
        self.injector.results = [inject.TARGET_GONE]
        self.dictate()
        self.assert_error(inject.TARGET_GONE)
        self.then_success(1)

    def test_foreground_changed_mid_typing_says_it_was_interrupted(self):
        self.injector.results = [inject.FOREGROUND_CHANGED]
        self.dictate()
        self.assert_error(inject.FOREGROUND_CHANGED, typed=3)
        self.assertTrue(self.indicator.last[2].endswith(S.INTERRUPTED))
        self.then_success(1)

    def test_pipeline_error(self):
        self.pipeline.error = ValueError("fake")
        self.dictate()
        self.assert_error(S.INTERNAL_ERROR)
        self.pipeline.error = None
        self.then_success(1)

    def test_release_failure_releases_the_hold(self):
        self.transcriber.fail_release = True
        with self.assertLogs("quill.session", level="ERROR"):
            self.press()
            self.release()
        self.assert_error(S.INTERNAL_ERROR)
        self.transcriber.fail_release = False
        self.then_success(1)

    def test_ollama_down_types_with_the_rules_and_says_so(self):
        self.pipeline.notice = S.CLEANUP_FALLBACK
        self.dictate()
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual(outcome.reason, S.CLEANUP_FALLBACK)
        self.assertEqual(len(self.injector.typed), 1)
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.CLEANUP_FALLBACK]))
        self.pipeline.notice = None
        self.then_success(1)


class SendClaudeTest(SessionCase):
    def setUp(self):
        super().setUp()
        self.ready()

    def test_enter_after_typing_into_claude_code(self):
        self.focus.targets = [CLAUDE]
        self.dictate(action="send_claude")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual(outcome.reason, S.SENT_ENTER)
        self.assertEqual(self.injector.typed, [("Texto inventado de teste.", CLAUDE)])
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(self.indicator.last, ("show", SENT, ""))

    def test_other_window_is_typed_without_enter_and_the_indicator_says_so(self):
        self.dictate(action="send_claude")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual(outcome.reason, S.NOT_CLAUDE)
        self.assertEqual(len(self.injector.typed), 1)
        self.assertEqual(self.injector.enters, [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.NOT_CLAUDE]))

    def test_normal_dictation_into_claude_code_never_presses_enter(self):
        self.focus.targets = [CLAUDE]
        self.dictate()
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.TYPED)
        self.assertEqual(self.injector.enters, [])

    def test_no_enter_when_typing_failed(self):
        self.focus.targets = [CLAUDE]
        self.injector.results = [inject.FOREGROUND_CHANGED]
        self.dictate(action="send_claude")
        self.assertEqual(self.wait_outcomes(1)[0].reason, inject.FOREGROUND_CHANGED)
        self.assertEqual(self.injector.enters, [])

    def test_refused_or_failing_enter_is_reported(self):
        self.focus.targets = [CLAUDE]
        self.injector.enter_reason = inject.FOREGROUND_CHANGED
        self.dictate(action="send_claude")
        self.injector.enter_error = OSError("fake")
        self.dictate(action="send_claude")
        outcomes = self.wait_outcomes(2)
        self.assertEqual([o.reason for o in outcomes], [S.ENTER_FAILED, S.ENTER_FAILED])
        self.assertEqual(len(self.injector.typed), 2)
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.ENTER_FAILED]))


class HelpersTest(unittest.TestCase):
    def test_voice_level(self):
        self.assertEqual(S.voice_level(b""), 0.0)
        self.assertEqual(S.voice_level(bytes(640)), 0.0)
        self.assertEqual(S.voice_level(b"\xff\x7f" * 320), 1.0)
        self.assertTrue(0.0 < S.voice_level(b"\x00\x02" * 320) < 0.5)

    def test_every_reason_has_a_portuguese_message(self):
        self.assertEqual(S.message("unknown"), S.MESSAGES[S.INTERNAL_ERROR])
        self.assertTrue(S.message(inject.TARGET_GONE, typed=2).endswith(S.INTERRUPTED))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
