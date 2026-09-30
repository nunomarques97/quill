"""Voice-command harness tests with invented data.

Project names, shortcuts, transcriptions and recordings are invented: the
shortcuts are synthetic .lnk files in a temporary folder, the recordings are
generated tones in a temporary folder that stands in for the ignored local/
folder, the streamer is a fake engine and the launcher only records what it
was asked to open. No GPU, microphone, sound or real shortcut is used.
"""

import json
import re
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from bench import record
from bench import settings as S
from bench import voice_commands as V
from bench.dataset import DatasetError, load_dataset, resolve_placeholders
from bench.settings import VOICE_SCRIPT, SettingsError, load_settings
from bench.tests.test_record import FakeWaveIn, ScriptedConsole, samples
from quill import shortcuts
from quill.tests.test_shortcuts import CODE, write_link
from quill.vocabulary import Vocabulary, load_vocabulary
from quill.voice import VOICE_NONE, VOICE_OPEN, default_parser, voice_hints
from quill.whisper import SessionHints

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "bench" / "bench.example.toml"
SUMMARY = REPO / "docs" / "research" / "voice-commands-summary.json"
STEPS = REPO / "docs" / "research" / "GRAVAR-COMANDOS.md"

# The invented mapping of bench.example.toml, with a sibling pair for each "irmão" pair.
NAMES = {
    "<projeto-1>": "nimbus-deck", "<projeto-2>": "orchard", "<projeto-3>": "tidepool", "<projeto-4>": "alfa",
    "<projeto-5>": "alfa-public", "<projeto-6>": "beacon", "<projeto-7>": "beacon-public",
    "<projeto-8>": "ledger-legal", "<projeto-9>": "kite-radar", "<projeto-10>": "quokkai",
    "<projeto-11>": "marble", "<projeto-12>": "harbor", "<projeto-13>": "compass",
}
# Shortcuts of the invented hub: every mapped name, the sibling of "ledger-legal" and an unrelated one.
HUB = sorted(set(NAMES.values()) | {"ledger", "willow"})

SMALL_SCRIPT = """# Invented voice script

| id | caso | frase | intenção | projeto |
|----|------|-------|----------|---------|
| vc-01 | exato | Abre VS Code no <projeto-1>. | abrir | <projeto-1> |
| vc-02 | irmão | Abre VS Code no <projeto-2>. | abrir | <projeto-2> |
| vc-03 | negativo | Abre VS Code no girassol. | nada | — |
"""


def word_tokens(text):
    """An invented token count: one token per word."""
    return len(text.split())


def toml_names(names):
    return "".join(f'"{key}" = "{value}"\n' for key, value in names.items())


class Hub:
    """A temporary shortcut folder with invented VS Code shortcuts."""

    def __init__(self, root: Path, names=HUB):
        self.folder = root / "hub"
        self.folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            write_link(self.folder, name, CODE)

    def folders(self):
        return (self.folder,)


class Case(unittest.TestCase):
    """A temporary root with local/ patched in, an invented hub and a bench config."""

    names = NAMES
    script = None  # None: the committed script

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.local = self.root / "local"
        for module in (record, S):
            patcher = mock.patch.object(module, "LOCAL_DIR", self.local)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.recordings = self.local / "recordings" / "voice"
        self.results = self.root / "results"
        self.hub = Hub(self.root)
        script_line = ""
        if self.script is not None:
            path = self.root / "comandos.md"
            path.write_text(self.script, encoding="utf-8")
            script_line = f'recording_script = "{path.as_posix()}"\n'
        self.config = self.root / "bench.toml"
        self.config.write_text(
            '[paths]\nrecordings_dir = "r"\nrecording_script = "s"\nreference_config = "c"\n'
            f'[dictation]\nrecordings_dir = "{(self.local / "dictation").as_posix()}"\n'
            f'[rewrite]\nrecordings_dir = "{(self.local / "rewrite").as_posix()}"\n'
            f'[voice]\nrecordings_dir = "{self.recordings.as_posix()}"\n{script_line}'
            + ('expected_takes = 3\nmin_takes = 3\n' if self.script is not None else '')
            + '[voice.projects]\n' + toml_names(self.names) + '[recorder]\ndevice = "Invented Mic"\n',
            encoding="utf-8")
        self.fake = FakeWaveIn()

    def voice(self):
        return load_settings(self.config).for_set("voice")

    def rows(self):
        return V.load_voice_rows(self.voice())

    def record(self, take_ids=None):
        """Save generated takes through the recorder's own save path (no microphone)."""
        voice = self.voice()
        rows = [row for row in record.load_script(voice) if take_ids is None or row.id in take_ids]
        session = record.Session(rows=rows, mapping=dict(voice.projects), known=frozenset(self.names.values()),
                                 recordings_dir=voice.recordings_dir, manifest_path=voice.manifest,
                                 capture_factory=None, console=ScriptedConsole([]),
                                 now=lambda: datetime(2026, 1, 2, 3, 4, 5))
        for row in rows:
            session.save(row, samples(1.2), 1.2)

    def spoken(self, row):
        """What a perfect transcription of ``row`` would read, with the invented names."""
        text, _ = resolve_placeholders(row.text, self.names, frozenset(self.names.values()), row.id)
        return text


# ---------------------------------------------------------------- the committed script


class ScriptTest(Case):
    def test_committed_script_shape(self):
        rows = self.rows()
        self.assertEqual([row.id for row in rows], [f"vc-{n:02d}" for n in range(1, 16)])
        cases = {row.case for row in rows}
        self.assertEqual(cases, set(V.CASES))
        negatives = [row for row in rows if row.case == V.NEGATIVE]
        self.assertGreaterEqual(len(negatives), 2)
        self.assertTrue(all(row.action == V.NOTHING and not row.project for row in negatives))
        self.assertGreaterEqual(sum(row.case == "irmão" for row in rows), 2)
        self.assertGreaterEqual(sum(row.case == "vocabulário" for row in rows), 2)
        # Spoken variants of "VS Code" and of the request itself.
        forms = {V.normalize(row.text) for row in rows}
        self.assertTrue(any("visual studio code" in form for form in forms))
        self.assertTrue(any(re.search(r"visual studio no", form) for form in forms))
        self.assertTrue(any(" vs code " in form for form in forms))
        self.assertTrue(any(form.startswith(("podes ", "abre me ")) for form in forms))

    def test_committed_script_names_only_placeholders(self):
        text = VOICE_SCRIPT.read_text(encoding="utf-8")
        for row in self.rows():
            if row.action == V.OPEN:
                self.assertRegex(row.project, r"^<projeto-\d+>$")
        # Every placeholder of the script has an invented name in the example config.
        example = EXAMPLE.read_text(encoding="utf-8").split("# [voice.projects]", 1)[1]
        documented = dict(re.findall(r'^# ("<projeto-\d+>") = "([^"]+)"', example, re.MULTILINE))
        documented = {key.strip('"'): value for key, value in documented.items()}
        self.assertEqual(set(documented), set(V.placeholders(self.rows())))
        self.assertEqual(documented, NAMES)
        self.assertNotIn("\\Users\\", text)

    def test_every_command_does_its_action_through_the_app_parser(self):
        commands = V.MeasuredCommands(self.hub.folders(), Vocabulary())
        for row in self.rows():
            outcome, opened = commands.run(self.spoken(row))
            with self.subTest(row=row.id):
                if row.action == V.OPEN:
                    self.assertEqual((outcome.state, opened), (VOICE_OPEN, self.names[row.project]))
                else:
                    self.assertEqual((outcome.reason, opened), (shortcuts.NO_MATCH, None))
        # Nothing was opened: the recording launcher only kept the requests.
        self.assertEqual(len(commands.launcher.opened), 13)

    def test_the_measured_commands_are_the_apps_commands(self):
        measured = V.MeasuredCommands(self.hub.folders(), Vocabulary())
        app = default_parser(self.hub.folders(), Vocabulary, object())
        self.assertEqual(measured.parser.names, app.names)

    def test_fixture_check(self):
        rows = self.rows()
        listing = shortcuts.list_shortcuts(self.hub.folders())
        check = V.check_fixture(rows, NAMES, listing)
        self.assertTrue(check.ok)
        self.assertEqual((check.named, check.found, check.negatives, check.negatives_clear), (13, 13, 2, 2))
        # A mapped name without a shortcut, or a negative that names one, is a problem (by id only).
        broken = dict(NAMES, **{"<projeto-3>": "nowhere"})
        self.assertEqual(V.check_fixture(rows, broken, listing).problems, ("vc-03",))
        with_negative = shortcuts.list_shortcuts([Hub(self.root / "more", [*HUB, "girassol"]).folder])
        self.assertEqual(V.check_fixture(rows, NAMES, with_negative).problems, ("vc-15",))
        partial = {key: value for key, value in NAMES.items() if key != "<projeto-13>"}
        self.assertFalse(V.check_fixture(rows, partial, listing).ok)


class ParseTest(unittest.TestCase):
    HEADER = "| id | caso | frase | intenção | projeto |\n|---|---|---|---|---|\n"

    def parse(self, line):
        return V.parse_voice_script(self.HEADER + line + "\n")

    def test_valid_rows(self):
        rows = self.parse("| vc-01 | exato | Abre VS Code no <projeto-1>. | abrir | <projeto-1> |")
        self.assertEqual((rows[0].action, rows[0].project), (V.OPEN, "<projeto-1>"))

    def test_invalid_rows(self):
        for line in (
            "| vc-01 | exato | Abre VS Code no lontra. | abrir | <projeto-1> |",  # no placeholder in frase
            "| vc-01 | exato | Abre VS Code no <projeto-1>. | abrir | <projeto-2> |",  # another placeholder
            "| vc-01 | negativo | Abre VS Code no <projeto-1>. | nada | — |",  # a negative with a project
            "| vc-01 | exato | Abre VS Code no lontra. | nada | — |",  # only negatives open nothing
            "| vc-01 | negativo | Abre VS Code no <projeto-1>. | abrir | <projeto-1> |",
            "| vc-01 | outro | Abre VS Code no <projeto-1>. | abrir | <projeto-1> |",  # unknown caso
            "| vc-01 | exato | Abre VS Code no <projeto-1>. | fechar | <projeto-1> |",  # unknown action
        ):
            with self.subTest(line=line), self.assertRaises(DatasetError):
                self.parse(line)


# ---------------------------------------------------------------- settings and recording


class SettingsTest(Case):
    def test_voice_set_defaults_and_names(self):
        voice = self.voice()
        self.assertEqual((voice.name, voice.id_prefix, voice.expected_takes, voice.minimum_takes),
                         ("voice", "vc", 15, 15))
        self.assertEqual(voice.recording_script, VOICE_SCRIPT)
        self.assertEqual(dict(voice.projects), NAMES)
        self.assertTrue(voice.own_names)

    def test_voice_recordings_must_stay_under_local(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace(
            self.recordings.as_posix(), (self.root / "elsewhere").as_posix()), encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.config)

    def test_bad_mapping_is_refused(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace(
            "[recorder]", '"project-x" = "alfa"\n[recorder]'), encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.config)

    def test_example_config_loads_with_the_voice_set(self):
        data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertNotIn("voice", data)  # documented, commented out: the real names stay local

    def test_recorded_names_must_be_mapped_names(self):
        self.record(["vc-01"])
        self.assertEqual([take.project_names for take in load_dataset(self.voice()).takes], [("nimbus-deck",)])
        # A take recorded with a name the mapping no longer has is refused.
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('"nimbus-deck"', '"otter"'),
                               encoding="utf-8")
        with self.assertRaises(DatasetError):
            load_dataset(self.voice())


class RecordTest(Case):
    def speak(self, seconds):
        def answer():
            for _ in range(round(seconds / 0.05)):
                self.fake.advance(0.05)
                self.capture.pump()
            return ""
        return answer

    def test_records_a_voice_take_showing_the_mapped_name(self):
        console = ScriptedConsole(["", self.speak(1.5), "q"])
        original = record.Capture

        def capture(api, index):
            self.capture = original(api, index, background=False, clock=self.fake.clock)
            return self.capture

        with mock.patch.object(record, "Capture", capture):
            code = record.main(["--config", str(self.config), "--set", "voice"], api=self.fake, console=console)
        self.assertEqual(code, 0)
        shown = "\n".join(console.lines)
        self.assertIn("[1/15] vc-01 · — · exato", shown)
        self.assertIn("Abre VS Code no nimbus-deck.", shown)
        self.assertNotIn("<projeto-1>", shown)
        manifest = json.loads((self.recordings / "manifesto.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["gravacoes"]["vc-01"]["projetos"], NAMES)
        self.assertEqual(self.fake.opened, self.fake.closed)
        dataset = load_dataset(self.voice())
        self.assertEqual(([t.id for t in dataset.takes], len(dataset.pending)), (["vc-01"], 14))

    def test_missing_names_stop_before_the_microphone(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('"<projeto-13>" = "compass"\n', ""),
                               encoding="utf-8")
        console = ScriptedConsole([""])
        self.assertEqual(record.main(["--config", str(self.config), "--set", "voice"], api=self.fake,
                                     console=console), 2)
        self.assertEqual(self.fake.opened, 0)
        self.assertIn("[voice.projects]", "\n".join(console.lines))
        self.assertNotIn("compass", "\n".join(console.lines))


# ---------------------------------------------------------------- dry run


class DryRunTest(Case):
    def dry_run(self):
        lines = []
        with mock.patch("bench.audio_mme.WinMM", side_effect=AssertionError("the dry run opened the microphone")):
            code = V.main(["--dry-run", "--config", str(self.config)], out=lines.append,
                          folders=self.hub.folders,
                          streamer_factory=mock.Mock(side_effect=AssertionError("the dry run loaded a model")))
        return code, "\n".join(lines)

    def test_nothing_recorded(self):
        code, shown = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("voice commands script: 15 rows", shown)
        self.assertIn("recorded 0 of 15, pending 15", shown)
        self.assertIn("complete: no", shown)
        self.assertIn("names: 13 of 13", shown)
        self.assertIn("negatives without a shortcut 2 of 2", shown)
        for name in NAMES.values():
            self.assertNotIn(name, shown)

    def test_partial_and_complete(self):
        self.record(["vc-01", "vc-14"])
        code, shown = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("recorded 2 of 15, pending 13", shown)
        self.assertIn("complete: no", shown)
        self.record()
        code, shown = self.dry_run()
        self.assertIn("recorded 15 of 15, pending 0, invalid 0", shown)
        self.assertIn("complete: yes", shown)

    def test_no_shortcut_folders_still_reports_the_counts(self):
        lines = []
        self.assertEqual(V.main(["--dry-run", "--config", str(self.config)], out=lines.append, folders=lambda: ()), 0)
        self.assertIn("complete: no", "\n".join(lines))
        self.assertIn("no [voice_commands] shortcut_dirs", "\n".join(lines))


# ---------------------------------------------------------------- measurement


class FakeStreamer:
    """The fake engine: returns the text set for each take, by take id."""

    def __init__(self, texts):
        self.texts = texts
        self.hints = None
        self.calls = 0
        self.closed = False

    def factory(self, hints):
        self.hints = hints

        def stream(takes):
            self.calls += 1
            return [(self.texts[take.id], 0.4) for take in takes]

        stream.close = self.close
        return stream, {"model": "fake-engine"}

    def close(self):
        self.closed = True


class MeasureTest(Case):
    script = SMALL_SCRIPT
    names = {"<projeto-1>": "nimbus-deck", "<projeto-2>": "alfa-public"}

    def run_main(self, texts, *args):
        streamer = FakeStreamer(texts)
        lines = []
        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text('names = ["nimbus-deck"]\n[variants]\n"nimbus-deck" = ["nimbos deque"]\n',
                              encoding="utf-8")
        code = V.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       *args], streamer_factory=streamer.factory, tokens_factory=lambda: word_tokens,
                      folders=self.hub.folders,
                      results_dir=self.results, out=lines.append)
        return code, lines, streamer, summary

    def test_all_correct(self):
        self.record()
        texts = {"vc-01": "Abre VS Code no nimbos deque.", "vc-02": "Abre o Visual Studio Code na pasta alfa public.",
                 "vc-03": "Abre VS Code no girassol."}
        code, lines, streamer, summary_path = self.run_main(texts)
        self.assertEqual(code, 0)
        self.assertTrue(streamer.closed)
        # Exactly the app's voice hints (from the listed hub and the vocabulary), never the dictation hints.
        vocabulary = load_vocabulary(self.root / "vocabulary.toml")
        self.assertIsInstance(streamer.hints, SessionHints)
        listed = [s.name for s in shortcuts.list_shortcuts(self.hub.folders()).shortcuts]
        self.assertEqual(streamer.hints, voice_hints(listed, vocabulary, tokens=word_tokens))
        self.assertIn("marble", streamer.hints.prompt)
        self.assertEqual(streamer.hints.language, "pt")
        self.assertIn("nimbus deck", streamer.hints.hotwords)
        self.assertIn("nimbos deque", streamer.hints.hotwords)
        self.assertNotIn("Vocabulário", streamer.hints.prompt)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual((summary["status"], summary["takes"], summary["correct"], summary["correct_rate"]),
                         ("measured", 3, 3, 1.0))
        self.assertEqual((summary["wrong_shortcuts"], summary["ambiguous"], summary["unrecognized"],
                          summary["no_match"]), (0, 0, 0, 1))
        self.assertEqual(summary["targets"], {"correct_rate_min": 0.95, "wrong_shortcuts_max": 0})
        self.assertEqual(summary["meets_targets"], {"correct_rate": True, "wrong_shortcuts": True, "all": True})
        self.assertEqual(summary["dataset"], {"script_rows": 3, "recorded": 3, "pending": 0, "invalid": 0})
        # The options record says the run used the voice hints, by kind and size only.
        self.assertEqual(summary["engine"]["model"], "fake-engine")
        self.assertEqual(summary["engine"]["hints"], {
            "kind": "voice", "language": "pt", "prompt_chars": len(streamer.hints.prompt),
            "hotwords_chars": len(streamer.hints.hotwords), "shortcut_names": len(HUB), "token_count": "unknown"})
        serialized = summary_path.read_text(encoding="utf-8")
        for secret in ("nimbus", "alfa", "girassol", "nimbos", "visual studio"):
            self.assertNotIn(secret, serialized.casefold())
        # Spoken text and names only under the results folder.
        run = next((self.results / "voice").iterdir())
        takes = json.loads((run / "takes.json").read_text(encoding="utf-8"))
        self.assertEqual([t["opened"] for t in takes], ["nimbus-deck", "alfa-public", None])
        self.assertIn("| Sponsor |", (run / "table.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((run / "summary.json").read_text(encoding="utf-8"))["correct"], 3)
        self.assertTrue(any("target >= 95.0 %: met" in line for line in lines))
        self.assertFalse(any("nimbus" in line or "alfa" in line for line in lines))

    def test_wrong_ambiguous_and_unrecognized_are_counted(self):
        self.record()
        texts = {"vc-01": "Fecha tudo agora.",  # unrecognized
                 "vc-02": "Abre VS Code no alfa.",  # the sibling: a wrong shortcut
                 "vc-03": "Abre VS Code no willow."}  # a negative that opens a shortcut: wrong
        code, lines, _, summary_path = self.run_main(texts)
        self.assertEqual(code, 0)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual((summary["correct"], summary["wrong_shortcuts"], summary["unrecognized"]), (0, 2, 1))
        self.assertEqual(summary["meets_targets"]["all"], False)
        self.assertTrue(any("NOT met" in line for line in lines))

    def test_incomplete_recordings_measure_nothing(self):
        self.record(["vc-01"])
        code, lines, streamer, summary_path = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIsNone(streamer.hints)  # no model was loaded
        self.assertFalse(summary_path.exists())
        self.assertIn("bench.record --set voice", lines[-1])

    def test_a_hub_that_does_not_fit_measures_nothing(self):
        self.record()
        for link in self.hub.folder.glob("alfa-public.lnk"):
            link.unlink()
        code, lines, streamer, summary_path = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIsNone(streamer.hints)
        self.assertFalse(summary_path.exists())
        self.assertIn("fixture problems (take ids): vc-02", "\n".join(lines))


class DefaultStreamerTest(unittest.TestCase):
    def test_every_take_is_replayed_as_a_voice_session_with_the_hints(self):
        hints = SessionHints(prompt="Abre o VS Code no projeto.", hotwords="zorblax", language="pt")
        seen = []

        def stream_takes(model, takes, vocabulary, options, *, hints=None):
            seen.append((type(model).__name__, model.loaded, list(vocabulary), hints))
            return []

        with mock.patch("bench.streaming.stream_takes", stream_takes):
            stream, options = V.default_streamer(hints)
            stream([])
        self.assertEqual(seen, [("Whisper", False, [], hints)])  # never loaded: the fake stood in
        self.assertEqual(options["model"], "large-v3-turbo")


class AggregateTest(unittest.TestCase):
    def result(self, expected, opened, reason, case="exato"):
        return V.TakeResult("vc-01", case, expected, "invented", reason, opened)

    def test_correct_and_wrong(self):
        results = [
            self.result("alfa", "alfa", shortcuts.OPENED),
            self.result("alfa-public", "alfa", shortcuts.OPENED, "irmão"),
            self.result("alfa", None, shortcuts.AMBIGUOUS),
            self.result(None, None, shortcuts.NO_MATCH, "negativo"),
            self.result(None, "alfa", shortcuts.OPENED, "negativo"),
            self.result("alfa", None, V.NO_SPEECH),
        ]
        summary = V.aggregate(results)
        self.assertEqual((summary["correct"], summary["wrong_shortcuts"], summary["ambiguous"], summary["no_match"]),
                         (2, 2, 1, 1))
        self.assertEqual(summary["by_case"]["negativo"], {"takes": 2, "correct": 1})
        self.assertFalse(summary["meets_targets"]["all"])

    def test_the_target_needs_every_one_of_15(self):
        ok = [self.result("alfa", "alfa", shortcuts.OPENED)] * 15
        self.assertTrue(V.aggregate(ok)["meets_targets"]["all"])
        missed = ok[:14] + [self.result("alfa", None, shortcuts.AMBIGUOUS)]
        summary = V.aggregate(missed)
        self.assertEqual(summary["correct_rate"], 0.9333)
        self.assertEqual(summary["meets_targets"], {"correct_rate": False, "wrong_shortcuts": True, "all": False})

    def test_empty_text_never_runs_a_command(self):
        commands = mock.Mock()
        take = mock.Mock(id="vc-01", project_names=("alfa",))
        row = V.VoiceRow("vc-01", "exato", "Abre VS Code no <projeto-1>.", V.OPEN, "<projeto-1>")
        results = V.measure([take], {"vc-01": row}, [("  ", 0.3)], commands)
        self.assertEqual((results[0].reason, results[0].opened, results[0].correct), (V.NO_SPEECH, None, False))
        commands.run.assert_not_called()

    def test_private_outputs_stay_under_results(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            V.write_private(Path(tmp) / "elsewhere", [], Path(tmp) / "results")


class RecordingLauncherTest(unittest.TestCase):
    def test_folder_targets_are_recorded_not_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "hub"
            project = Path(tmp) / "orchard"
            folder.mkdir()
            project.mkdir()
            write_link(folder, "orchard", str(project), directory=True)
            commands = V.MeasuredCommands([folder], Vocabulary())
            with mock.patch.object(shortcuts, "find_vscode", return_value=Path("C:/Invented/Code.exe")):
                outcome, opened = commands.run("abre vs code no orchard")
        self.assertEqual(opened, "orchard")
        self.assertEqual(outcome.state, VOICE_OPEN)
        self.assertEqual(len(commands.launcher.started), 1)
        self.assertEqual(commands.launcher.opened, [])

    def test_nothing_to_open_shows_the_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Hub(Path(tmp))
            outcome, opened = V.MeasuredCommands(hub.folders(), Vocabulary()).run("abre vs code no zebra")
        self.assertEqual((opened, outcome.state), (None, VOICE_NONE))


# ---------------------------------------------------------------- committed files


class CommittedFilesTest(unittest.TestCase):
    def test_summary_has_the_targets_and_no_text(self):
        summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(summary["kind"], "voice_commands")
        self.assertEqual(summary["targets"], V.targets())
        self.assertIn(summary["status"], ("pending_recordings", "measured"))
        self.assertEqual(summary["dataset"]["script_rows"], 15)
        if summary["status"] == "pending_recordings":
            self.assertIsNone(summary["correct_rate"])
            self.assertLess(summary["dataset"]["recorded"], 15)
        else:
            self.assertEqual(summary["takes"], 15)
            self.assertIsInstance(summary["meets_targets"]["all"], bool)
        for key in ("heard", "expected", "opened", "text", "shown"):
            self.assertNotIn(f'"{key}"', SUMMARY.read_text(encoding="utf-8"))

    def test_recording_steps_are_numbered_and_name_the_commands(self):
        text = STEPS.read_text(encoding="utf-8")
        steps = re.findall(r"^(\d+)\. ", text, re.MULTILINE)
        self.assertEqual(steps, [str(n) for n in range(1, len(steps) + 1)])
        self.assertIn("py -3.12 -m bench.record --set voice", text)
        self.assertIn("py -3.12 -m bench.voice_commands --dry-run", text)


if __name__ == "__main__":
    unittest.main()
