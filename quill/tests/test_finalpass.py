"""quill.finalpass tests with fake models and a fake clock.

The streaming final comes from one transcriber (the engine) and the pass from
another (the shared voice model), both on fake models that "hear" invented
words (see test_streaming). Pass calls are told apart by their beam (7). No
GPU, model file, microphone or sound is used.
"""

import threading
import unittest
from dataclasses import replace

from quill import finalpass
from quill.finalpass import FinalPassOutcome, FinalPassSettings
from quill.streaming import StreamingTranscriber, StreamOptions
from quill.tests.test_streaming import FakeClock, FakeModel, replay, speech, text
from quill.whisper import SessionHints

PASS_BEAM = 7
SETTINGS = FinalPassSettings(beam_size=PASS_BEAM, timeout_s=5.0)
SESSION_HINTS = SessionHints(prompt="Vocabulário: Zorblax.", hotwords="Zorblax", language="pt")
HEARD_HINTS = SessionHints(prompt="Vocabulário: Quorvex, Zorblax.", hotwords="Quorvex Zorblax", language="pt")


class PassModel(FakeModel):
    """A fake model whose pass calls (beam 7) may answer a fixed text."""

    def __init__(self, clock, cost_s=0.0):
        super().__init__(clock, cost_s)
        self.reply = None

    def transcribe(self, pcm, options):
        transcript = super().transcribe(pcm, options)
        if self.reply is not None and options.beam_size == PASS_BEAM:
            return replace(transcript, text=self.reply)
        return transcript


class Source:
    """A hint source recording what it was asked; ``choose`` decides the answer."""

    def __init__(self, choose):
        self.choose = choose
        self.heard = []

    def __call__(self, heard):
        self.heard.append(heard)
        return self.choose(heard)


class Case(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.engine_model = FakeModel(self.clock)
        self.engine_model.suffix = lambda call: "x"  # the streaming text differs from the pass text
        self.pass_model = PassModel(self.clock, cost_s=0.9)
        self.started = []
        self.engine = self.start(self.engine_model, commit=False)
        self.voice = self.start(self.pass_model)

    def tearDown(self):
        for model in (self.engine_model, self.pass_model):
            if model.gate is not None:
                model.gate.set()
        for transcriber in self.started:
            transcriber.stop(timeout=5)

    def start(self, model, **options):
        transcriber = StreamingTranscriber(model, StreamOptions(**options), ["Zorblax", "deploy"], clock=self.clock)
        transcriber.start()
        self.assertTrue(transcriber.ready.wait(5))
        self.started.append(transcriber)
        return transcriber

    def hold(self, indexes=(1, 2, 3), hints=None, **shape):
        session = self.engine.open(hints=hints) if hints is not None else self.engine.open()
        pcm = speech(list(indexes), **shape)
        for start in range(0, len(pcm), 1600):
            session.feed(pcm[start : start + 1600])
            self.assertTrue(self.engine.drain(5))
        final = session.release().wait(5)
        self.assertTrue(final.ok)
        return session, final

    def passes(self):
        return [call for call in self.pass_model.calls if call.options.beam_size == PASS_BEAM]

    def run_pass(self, session, final, settings=SETTINGS, transcriber=None):
        return finalpass.run_session(transcriber or self.voice, session, final, settings, clock=self.clock)


class OkTest(Case):
    def test_the_pass_text_replaces_the_streaming_text(self):
        session, final = self.hold(lead=1.0, trail=1.0)
        self.assertEqual(final.text, "w1x w2x w3x")
        before = self.clock()
        outcome = self.run_pass(session, final)
        self.assertEqual((outcome.text, outcome.reason, outcome.used_pass, outcome.hints),
                         (text([1, 2, 3]), "ok", True, "session"))
        calls = self.passes()
        self.assertEqual(len(calls), 1)
        audio = session.released_audio()
        self.assertAlmostEqual(calls[0].seconds, audio.seconds)  # trimmed: the final's speech range
        self.assertAlmostEqual(outcome.audio_s, audio.seconds)
        self.assertAlmostEqual(outcome.compute_s, 0.9)
        self.assertAlmostEqual(outcome.elapsed_s, self.clock() - before)
        options = calls[0].options
        self.assertEqual((options.initial_prompt, options.hotwords), ("Vocabulário: Zorblax, deploy.", "Zorblax deploy"))
        self.assertEqual(self.engine_model.calls[-1].options.beam_size, 5)  # the engine decoded no pass

    def test_settings_reach_the_decode(self):
        session, final = self.hold(lead=1.0, trail=1.0)
        settings = replace(SETTINGS, temperature_fallback=True, condition_on_previous_text=True, vad_filter=True,
                           trim=False)
        outcome = self.run_pass(session, final, settings)
        self.assertEqual(outcome.reason, "ok")
        call = self.passes()[0]
        self.assertEqual((call.options.temperature_fallback, call.options.condition_on_previous_text,
                          call.options.vad_filter), (True, True, True))
        self.assertAlmostEqual(call.seconds, final.audio_s)  # not trimmed: the whole capture

    def test_hints_off_decode_without_a_prompt(self):
        session, final = self.hold(hints=SESSION_HINTS)
        outcome = self.run_pass(session, final, replace(SETTINGS, hints=False))
        self.assertEqual((outcome.reason, outcome.hints), ("ok", "none"))
        options = self.passes()[0].options
        self.assertEqual((options.initial_prompt, options.hotwords, options.language), (None, None, "pt"))

    def test_the_session_hints_are_used(self):
        session, final = self.hold(hints=SESSION_HINTS)
        outcome = self.run_pass(session, final)
        self.assertEqual((outcome.reason, outcome.hints), ("ok", "session"))
        options = self.passes()[0].options
        self.assertEqual((options.initial_prompt, options.hotwords, options.language),
                         (SESSION_HINTS.prompt, SESSION_HINTS.hotwords, "pt"))

    def test_log_fields_hold_no_text(self):
        session, final = self.hold()
        outcome = self.run_pass(session, final)
        line = outcome.log_fields()
        self.assertIn("final pass ok", line)
        self.assertIn("compute 900 ms", line)
        for word in outcome.text.split() + final.text.split():
            self.assertNotIn(word, line)
        self.assertNotIn(outcome.text, repr(outcome))


class HintSourceTest(Case):
    def test_the_source_is_asked_with_the_streaming_text(self):
        source = Source(lambda heard: HEARD_HINTS if "w3x" in heard.split() else SESSION_HINTS)
        session, final = self.hold(hints=source)
        outcome = self.run_pass(session, final)
        self.assertEqual((outcome.reason, outcome.hints), ("ok", "source"))
        self.assertEqual(source.heard[-1], final.text)
        options = self.passes()[0].options
        self.assertEqual((options.initial_prompt, options.hotwords), (HEARD_HINTS.prompt, HEARD_HINTS.hotwords))

    def test_a_source_choosing_none_gives_the_vocabulary_hints(self):
        source = Source(lambda heard: SESSION_HINTS if heard == "" else None)
        session, final = self.hold(hints=source)
        outcome = self.run_pass(session, final)
        self.assertEqual((outcome.reason, outcome.hints), ("ok", "source"))
        self.assertEqual(self.passes()[0].options.initial_prompt, "Vocabulário: Zorblax, deploy.")

    def test_a_raising_or_wrong_source_keeps_the_session_hints(self):
        for answer in ("boom", "not hints"):
            with self.subTest(answer=answer):
                calls = len(self.passes())

                def choose(heard, answer=answer):
                    if heard == "":
                        return SESSION_HINTS
                    if answer == "boom":
                        raise RuntimeError("source failed")
                    return answer

                session, final = self.hold(hints=Source(choose))
                self.assertEqual(session.hints, SESSION_HINTS)
                outcome = self.run_pass(session, final)
                self.assertEqual((outcome.text, outcome.reason, outcome.hints), (text([1, 2, 3]), "ok", "source_failed"))
                options = self.passes()[calls].options
                self.assertEqual((options.initial_prompt, options.hotwords), (SESSION_HINTS.prompt, SESSION_HINTS.hotwords))


class FallbackTest(Case):
    def assert_fallback(self, outcome, final, reason):
        self.assertEqual((outcome.text, outcome.reason, outcome.used_pass), (final.text, reason, False))

    def test_off_never_decodes(self):
        session, final = self.hold()
        self.assert_fallback(self.run_pass(session, final, replace(SETTINGS, enabled=False)), final, "off")
        self.assertEqual(self.passes(), [])

    def test_empty_pass_text(self):
        session, final = self.hold()
        for reply in ("", " ... ", "?!"):
            with self.subTest(reply=reply):
                self.pass_model.reply = reply
                outcome = self.run_pass(session, final)
                self.assert_fallback(outcome, final, "empty")
                self.assertAlmostEqual(outcome.compute_s, 0.9)

    def test_model_error(self):
        self.pass_model.fail = lambda call: RuntimeError("boom")
        session, final = self.hold()
        self.assert_fallback(self.run_pass(session, final), final, "model_error")

    def test_load_error(self):
        broken = PassModel(self.clock)
        broken.fail_load = RuntimeError("no model")
        transcriber = self.start(broken)
        session, final = self.hold()
        self.assert_fallback(self.run_pass(session, final, transcriber=transcriber), final, "load_error")
        self.assertEqual(broken.calls, [])

    def test_not_ready_or_not_running(self):
        session, final = self.hold()
        idle = StreamingTranscriber(self.pass_model, clock=self.clock)
        self.assert_fallback(self.run_pass(session, final, transcriber=idle), final, "not_ready")
        loading = StreamingTranscriber(self.pass_model, clock=self.clock)
        gate = threading.Event()
        self.pass_model.load = lambda: gate.wait(10)
        loading.start()
        self.started.append(loading)
        try:
            self.assert_fallback(self.run_pass(session, final, transcriber=loading), final, "not_ready")
        finally:
            gate.set()
        self.assertEqual(self.passes(), [])

    def test_no_speech_or_a_failed_streaming_final(self):
        session, final = self.hold()
        failed = replace(final, text="", error="transcription failed: RuntimeError")
        self.assert_fallback(self.run_pass(session, failed), failed, "streaming_error")
        silent = self.engine.open()
        silent.feed(speech([]))
        quiet = silent.release().wait(5)
        self.assert_fallback(self.run_pass(silent, quiet), quiet, "no_speech")
        self.assert_fallback(finalpass.run(self.voice, final, None, SETTINGS, clock=self.clock), final, "no_speech")
        self.assertEqual(self.passes(), [])

    def test_timeout_falls_back_and_the_late_result_is_ignored(self):
        self.pass_model.gate = threading.Event()
        self.pass_model.gate_when = lambda call: call.options.beam_size == PASS_BEAM
        session, final = self.hold()
        outcome = self.run_pass(session, final, replace(SETTINGS, timeout_s=0.05))
        self.assert_fallback(outcome, final, "timeout")
        self.assertEqual(outcome.hints, "session")
        self.pass_model.gate.set()
        self.assertTrue(self.voice.drain(5))
        self.assertEqual(len(self.passes()), 1)

    def test_a_timed_out_pass_still_queued_is_never_decoded(self):
        self.pass_model.gate = threading.Event()
        self.pass_model.gate_when = lambda call: call.index == 0
        blocker = self.voice.decode_once(speech([9]))  # holds the worker
        self.assertTrue(self.pass_model.entered.wait(5))
        session, final = self.hold()
        self.assert_fallback(self.run_pass(session, final, replace(SETTINGS, timeout_s=0.05)), final, "timeout")
        self.pass_model.gate.set()
        self.assertEqual(blocker.wait(5).text, "w9")
        self.assertTrue(self.voice.drain(5))
        self.assertEqual(self.passes(), [])

    def test_stop_during_a_pending_pass(self):
        self.pass_model.gate = threading.Event()
        self.pass_model.gate_when = lambda call: call.options.beam_size == PASS_BEAM
        session, final = self.hold()
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(self.run_pass(session, final)))
        worker.start()
        self.assertTrue(self.pass_model.entered.wait(5))
        self.voice.stop(timeout=0.05)  # the pass is inside the model call
        worker.join(5)
        self.assert_fallback(outcomes[0], final, "stopped")
        self.pass_model.gate.set()  # its success arrives after the failure: ignored
        for thread in threading.enumerate():
            if thread.name == "quill-asr" and thread is not threading.current_thread():
                thread.join(0.5)
        self.assertEqual(outcomes[0].reason, "stopped")
        self.assertEqual(len(outcomes), 1)

    def test_overlapping_success_and_failure_resolve_once(self):
        # A pass resolved as stopped while its model call later succeeds, then a new pass after a restart.
        self.pass_model.gate = threading.Event()
        self.pass_model.gate_when = lambda call: call.index == 0
        session, final = self.hold()
        handle = self.voice.decode_once(session.released_audio().pcm)
        self.assertTrue(self.pass_model.entered.wait(5))
        self.voice.stop(timeout=0.05)
        self.assertEqual(handle.wait(1).reason, "stopped")
        self.voice.start()
        self.pass_model.gate.set()
        self.assertTrue(self.voice.ready.wait(5))
        outcome = self.run_pass(session, final)
        self.assertEqual((outcome.reason, outcome.text), ("ok", text([1, 2, 3])))
        self.assertEqual(handle.wait(0).reason, "stopped")


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        settings = FinalPassSettings()
        self.assertEqual((settings.enabled, settings.model, settings.beam_size, settings.trim, settings.hints),
                         (True, "large-v3", 5, True, True))
        self.assertEqual((settings.temperature_fallback, settings.condition_on_previous_text, settings.vad_filter),
                         (False, False, False))
        self.assertEqual(settings.pass_options().beam_size, 5)

    def test_invalid_values_are_refused(self):
        for changes in ({"model": "tiny"}, {"beam_size": 0}, {"beam_size": 11}, {"beam_size": True},
                        {"timeout_s": 0}, {"timeout_s": 31}, {"timeout_s": "4"}, {"enabled": "yes"},
                        {"trim": 1}, {"hints": None}, {"vad_filter": "no"}, {"temperature_fallback": 0}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    FinalPassSettings(**changes)

    def test_outcome_without_a_pass(self):
        outcome = FinalPassOutcome("typed words", "off")
        self.assertFalse(outcome.used_pass)
        self.assertNotIn("typed", outcome.log_fields())


if __name__ == "__main__":
    unittest.main()
