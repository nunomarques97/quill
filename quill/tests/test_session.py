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

from quill import autorewrite as R
from quill import inject
from quill import session as S
from quill.focus import CLICKED, NO_WINDOW, FocusResult
from quill.indicator.render import (CLAUDE_DONE, CLAUDE_PERMISSION, ERROR, LISTENING, LOADING, REVIEWING, SENT,
                                    TRANSCRIBING)
from quill.inject import InjectResult, Target
from quill.session import Processed, SessionManager
from quill.tests.fakes import FakeCaptures, FakeClock, FakeIndicator, FakePlayer
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
        self.options = []  # the InjectOptions of each inject call
        self.typed_before_enter = []  # how many texts were typed when each Enter was pressed

    def inject(self, text, target, options=None):
        self.options.append(options)
        self.clock.now += 0.1
        reason = self.results.pop(0) if self.results else inject.OK
        if reason == inject.OK:
            self.typed.append((text, target))
            return InjectResult(reason, len(text), len(text))
        return InjectResult(reason, 3 if reason == inject.FOREGROUND_CHANGED else 0, len(text))

    def press_enter(self, target):
        self.enters.append(target)
        self.typed_before_enter.append(len(self.typed))
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
        return Processed(raw.strip().capitalize() + ".", "claude-code" if claude else "default", claude, self.notice,
                         keep=("Invented",), project="projeto", rewrite_profile="vscode" if claude else None,
                         newline=inject.NEWLINE_SHIFT_ENTER if claude else inject.NEWLINE_SPACE)


class FakeRewriter:
    """``quill.autorewrite.AutoRewriter`` stand-in: long means ``min_words`` words or ``min_audio_s``."""

    def __init__(self, min_words=6, min_audio_s=15.0):
        self.min_words = min_words
        self.min_audio_s = min_audio_s
        self.asked = []  # (text, audio_s) of each wants call
        self.calls = []  # keyword arguments of each rewrite call
        self.forced = []  # the force flag of each rewrite call
        self.reason = R.REWRITTEN
        self.reply = None  # the rewritten text; None: the input in upper case
        self.error = None
        self.gate = None  # a threading.Event the rewrite waits for
        self.started = threading.Event()

    def wants(self, text, audio_s):
        self.asked.append((text, audio_s))
        return len(text.split()) >= self.min_words or audio_s >= self.min_audio_s

    def rewrite(self, text, *, audio_s, profile="default", keep=(), project="", force=False):
        self.calls.append({"text": text, "audio_s": audio_s, "profile": profile, "keep": keep, "project": project})
        self.forced.append(force)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        if self.reason == R.REWRITTEN:
            return R.AutoRewrite(self.reply or text.upper(), text, R.REWRITTEN)
        return R.AutoRewrite(text, text, self.reason)


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.transcriber = FakeTranscriber()
        self.captures = FakeCaptures()
        self.focus = FakeFocus()
        self.injector = FakeInjector(self.clock)
        self.indicator = FakeIndicator()
        self.pipeline = FakePipeline()
        self.rewriter = self.make_rewriter()
        self.player = self.make_player()
        self.started = 0
        self.typed_hook = []
        self.undoable = []  # (original, newline) of each on_typed call
        self.manager = SessionManager(
            transcriber=self.transcriber, capture_factory=self.captures, focus=self.focus, injector=self.injector,
            indicator=self.indicator, pipeline=self.pipeline, rewriter=self.rewriter, on_session_start=self._started,
            on_typed=self._typed, player=self.player, clock=self.clock,
            final_timeout_s=5.0, poll_s=0.05)
        self.manager.start()
        self.addCleanup(self.manager.stop)

    def make_rewriter(self):
        return None

    def make_player(self):
        return None

    def _started(self):
        self.started += 1

    def _typed(self, target, text, original=None, newline=inject.NEWLINE_SPACE):
        self.typed_hook.append((target, text))
        self.undoable.append((original, newline))

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


LONG = "um dois três quatro cinco seis sete"  # 7 words: long for FakeRewriter
LONG_TYPED = "Um dois três quatro cinco seis sete."


def wait_until(condition, what):
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


class RewriteTest(SessionCase):
    """The automatic rewrite of long dictations, with a fake rewriter."""

    def setUp(self):
        super().setUp()
        self.ready()

    def make_rewriter(self):
        return FakeRewriter()

    def hold_long(self, partial="um dois"):
        """A long hold whose rewrite waits for ``self.rewriter.gate``; returns once the rewrite started."""
        self.rewriter.gate = threading.Event()
        self.press()
        self.captures.made[-1].push(PCM)
        self.transcriber.sessions[-1].partial(partial)
        self.release()
        self.transcriber.sessions[-1].handle.resolve(LONG)
        self.assertTrue(self.rewriter.started.wait(5))

    def test_short_dictation_never_asks_the_rewriter(self):
        self.dictate("frase curta")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.TYPED, None))
        self.assertEqual(self.rewriter.calls, [])
        self.assertEqual(self.rewriter.asked, [("Frase curta.", 0.1)])  # the audio length in seconds
        self.assertEqual(self.injector.typed, [("Frase curta.", TARGET)])
        self.assertNotIn(REVIEWING, self.indicator.states)
        self.assertEqual(self.undoable, [(None, inject.NEWLINE_SPACE)])
        self.assertAlmostEqual(outcome.release_to_typed_s, 0.1)  # the same path as without a rewriter
        self.assertEqual(self.indicator.last, ("hide",))

    def test_long_dictation_is_rewritten_while_the_indicator_says_so(self):
        self.hold_long()
        self.assertEqual(self.indicator.last, ("show", REVIEWING, "um dois"))
        self.assertEqual(self.injector.typed, [])
        self.rewriter.gate.set()
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.TYPED, R.REWRITTEN))
        self.assertEqual(self.injector.typed, [(LONG_TYPED.upper(), TARGET)])
        self.assertEqual(self.rewriter.calls, [{"text": LONG_TYPED, "audio_s": 0.1, "profile": "default",
                                                "keep": ("Invented",), "project": "projeto"}])
        self.assertEqual(self.typed_hook, [(TARGET, LONG_TYPED.upper())])
        self.assertEqual(self.undoable, [(LONG_TYPED, inject.NEWLINE_SPACE)])
        self.assertEqual(self.indicator.last, ("hide",))
        self.assert_released()

    def test_long_audio_alone_is_enough(self):
        self.press()
        for _ in range(160):  # 16 s of audio
            self.captures.made[0].push(PCM)
        self.release()
        self.transcriber.sessions[0].handle.resolve("frase curta")
        self.assertEqual(self.wait_outcomes(1)[0].rewrite, R.REWRITTEN)
        self.assertAlmostEqual(self.rewriter.calls[0]["audio_s"], 16.0)

    def test_failure_timeout_or_refusal_types_the_original_with_a_notice(self):
        for number, reason in enumerate((R.FAILED, R.TIMEOUT, R.REFUSED), start=1):
            self.rewriter.reason = reason
            self.dictate(LONG)
            outcome = self.wait_outcomes(number)[-1]
            self.assertEqual((outcome.reason, outcome.rewrite, outcome.notice), (reason, reason, reason))
            self.assertEqual(self.injector.typed[-1], (LONG_TYPED, TARGET))
            self.assertEqual(self.indicator.last, ("show", ERROR, R.MESSAGES[reason]))
            self.assertEqual(self.undoable[-1], (None, inject.NEWLINE_SPACE))
        self.assert_released()

    def test_unchanged_text_is_typed_without_a_notice(self):
        self.rewriter.reason = R.UNCHANGED
        self.dictate(LONG)
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.rewrite, outcome.notice), (S.TYPED, R.UNCHANGED, None))
        self.assertEqual(self.undoable, [(None, inject.NEWLINE_SPACE)])
        self.assertEqual(self.indicator.last, ("hide",))

    def test_a_raising_or_broken_rewriter_types_the_original(self):
        self.rewriter.error = RuntimeError("fake")
        with self.assertLogs("quill.session", level="ERROR"):
            self.dictate(LONG)
            outcome = self.wait_outcomes(1)[0]
        self.assertEqual(outcome.reason, R.FAILED)
        self.assertEqual(self.injector.typed, [(LONG_TYPED, TARGET)])
        self.rewriter.error = None
        self.rewriter.reply = "   "  # an empty rewrite is never typed
        with self.assertLogs("quill.session", level="ERROR"):
            self.dictate(LONG)
            self.assertEqual(self.wait_outcomes(2)[-1].reason, R.FAILED)
        self.assertEqual(self.injector.typed[-1], (LONG_TYPED, TARGET))
        self.rewriter.wants = lambda text, audio_s: 1 / 0
        with self.assertLogs("quill.session", level="ERROR"):
            self.dictate(LONG)
            self.assertEqual(self.wait_outcomes(3)[-1].reason, S.TYPED)
        self.assertEqual(self.injector.typed[-1], (LONG_TYPED, TARGET))
        self.assert_released()

    def test_typing_failure_after_a_rewrite_is_the_typing_error(self):
        self.injector.results = [inject.FOREGROUND_CHANGED]
        self.dictate(LONG)
        self.assertEqual(self.wait_outcomes(1)[0].reason, inject.FOREGROUND_CHANGED)
        self.assertEqual(self.typed_hook, [])
        self.assert_released()

    def test_a_newer_session_during_a_rewrite_keeps_its_words_and_the_order(self):
        self.focus.targets = [TARGET, OTHER]
        self.hold_long()
        self.assertEqual(self.indicator.last[:2], ("show", REVIEWING))
        self.press()  # records at once while the first one is being rewritten
        self.assertEqual(self.indicator.last, ("show", LISTENING, ""))
        self.captures.made[1].push(PCM)
        self.transcriber.sessions[1].partial("segunda")
        self.release()
        self.assertEqual(self.indicator.last, ("show", TRANSCRIBING, "segunda"))
        self.transcriber.sessions[1].handle.resolve("segunda frase")
        time.sleep(0.05)
        self.assertEqual(self.injector.typed, [])  # typed only after the older one
        self.rewriter.gate.set()
        outcomes = self.wait_outcomes(2)
        self.assertEqual([(o.session, o.reason) for o in outcomes], [(1, S.TYPED), (2, S.TYPED)])
        self.assertEqual(self.injector.typed, [(LONG_TYPED.upper(), TARGET), ("Segunda frase.", OTHER)])
        self.assertEqual(self.indicator.states.count(REVIEWING), 1)  # never over the newer session's words
        self.assertEqual(self.indicator.last, ("hide",))
        self.assert_released()

    def test_older_rewrite_is_shown_again_when_the_newer_session_ends_first(self):
        self.hold_long()
        self.manager.handle(signal(START))
        self.assertEqual(self.indicator.last, ("show", LISTENING, ""))
        self.manager.handle(signal(CANCEL, reason="short_hold"))  # the newer one ends first
        wait_until(lambda: self.indicator.last == ("show", REVIEWING, "um dois"), "the rewrite shown again")
        self.rewriter.gate.set()
        self.wait_outcomes(2)
        self.assertEqual(self.indicator.last, ("hide",))
        self.assert_released()

    def test_stop_during_a_rewrite_still_types_once_and_releases(self):
        self.hold_long()
        stopper = threading.Thread(target=self.manager.stop)
        stopper.start()
        self.rewriter.gate.set()
        stopper.join(5)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(len(self.injector.typed), 1)
        self.assertEqual(self.manager.outcomes[-1].rewrite, R.REWRITTEN)
        self.assert_released()


class JobTest(SessionCase):
    """Work queued on the finalizer thread (the undo key) and indicator notices."""

    def setUp(self):
        super().setUp()
        self.ready()

    def test_a_job_runs_after_the_sessions_released_before_it(self):
        order = []
        self.press()
        self.captures.made[0].push(PCM)
        self.release()
        self.assertTrue(self.manager.submit(lambda: order.append(("job", len(self.injector.typed)))))
        time.sleep(0.05)
        self.assertEqual(order, [])  # waits for the session
        self.transcriber.sessions[0].handle.resolve()
        wait_until(lambda: order, "the job")
        self.assertEqual(order, [("job", 1)])

    def test_a_failing_job_is_logged_and_the_next_session_works(self):
        with self.assertLogs("quill.session", level="ERROR"):
            self.manager.submit(lambda: 1 / 0)
            self.dictate()
            self.wait_outcomes(1)
        self.assertEqual(self.manager.outcomes[0].reason, S.TYPED)

    def test_no_job_after_stop(self):
        self.manager.stop()
        self.assertFalse(self.manager.submit(lambda: None))

    def test_notify_never_covers_a_live_session(self):
        self.assertTrue(self.manager.notify(ERROR, "aviso"))
        self.assertEqual(self.indicator.last, ("show", ERROR, "aviso"))
        self.assertTrue(self.manager.notify(None))
        self.assertEqual(self.indicator.last, ("hide",))
        self.press()
        self.assertFalse(self.manager.notify(ERROR, "aviso"))
        self.assertEqual(self.indicator.last, ("show", LISTENING, ""))
        self.release()
        self.assertFalse(self.manager.notify(None))  # still finalizing
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.assertTrue(self.manager.notify(ERROR, "aviso"))


class RewriteClaudeTest(SessionCase):
    def setUp(self):
        super().setUp()
        self.ready()
        self.focus.targets = [CLAUDE]

    def make_rewriter(self):
        return FakeRewriter()

    def test_send_claude_types_the_lines_with_shift_enter_then_one_enter(self):
        self.rewriter.reply = "Um dois três.\nQuatro cinco seis sete."
        self.dictate(LONG, action="send_claude")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.SENT_ENTER, R.REWRITTEN))
        self.assertEqual(self.injector.typed, [("Um dois três.\nQuatro cinco seis sete.", CLAUDE)])
        self.assertEqual(self.injector.options[-1].newline, inject.NEWLINE_SHIFT_ENTER)
        self.assertEqual(self.injector.enters, [CLAUDE])  # once, after the whole text
        self.assertEqual(self.rewriter.calls[0]["profile"], "vscode")  # the pipeline's rewrite profile
        self.assertEqual(self.undoable, [(None, inject.NEWLINE_SHIFT_ENTER)])  # after Enter: nothing to undo
        self.assertEqual(self.indicator.last, ("show", SENT, ""))

    def test_dictation_into_claude_code_keeps_the_lines_and_may_be_undone(self):
        self.rewriter.reply = "Um dois três.\nQuatro cinco seis sete."
        self.dictate(LONG)
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.TYPED)
        self.assertEqual(self.injector.enters, [])
        self.assertEqual(self.typed_hook, [(CLAUDE, "Um dois três.\nQuatro cinco seis sete.")])
        self.assertEqual(self.undoable, [(LONG_TYPED, inject.NEWLINE_SHIFT_ENTER)])

    def test_send_claude_with_a_failed_rewrite_sends_the_original_and_says_so(self):
        self.rewriter.reason = R.TIMEOUT
        self.dictate(LONG, action="send_claude")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.notice), (S.SENT_ENTER, R.TIMEOUT))
        self.assertEqual(self.injector.typed, [(LONG_TYPED, CLAUDE)])
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(self.indicator.last, ("show", SENT, R.MESSAGES[R.TIMEOUT]))

    def test_no_enter_when_the_rewrite_is_not_typed(self):
        self.injector.results = [inject.TARGET_GONE]
        self.dictate(LONG, action="send_claude")
        self.assertEqual(self.wait_outcomes(1)[0].reason, inject.TARGET_GONE)
        self.assertEqual(self.injector.enters, [])

class SendPolishedRawTest(SessionCase):
    """Mouse 5 (send_polished: always rewritten) and middle click (send_raw: never), Enter in Claude Code only."""

    def setUp(self):
        super().setUp()
        self.ready()
        self.focus.targets = [CLAUDE]

    def make_rewriter(self):
        return FakeRewriter()

    def test_send_polished_rewrites_a_short_dictation_then_presses_one_enter(self):
        self.rewriter.gate = threading.Event()
        self.press("send_polished")
        self.captures.made[-1].push(PCM)
        self.transcriber.sessions[-1].partial("frase")
        self.release("send_polished")
        self.transcriber.sessions[-1].handle.resolve("frase curta")
        self.assertTrue(self.rewriter.started.wait(5))
        self.assertEqual(self.indicator.last, ("show", REVIEWING, "frase"))
        self.assertEqual((self.injector.typed, self.injector.enters), ([], []))
        self.rewriter.gate.set()
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.action, outcome.reason, outcome.rewrite), ("send_polished", S.SENT_ENTER,
                                                                             R.REWRITTEN))
        self.assertEqual(self.rewriter.asked, [])  # never asked whether it is long: always rewritten
        self.assertEqual(self.rewriter.forced, [True])
        self.assertEqual(self.rewriter.calls[0]["text"], "Frase curta.")
        self.assertEqual(self.injector.typed, [("FRASE CURTA.", CLAUDE)])
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(self.injector.typed_before_enter, [1])  # after the whole text
        self.assertEqual(self.undoable, [(None, inject.NEWLINE_SHIFT_ENTER)])  # followed by Enter: never undone
        self.assertEqual(self.indicator.last, ("show", SENT, ""))
        self.assertEqual(self.focus.calls, [("send_polished", "xbutton1")])  # clicked to focus like send_claude
        self.assert_released()

    def test_send_polished_types_the_original_with_the_notice_on_refusal_failure_or_timeout(self):
        cases = ((R.REFUSED, None), (R.FAILED, None), (R.TIMEOUT, None), (R.FAILED, RuntimeError("fake")))
        for number, (reason, error) in enumerate(cases, start=1):
            with self.subTest(reason=reason, error=error):
                self.rewriter.reason, self.rewriter.error = reason, error
                self.dictate("frase curta", action="send_polished")
                outcome = self.wait_outcomes(number)[-1]
                self.assertEqual((outcome.reason, outcome.rewrite, outcome.notice), (S.SENT_ENTER, reason, reason))
                self.assertEqual(self.injector.typed[-1], ("Frase curta.", CLAUDE))
                self.assertEqual(len(self.injector.enters), number)
                self.assertEqual(self.undoable[-1], (None, inject.NEWLINE_SHIFT_ENTER))
                self.assertEqual(self.indicator.last, ("show", SENT, R.MESSAGES[reason]))
        self.assertIn(REVIEWING, self.indicator.states)
        self.assertEqual(self.injector.typed_before_enter, [1, 2, 3, 4])

    def test_send_polished_outside_claude_code_types_the_rewrite_without_enter(self):
        self.focus.targets = [TARGET]
        self.dictate("frase curta", action="send_polished")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.NOT_CLAUDE, R.REWRITTEN))
        self.assertEqual(self.injector.typed, [("FRASE CURTA.", TARGET)])
        self.assertEqual(self.injector.enters, [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.NOT_CLAUDE]))

    def test_send_raw_never_calls_the_rewriter_even_when_long(self):
        self.press("send_raw")
        for _ in range(160):  # 16 s of audio: over min_audio_s, and LONG is over min_words
            self.captures.made[-1].push(PCM)
        self.release("send_raw")
        self.transcriber.sessions[-1].handle.resolve(LONG)
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.action, outcome.reason, outcome.rewrite), ("send_raw", S.SENT_ENTER, None))
        self.assertEqual((self.rewriter.asked, self.rewriter.calls), ([], []))
        self.assertNotIn(REVIEWING, self.indicator.states)
        self.assertEqual(self.injector.typed, [(LONG_TYPED, CLAUDE)])
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(self.injector.typed_before_enter, [1])
        self.assertEqual(self.focus.calls, [("send_raw", "xbutton1")])

    def test_send_raw_outside_claude_code_types_without_enter_and_says_so(self):
        self.focus.targets = [TARGET]
        self.dictate(LONG, action="send_raw")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual(outcome.reason, S.NOT_CLAUDE)
        self.assertEqual(self.injector.typed, [(LONG_TYPED, TARGET)])
        self.assertEqual((self.injector.enters, self.rewriter.calls), ([], []))
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.NOT_CLAUDE]))

    def test_dictation_is_unchanged_no_enter_and_only_long_texts_are_rewritten(self):
        self.dictate("frase curta")
        self.dictate(LONG)
        outcomes = self.wait_outcomes(2)
        self.assertEqual([(o.reason, o.rewrite) for o in outcomes], [(S.TYPED, None), (S.TYPED, R.REWRITTEN)])
        self.assertEqual(self.rewriter.forced, [False])
        self.assertEqual(self.injector.enters, [])
        self.assertEqual(self.undoable[-1], (LONG_TYPED, inject.NEWLINE_SHIFT_ENTER))  # no Enter: may be undone

    def test_no_enter_when_the_text_is_not_typed(self):
        self.injector.results = [inject.FOREGROUND_CHANGED, inject.FOREGROUND_CHANGED]
        for action in ("send_polished", "send_raw"):
            self.dictate("frase curta", action=action)
        self.assertEqual([o.reason for o in self.wait_outcomes(2)], [inject.FOREGROUND_CHANGED] * 2)
        self.assertEqual(self.injector.enters, [])


class AlertTest(SessionCase):
    """Claude Code alerts: shown and rung only when no session needs the screen or the microphone."""

    def make_player(self):
        return FakePlayer(recording=lambda: bool(self.captures.open))

    def setUp(self):
        super().setUp()
        self.ready()

    def wait_shown(self, state, count=1):
        deadline = time.monotonic() + 5
        while self.indicator.states.count(state) < count:
            if time.monotonic() > deadline:
                self.fail(f"{state} not shown {count} time(s): {self.indicator.states}")
            time.sleep(0.005)

    def test_an_alert_while_idle_shows_and_rings_at_once(self):
        with self.assertLogs("quill.session", level="INFO") as logs:
            self.assertTrue(self.manager.alert(S.sound.DONE))
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, ""))
        self.assertEqual(self.player.plays, [S.sound.DONE])
        self.assertTrue(self.manager.alert(S.sound.PERMISSION) and self.player.plays == [S.sound.DONE])
        self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION, ""))
        self.assertNotIn("texto", "\n".join(logs.output))

    def test_an_alert_during_a_recording_waits_until_the_session_ends(self):
        self.press()
        self.captures.made[0].push(PCM)
        self.assertTrue(self.manager.alert(S.sound.DONE))
        self.assertEqual((self.player.plays, self.indicator.last), ([], ("show", LISTENING, "")))
        self.release()
        # Still finalizing: the transcript is not typed yet, so the alert keeps waiting.
        time.sleep(0.15)
        self.assertEqual(self.player.plays, [])
        self.assertNotIn(CLAUDE_DONE, self.indicator.states)
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.wait_shown(CLAUDE_DONE)
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, ""))
        self.assertEqual(self.player.plays, [S.sound.DONE])
        self.assertEqual(self.player.plays_while_recording, 0)
        self.assertEqual(self.injector.typed, [("Texto inventado de teste.", TARGET)])

    def test_alerts_kept_together_show_once_and_ring_once(self):
        self.press()
        for kind in (S.sound.DONE, S.sound.DONE, S.sound.PERMISSION, S.sound.DONE, S.sound.PERMISSION):
            self.manager.alert(kind)
        self.release()
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.wait_shown(CLAUDE_PERMISSION)
        time.sleep(0.15)  # several finalizer polls: nothing more is shown
        shown = [call for call in self.indicator.calls if call[0] == "show" and call[1] in S.ALERT_STATES.values()]
        self.assertEqual(shown, [("show", CLAUDE_PERMISSION, S.ALSO_DONE)])
        self.assertEqual(self.player.plays, [S.sound.PERMISSION])

    def test_a_press_stops_the_sound_before_the_microphone_opens(self):
        self.manager.alert(S.sound.DONE)
        self.assertTrue(self.player.playing)
        playing_at_start = []
        self.captures.on_start = lambda capture: playing_at_start.append(self.player.playing)
        self.press()
        self.assertEqual(playing_at_start, [False])
        self.assertGreaterEqual(self.player.stops, 1)

    def test_alerts_close_together_ring_once(self):
        self.manager.alert(S.sound.DONE)
        self.clock.now += S.ALERT_REPEAT_S - 1
        self.manager.alert(S.sound.DONE)
        self.manager.alert(S.sound.PERMISSION)
        self.assertEqual(self.player.plays, [S.sound.DONE])
        self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION, ""))
        self.clock.now += 1
        self.manager.alert(S.sound.PERMISSION)
        self.assertEqual(self.player.plays, [S.sound.DONE, S.sound.PERMISSION])

    def test_an_outcome_on_screen_is_seen_before_the_alert(self):
        self.press("command", "f14")  # command mode unavailable: an error shown for a few seconds
        self.release("command", "f14")
        self.wait_outcomes(1)
        self.manager.alert(S.sound.DONE)
        time.sleep(0.15)
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.COMMAND_UNAVAILABLE]))
        self.assertEqual(self.player.plays, [])
        self.clock.now += S.ERROR_SHOW_S
        self.wait_shown(CLAUDE_DONE)
        self.assertEqual(self.player.plays, [S.sound.DONE])

    def test_stop_drops_waiting_alerts_and_start_again_works(self):
        self.press()
        self.manager.alert(S.sound.DONE)
        self.manager.stop()
        self.assertEqual(self.player.plays, [])
        self.assertFalse(self.manager.alert(S.sound.DONE))
        self.assertEqual(self.player.plays, [])
        self.manager.start()
        self.manager.alert(S.sound.PERMISSION)  # still loading: waits
        self.assertEqual(self.player.plays, [])
        self.manager.loaded()
        self.assertEqual(self.player.plays, [S.sound.PERMISSION])
        self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION, ""))

    def test_a_broken_player_still_shows_the_alert(self):
        self.player.fail = True
        with self.assertLogs("quill.session", level="ERROR"):
            self.manager.alert(S.sound.DONE)
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, ""))

    def test_unknown_kinds_are_refused(self):
        with self.assertRaises(ValueError):
            self.manager.alert("stop")

    def test_alerts_from_another_thread_never_ring_into_a_recording(self):
        done = threading.Event()

        def alerts():
            while not done.is_set():
                self.manager.alert(S.sound.DONE)
                self.clock.now += S.ALERT_REPEAT_S
                time.sleep(0.001)
        thread = threading.Thread(target=alerts)
        thread.start()
        try:
            for number in range(20):
                self.dictate(f"frase {number}")
                self.wait_outcomes(number + 1)
        finally:
            done.set()
            thread.join()
        self.assertGreater(len(self.player.plays), 0)
        self.assertEqual(self.player.plays_while_recording, 0)
        self.assertEqual(len(self.injector.typed), 20)


class SilentAlertTest(SessionCase):
    def test_without_a_player_the_alert_is_only_shown(self):
        self.manager.alert(S.sound.DONE)
        self.assertEqual(self.indicator.states, [])  # still loading
        self.ready()
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, ""))


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
