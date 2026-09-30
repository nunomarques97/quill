"""quill.speech tests: the spoken project names with a fake speech engine and a fake clock.

No test starts PowerShell or plays sound: importing ``quill.tests`` makes the
real engine's constructor fail, and ``subprocess.Popen`` is replaced where
the real engine's start is checked. The names are invented.
"""

import contextlib
import io
import json
import logging
import time
import unittest
from pathlib import Path
from unittest import mock

from quill import sound
from quill import speech as SP
from quill.config import ClaudeAlert
from quill.tests.fakes import FakeClock, FakePlayer, FakeSpeechEngine


def wait_for(condition, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


class GuardTest(unittest.TestCase):
    def test_the_real_engine_is_forbidden_in_tests(self):
        with self.assertRaises(AssertionError):
            SP.PowerShellSpeech()
        with self.assertRaises(AssertionError):
            SP.PowerShellSpeech(Path("C:/Windows"))


class NamesTest(unittest.TestCase):
    def test_separators_are_spoken_as_spaces(self):
        self.assertEqual(SP.spoken("zorblat-kit"), "zorblat kit")
        self.assertEqual(SP.spoken("vellum_app.io"), "vellum app io")
        self.assertEqual(SP.spoken("  --quenta__tree..  "), "quenta tree")
        self.assertEqual(SP.spoken("Orrin Ops"), "Orrin Ops")

    def test_control_characters_are_dropped_and_length_is_bounded(self):
        self.assertEqual(SP.spoken("brask\nlab\x07\x85x"), "brask lab x")
        self.assertEqual(SP.spoken("-_."), "")
        self.assertLessEqual(len(SP.spoken("a" * 500)), SP.MAX_NAME_CHARS)

    def test_permission_first_each_name_once_at_most_three(self):
        alerts = [("done", "zorblat-kit"), ("done", None), ("permission", "quenta-tree"), ("done", "zorblat_kit"),
                  ("done", "vellum-app"), ("permission", "brask-lab"), ("done", "orrin-ops")]
        self.assertEqual(SP.spoken_names(alerts), ["quenta tree", "brask lab", "zorblat kit"])
        self.assertEqual(SP.spoken_names([("done", None), ("permission", None)]), [])
        self.assertEqual(SP.spoken_names([("done", "-")]), [])

    def test_payload_is_utf8_json(self):
        data = SP.payload(["projeto ação"], 1.5, 70)
        self.assertEqual(json.loads(data.decode("utf-8")),
                         {"names": ["projeto ação"], "rate": 1.5, "volume": 70, "probe": False})
        self.assertIn("ação".encode("utf-8"), data)

    def test_rate_mapping_for_the_system_speech_fallback(self):
        self.assertEqual([SP.rate_to_sapi(rate) for rate in (0.5, 1.0, 3.0, 6.0)], [-6, 0, 10, 10])


class CommandLineTest(unittest.TestCase):
    """The fixed command: absolute PowerShell 5.1 path, flags, the constant script; never a name."""

    def test_the_command_is_fixed_and_carries_no_name(self):
        path = SP.powershell_path(Path("C:/Windows"))
        self.assertEqual(path, Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))
        args = SP.command_line(path)
        self.assertEqual(args[:-1], [str(path), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIs(args[-1], SP.SCRIPT)

    def test_a_relative_windows_folder_is_refused(self):
        with self.assertRaises(OSError):
            SP.powershell_path(Path("Windows"))

    def test_the_script_reads_everything_from_stdin_as_plain_text(self):
        script = SP.SCRIPT
        self.assertNotIn('"', script)  # passes through the command line unchanged
        self.assertIn("[Console]::OpenStandardInput()", script)
        self.assertIn("[Text.Encoding]::UTF8.GetString", script)
        lowered = script.lower()
        for unsafe in ("invoke-expression", "iex ", "ssml", "start-process", "$args", "http"):
            self.assertNotIn(unsafe, lowered)
        # The voice order: pt-PT, another pt voice, the default WinRT voice, then System.Speech.
        order = [script.index(part) for part in ("-eq 'pt-PT'", "-like 'pt-*'", "::DefaultVoice",
                                                  "System.Speech.Synthesis.SpeechSynthesizer")]
        self.assertEqual(order, sorted(order))

    def test_start_uses_a_hidden_process_and_writes_the_names_to_stdin_only(self):
        engine = object.__new__(SP.PowerShellSpeech)  # the real constructor is forbidden in tests
        engine.args = SP.command_line(Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))
        with mock.patch.object(SP.subprocess, "Popen") as popen:
            process = engine.start(SP.payload(["zorblat kit"], 1.0, 80))
        (args,), options = popen.call_args
        self.assertEqual(args, engine.args)
        self.assertNotIn("zorblat", " ".join(args))
        self.assertEqual(options["creationflags"], SP.CREATE_NO_WINDOW)
        self.assertEqual(options["startupinfo"].wShowWindow, SP.SW_HIDE)
        self.assertEqual((options["stdin"], options["stdout"], options["stderr"]),
                         (SP.subprocess.PIPE, SP.subprocess.DEVNULL, SP.subprocess.DEVNULL))
        written = process.stdin.write.call_args.args[0]
        self.assertEqual(json.loads(written.decode("utf-8"))["names"], ["zorblat kit"])
        process.stdin.close.assert_called_once()

    def test_a_failed_stdin_write_kills_the_process(self):
        engine = object.__new__(SP.PowerShellSpeech)
        engine.args = ["powershell.exe"]
        with mock.patch.object(SP.subprocess, "Popen") as popen:
            popen.return_value.stdin.write.side_effect = BrokenPipeError()
            with self.assertRaises(OSError):
                engine.start(b"{}")
        popen.return_value.kill.assert_called_once()


class SpeakerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.engine = FakeSpeechEngine()
        self.speaker = SP.Speaker(self.engine, rate=0.8, volume=40, clock=self.clock, threaded=False)

    def test_nothing_starts_before_the_gap(self):
        self.speaker.say(["zorblat kit"])
        self.assertTrue(self.speaker.pending)
        self.clock.now += SP.CHIME_GAP_S - 0.001
        self.assertFalse(self.speaker.tick())
        self.clock.now += 0.001
        self.assertTrue(self.speaker.tick())
        self.assertEqual(self.engine.spoken, [["zorblat kit"]])
        request = json.loads(self.engine.processes[0].data.decode("utf-8"))
        self.assertEqual((request["rate"], request["volume"]), (0.8, 40))

    def test_stop_in_the_gap_cancels_and_stop_while_speaking_terminates(self):
        self.speaker.say(["zorblat kit"])
        self.speaker.stop()
        self.clock.now += SP.CHIME_GAP_S
        self.assertFalse(self.speaker.tick())
        self.assertEqual(self.engine.processes, [])
        self.speaker.say(["vellum app"])
        self.clock.now += SP.CHIME_GAP_S
        self.assertTrue(self.speaker.tick())
        self.speaker.stop()
        self.speaker.stop()  # nothing left: no second terminate
        self.assertEqual(self.engine.processes[0].terminated, 1)

    def test_at_most_three_names_and_empty_requests_cancel(self):
        self.speaker.say(["a", "b", "c", "d"])
        self.clock.now += SP.CHIME_GAP_S
        self.speaker.tick()
        self.assertEqual(self.engine.spoken, [["a", "b", "c"]])
        self.speaker.say([])
        self.assertEqual(self.engine.processes[0].terminated, 1)
        self.assertFalse(self.speaker.pending)

    def test_a_stop_while_the_process_starts_terminates_it(self):
        self.engine.on_start = self.speaker.stop
        self.speaker.say(["zorblat kit"])
        self.clock.now += SP.CHIME_GAP_S
        self.assertFalse(self.speaker.tick())
        self.assertEqual(self.engine.processes[0].terminated, 1)
        self.engine.on_start = None
        self.speaker.say(["zorblat kit"])  # the next request speaks again
        self.clock.now += SP.CHIME_GAP_S
        self.assertTrue(self.speaker.tick())

    def test_a_failing_engine_or_terminate_is_logged_by_type_only(self):
        self.engine.fail = True
        self.speaker.say(["zorblat kit"])
        self.clock.now += SP.CHIME_GAP_S
        with self.assertLogs("quill.speech", level="ERROR") as logs:
            self.assertFalse(self.speaker.tick())
        self.engine.fail = False
        self.speaker.say(["zorblat kit"])
        self.clock.now += SP.CHIME_GAP_S
        self.speaker.tick()
        self.engine.processes[0].terminate = lambda: (_ for _ in ()).throw(PermissionError("gone"))
        with self.assertLogs("quill.speech", level="ERROR") as more:
            self.speaker.stop()
        output = "\n".join(logs.output + more.output)
        self.assertIn("OSError", output)
        self.assertIn("PermissionError", output)
        self.assertNotIn("zorblat", output)

    def test_the_threaded_speaker_starts_after_the_gap(self):
        speaker = SP.Speaker(self.engine, gap_s=0.05)
        started = time.monotonic()
        speaker.say(["zorblat kit"])
        self.assertEqual(self.engine.processes, [])  # say never blocks on the start
        wait_for(lambda: self.engine.processes, "the speech process")
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        speaker.stop()
        self.assertEqual(self.engine.processes[0].terminated, 1)
        speaker.say(["vellum app"])
        speaker.stop()  # cancelled in the gap: the thread starts nothing
        time.sleep(0.15)
        self.assertEqual(len(self.engine.processes), 1)

    def test_every_request_after_a_finished_one_is_spoken(self):
        speaker = SP.Speaker(self.engine, gap_s=0.0)
        for number in range(1, 31):
            speaker.say([f"brask {number}"])
            wait_for(lambda: len(self.engine.processes) == number, f"speech {number}")
        self.assertEqual(self.engine.spoken[-1], ["brask 30"])

    def test_the_real_speaker_is_skipped_when_powershell_cannot_be_located(self):
        with mock.patch.object(SP.PowerShellSpeech, "__init__", side_effect=OSError("no folder")):
            with self.assertLogs("quill.speech", level="ERROR") as logs:
                self.assertIsNone(SP.real_speaker(1.0, 80))
        self.assertIn("OSError", logs.output[0])
        with mock.patch.object(SP.PowerShellSpeech, "__init__", return_value=None):
            speaker = SP.real_speaker(1.5, 30)
        self.assertEqual((speaker.rate, speaker.volume, speaker.gap_s, speaker.threaded),
                         (1.5, 30, SP.CHIME_GAP_S, True))


class ProbeTest(unittest.TestCase):
    def run_main(self, argv, engine):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = SP.main(argv, engine=engine)
        return code, out.getvalue(), err.getvalue()

    def test_a_voice_that_synthesized_prints_its_language_and_size_only(self):
        engine = FakeSpeechEngine()
        engine.probe_result = (0, "pt-PT 63086")
        code, out, _ = self.run_main(["--probe"], engine)
        self.assertEqual((code, out), (0, "speech probe: voice pt-PT, 63086 bytes\n"))
        request = json.loads(engine.probes[0].decode("utf-8"))
        self.assertEqual((request["probe"], request["names"]), (True, [SP.spoken(SP.EXAMPLE_NAME)]))

    def test_no_voice_exits_1(self):
        for result in ((1, ""), (0, ""), (0, "pt-PT 0"), (0, "garbage output here"), (0, "pt-PT 12 extra")):
            with self.subTest(result=result):
                engine = FakeSpeechEngine()
                engine.probe_result = result
                code, out, _ = self.run_main(["--probe"], engine)
                self.assertEqual(code, 1)
                self.assertNotIn("garbage", out)
        engine = FakeSpeechEngine()
        engine.fail = True
        with self.assertLogs("quill.speech", level="ERROR"):
            self.assertEqual(self.run_main(["--probe"], engine)[0], 1)

    def test_without_the_flag_it_does_nothing(self):
        engine = FakeSpeechEngine()
        self.assertEqual(self.run_main([], engine)[0], 2)
        self.assertEqual(engine.probes, [])


class SoundSpeakTest(unittest.TestCase):
    """``python -m quill.sound --play-sound --speak``: the chime, then an invented name."""

    def run_main(self, argv, **parts):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sound.main(argv, **parts)
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.player, self.engine, self.sleeps = FakePlayer(), FakeSpeechEngine(), []
        self.settings = ClaudeAlert(speech_rate=1.5, speech_volume=55)

    def parts(self):
        return {"player": self.player, "engine": self.engine, "sleep": self.sleeps.append, "settings": self.settings}

    def test_the_chime_then_the_example_name(self):
        order = []
        self.engine.on_start = lambda: order.append(("speak", list(self.player.plays), list(self.sleeps)))
        code, _, _ = self.run_main(["--play-sound", "--speak"], **self.parts())
        self.assertEqual(code, 0)
        self.assertEqual(order, [("speak", [sound.DONE], [SP.CHIME_GAP_S])])
        self.assertEqual(self.engine.spoken, [["projeto exemplo"]])
        request = json.loads(self.engine.processes[0].data.decode("utf-8"))
        self.assertEqual((request["rate"], request["volume"]), (1.5, 55))

    def test_a_chosen_project_name(self):
        self.assertEqual(self.run_main(["--play-sound", "--speak", "--project", "zorblat-kit"], **self.parts())[0], 0)
        self.assertEqual(self.engine.spoken, [["zorblat kit"]])

    def test_it_refuses_without_the_flag(self):
        code, _, err = self.run_main(["--speak"], **self.parts())
        self.assertEqual(code, 2)
        self.assertIn("--play-sound", err)
        self.assertEqual((self.player.plays, self.engine.processes), ([], []))

    def test_no_voice_is_reported(self):
        self.engine.code = 1
        code, _, err = self.run_main(["--play-sound", "--speak"], **self.parts())
        self.assertEqual(code, 1)
        self.assertIn("only the chime", err)
        self.engine.fail = True
        code, _, err = self.run_main(["--play-sound", "--speak"], **self.parts())
        self.assertEqual(code, 1)
        self.assertIn("OSError", err)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
