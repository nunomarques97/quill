"""quill.session tests: ordered dictation sessions with fake parts.

The transcriber, capture, click-to-focus, injector, pipeline and indicator
are fakes; no microphone, window, hook or input is touched. The texts are
invented.
"""

import dataclasses
import json
import logging
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from quill import autorewrite as R
from quill import command as C
from quill import enrich as E
from quill import inject
from quill import session as S
from quill.focus import CLICKED, NO_WINDOW, FocusResult
from quill.indicator.render import (CLAUDE_DONE, CLAUDE_PERMISSION, ERROR, LISTENING, LOADING, REVIEWING, SENT,
                                    TRANSCRIBING, VOICE, VOICE_NONE, VOICE_OPEN)
from quill.inject import InjectResult, Target
from quill.session import Processed, SessionManager
from quill import sound
from quill import speech as SP
from quill.tests.fakes import FakeCaptures, FakeClock, FakeIndicator, FakePlayer, FakeSpeechEngine
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
        self.folder = None  # the project folder found for the Claude Code window
        self.terminal = False  # Claude Code in a terminal: one paragraph, spaces for line breaks

    def __call__(self, raw, target):
        self.calls += 1
        if self.error is not None:
            raise self.error
        claude = target == CLAUDE
        newline = inject.NEWLINE_SHIFT_ENTER if claude and not self.terminal else inject.NEWLINE_SPACE
        return Processed(raw.strip().capitalize() + ".", "claude-code" if claude else "default", claude, self.notice,
                         keep=("Invented",), project="projeto", project_folder=self.folder if claude else None,
                         rewrite_profile="vscode" if claude else None, newline=newline)


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
        self.polished = []  # the extra keyword arguments of each call (mouse 5 into Claude Code)
        self.enrichment = ""  # the enrichment's reason code when it is asked ("": none)
        self.enriched = None  # the enriched prompt; None: the corrected text with a label
        self.enrich_gate = None  # a threading.Event the enrichment waits for
        self.enriching = threading.Event()

    def wants(self, text, audio_s):
        self.asked.append((text, audio_s))
        return len(text.split()) >= self.min_words or audio_s >= self.min_audio_s

    def rewrite(self, text, *, audio_s, profile="default", keep=(), project="", force=False, **polish):
        self.calls.append({"text": text, "audio_s": audio_s, "profile": profile, "keep": keep, "project": project})
        self.forced.append(force)
        self.polished.append(polish)
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        if self.reason == R.REWRITTEN:
            result = R.AutoRewrite(self.reply or text.upper(), text, R.REWRITTEN)
        else:
            result = R.AutoRewrite(text, text, self.reason)
        if not (polish.get("enrich_prompt") and self.enrichment) or self.reason not in (R.REWRITTEN, R.UNCHANGED):
            return result
        polish["on_enrich"]()
        self.enriching.set()
        if self.enrich_gate is not None:
            self.enrich_gate.wait(5)
        typed = result.text
        if self.enrichment == E.ENRICHED:
            typed = self.enriched or f"Pedido: {result.text}"
        return dataclasses.replace(result, text=typed, enrichment=self.enrichment)


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
        self.speaker = self.make_speaker()
        self.voice = self.make_voice()
        self.started = 0
        self.started_actions = []  # the action passed to each on_session_start call
        self.scheduled = []  # (seconds, job) of each indicator restore after the "Aguarde" notice
        self.typed_hook = []
        self.undoable = []  # (original, newline) of each on_typed call
        self.manager = SessionManager(
            transcriber=self.transcriber, capture_factory=self.captures, focus=self.focus, injector=self.injector,
            indicator=self.indicator, pipeline=self.pipeline, rewriter=self.rewriter, voice=self.voice,
            command=self.make_command(), on_session_start=self._started,
            on_typed=self._typed, player=self.player, speaker=self.speaker, context_pack=self.make_context_pack(),
            schedule=self._schedule, clock=self.clock, final_timeout_s=5.0, poll_s=0.05)
        self.manager.start()
        self.addCleanup(self.manager.stop)

    def make_rewriter(self):
        return None

    def make_player(self):
        return None

    def make_speaker(self):
        return None

    def make_voice(self):
        return None

    def make_command(self):
        return None

    def make_context_pack(self):
        return None

    def _started(self, action):
        self.started += 1
        self.started_actions.append(action)

    def _schedule(self, seconds, job):
        self.scheduled.append((seconds, job))

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

    def settle(self):
        """Wait until no released session is pending, so the next press starts a session."""
        deadline = time.monotonic() + 5
        while any(hold.handle is not None for hold in list(self.manager._live)):
            if time.monotonic() > deadline:
                self.fail("a released session is still pending")
            time.sleep(0.005)

    def dictate(self, text=SPOKEN, action="dictation", error=None):
        """One full hold after the earlier ones ended: press, audio, release, final result."""
        self.settle()
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

    def test_new_press_during_finalization_is_ignored_and_the_pending_session_is_unaffected(self):
        self.focus.targets = [TARGET, OTHER]
        self.press()
        self.captures.made[0].push(PCM)
        self.transcriber.sessions[0].partial("primeira")
        self.release()
        first = self.transcriber.sessions[0]
        # The first session is still being finalized: the next press starts nothing.
        with self.assertLogs("quill.session", level="INFO") as logs:
            self.press()
        self.assertEqual(len(self.captures.made), 1)
        self.assertEqual(len(self.transcriber.sessions), 1)
        self.assertEqual(self.focus.calls, [("dictation", "xbutton1")])  # no second click
        self.assertEqual(self.started, 1)
        self.assertEqual(self.indicator.last, ("show", TRANSCRIBING, S.MESSAGES[S.PREVIOUS_PENDING]))
        self.assertEqual([(o.session, o.reason) for o in self.manager.outcomes], [(2, S.PREVIOUS_PENDING)])
        self.assertIn("pressed while session 1 is still pending; ignored (previous_pending)", "\n".join(logs.output))
        self.release()  # its release does nothing
        self.assertEqual(len(self.manager.outcomes), 1)
        # After the notice the pending session's words come back.
        [(seconds, restore)] = self.scheduled
        self.assertEqual(seconds, S.BUSY_SHOW_S)
        restore()
        self.assertEqual(self.indicator.last, ("show", TRANSCRIBING, "primeira"))
        first.handle.resolve("primeira frase")
        outcomes = self.wait_outcomes(2)
        self.assertEqual([(o.session, o.reason) for o in outcomes], [(2, S.PREVIOUS_PENDING), (1, S.TYPED)])
        self.assertEqual(self.injector.typed, [("Primeira frase.", TARGET)])
        self.assertEqual(self.indicator.last, ("hide",))
        # The first press after it works normally.
        self.dictate("segunda frase")
        self.assertEqual(self.wait_outcomes(3)[-1].reason, S.TYPED)
        self.assertEqual(self.injector.typed[-1], ("Segunda frase.", OTHER))
        self.assert_released()

    def test_a_session_pending_past_the_limit_no_longer_blocks_and_ends_without_hiding_the_live_words(self):
        self.press()
        self.release()
        self.clock.now += S.PENDING_LIMIT_S  # the finalization hangs
        with self.assertLogs("quill.session", level="WARNING") as logs:
            self.press()
        self.assertIn("session 1 still pending after 120 s", "\n".join(logs.output))
        self.assertTrue(self.captures.made[1].started)
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


class EnterCheckTest(SessionCase):
    """The target is described again just before a send trigger's Enter."""

    def setUp(self):
        super().setUp()
        self.checks = []  # (target, window) of each check
        self.problem = None
        self.check_error = None
        self.manager.enter_check = self._check
        self.ready()

    def _check(self, target, window):
        self.checks.append((target, window))
        if self.check_error is not None:
            raise self.check_error
        return self.problem

    def test_enter_when_the_target_is_still_claude_code(self):
        self.focus.targets = [CLAUDE]
        self.dictate(action="send_polished")
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.SENT_ENTER)
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(len(self.checks), 1)
        self.assertEqual(self.checks[0][0], CLAUDE)

    def test_no_enter_when_the_check_refuses_on_every_send_trigger(self):
        self.problem = "not_claude_code"
        for number, action in enumerate(("send_polished", "send_claude", "send_raw"), 1):
            self.focus.targets = [CLAUDE]
            self.dictate(action=action)
            outcome = self.wait_outcomes(number)[-1]
            self.assertEqual(outcome.reason, S.ENTER_WITHHELD, action)
            self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.ENTER_WITHHELD]))
        self.assertEqual(len(self.injector.typed), 3)
        self.assertEqual(self.injector.enters, [])

    def test_a_failing_check_never_gives_an_enter(self):
        self.check_error = OSError("fake")
        self.focus.targets = [CLAUDE]
        self.dictate(action="send_claude")
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.ENTER_WITHHELD)
        self.assertEqual(self.injector.enters, [])
        self.assertEqual(len(self.injector.typed), 1)

    def test_no_check_outside_claude_code_or_without_a_send_trigger(self):
        self.dictate(action="send_polished")  # another window: typed without Enter, nothing to check
        self.focus.targets = [CLAUDE]
        self.dictate()  # dictation into Claude Code: never an Enter
        outcomes = self.wait_outcomes(2)
        self.assertEqual([o.reason for o in outcomes], [S.NOT_CLAUDE, S.TYPED])
        self.assertEqual(self.checks, [])
        self.assertEqual(self.injector.enters, [])

    def test_no_check_when_typing_failed(self):
        self.focus.targets = [CLAUDE]
        self.injector.results = [inject.FOREGROUND_CHANGED]
        self.dictate(action="send_claude")
        self.assertEqual(self.wait_outcomes(1)[0].reason, inject.FOREGROUND_CHANGED)
        self.assertEqual((self.checks, self.injector.enters), ([], []))


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

    def test_a_press_during_a_rewrite_is_ignored_and_the_rewrite_is_typed_once(self):
        self.focus.targets = [TARGET, OTHER]
        self.hold_long()
        self.assertEqual(self.indicator.last, ("show", REVIEWING, "um dois"))
        self.press()  # nothing starts while the first one is being rewritten
        self.assertEqual(len(self.captures.made), 1)
        self.assertEqual(len(self.focus.calls), 1)
        self.assertEqual(self.indicator.last, ("show", REVIEWING, S.MESSAGES[S.PREVIOUS_PENDING]))
        self.release()
        self.scheduled[-1][1]()
        self.assertEqual(self.indicator.last, ("show", REVIEWING, "um dois"))
        self.rewriter.gate.set()
        outcomes = self.wait_outcomes(2)
        self.assertEqual([(o.session, o.reason) for o in outcomes], [(2, S.PREVIOUS_PENDING), (1, S.TYPED)])
        self.assertEqual(self.injector.typed, [(LONG_TYPED.upper(), TARGET)])
        self.assertEqual(len(self.rewriter.calls), 1)
        self.assertEqual(self.indicator.last, ("hide",))
        self.assert_released()

    def test_the_notice_never_comes_back_over_a_newer_state(self):
        self.hold_long()
        self.manager.handle(signal(START))
        self.assertEqual(self.indicator.last, ("show", REVIEWING, S.MESSAGES[S.PREVIOUS_PENDING]))
        self.manager.handle(signal(CANCEL, reason="short_hold"))  # the ignored press: nothing to cancel
        self.assertEqual([o.reason for o in self.manager.outcomes], [S.PREVIOUS_PENDING])
        self.rewriter.gate.set()
        self.wait_outcomes(2)
        self.assertEqual(self.indicator.last, ("hide",))
        self.scheduled[-1][1]()  # too late: the session ended
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


class FakeContextPacks:
    """``ContextPacks.get`` stand-in: records the folders asked for; a pack, None or an error."""

    def __init__(self):
        self.folders = []
        self.pack = SimpleNamespace(summary="Projeto inventado.", terms=("carteira",))
        self.error = None

    def __call__(self, folder):
        self.folders.append(folder)
        if self.error is not None:
            raise self.error
        return self.pack


class PolishTest(SessionCase):
    """Mouse 5 into Claude Code: the project's context pack, the correction, then the enrichment."""

    FOLDER = Path("C:/Invented/projeto")
    ENRICHED = "Objetivo: frase curta.\nPedido:\n- um\n- dois"

    def setUp(self):
        super().setUp()
        self.ready()
        self.focus.targets = [CLAUDE]
        self.pipeline.folder = self.FOLDER
        self.rewriter.enrichment = E.ENRICHED
        self.rewriter.enriched = self.ENRICHED

    def make_rewriter(self):
        return FakeRewriter()

    def make_context_pack(self):
        self.packs = FakeContextPacks()
        return self.packs

    def test_the_panel_gets_the_pack_and_the_enriched_lines_with_shift_enter_then_one_enter(self):
        self.rewriter.enrich_gate = threading.Event()
        self.dictate("frase curta", action="send_polished")
        self.assertTrue(self.rewriter.enriching.wait(5))
        self.assertEqual(self.indicator.last, ("show", REVIEWING, S.ENRICHING))
        self.assertEqual(self.injector.typed, [])
        self.rewriter.enrich_gate.set()
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.rewrite, outcome.enrichment, outcome.notice),
                         (S.SENT_ENTER, R.REWRITTEN, E.ENRICHED, E.ENRICHED))
        self.assertEqual(self.packs.folders, [self.FOLDER])
        polish = self.rewriter.polished[0]
        self.assertIs(polish["pack"], self.packs.pack)
        self.assertEqual((polish["enrich_prompt"], polish["context"]), (True, True))
        self.assertEqual(self.rewriter.calls[0]["project"], "projeto")
        self.assertEqual(self.injector.typed, [(self.ENRICHED, CLAUDE)])  # lines kept: Shift+Enter
        self.assertEqual(self.injector.options[-1].newline, inject.NEWLINE_SHIFT_ENTER)
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(self.injector.typed_before_enter, [1])  # one plain Enter after the whole text
        self.assertEqual(self.undoable, [(None, inject.NEWLINE_SHIFT_ENTER)])  # followed by Enter: never undone
        self.assertEqual(self.indicator.last, ("show", SENT, "Prompt enriquecido e enviado"))
        self.assert_released()

    def test_the_terminal_gets_one_paragraph_with_the_labels(self):
        self.pipeline.terminal = True
        self.dictate("frase curta", action="send_polished")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, E.ENRICHED))
        self.assertTrue(self.rewriter.polished[0]["context"])  # context mode despite the vscode layout profile
        self.assertEqual(self.injector.typed, [("Objetivo: frase curta. Pedido: um; dois", CLAUDE)])
        self.assertEqual(self.injector.options[-1].newline, inject.NEWLINE_SPACE)
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(self.injector.typed_before_enter, [1])

    def test_without_a_project_folder_it_enriches_without_a_pack(self):
        self.pipeline.folder = None
        self.dictate("frase curta", action="send_polished")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, E.ENRICHED))
        self.assertEqual(self.packs.folders, [])
        self.assertIsNone(self.rewriter.polished[0]["pack"])
        self.assertTrue(self.rewriter.polished[0]["enrich_prompt"])
        self.assertEqual(self.injector.typed, [(self.ENRICHED, CLAUDE)])

    def test_a_missing_or_failing_pack_never_loses_the_dictation(self):
        for number, error in enumerate((None, OSError("fake"), TimeoutError()), start=1):
            with self.subTest(error=error):
                self.packs.pack, self.packs.error = None, error
                self.dictate("frase curta", action="send_polished")
                outcome = self.wait_outcomes(number)[-1]
                self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, E.ENRICHED))
                self.assertIsNone(self.rewriter.polished[-1]["pack"])
                self.assertEqual(self.injector.typed[-1], (self.ENRICHED, CLAUDE))
        self.assertEqual(len(self.packs.folders), 3)

    def test_a_refused_failed_or_slow_enrichment_sends_the_corrected_text_and_says_so(self):
        for number, reason in enumerate((E.REFUSED, E.FAILED, E.TIMEOUT), start=1):
            with self.subTest(reason=reason):
                self.rewriter.enrichment = reason
                self.dictate("frase curta", action="send_polished")
                outcome = self.wait_outcomes(number)[-1]
                self.assertEqual((outcome.reason, outcome.rewrite, outcome.enrichment, outcome.notice),
                                 (S.SENT_ENTER, R.REWRITTEN, reason, reason))
                self.assertEqual(self.injector.typed[-1], ("FRASE CURTA.", CLAUDE))  # the corrected text
                self.assertEqual(self.indicator.last, ("show", SENT, E.MESSAGES[reason]))
                self.assertEqual(self.undoable[-1], (None, inject.NEWLINE_SHIFT_ENTER))
        self.assertEqual(self.injector.typed_before_enter, [1, 2, 3])

    def test_a_short_dictation_not_enriched_is_sent_as_today(self):
        self.rewriter.enrichment = E.SHORT
        self.dictate("frase curta", action="send_polished")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.enrichment, outcome.notice), (S.SENT_ENTER, E.SHORT, None))
        self.assertEqual(self.injector.typed, [("FRASE CURTA.", CLAUDE)])
        self.assertEqual(self.indicator.last, ("show", SENT, ""))

    def test_a_refused_or_failed_correction_types_the_dictation_without_enrichment(self):
        for number, (reason, error) in enumerate(((R.REFUSED, None), (R.FAILED, RuntimeError("fake"))), start=1):
            with self.subTest(reason=reason):
                self.rewriter.reason, self.rewriter.error = reason, error
                self.dictate("frase curta", action="send_polished")
                outcome = self.wait_outcomes(number)[-1]
                self.assertEqual((outcome.reason, outcome.enrichment, outcome.notice), (S.SENT_ENTER, None, reason))
                self.assertEqual(self.injector.typed[-1], ("Frase curta.", CLAUDE))  # the dictation
                self.assertEqual(self.indicator.last, ("show", SENT, R.MESSAGES[reason]))
        self.assertNotIn(("show", REVIEWING, S.ENRICHING), self.indicator.calls)

    def test_an_unchanged_correction_is_still_enriched(self):
        self.rewriter.reason, self.rewriter.enriched = R.UNCHANGED, None
        self.dictate("frase curta", action="send_polished")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.rewrite, outcome.enrichment), (R.UNCHANGED, E.ENRICHED))
        self.assertEqual(self.injector.typed, [("Pedido: Frase curta.", CLAUDE)])

    def test_a_second_mouse_5_press_while_enriching_is_ignored_and_the_first_is_sent_once(self):
        # The logged case: a second mouse 5 press 1 s after the first, while it is being rewritten.
        self.rewriter.enrich_gate = threading.Event()
        self.dictate("frase curta", action="send_polished")
        self.assertTrue(self.rewriter.enriching.wait(5))
        self.clock.now += 1.0
        self.press("send_polished", "xbutton2")
        self.assertEqual(self.focus.calls, [("send_polished", "xbutton1")])  # the first press's click only
        self.assertEqual(len(self.captures.made), 1)
        self.assertEqual(self.indicator.last, ("show", REVIEWING, S.MESSAGES[S.PREVIOUS_PENDING]))
        self.scheduled[-1][1]()
        self.assertEqual(self.indicator.last, ("show", REVIEWING, S.ENRICHING))
        self.release("send_polished", "xbutton2")
        self.rewriter.enrich_gate.set()
        outcomes = self.wait_outcomes(2)
        self.assertEqual([o.reason for o in outcomes], [S.PREVIOUS_PENDING, S.SENT_ENTER])
        self.assertEqual(outcomes[-1].enrichment, E.ENRICHED)
        self.assertEqual(self.injector.typed, [(self.ENRICHED, CLAUDE)])
        self.assertEqual(self.injector.enters, [CLAUDE])  # one Enter, after the whole text
        self.assertEqual(len(self.rewriter.calls), 1)
        self.assertEqual(self.indicator.last, ("show", SENT, "Prompt enriquecido e enviado"))

    def test_other_triggers_and_windows_keep_todays_rewrite(self):
        self.dictate(LONG)  # mouse 4, long: the automatic rewrite
        self.dictate(LONG, action="send_claude")
        self.dictate("frase curta", action="send_raw")
        self.focus.targets = [TARGET]
        self.dictate("frase curta", action="send_polished")  # not Claude Code
        outcomes = self.wait_outcomes(4)
        self.assertEqual([o.enrichment for o in outcomes], [None, None, None, None])
        self.assertEqual(self.rewriter.polished, [{}, {}, {}])  # no pack, no enrichment; send_raw never asks
        self.assertEqual(self.rewriter.forced, [False, False, True])
        self.assertEqual(self.packs.folders, [])
        self.assertNotIn(("show", REVIEWING, S.ENRICHING), self.indicator.calls)
        self.assertEqual(self.undoable[0], (LONG_TYPED, inject.NEWLINE_SHIFT_ENTER))  # mouse 4: may be undone
        self.assertEqual(self.undoable[-1], ("Frase curta.", inject.NEWLINE_SPACE))  # no Enter: the dictation


class PolishWithoutPacksTest(SessionCase):
    """No context packs configured (``context_pack`` None): mouse 5 into Claude Code enriches without a pack."""

    def make_rewriter(self):
        return FakeRewriter()

    def test_enriches_without_a_pack(self):
        self.ready()
        self.focus.targets = [CLAUDE]
        self.pipeline.folder = Path("C:/Invented/projeto")
        self.rewriter.enrichment = E.ENRICHED
        self.dictate("frase curta", action="send_polished")
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, E.ENRICHED))
        self.assertIsNone(self.rewriter.polished[0]["pack"])
        self.assertEqual(self.injector.typed, [("Pedido: FRASE CURTA.", CLAUDE)])


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
        # The finished reply is still on screen: the permission request is shown together with it.
        self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION, S.ALSO_DONE))
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
        self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION, S.ALSO_DONE))
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

    # Project names (invented).

    def test_a_named_alert_shows_its_project(self):
        with self.assertLogs("quill.session", level="INFO") as logs:
            self.assertTrue(self.manager.alert(S.sound.DONE, "zorblat-kit"))
            self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, "zorblat-kit: Claude acabou"))
            self.clock.now += S.ALERT_SHOW_S  # the first alert is gone from the screen
            self.assertTrue(self.manager.alert(S.sound.PERMISSION, "quenta-tree"))
            self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION, "quenta-tree: Claude pede permissão"))
        self.assertEqual(self.player.plays, [S.sound.DONE, S.sound.PERMISSION])
        output = "\n".join(logs.output)
        self.assertNotIn("zorblat", output)
        self.assertNotIn("quenta", output)
        self.assertIn("1 named", output)

    def test_named_alerts_kept_together_name_every_project_permission_last(self):
        self.press()
        for kind, project in ((S.sound.DONE, "zorblat-kit"), (S.sound.DONE, None), (S.sound.PERMISSION, "quenta-tree"),
                              (S.sound.DONE, "vellum-app"), (S.sound.PERMISSION, None), (S.sound.DONE, "zorblat-kit"),
                              (S.sound.PERMISSION, "brask-lab")):
            self.manager.alert(kind, project)
        self.release()
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.wait_shown(CLAUDE_PERMISSION)
        time.sleep(0.15)  # several finalizer polls: nothing more is shown
        shown = [call for call in self.indicator.calls if call[0] == "show" and call[1] in S.ALERT_STATES.values()]
        self.assertEqual(shown, [("show", CLAUDE_PERMISSION,
                                  "zorblat-kit, vellum-app: Claude acabou; quenta-tree, brask-lab: Claude pede permissão")])
        self.assertEqual(self.player.plays, [S.sound.PERMISSION])
        self.assertEqual(self.player.plays_while_recording, 0)

    def test_a_named_alert_within_the_repeat_time_is_shown_without_sound(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.clock.now += S.ALERT_REPEAT_S - 1
        self.manager.alert(S.sound.DONE, "vellum-app")
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, "zorblat-kit, vellum-app: Claude acabou"))
        self.assertEqual(self.player.plays, [S.sound.DONE])

    def test_named_alerts_arriving_while_idle_are_all_shown(self):
        names = ("zorblat-kit", "quenta-tree", "vellum-app", "brask-lab", "orrin-ops")
        for number, name in enumerate(names):
            self.manager.alert(S.sound.DONE, name)
            self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, f"{', '.join(names[:number + 1])}: Claude acabou"))
            self.clock.now += 1
        self.assertEqual(self.player.plays, [S.sound.DONE])  # all within alert_repeat_s of the first
        self.manager.alert(S.sound.DONE, "quenta-tree")  # already shown: nothing new
        self.assertEqual(self.indicator.last[2], f"{', '.join(names)}: Claude acabou")

    def test_a_permission_on_screen_is_not_hidden_by_a_later_finished_reply(self):
        self.manager.alert(S.sound.PERMISSION, "quenta-tree")
        self.clock.now += 1
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION,
                                               "zorblat-kit: Claude acabou; quenta-tree: Claude pede permissão"))
        self.clock.now += 1
        self.manager.alert(S.sound.DONE)  # nameless: absorbed by the named finished reply
        self.assertEqual(self.indicator.last[1:], (CLAUDE_PERMISSION,
                                                   "zorblat-kit: Claude acabou; quenta-tree: Claude pede permissão"))
        self.assertEqual(self.player.plays, [S.sound.PERMISSION])

    def test_an_alert_after_the_shown_ones_left_the_screen_is_shown_alone(self):
        self.manager.alert(S.sound.PERMISSION, "quenta-tree")
        self.clock.now += S.ALERT_SHOW_S
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, "zorblat-kit: Claude acabou"))
        self.assertEqual(self.player.plays, [S.sound.PERMISSION, S.sound.DONE])

    def test_a_dictation_replaces_the_shown_alerts(self):
        self.manager.alert(S.sound.PERMISSION, "quenta-tree")
        self.dictate("frase inventada")
        self.wait_outcomes(1)
        self.clock.now += S.SENT_SHOW_S + S.ERROR_SHOW_S  # the outcome has been seen, the first alert is still recent
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, "zorblat-kit: Claude acabou"))

    def test_a_notice_replaces_the_shown_alerts(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertTrue(self.manager.notify(ERROR, "Aviso inventado", 0.5))
        self.clock.now += 1
        self.manager.alert(S.sound.DONE, "vellum-app")
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, "vellum-app: Claude acabou"))

    def test_a_named_alert_waits_for_an_outcome_on_screen(self):
        self.press("command", "f14")  # command mode unavailable: an error shown for a few seconds
        self.release("command", "f14")
        self.wait_outcomes(1)
        self.manager.alert(S.sound.PERMISSION, "quenta-tree")
        time.sleep(0.15)
        self.assertEqual(self.player.plays, [])
        self.clock.now += S.ERROR_SHOW_S
        self.wait_shown(CLAUDE_PERMISSION)
        self.assertEqual(self.indicator.last, ("show", CLAUDE_PERMISSION, "quenta-tree: Claude pede permissão"))

    def test_an_empty_name_is_nameless_and_other_values_are_refused(self):
        self.manager.alert(S.sound.DONE, "")
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, ""))
        for bad in (5, b"zorblat", ["zorblat"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.manager.alert(S.sound.DONE, bad)

    def test_many_waiting_projects_are_bounded_keeping_the_permission_requests(self):
        self.press()
        self.manager.alert(S.sound.PERMISSION, "quenta-tree")
        for index in range(S.MAX_ALERTS + 10):
            self.manager.alert(S.sound.DONE, f"brask-{index}")
        self.release()
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.wait_shown(CLAUDE_PERMISSION)
        text = self.indicator.last[2]
        self.assertTrue(text.endswith("; quenta-tree: Claude pede permissão"))
        self.assertEqual(text.count("brask-"), S.MAX_ALERTS - 1)
        self.assertIn(f"brask-{S.MAX_ALERTS + 9}", text)
        self.assertNotIn("brask-0,", text)


class SilentAlertTest(SessionCase):
    def test_without_a_player_the_alert_is_only_shown(self):
        self.manager.alert(S.sound.DONE)
        self.assertEqual(self.indicator.states, [])  # still loading
        self.ready()
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, ""))


class AlertTextTest(unittest.TestCase):
    """The words line of the waiting alerts (invented project names)."""

    DONE, PERMISSION = S.sound.DONE, S.sound.PERMISSION

    def test_nameless_alerts_keep_the_plain_texts(self):
        self.assertEqual(S.alert_text([(self.DONE, None)]), (self.DONE, ""))
        self.assertEqual(S.alert_text([(self.PERMISSION, None)]), (self.PERMISSION, ""))
        self.assertEqual(S.alert_text([(self.DONE, None), (self.PERMISSION, None)]), (self.PERMISSION, S.ALSO_DONE))
        self.assertEqual(S.alert_text([(self.PERMISSION, None), (self.DONE, None)]), (self.PERMISSION, S.ALSO_DONE))

    def test_one_named_alert(self):
        self.assertEqual(S.alert_text([(self.DONE, "zorblat-kit")]), (self.DONE, "zorblat-kit: Claude acabou"))
        self.assertEqual(S.alert_text([(self.PERMISSION, "zorblat-kit")]),
                         (self.PERMISSION, "zorblat-kit: Claude pede permissão"))

    def test_a_nameless_alert_is_absorbed_by_a_named_one_of_its_kind(self):
        self.assertEqual(S.alert_text([(self.DONE, None), (self.DONE, "zorblat-kit")]),
                         (self.DONE, "zorblat-kit: Claude acabou"))
        self.assertEqual(S.alert_text([(self.PERMISSION, "quenta-tree"), (self.PERMISSION, None)]),
                         (self.PERMISSION, "quenta-tree: Claude pede permissão"))

    def test_a_nameless_alert_of_the_other_kind_keeps_its_phrase(self):
        self.assertEqual(S.alert_text([(self.DONE, None), (self.PERMISSION, "quenta-tree")]),
                         (self.PERMISSION, "Claude acabou; quenta-tree: Claude pede permissão"))
        self.assertEqual(S.alert_text([(self.PERMISSION, None), (self.DONE, "zorblat-kit")]),
                         (self.PERMISSION, "zorblat-kit: Claude acabou; Claude pede permissão"))

    def test_several_projects_permission_last(self):
        alerts = [(self.PERMISSION, "quenta-tree"), (self.DONE, "zorblat-kit"), (self.DONE, "vellum-app"),
                  (self.PERMISSION, "zorblat-kit")]
        self.assertEqual(S.alert_text(alerts), (self.PERMISSION, "zorblat-kit, vellum-app: Claude acabou; "
                                                                 "quenta-tree, zorblat-kit: Claude pede permissão"))

    def test_the_indicator_truncation_never_hides_the_permission_requests(self):
        from quill.indicator.render import METRICS, fit_words

        done = [(self.DONE, f"brask-lab-{index:02d}-{'x' * 30}") for index in range(20)]
        permission = [(self.PERMISSION, "quenta-tree"), (self.PERMISSION, "zorblat-kit")]
        _, text = S.alert_text(permission[:1] + done + permission[1:])
        for scale in (1.0, 1.5):
            room = (METRICS.max_width - METRICS.orb_area - METRICS.pad_right) * scale
            # A wide estimate of the words font (the real one is narrower): wider can only cut more.
            lines = fit_words(text, room, lambda line: 9.5 * scale * len(line))
            shown = " ".join(lines)
            with self.subTest(scale=scale):
                self.assertTrue(lines[0].startswith("…"))
                self.assertTrue(shown.endswith("quenta-tree, zorblat-kit: Claude pede permissão"))


class HelpersTest(unittest.TestCase):
    def test_voice_level(self):
        self.assertEqual(S.voice_level(b""), 0.0)
        self.assertEqual(S.voice_level(bytes(640)), 0.0)
        self.assertEqual(S.voice_level(b"\xff\x7f" * 320), 1.0)
        self.assertTrue(0.0 < S.voice_level(b"\x00\x02" * 320) < 0.5)

    def test_every_reason_has_a_portuguese_message(self):
        self.assertEqual(S.message("unknown"), S.MESSAGES[S.INTERNAL_ERROR])
        self.assertTrue(S.message(inject.TARGET_GONE, typed=2).endswith(S.INTERRUPTED))


class FakeVoice:
    """A quill.voice.VoiceCommands stand-in: records the texts and returns a set outcome."""

    def __init__(self):
        self.texts = []
        self.outcome = SimpleNamespace(ok=True, reason="opened", state=VOICE_OPEN, text="A abrir orla-inventado")
        self.error = None

    def run(self, text):
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.outcome


class VoiceSessionTest(SessionCase):
    def make_voice(self):
        return FakeVoice()

    def setUp(self):
        super().setUp()
        self.ready()

    def speak(self, text="abre vs code no orla inventado"):
        self.press("voice", "f9")
        capture = self.captures.made[-1]
        capture.push(PCM)
        asr = self.transcriber.sessions[-1]
        asr.partial("abre vs code")
        self.release("voice", "f9")
        asr.handle.resolve(text)
        return self.wait_outcomes(len(self.manager.outcomes) + 1)[-1]

    def test_a_voice_command_never_clicks_types_or_presses_enter(self):
        with self.assertLogs("quill.session", level="DEBUG") as logs:
            outcome = self.speak()
        self.assertEqual((outcome.action, outcome.reason, outcome.typed), ("voice", "opened", 0))
        self.assertEqual(self.voice.texts, ["abre vs code no orla inventado"])
        self.assertEqual(self.focus.calls, [])
        self.assertEqual((self.injector.typed, self.injector.enters), ([], []))
        self.assertEqual(self.typed_hook, [])
        self.assertEqual(self.pipeline.calls, 0)  # no text pipeline: nothing is typed
        # Listening shows the voice state with the live words, then the command's outcome.
        self.assertEqual(self.indicator.states, ["hide", VOICE, TRANSCRIBING, VOICE_OPEN])  # hide: model ready
        self.assertIn(("text", "abre vs code"), self.indicator.calls)
        self.assertTrue(any(call[0] == "level" for call in self.indicator.calls))
        self.assertEqual(self.indicator.last, ("show", VOICE_OPEN, "A abrir orla-inventado"))
        text = "\n".join(logs.output)
        self.assertIn("voice opened", text)
        for word in ("orla", "abre", "inventado"):
            self.assertNotIn(word, text.casefold())
        self.assert_released()

    def test_no_match_shows_the_options_and_errors_show_the_reason(self):
        self.voice.outcome = SimpleNamespace(ok=False, reason="no_match", state=VOICE_NONE,
                                             text="Nenhum projeto com esse nome. Parecidos: orla, orla-public")
        self.assertEqual(self.speak().reason, "no_match")
        self.assertEqual(self.indicator.last, ("show", VOICE_NONE,
                                               "Nenhum projeto com esse nome. Parecidos: orla, orla-public"))
        self.voice.outcome = SimpleNamespace(ok=False, reason="launch_failed", state=ERROR,
                                             text="O Windows não abriu o atalho")
        self.assertEqual(self.speak().reason, "launch_failed")
        self.assertEqual(self.indicator.last, ("show", ERROR, "O Windows não abriu o atalho"))
        self.assertEqual((self.focus.calls, self.injector.typed, self.injector.enters), ([], [], []))

    def test_silence_runs_nothing(self):
        self.assertEqual(self.speak("   ").reason, S.NO_SPEECH)
        self.assertEqual(self.voice.texts, [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.NO_SPEECH]))

    def test_a_raising_voice_command_is_an_internal_error_and_the_next_session_works(self):
        self.voice.error = RuntimeError("orla inventado")
        with self.assertLogs("quill.session", level="ERROR") as logs:
            self.assertEqual(self.speak().reason, S.INTERNAL_ERROR)
        self.assertNotIn("orla", "\n".join(logs.output))
        self.voice.error = None
        self.assertEqual(self.speak().reason, "opened")
        self.assert_released()

    def test_a_dictation_after_a_voice_command_still_clicks_and_types(self):
        self.speak()
        self.dictate()
        self.assertEqual(self.wait_outcomes(2)[-1].reason, S.TYPED)
        self.assertEqual(self.focus.calls, [("dictation", "xbutton1")])
        self.assertEqual(self.injector.typed, [("Texto inventado de teste.", TARGET)])


class VoiceModelTest(VoiceSessionTest):
    """A voice transcriber with its own model decodes the voice holds once it is ready."""

    def setUp(self):
        super().setUp()
        self.voice_model = FakeTranscriber()
        self.voice_model.ready = threading.Event()
        self.voice_model.load_error = None
        self.manager.voice_transcriber = self.voice_model

    def speak_with(self, transcriber, text="abre vs code no orla inventado"):
        before = len(transcriber.sessions)
        self.press("voice", "f9")
        self.captures.made[-1].push(PCM)
        self.assertEqual(len(transcriber.sessions), before + 1)
        self.release("voice", "f9")
        transcriber.sessions[-1].handle.resolve(text)
        return self.wait_outcomes(len(self.manager.outcomes) + 1)[-1]

    def test_the_voice_model_decodes_voice_holds_once_ready(self):
        self.voice_model.ready.set()
        self.assertEqual(self.speak_with(self.voice_model).reason, "opened")
        self.assertEqual(self.voice.texts, ["abre vs code no orla inventado"])
        self.assertEqual(self.transcriber.sessions, [])
        # Dictation keeps the engine model.
        self.dictate()
        self.assertEqual(self.wait_outcomes(2)[-1].reason, S.TYPED)
        self.assertEqual((len(self.transcriber.sessions), len(self.voice_model.sessions)), (1, 1))

    def test_the_engine_model_decodes_while_the_voice_model_loads_or_after_it_failed(self):
        with self.assertLogs("quill.session", level="INFO") as logs:
            self.assertEqual(self.speak_with(self.transcriber).reason, "opened")
        self.assertIn("voice model still loading", "\n".join(logs.output))
        self.voice_model.load_error = "invented load failure"
        self.voice_model.ready.set()
        with self.assertLogs("quill.session", level="INFO") as logs:
            self.assertEqual(self.speak_with(self.transcriber).reason, "opened")
        self.assertIn("voice model not loaded", "\n".join(logs.output))
        self.assertNotIn("invented", "\n".join(logs.output))
        self.assertEqual(self.voice_model.sessions, [])


class VoiceUnavailableTest(SessionCase):
    def test_the_voice_trigger_says_it_is_unavailable_without_recording(self):
        self.ready()
        self.press("voice", "f9")
        self.release("voice", "f9")
        self.assertEqual(self.captures.made, [])
        self.assertEqual(self.focus.calls, [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.VOICE_UNAVAILABLE]))
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.VOICE_UNAVAILABLE)


class SpokenNameTest(SessionCase):
    """The project names spoken after the chime: a fake speech engine and the fake clock; the
    speaker has no thread here, ``tick`` starts a due speech. The names are invented."""

    def make_player(self):
        return FakePlayer(recording=lambda: bool(self.captures.open))

    def make_speaker(self):
        self.engine = FakeSpeechEngine()
        return SP.Speaker(self.engine, rate=1.25, volume=60, clock=self.clock, threaded=False)

    def setUp(self):
        super().setUp()
        self.ready()

    def after_gap(self):
        self.clock.now += SP.CHIME_GAP_S
        return self.speaker.tick()

    def test_the_name_is_spoken_after_the_chime_gap_with_the_configured_rate_and_volume(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertEqual(self.player.plays, [S.sound.DONE])
        self.assertFalse(self.speaker.tick())  # the chime has just started
        self.clock.now += SP.CHIME_GAP_S - 0.01
        self.assertFalse(self.speaker.tick())
        self.assertEqual(self.engine.processes, [])
        self.clock.now += 0.01
        self.assertTrue(self.speaker.tick())
        self.assertEqual(self.engine.spoken, [["zorblat kit"]])
        request = json.loads(self.engine.processes[0].data.decode("utf-8"))
        self.assertEqual((request["rate"], request["volume"], request["probe"]), (1.25, 60, False))
        self.assertFalse(self.speaker.tick())  # started once

    def test_a_hold_during_the_chime_gap_starts_no_process(self):
        self.manager.alert(S.sound.PERMISSION, "quenta-tree")
        self.clock.now += SP.CHIME_GAP_S / 2
        self.press()
        self.assertFalse(self.speaker.pending)
        self.clock.now += SP.CHIME_GAP_S
        self.assertFalse(self.speaker.tick())
        self.assertEqual(self.engine.processes, [])

    def test_a_hold_during_speech_terminates_it_before_the_capture_opens(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertTrue(self.after_gap())
        process = self.engine.processes[0]
        at_start = []
        self.captures.on_start = lambda capture: at_start.append((process.terminated, self.player.playing))
        self.press()
        self.assertEqual(at_start, [(1, False)])
        self.release()
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.assertEqual(process.terminated, 1)  # terminated once, never waited for

    def test_the_next_alert_speaks_again(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.clock.now += SP.CHIME_GAP_S / 2
        self.press()
        self.release()
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.clock.now += S.ALERT_REPEAT_S
        self.manager.alert(S.sound.PERMISSION, "vellum-app")
        self.wait_until(lambda: self.player.plays == [S.sound.DONE, S.sound.PERMISSION])
        self.assertTrue(self.after_gap())
        self.assertEqual(self.engine.spoken, [["vellum app"]])

    def test_a_hold_that_starts_while_the_process_starts_terminates_it(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.engine.on_start = self.press  # the hold wins the race with the starting process
        self.assertFalse(self.after_gap())
        self.assertEqual(self.engine.processes[0].terminated, 1)

    def test_an_alert_shown_without_a_ring_speaks_nothing(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertTrue(self.after_gap())
        self.clock.now += 1  # still within alert_repeat_s of the ring
        self.manager.alert(S.sound.PERMISSION, "quenta-tree")
        self.assertEqual(self.player.plays, [S.sound.DONE])
        self.assertEqual(self.indicator.last[1], CLAUDE_PERMISSION)
        self.assertFalse(self.speaker.pending)
        self.assertEqual(self.engine.processes[0].terminated, 0)  # the first name is not cut
        self.clock.now += SP.CHIME_GAP_S
        self.assertFalse(self.speaker.tick())
        self.assertEqual(self.engine.spoken, [["zorblat kit"]])

    def test_several_names_permission_first_at_most_three(self):
        self.press()
        for kind, project in ((S.sound.DONE, "zorblat-kit"), (S.sound.DONE, "vellum_app"),
                              (S.sound.PERMISSION, "quenta.tree"), (S.sound.DONE, "brask-lab"),
                              (S.sound.PERMISSION, None)):
            self.manager.alert(kind, project)
        self.release()
        self.transcriber.sessions[0].handle.resolve()
        self.wait_outcomes(1)
        self.wait_until(lambda: self.player.plays == [S.sound.PERMISSION])
        self.assertTrue(self.after_gap())
        self.assertEqual(self.engine.spoken, [["quenta tree", "zorblat kit", "vellum app"]])

    def test_a_nameless_alert_speaks_nothing(self):
        self.manager.alert(S.sound.DONE)
        self.assertEqual(self.player.plays, [S.sound.DONE])
        self.assertFalse(self.speaker.pending)
        self.assertFalse(self.after_gap())
        self.assertEqual(self.engine.processes, [])

    def test_a_new_ring_ends_the_previous_speech(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertTrue(self.after_gap())
        self.clock.now += S.ALERT_REPEAT_S + S.ALERT_SHOW_S
        self.manager.alert(S.sound.DONE, "vellum-app")
        self.assertEqual(self.engine.processes[0].terminated, 1)
        self.assertTrue(self.after_gap())
        self.assertEqual(self.engine.spoken, [["zorblat kit"], ["vellum app"]])

    def test_an_engine_failure_is_logged_by_type_only_and_the_alert_stays(self):
        self.engine.fail = True
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        with self.assertLogs("quill.speech", level="ERROR") as logs:
            self.assertFalse(self.after_gap())
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, "zorblat-kit: Claude acabou"))
        self.assertIn("OSError", "\n".join(logs.output))
        self.assertNotIn("zorblat", "\n".join(logs.output))

    def test_stop_ends_the_speech(self):
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertTrue(self.after_gap())
        self.manager.stop()
        self.assertEqual(self.engine.processes[0].terminated, 1)

    def wait_until(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            time.sleep(0.005)


class SpokenNameWithoutSoundTest(SessionCase):
    """Without a player (sound = false) the speaker is never asked to speak."""

    def make_speaker(self):
        self.engine = FakeSpeechEngine()
        return SP.Speaker(self.engine, clock=self.clock, threaded=False)

    def test_no_sound_no_name(self):
        self.ready()
        self.manager.alert(S.sound.DONE, "zorblat-kit")
        self.assertEqual(self.indicator.last, ("show", CLAUDE_DONE, "zorblat-kit: Claude acabou"))
        self.assertFalse(self.speaker.pending)
        self.clock.now += SP.CHIME_GAP_S
        self.assertFalse(self.speaker.tick())
        self.assertEqual(self.engine.processes, [])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()


class FakeCommand:
    """A quill.command.CommandMode stand-in: records the instructions and rewrites the selection."""

    def __init__(self):
        self.instructions = []

    def run(self, instruction, target):
        self.instructions.append((instruction, target))
        return SimpleNamespace(ok=True, reason=C.REWRITTEN, typed=3, timings={})


class PendingPressTest(SessionCase):
    """A press while an earlier released session is still pending starts nothing."""

    NOTICE = S.MESSAGES[S.PREVIOUS_PENDING]
    TRIGGERS = (("dictation", "xbutton1"), ("send_polished", "xbutton2"), ("send_raw", "mbutton"),
                ("send_claude", "f15"), ("command", "f14"), ("voice", "f9"))

    def setUp(self):
        super().setUp()
        self.ready()
        self.focus.targets = [CLAUDE]

    def make_rewriter(self):
        return FakeRewriter()

    def make_voice(self):
        return FakeVoice()

    def make_command(self):
        self.command = FakeCommand()
        return self.command

    def make_player(self):
        return FakePlayer()

    def pending(self, action="send_polished"):
        """A released mouse 5 session whose correction waits for ``self.rewriter.gate``."""
        self.settle()
        self.rewriter.gate = threading.Event()
        self.rewriter.started.clear()
        self.press(action, "xbutton2")
        self.captures.made[-1].push(PCM)
        self.transcriber.sessions[-1].partial("um dois")
        self.release(action, "xbutton2")
        self.transcriber.sessions[-1].handle.resolve(LONG)
        self.assertTrue(self.rewriter.started.wait(5))

    def assert_nothing_started(self, captures=1, clicks=1):
        self.assertEqual(len(self.captures.made), captures)
        self.assertEqual(len(self.transcriber.sessions), captures)
        self.assertEqual(len(self.focus.calls), clicks)
        self.assertEqual(self.started, captures)

    def test_every_trigger_is_ignored_and_the_pending_session_sends_once(self):
        self.pending()
        with self.assertLogs("quill.session", level="INFO") as logs:
            for action, trigger in self.TRIGGERS:
                self.press(action, trigger)
                self.assertEqual(self.indicator.last, ("show", REVIEWING, self.NOTICE))
                self.release(action, trigger)
        self.assert_nothing_started()
        self.assertEqual([(o.action, o.reason) for o in self.manager.outcomes],
                         [(action, S.PREVIOUS_PENDING) for action, _ in self.TRIGGERS])
        self.rewriter.gate.set()
        outcome = self.wait_outcomes(7)[-1]
        self.assertEqual((outcome.session, outcome.reason), (1, S.SENT_ENTER))
        self.assertEqual(self.injector.typed, [(LONG_TYPED.upper(), CLAUDE)])
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assertEqual(self.command.instructions, [])
        self.assertEqual(self.voice.texts, [])
        text = "\n".join(logs.output)
        self.assertEqual(text.count("still pending; ignored (previous_pending)"), len(self.TRIGGERS))
        for word in ("um", "dois", "aguarde"):
            self.assertNotRegex(text.casefold(), rf"\b{word}\b")
        self.assert_released()

    def test_a_pending_command_or_voice_session_blocks_a_dictation_press(self):
        for number, (action, trigger) in enumerate((("command", "f14"), ("voice", "f9"))):
            with self.subTest(action):
                self.settle()
                self.press(action, trigger)
                self.captures.made[-1].push(PCM)
                self.release(action, trigger)
                made, clicks = len(self.captures.made), len(self.focus.calls)
                self.press()
                self.release()
                self.assert_nothing_started(made, clicks)
                self.assertEqual(self.manager.outcomes[-1].reason, S.PREVIOUS_PENDING)
                self.transcriber.sessions[-1].handle.resolve("frase inventada")
                outcome = self.wait_outcomes(2 * number + 2)[-1]
                self.assertEqual(outcome.action, action)
        self.assertEqual(len(self.command.instructions), 1)
        self.assertEqual(len(self.voice.texts), 1)
        self.assertEqual(self.injector.typed, [])
        self.assert_released()

    def test_a_pending_session_failing_while_the_ignored_press_is_held_shows_its_error(self):
        for number, failure in enumerate(("engine", "typing")):
            with self.subTest(failure):
                self.settle()
                self.scheduled.clear()
                self.press("send_polished", "xbutton2")
                self.captures.made[-1].push(PCM)
                self.release("send_polished", "xbutton2")
                self.press()  # ignored, still held
                if failure == "engine":
                    self.transcriber.sessions[-1].handle.resolve("", error="fake engine failure")
                    reason = S.ENGINE_ERROR
                else:
                    self.injector.results = [inject.FOREGROUND_CHANGED]
                    self.transcriber.sessions[-1].handle.resolve("frase curta")
                    reason = inject.FOREGROUND_CHANGED
                outcomes = self.wait_outcomes(2 * number + 2)
                self.assertEqual([o.reason for o in outcomes[-2:]], [S.PREVIOUS_PENDING, reason])
                self.assertEqual(self.indicator.last[:2], ("show", ERROR))
                shown = self.indicator.last
                self.scheduled[-1][1]()  # the notice's restore never hides the error
                self.release()
                self.assertEqual(self.indicator.last, shown)
                self.assertEqual(len(self.manager.outcomes), 2 * number + 2)
        self.assertEqual(self.injector.enters, [])
        self.dictate()  # the next press works normally
        self.assertEqual(self.wait_outcomes(5)[-1].reason, S.TYPED)
        self.assert_released()

    def test_a_pending_session_succeeding_while_the_ignored_press_is_held_and_alerts_wait_for_its_release(self):
        self.pending()
        self.press()  # ignored, still held
        self.rewriter.gate.set()
        self.assertEqual(self.wait_outcomes(2)[-1].reason, S.SENT_ENTER)
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.manager.alert(sound.DONE)
        time.sleep(0.1)  # several finalizer polls
        self.assertEqual(self.player.plays, [])  # the press is still held: no sound
        self.clock.now += S.ERROR_SHOW_S  # the outcome was seen
        self.release()
        self.assertEqual(self.player.plays, [sound.DONE])
        self.assertEqual(self.indicator.last[1], CLAUDE_DONE)
        self.assertEqual(len(self.manager.outcomes), 2)
        self.dictate()  # the next press works normally
        self.assertEqual(self.wait_outcomes(3)[-1].reason, S.TYPED)

    def test_stop_while_pending_with_an_ignored_press_held_finishes_the_session_once(self):
        self.pending()
        self.press()  # ignored, still held
        stopper = threading.Thread(target=self.manager.stop)
        stopper.start()
        self.rewriter.gate.set()
        stopper.join(5)
        self.assertFalse(stopper.is_alive())
        self.assertEqual([o.reason for o in self.manager.outcomes], [S.PREVIOUS_PENDING, S.SENT_ENTER])
        self.assertEqual(len(self.injector.typed), 1)
        self.assertEqual(self.injector.enters, [CLAUDE])
        self.assert_released()
        self.release()  # after stop: nothing
        self.assertEqual(len(self.manager.outcomes), 2)
        self.manager.start()
        self.manager.loaded()
        self.dictate()
        self.assertEqual(self.wait_outcomes(3)[-1].reason, S.TYPED)

    def test_repeated_press_release_cycles(self):
        for cycle in range(1, 3):
            self.scheduled.clear()
            self.pending()
            for _ in range(3):
                self.press()
                self.release()
                self.assertEqual(self.indicator.last, ("show", REVIEWING, self.NOTICE))
            for _, restore in self.scheduled:  # the older restores are stale: only the last one shows
                restore()
            # Each cycle: the correction's state once, then the last restore once.
            self.assertEqual(self.indicator.calls.count(("show", REVIEWING, "um dois")), 2 * cycle)
            self.assertEqual(self.indicator.last, ("show", REVIEWING, "um dois"))
            self.rewriter.gate.set()
            self.assertEqual(self.wait_outcomes(4 * cycle)[-1].reason, S.SENT_ENTER)
            self.assert_nothing_started(cycle, cycle)
        self.assertEqual(self.injector.enters, [CLAUDE, CLAUDE])
        self.assertEqual([o.reason for o in self.manager.outcomes].count(S.PREVIOUS_PENDING), 6)
        self.assert_released()

    def test_a_held_session_keeps_todays_one_hold_rule(self):
        with self.assertLogs("quill.session", level="WARNING"):
            self.press()  # held, not released: not pending
            self.press()  # the machine allows one hold: the stale one is dropped, as today
        self.assertEqual(len(self.captures.made), 2)
        self.assertTrue(self.captures.made[1].started)
        self.assertEqual([o.reason for o in self.manager.outcomes], [S.INTERNAL_ERROR])
        self.release()
        self.transcriber.sessions[-1].handle.resolve("frase curta")
        self.assertEqual(self.wait_outcomes(2)[-1].reason, S.TYPED)

    def test_a_failing_schedule_is_logged_and_the_pending_session_finishes(self):
        def broken(seconds, job):
            raise RuntimeError("fake timer failure")

        self.manager.schedule = broken
        self.pending()
        with self.assertLogs("quill.session", level="ERROR") as logs:
            self.press()
        self.assertIn("indicator restore could not be scheduled (RuntimeError)", "\n".join(logs.output))
        self.release()
        self.rewriter.gate.set()
        self.assertEqual(self.wait_outcomes(2)[-1].reason, S.SENT_ENTER)
        self.assert_released()
