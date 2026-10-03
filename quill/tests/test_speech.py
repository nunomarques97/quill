"""quill.speech tests: the spoken project names with a fake speech engine and a fake clock.

No test starts PowerShell or plays sound: importing ``quill.tests`` makes the
real engine's constructor fail, and ``subprocess.Popen`` is replaced where
the real engine's start is checked. The own-voice clips are generated tones
in temporary folders. The names are invented.
"""

import contextlib
import io
import struct
import tempfile
import json
import logging
import threading
import time
import unittest
import wave
from array import array
from datetime import datetime
from pathlib import Path
from unittest import mock

from quill import clips as C
from quill import sound
from quill import speech as SP
from quill.clips import ClipStore
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
                         {"segments": [{"say": ["projeto ação"]}], "rate": 1.5, "volume": 70, "probe": False})
        self.assertIn("ação".encode("utf-8"), data)

    def test_consecutive_names_share_a_segment_and_clips_are_base64(self):
        data = json.loads(SP.payload(["zorblat kit", "quenta tree", b"RIFF-a", "vellum app", b"RIFF-b"], 1.0, 80))
        self.assertEqual(data["segments"], [{"say": ["zorblat kit", "quenta tree"]}, {"wav": "UklGRi1h"},
                                            {"say": ["vellum app"]}, {"wav": "UklGRi1i"}])

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
            wait_for(lambda: process.stdin.close.called, "the stdin writer")
        (args,), options = popen.call_args
        self.assertEqual(args, engine.args)
        self.assertNotIn("zorblat", " ".join(args))
        self.assertEqual(options["creationflags"], SP.CREATE_NO_WINDOW)
        self.assertEqual(options["startupinfo"].wShowWindow, SP.SW_HIDE)
        self.assertEqual((options["stdin"], options["stdout"], options["stderr"]),
                         (SP.subprocess.PIPE, SP.subprocess.DEVNULL, SP.subprocess.DEVNULL))
        written = process.stdin.write.call_args.args[0]
        self.assertEqual(json.loads(written.decode("utf-8"))["segments"], [{"say": ["zorblat kit"]}])
        process.stdin.close.assert_called_once()

    def test_start_returns_before_a_large_request_is_read(self):
        engine = object.__new__(SP.PowerShellSpeech)
        engine.args = ["powershell.exe"]
        release = threading.Event()
        with mock.patch.object(SP.subprocess, "Popen") as popen:
            popen.return_value.stdin.write.side_effect = lambda data: release.wait(5)
            process = engine.start(b"x" * 1_000_000)  # returns although nothing reads stdin yet
            self.assertFalse(process.stdin.close.called)
            release.set()
            wait_for(lambda: process.stdin.close.called, "the stdin writer")

    def test_a_failed_stdin_write_kills_the_process(self):
        engine = object.__new__(SP.PowerShellSpeech)
        engine.args = ["powershell.exe"]
        with mock.patch.object(SP.subprocess, "Popen") as popen:
            popen.return_value.stdin.write.side_effect = BrokenPipeError()
            with self.assertLogs("quill.speech", level="WARNING") as logs:
                engine.start(b"{}")
                wait_for(lambda: popen.return_value.kill.called, "the kill")
        popen.return_value.kill.assert_called_once()
        self.assertIn("BrokenPipeError", logs.output[0])


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


def tone(samples=(1000, -1000, 32767, -32768), repeat=800):
    """PCM16 bytes of a short invented signal (0.2 s at 16 kHz by default)."""
    return array("h", list(samples) * repeat).tobytes()


def wav_file(path, pcm, channels=1, width=2, rate=16000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def wav_samples(data):
    """(channels, width, rate, samples) of WAV bytes."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        params = handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    return (*params, list(samples))


class CountingStore:
    """A clip store that records each lookup and can run a hook in it (a hold arriving meanwhile)."""

    def __init__(self, store):
        self.store = store
        self.calls = []
        self.during = None

    def clips_for(self, names):
        self.calls.append(list(names))
        if self.during is not None:
            self.during()
        return self.store.clips_for(names)


class OwnVoiceTest(unittest.TestCase):
    """Recorded clips play instead of the Windows voice, in the spoken order; problems fall back to the voice."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.local = self.base / "local"
        self.store = ClipStore(self.local / "names", local_root=self.local)
        self.counting = CountingStore(self.store)
        self.clock = FakeClock()
        self.engine = FakeSpeechEngine()
        self.speaker = SP.Speaker(self.engine, rate=0.8, volume=50, clock=self.clock, threaded=False,
                                  clips=self.counting)
        self.pcm = tone()

    def record(self, name, pcm=None):
        self.store.save_clip(name, self.pcm if pcm is None else pcm, datetime(2026, 1, 1, 10, 0, 0))
        return self.store.folder / C.clip_file_name(C.clip_key(name))

    def run_due(self, names):
        self.speaker.say(names)
        self.clock.now += SP.CHIME_GAP_S
        return self.speaker.tick()

    def assert_private(self, text):
        for private in ("zorblat", "quenta", "vellum", "brask", str(self.base), self.base.name, "clip-"):
            self.assertNotIn(private, text)

    def test_a_name_with_a_clip_plays_it_scaled_to_the_volume(self):
        self.record("Zorblat_Kit")  # recorded under another spelling of the same key
        self.assertTrue(self.run_due(["zorblat kit"]))
        [[(kind, data)]] = self.engine.played
        self.assertEqual(kind, "wav")
        channels, width, rate, samples = wav_samples(data)
        self.assertEqual((channels, width, rate), (1, 2, 16000))
        self.assertEqual(samples[:4], [500, -500, 16384, -16384])  # volume 50: each sample halved
        self.assertEqual(len(samples), len(self.pcm) // 2)
        text = self.engine.processes[0].data.decode("utf-8")
        self.assert_private(text)  # neither the name nor a path reaches the process
        self.assertEqual(json.loads(text)["rate"], 0.8)

    def test_volume_scaling_of_the_clip_bytes(self):
        clip = C.Clip(array("h", [1000, -1000, 32767, -32768, 3]).tobytes(), 22050)
        for volume, expected in ((100, [1000, -1000, 32767, -32768, 3]), (80, [800, -800, 26214, -26214, 2]),
                                 (0, [0, 0, 0, 0, 0])):
            with self.subTest(volume=volume):
                self.assertEqual(wav_samples(SP.clip_wav(clip, volume)), (1, 2, 22050, expected))

    def test_mixed_clip_and_voice_names_keep_their_order(self):
        self.record("zorblat-kit")
        self.record("vellum-app")
        self.assertTrue(self.run_due(["zorblat kit", "quenta tree", "vellum app"]))
        kinds = [(kind, item if kind == "say" else "clip") for kind, item in self.engine.played[0]]
        self.assertEqual(kinds, [("wav", "clip"), ("say", "quenta tree"), ("wav", "clip")])
        self.assertTrue(self.run_due(["quenta tree", "brask lab", "zorblat kit"]))
        segments = json.loads(self.engine.processes[1].data)["segments"]
        self.assertEqual([list(segment) for segment in segments], [["say"], ["wav"]])
        self.assertEqual(segments[0]["say"], ["quenta tree", "brask lab"])  # spoken together, as before
        self.assertEqual(len(self.engine.processes), 2)  # one process per alert

    def test_at_most_three_names_even_with_clips(self):
        for name in ("zorblat-kit", "quenta-tree", "vellum-app", "brask-lab"):
            self.record(name)
        self.assertTrue(self.run_due(["zorblat kit", "quenta tree", "vellum app", "brask lab"]))
        self.assertEqual([kind for kind, _ in self.engine.played[0]], ["wav", "wav", "wav"])
        self.assertEqual(self.counting.calls, [["zorblat kit", "quenta tree", "vellum app"]])

    def test_say_reads_nothing_and_the_gap_is_unchanged(self):
        self.record("zorblat-kit")
        self.speaker.say(["zorblat kit"])
        self.assertEqual(self.counting.calls, [])  # no file read under the session lock
        self.clock.now += SP.CHIME_GAP_S - 0.001
        self.assertFalse(self.speaker.tick())
        self.assertEqual((self.counting.calls, self.engine.processes), ([], []))
        self.clock.now += 0.001
        self.assertTrue(self.speaker.tick())
        self.assertEqual(self.counting.calls, [["zorblat kit"]])

    def test_stop_before_and_during_playback(self):
        self.record("zorblat-kit")
        self.speaker.say(["zorblat kit"])
        self.speaker.stop()
        self.clock.now += SP.CHIME_GAP_S
        self.assertFalse(self.speaker.tick())
        self.assertEqual((self.counting.calls, self.engine.processes), ([], []))
        self.assertTrue(self.run_due(["zorblat kit"]))
        self.speaker.stop()
        self.assertEqual(self.engine.processes[0].terminated, 1)  # the clip and any voice end together

    def test_a_stop_while_the_clips_are_read_starts_nothing(self):
        self.record("zorblat-kit")
        self.counting.during = self.speaker.stop
        self.assertFalse(self.run_due(["zorblat kit"]))
        self.assertEqual(self.engine.processes, [])
        self.counting.during = None
        self.assertTrue(self.run_due(["zorblat kit"]))  # the next alert plays again

    def test_a_stop_while_the_process_starts_terminates_it(self):
        self.record("zorblat-kit")
        self.engine.on_start = self.speaker.stop
        self.assertFalse(self.run_due(["zorblat kit"]))
        self.assertEqual(self.engine.processes[0].terminated, 1)

    def test_a_newer_alert_while_the_clips_are_read_replaces_the_older(self):
        self.record("zorblat-kit")

        def newer():
            self.counting.during = None
            self.speaker.say(["quenta tree"])
        self.counting.during = newer
        self.assertFalse(self.run_due(["zorblat kit"]))
        self.assertEqual(self.engine.processes, [])
        self.clock.now += SP.CHIME_GAP_S
        self.assertTrue(self.speaker.tick())
        self.assertEqual(self.engine.played, [[("say", "quenta tree")]])

    def test_the_threaded_speaker_reads_clips_on_its_own_thread(self):
        self.record("zorblat-kit")
        threads = []
        self.counting.during = lambda: threads.append(threading.current_thread().name)
        speaker = SP.Speaker(self.engine, gap_s=0.0, clips=self.counting)
        speaker.say(["zorblat kit"])
        wait_for(lambda: self.engine.processes, "the clip process")
        self.assertEqual(threads, ["quill-speech"])
        self.assertEqual([kind for kind, _ in self.engine.played[0]], ["wav"])
        speaker.stop()
        self.assertEqual(self.engine.processes[0].terminated, 1)

    def broken(self, problem):
        """Make the recorded clip of 'zorblat-kit' unusable by ``problem``; returns the expected reason."""
        path = self.record("zorblat-kit")
        if problem == "missing":
            path.unlink()
        elif problem == "corrupt":
            path.write_bytes(b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x10\x00\x00\x00\x01\x00")
        elif problem == "truncated":
            path.write_bytes(path.read_bytes()[:-100])
        elif problem == "not_wav":
            path.write_bytes(b"ID3" + bytes(400))
        elif problem == "oversized":
            path.write_bytes(path.read_bytes() + bytes(C.MAX_CLIP_BYTES))
        elif problem == "too_long":
            wav_file(path, bytes(int(C.MAX_CLIP_S * 16000 + 1) * 2))
        elif problem == "stereo":
            wav_file(path, self.pcm, channels=2)
        elif problem == "8bit":
            wav_file(path, self.pcm, width=1)
        elif problem == "rate":
            wav_file(path, self.pcm, rate=96000)
        elif problem == "float":
            data = bytearray(path.read_bytes())
            data[20:22] = struct.pack("<H", 3)  # WAVE_FORMAT_IEEE_FLOAT
            path.write_bytes(bytes(data))
        elif problem == "empty":
            wav_file(path, b"")
        elif problem == "manifest":
            self.store.manifest_path.write_text("{not json", encoding="utf-8")
        return {"truncated": C.CORRUPT, "not_wav": C.FORMAT, "too_long": C.OVERSIZED, "stereo": C.FORMAT,
                "8bit": C.FORMAT, "rate": C.FORMAT, "float": C.FORMAT, "empty": C.CORRUPT}.get(problem, problem)

    def test_an_unusable_clip_falls_back_to_the_voice_and_logs_a_reason_code_only(self):
        problems = ("missing", "corrupt", "truncated", "not_wav", "oversized", "too_long", "stereo", "8bit", "rate",
                    "float", "empty", "manifest")
        for problem in problems:
            with self.subTest(problem=problem):
                self.setUp()
                self.record("quenta-tree")
                reason = self.broken(problem)
                self.assertIn(reason, C.REASONS)
                with self.assertLogs("quill", level="DEBUG") as logs:
                    self.assertTrue(self.run_due(["zorblat kit", "quenta tree"]))
                played = [item if kind == "say" else "clip" for kind, item in self.engine.played[0]]
                self.assertEqual(played, ["zorblat kit", "quenta tree" if problem == "manifest" else "clip"])
                self.assertEqual(len(logs.records), 1)
                self.assertIn(f"({reason})", logs.output[0])
                self.assert_private("\n".join(logs.output))

    def test_an_unreadable_clip_is_a_reason_code(self):
        self.record("zorblat-kit")
        manifest = self.store.load()
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(C.ClipUnusable) as caught:
                self.store.read_clip(manifest, "zorblat kit")
        self.assertEqual(caught.exception.reason, C.UNREADABLE)
        with mock.patch.object(C.ClipStore, "read_clip", side_effect=C.ClipUnusable(C.UNREADABLE)):
            with self.assertLogs("quill.clips", level="WARNING") as logs:
                self.assertEqual(self.store.clips_for(["zorblat kit"]), [None])
        self.assertEqual(logs.output, ["WARNING:quill.clips:own-voice clip not used (unreadable)"])

    def test_a_name_without_a_clip_logs_nothing(self):
        self.record("zorblat-kit")
        with self.assertNoLogs("quill", level="DEBUG"):
            self.assertTrue(self.run_due(["quenta tree"]))
        self.assertEqual(self.engine.played, [[("say", "quenta tree")]])

    def test_a_failing_lookup_keeps_every_voice_and_logs_its_type(self):
        self.counting.clips_for = lambda names: (_ for _ in ()).throw(RuntimeError("zorblat"))
        with self.assertLogs("quill.speech", level="ERROR") as logs:
            self.assertTrue(self.run_due(["zorblat kit", "quenta tree"]))
        self.assertEqual(self.engine.spoken, [["zorblat kit", "quenta tree"]])
        self.assertEqual(logs.output, ["ERROR:quill.speech:own-voice clips not used (RuntimeError)"])
        self.counting.clips_for = lambda names: [None]  # a wrong number of results is refused too
        with self.assertLogs("quill.speech", level="ERROR"):
            self.assertTrue(self.run_due(["zorblat kit", "quenta tree"]))
        self.assertEqual(self.engine.spoken[-1], ["zorblat kit", "quenta tree"])

    def test_a_failed_engine_start_logs_its_type_only(self):
        self.record("zorblat-kit")
        self.engine.fail = True
        with self.assertLogs("quill.speech", level="ERROR") as logs:
            self.assertFalse(self.run_due(["zorblat kit"]))
        self.assertEqual(logs.output, ["ERROR:quill.speech:speech start failed (OSError)"])

    def test_without_a_store_every_name_is_spoken(self):
        self.record("zorblat-kit")
        speaker = SP.Speaker(self.engine, clock=self.clock, threaded=False)
        speaker.say(["zorblat kit"])
        self.clock.now += SP.CHIME_GAP_S
        self.assertTrue(speaker.tick())
        self.assertEqual(self.engine.played, [[("say", "zorblat kit")]])

    def test_the_clip_count_skips_missing_files(self):
        self.assertEqual(self.store.count(), 0)
        self.record("zorblat-kit")
        self.record("quenta-tree").unlink()
        self.assertEqual(self.store.count(), 1)
        self.store.manifest_path.write_text("[]", encoding="utf-8")
        self.assertEqual(self.store.count(), 0)

    def test_the_real_speaker_gets_the_store(self):
        with mock.patch.object(SP.PowerShellSpeech, "__init__", return_value=None):
            speaker = SP.real_speaker(1.0, 80, self.store)
        self.assertIs(speaker.clips, self.store)

    def test_the_tests_never_read_the_apps_clips_folder(self):
        with self.assertRaises(AssertionError):
            C.default_store()


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
        self.assertEqual((request["probe"], request["segments"]), (True, [{"say": [SP.spoken(SP.EXAMPLE_NAME)]}]))

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
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.local = Path(folder.name).resolve() / "local"
        self.store = ClipStore(self.local / "names", local_root=self.local)

    def parts(self):
        return {"player": self.player, "engine": self.engine, "sleep": self.sleeps.append, "settings": self.settings,
                "clips": self.store}

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

    def test_a_recorded_name_plays_its_clip(self):
        self.store.save_clip("zorblat-kit", tone(), datetime(2026, 1, 1))
        code, out, _ = self.run_main(["--play-sound", "--speak", "--project", "Zorblat Kit"], **self.parts())
        self.assertEqual(code, 0)
        [[(kind, data)]] = self.engine.played
        self.assertEqual(kind, "wav")
        self.assertEqual(wav_samples(data)[3][:2], [550, -550])  # speech_volume 55
        self.assertIn("own-voice clip", out)
        self.assertNotIn("zorblat", self.engine.processes[0].data.decode("utf-8").lower())
        # Another name, or own_voice off, keeps the Windows voice.
        self.run_main(["--play-sound", "--speak", "--project", "quenta-tree"], **self.parts())
        self.settings = ClaudeAlert(speech_rate=1.5, speech_volume=55, own_voice=False)
        code, out, _ = self.run_main(["--play-sound", "--speak", "--project", "zorblat-kit"], **self.parts())
        self.assertEqual(code, 0)
        self.assertEqual(self.engine.played[1:], [[("say", "quenta tree")], [("say", "zorblat kit")]])
        self.assertIn("Windows voice", out)

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
