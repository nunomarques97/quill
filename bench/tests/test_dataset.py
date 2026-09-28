"""Dataset loader tests on invented fixtures written to a temporary folder.

The WAV fixtures are generated tones; no real recording is read and no sound
is played.
"""

import contextlib
import io
import json
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from bench import run
from bench.dataset import DatasetError, load_dataset, load_private_text, parse_script
from bench.settings import SettingsError, load_settings

RATE = 16_000
NAMES = ("zeta-board", "omega")

SCRIPT = """# Invented script

| id | caso | frase | intenção | projeto | ativação |
|----|------|-------|----------|---------|----------|
| pt-01 | a | liga o painel do <projeto-1> com calma | ditar | <projeto-1> | não |
| pt-02 | b | fecha o <projeto-2> antes do jantar | estado | <projeto-2> | sim |
| pt-03 | c | toca a campainha verde | local | — | não |
"""


def tone(seconds, rate=RATE):
    frames = int(seconds * rate)
    return array("h", [300 if i % 2 else -300 for i in range(frames)])


def write_wav(path, samples, rate=RATE, channels=1, width=2):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        if width == 2:
            handle.writeframes(samples.tobytes())
        else:
            handle.writeframes(bytes(len(samples) * width))


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.recordings = root / "recordings"
        self.recordings.mkdir()
        self.script = root / "script.md"
        self.script.write_text(SCRIPT, encoding="utf-8")
        self.config = root / "config.toml"
        self.config.write_text(
            "".join(f'[[projetos]]\nnome = "{name}"\n\n' for name in (*NAMES, "unused")), encoding="utf-8"
        )
        self.entries = {}
        for take_id in ("pt-01", "pt-02", "pt-03"):
            self.add(take_id, tone(1.0))
        write_wav(self.recordings / "pt-01.invalida-20200101-000000.wav", tone(0.2))
        self.write_manifest()
        self.settings_path = root / "bench.toml"
        self.write_settings(3)

    def add(self, take_id, samples, duration=None, mapping=None, **writer):
        write_wav(self.recordings / f"{take_id}.wav", samples, **writer)
        self.entries[take_id] = {
            "ficheiro": f"{take_id}.wav",
            "origem": "microfone",
            "projetos": mapping or {"<projeto-1>": NAMES[0], "<projeto-2>": NAMES[1]},
            "duracao_s": round(len(samples) / RATE, 3) if duration is None else duration,
        }

    def write_manifest(self):
        (self.recordings / "manifesto.json").write_text(
            json.dumps({"versao": 1, "gravacoes": self.entries}), encoding="utf-8"
        )

    def write_settings(self, expected):
        self.settings_path.write_text(
            "[paths]\n"
            f"recordings_dir = {json.dumps(str(self.recordings))}\n"
            f"recording_script = {json.dumps(str(self.script))}\n"
            f"reference_config = {json.dumps(str(self.config))}\n"
            f"[dataset]\nexpected_takes = {expected}\n",
            encoding="utf-8",
        )

    def load(self):
        self.write_manifest()
        return load_dataset(load_settings(self.settings_path))


class DatasetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def reasons(self, dataset):
        return {item.id: item.reason for item in dataset.invalid}

    def test_valid_takes_read_in_place(self):
        before = sorted(p.name for p in self.fx.recordings.iterdir())
        dataset = self.fx.load()
        self.assertEqual([t.id for t in dataset.takes], ["pt-01", "pt-02", "pt-03"])
        self.assertEqual(dataset.invalid, ())
        self.assertEqual(dataset.discarded, 1)
        self.assertTrue(all(t.path.parent == self.fx.recordings for t in dataset.takes))
        self.assertEqual(sorted(p.name for p in self.fx.recordings.iterdir()), before)
        self.assertEqual(dataset.origins, {"microfone": 3})

    def test_placeholders_resolved_per_recording(self):
        self.fx.add("pt-02", tone(1.0), mapping={"<projeto-1>": NAMES[1], "<projeto-2>": NAMES[0]})
        dataset = self.fx.load()
        by_id = {t.id: t for t in dataset.takes}
        self.assertEqual(by_id["pt-01"].reference, "liga o painel do zeta-board com calma")
        self.assertEqual(by_id["pt-02"].reference, "fecha o zeta-board antes do jantar")
        self.assertEqual(by_id["pt-02"].project_names, ("zeta-board",))
        self.assertEqual(by_id["pt-03"].project_names, ())
        self.assertTrue(dataset.placeholders_resolved)
        self.assertEqual(dataset.names, frozenset(NAMES))

    def test_missing_name_is_error(self):
        self.fx.add("pt-01", tone(1.0), mapping={"<projeto-2>": NAMES[1]})
        with self.assertRaises(DatasetError) as ctx:
            self.fx.load()
        self.assertNotIn("zeta", str(ctx.exception))

    def test_unknown_name_is_error_without_echoing_it(self):
        self.fx.add("pt-02", tone(1.0), mapping={"<projeto-1>": NAMES[0], "<projeto-2>": "kappa-x"})
        with self.assertRaises(DatasetError) as ctx:
            self.fx.load()
        self.assertNotIn("kappa", str(ctx.exception))

    def test_repr_hides_text_and_names(self):
        text = repr(self.fx.load())
        self.assertNotIn("painel", text)
        self.assertNotIn("zeta", text)

    def test_wrong_format_is_invalid(self):
        self.fx.add("pt-01", tone(1.0, 44_100), rate=44_100)
        self.fx.add("pt-02", tone(1.0), channels=2)
        self.fx.add("pt-03", tone(1.0), width=1)
        dataset = self.fx.load()
        self.assertEqual(dataset.takes, ())
        self.assertEqual(set(self.reasons(dataset).values()), {"not 16 kHz mono PCM16"})

    def test_digital_zero_run(self):
        head = tone(0.5)
        self.fx.add("pt-01", head + array("h", [0] * 8000) + head)  # exactly 0.5 s: invalid
        self.fx.add("pt-02", head + array("h", [0] * 7999) + head)  # just under: valid
        dataset = self.fx.load()
        self.assertEqual(self.reasons(dataset), {"pt-01": "0.5 s or more of exact digital zeros"})
        self.assertEqual([t.id for t in dataset.takes], ["pt-02", "pt-03"])

    def test_duration_mismatch(self):
        self.fx.add("pt-01", tone(1.0), duration=1.06)
        self.fx.add("pt-02", tone(1.0), duration=1.04)
        dataset = self.fx.load()
        self.assertEqual(self.reasons(dataset), {"pt-01": "duration does not match the manifest"})

    def test_missing_and_unexpected_files(self):
        (self.fx.recordings / "pt-03.wav").unlink()
        write_wav(self.fx.recordings / "pt-99.wav", tone(0.3))
        del self.fx.entries["pt-02"]
        self.fx.entries["pt-07"] = dict(self.fx.entries["pt-01"], ficheiro="pt-07.wav")
        dataset = self.fx.load()
        reasons = self.reasons(dataset)
        self.assertEqual(reasons["pt-03"], "audio file missing")
        self.assertEqual(reasons["pt-02"], "not in manifest")
        self.assertEqual(reasons["pt-07"], "not in recording script")
        self.assertEqual(reasons["pt-99.wav"], "unexpected audio file")
        self.assertEqual([t.id for t in dataset.takes], ["pt-01"])

    def test_manifest_pointing_to_discarded_take(self):
        self.fx.entries["pt-01"]["ficheiro"] = "pt-01.invalida-20200101-000000.wav"
        dataset = self.fx.load()
        self.assertEqual(self.reasons(dataset), {"pt-01": "manifest points to a discarded take"})

    def test_private_text_for_guard(self):
        self.fx.write_manifest()
        texts, names = load_private_text(load_settings(self.fx.settings_path))
        self.assertIn("liga o painel do <projeto-1> com calma", texts)
        self.assertIn("liga o painel do zeta-board com calma", texts)
        self.assertEqual(names, frozenset(NAMES))

    def test_parse_script_rejects_duplicates(self):
        with self.assertRaises(DatasetError):
            parse_script(SCRIPT + "| pt-01 | a | x | y | — | não |\n")
        with self.assertRaises(DatasetError):
            parse_script("no table here")


class SettingsTest(unittest.TestCase):
    def test_example_file_parses(self):
        example = Path(__file__).resolve().parent.parent / "bench.example.toml"
        settings = load_settings(example)
        self.assertEqual(settings.expected_takes, 44)
        self.assertEqual(settings.manifest.name, "manifesto.json")

    def test_missing_file(self):
        with self.assertRaises(SettingsError):
            load_settings(Path(tempfile.gettempdir()) / "does-not-exist-bench.toml")

    def test_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bench.toml"
            path.write_text('[paths]\nrecordings_dir = "x"\n', encoding="utf-8")
            with self.assertRaises(SettingsError):
                load_settings(path)


class DryRunTest(unittest.TestCase):
    def dry_run(self, fx):
        fx.write_manifest()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = run.main(["--dry-run", "--config", str(fx.settings_path)])
        return code, out.getvalue()

    def test_counts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            code, output = self.dry_run(fx)
            self.assertEqual(code, 0)
            self.assertIn("valid takes: 3 (expected 3)", output)
            self.assertIn("placeholders resolved: yes", output)
            for private in ("painel", "zeta", "omega", tmp):
                self.assertNotIn(private, output)

    def test_fails_when_count_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.write_settings(44)
            code, output = self.dry_run(fx)
            self.assertEqual(code, 1)
            self.assertIn("FAIL", output)

    def test_fails_on_unresolved_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(Path(tmp))
            fx.entries["pt-01"]["projetos"] = {}
            code, output = self.dry_run(fx)
            self.assertEqual(code, 1)
            self.assertIn("placeholders resolved: no", output)


if __name__ == "__main__":
    unittest.main()
