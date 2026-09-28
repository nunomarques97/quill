"""Recorder tests with a fake waveIn: the microphone is never opened and no sound is played.

Phrases and names are invented; recordings are generated samples written to a
temporary folder that stands in for the ignored local/ folder.
"""

import json
import tempfile
import unittest
import wave
from array import array
from datetime import datetime
from pathlib import Path
from unittest import mock

from bench import record
from bench.audio_mme import (
    BYTES_PER_SECOND,
    AudioError,
    Capture,
    CaptureResult,
    MmeBuffer,
    input_devices,
    name_matches,
    select_device,
    take_problem,
)
from bench.dataset import load_dataset, parse_script
from bench.settings import Settings
from bench.tests.test_dictation_dataset import NAMES, SCRIPT


def samples(seconds, amplitude=3000):
    return array("h", [amplitude if i % 2 else -amplitude for i in range(int(seconds * 16_000))]).tobytes()


class FakeWaveIn:
    """Stands in for WinMM: queued buffers are filled by ``advance``."""

    def __init__(self, names=("Invented Mic (USB)", "Other input")):
        self.names = list(names)
        self.now = 0.0
        self.pending = []
        self.opened = self.closed = self.unprepared = 0
        self.fail_open = False
        self.fail_add_after = None
        self.adds = 0
        self.source = lambda seconds: samples(seconds)

    def clock(self):
        return self.now

    # WinMM interface
    def device_count(self):
        return len(self.names)

    def device_name(self, index):
        return self.names[index]

    def open(self, index, rate):
        if self.fail_open:
            raise AudioError("MME open failed (4): invented")
        self.opened += 1
        return f"handle-{index}"

    def new_buffer(self, handle, size):
        return MmeBuffer(size, None, {"done": False, "data": b""})

    def add(self, handle, buffer):
        self.adds += 1
        if self.fail_add_after is not None and self.adds > self.fail_add_after:
            raise AudioError("MME add buffer failed (11): invented")
        buffer.header.update(done=False, data=b"")
        self.pending.append(buffer)

    def is_done(self, buffer):
        return buffer.header["done"]

    def recorded(self, buffer):
        return buffer.header["data"]

    def start(self, handle):
        pass

    def reset(self, handle):
        for buffer in self.pending:
            buffer.header["done"] = True
        self.pending = []

    def unprepare(self, handle, buffer):
        self.unprepared += 1

    def close(self, handle):
        self.closed += 1

    # test control
    def advance(self, seconds, clock_s=None):
        data = self.source(seconds)
        self.now += seconds if clock_s is None else clock_s
        while data and self.pending:
            buffer = self.pending[0]
            room = buffer.size - len(buffer.header["data"])
            buffer.header["data"] += data[:room]
            data = data[room:]
            if len(buffer.header["data"]) == buffer.size:
                buffer.header["done"] = True
                self.pending.pop(0)


class DeviceTest(unittest.TestCase):
    def test_name_matching(self):
        self.assertTrue(name_matches("Microphone (Invented Headset Pro)", "invented headset"))
        # MME truncates to 31 characters: a long enough prefix matches.
        self.assertTrue(name_matches("Microfone (Invented BlackBox V2", "Microfone (Invented BlackBox V2 Pro 2.4)"))
        self.assertFalse(name_matches("Mic", "Microphone array"))
        self.assertFalse(name_matches("", "x"))

    def test_select_device(self):
        names = ["Microfone (Invented BlackBox V2", "Microfone (Webcam)"]
        self.assertEqual(select_device(names, "Microfone (Invented BlackBox V2 Pro 2.4)"), 0)
        with self.assertRaises(AudioError):
            select_device(names, "Nothing like it")
        with self.assertRaises(AudioError):
            select_device(names, "Microfone")

    def test_listing_never_opens(self):
        fake = FakeWaveIn()
        self.assertEqual(input_devices(fake), ["Invented Mic (USB)", "Other input"])
        lines = []
        console = mock.Mock(say=lines.append)
        with mock.patch.object(record, "load_settings", return_value=Settings(Path("r"), Path("s"), Path("c"), Path("m"), recorder_device="invented mic")):
            self.assertEqual(record.main(["--list-devices"], api=fake, console=console), 0)
        self.assertEqual(fake.opened, 0)
        self.assertTrue(any("0: Invented Mic (USB)" in line and "configured" in line for line in lines))
        self.assertFalse(any("Other input" in line and "configured" in line for line in lines))


class CaptureTest(unittest.TestCase):
    def test_collects_in_order_and_releases(self):
        fake = FakeWaveIn()
        fake.source = lambda seconds: bytes(range(256)) * int(seconds * BYTES_PER_SECOND / 256)
        capture = Capture(fake, 0, buffer_s=0.05, buffers=4, background=False, clock=fake.clock)
        capture.start()
        for _ in range(10):
            fake.advance(0.032)  # not a multiple of the buffer size
            capture.pump()
        fake.advance(0.01)
        result = capture.stop()
        expected = b"".join(fake.source(0.032) for _ in range(10)) + fake.source(0.01)
        self.assertEqual(result.pcm, expected)
        self.assertAlmostEqual(result.elapsed_s, 0.33)
        self.assertEqual((fake.opened, fake.closed, fake.unprepared), (1, 1, 4))
        self.assertFalse(capture.active)
        with self.assertRaises(AudioError):
            capture.stop()

    def test_open_or_queue_failure_releases(self):
        fake = FakeWaveIn()
        fake.fail_open = True
        with self.assertRaises(AudioError):
            Capture(fake, 0, background=False).start()
        fake = FakeWaveIn()
        fake.fail_add_after = 2
        capture = Capture(fake, 0, buffers=4, background=False)
        with self.assertRaises(AudioError):
            capture.start()
        self.assertEqual((fake.opened, fake.closed), (1, 1))
        self.assertFalse(capture.active)

    def test_background_thread_error_is_reported(self):
        fake = FakeWaveIn()
        capture = Capture(fake, 0, buffers=2, background=True, poll_s=0.001)
        capture.start()
        fake.fail_add_after = fake.adds
        fake.advance(0.2)
        capture._thread.join(timeout=2)
        with self.assertRaises(AudioError):
            capture.stop()
        self.assertEqual(fake.closed, 1)


class TakeProblemTest(unittest.TestCase):
    def test_good_take(self):
        self.assertIsNone(take_problem(CaptureResult(samples(3.0), 3.1)))

    def test_rejections(self):
        zeros = samples(1.0) + bytes(int(0.5 * BYTES_PER_SECOND)) + samples(1.0)
        cases = {
            "zeros": CaptureResult(zeros, 2.5),
            "behind": CaptureResult(samples(2.0), 3.0),
            "faster": CaptureResult(samples(3.0), 2.0),
            "level": CaptureResult(samples(3.0, amplitude=100), 3.0),
            "short": CaptureResult(samples(0.5), 0.5),
        }
        for word, result in cases.items():
            self.assertIn(word, take_problem(result), word)
        almost = samples(1.0) + bytes(int(0.49 * BYTES_PER_SECOND)) + samples(1.0)
        self.assertIsNone(take_problem(CaptureResult(almost, 2.49)))


class ScriptedConsole:
    def __init__(self, answers):
        self.answers = list(answers)
        self.lines = []

    def say(self, text=""):
        self.lines.append(text)

    def ask(self, prompt):
        self.lines.append(prompt)
        answer = self.answers.pop(0)
        return answer() if callable(answer) else answer


class SessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.local = self.root / "local"
        self.recordings = self.local / "recordings" / "dictation"
        patcher = mock.patch.object(record, "LOCAL_DIR", self.local)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.fake = FakeWaveIn()
        self.rows = parse_script(SCRIPT, "dt", markup=True)
        self.mapping = {"<projeto-1>": NAMES[0], "<projeto-2>": NAMES[1]}
        self.config = self.root / "config.toml"
        self.config.write_text("".join(f'[[projetos]]\nnome = "{n}"\n\n' for n in NAMES), encoding="utf-8")

    def session(self, answers, recordings=None):
        self.console = ScriptedConsole(answers)
        return record.Session(
            rows=self.rows,
            mapping=self.mapping,
            known=frozenset(NAMES),
            recordings_dir=recordings or self.recordings,
            manifest_path=(recordings or self.recordings) / "manifesto.json",
            capture_factory=self.new_capture,
            console=self.console,
            now=lambda: datetime(2026, 1, 2, 3, 4, 5),
        )

    def new_capture(self):
        self.capture = Capture(self.fake, 0, background=False, clock=self.fake.clock)
        return self.capture

    def speak(self, seconds, clock_s=None, amplitude=3000):
        """Feed ``seconds`` of audio in 50 ms steps, collecting like the pump thread."""
        def answer():
            self.fake.source = lambda s: samples(s, amplitude=amplitude)
            steps = round(seconds / 0.05)
            for _ in range(steps):
                self.fake.advance(0.05, None if clock_s is None else clock_s / steps)
                self.capture.pump()
            return ""
        return answer

    def silent(self, seconds):
        return self.speak(seconds, amplitude=20)

    def test_record_redo_skip_reject_quit_and_resume(self):
        answers = [
            "", self.speak(2.0), "r",            # dt-01 saved, then repeated
            "", self.speak(3.0), "",             # dt-01 again, next
            "s",                                 # dt-02 skipped
            "", self.silent(2.0),                # dt-03 near-silent: rejected
            "", self.speak(2.0, clock_s=3.0),    # dt-03 behind the clock: rejected
            "", self.speak(2.5), "q",            # dt-03 saved, quit
        ]
        report = self.session(answers).run()
        self.assertEqual((report.saved, report.skipped, report.rejected, report.quit), (["dt-01", "dt-03"], ["dt-02"], 2, True))
        files = sorted(p.name for p in self.recordings.iterdir())
        self.assertEqual(files, ["dt-01.invalida-1.wav", "dt-01.wav", "dt-03.wav", "manifesto.json"])
        manifest = json.loads((self.recordings / "manifesto.json").read_text(encoding="utf-8"))
        entry = manifest["gravacoes"]["dt-01"]
        self.assertEqual((entry["ficheiro"], entry["duracao_s"], entry["substituiu"]), ("dt-01.wav", 3.0, "dt-01.invalida-1.wav"))
        self.assertEqual(entry["projetos"], self.mapping)
        self.assertEqual(entry["gravado_em"], "2026-01-02T03:04:05")
        with wave.open(str(self.recordings / "dt-01.wav"), "rb") as handle:
            self.assertEqual((handle.getframerate(), handle.getnchannels(), handle.getsampwidth()), (16_000, 1, 2))
        shown = "\n".join(self.console.lines)
        self.assertIn("hum… abre o painel do zeta-board e corre os… corre os testes", shown)
        self.assertNotIn("<projeto-", shown)
        self.assertIn("Gravadas 2 de 4", shown)
        self.assertEqual(self.fake.opened, self.fake.closed)

        # The loader accepts what the recorder wrote.
        settings = Settings(self.recordings, self.root / "s.md", self.config, self.recordings / "manifesto.json",
                            expected_takes=4, name="dictation", id_prefix="dt", min_takes=2, markup=True, allow_pending=True)
        (self.root / "s.md").write_text(SCRIPT, encoding="utf-8")
        dataset = load_dataset(settings)
        self.assertEqual(([t.id for t in dataset.takes], dataset.pending, dataset.discarded), (["dt-01", "dt-03"], ("dt-02", "dt-04"), 1))

        # Resume starts at the first take not recorded yet.
        resumed = self.session(["q"])
        self.assertEqual([row.id for row in resumed.queue()], ["dt-02", "dt-04"])
        self.assertEqual([row.id for row in resumed.queue(["dt-03"])], ["dt-03"])

    def test_redo_keeps_every_previous_take(self):
        self.session(["", self.speak(1.5), ""] + ["q"]).run(["dt-04"])
        self.session(["", self.speak(1.5), "", ]).run(["dt-04"])
        self.session(["", self.speak(1.5), "", ]).run(["dt-04"])
        names = sorted(p.name for p in self.recordings.glob("dt-04*"))
        self.assertEqual(names, ["dt-04.invalida-1.wav", "dt-04.invalida-2.wav", "dt-04.wav"])

    def test_unknown_redo_and_outside_local(self):
        with self.assertRaises(record.DatasetError):
            self.session([]).queue(["dt-99"])
        with self.assertRaises(record.SettingsError):
            self.session([], recordings=self.root / "elsewhere")

    def test_all_recorded(self):
        self.session(["", self.speak(1.5), "", "", self.speak(1.5), "", "", self.speak(1.5), "", "", self.speak(1.5), ""]).run()
        console_session = self.session([])
        console_session.run()
        self.assertIn("Todas as frases do guião já estão gravadas.", self.console.lines)


if __name__ == "__main__":
    unittest.main()
