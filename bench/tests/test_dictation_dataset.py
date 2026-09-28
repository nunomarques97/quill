"""Dictation set tests: markup, placeholders, pending, invalid and discarded takes.

Every phrase, name and recording here is invented; WAV fixtures are generated
tones written to a temporary folder. No sound is played.
"""

import json
import tempfile
import unittest
from array import array
from pathlib import Path

from bench.dataset import (
    DatasetError,
    dictation_projects,
    display_text,
    clean_text,
    load_dataset,
    load_dictation_private_text,
    parse_markup,
    parse_script,
    strip_markup,
    verbatim_text,
)
from bench.metrics import removal_counts
from bench.settings import DICTATION_RECORDINGS, LOCAL_DIR, Settings, SettingsError, load_settings
from bench.tests.test_dataset import tone, write_wav

NAMES = ("zeta-board", "omega")
SCRIPT = """# Invented dictation script

| id | caso | frase | intenção | projeto | ativação | estilo |
|----|------|-------|----------|---------|----------|--------|
| dt-01 | curto | {hum} abre o painel do <projeto-1> e [corre os] corre os testes de deploy. | ditar | <projeto-1> | não | claude-code |
| dt-02 | médio | Olá, {pronto} amanhã levo o bolo verde. | ditar | — | não | whatsapp |
| dt-03 | longo | {tipo} o commit do <projeto-2> está pronto para review, {é pá} obrigado. | ditar | <projeto-2> | não | email |
| dt-04 | médio | [fecha a] fecha a janela azul do terminal. | ditar | — | não | vscode |
"""


class MarkupTest(unittest.TestCase):
    def test_segments_and_texts(self):
        segments = parse_markup("{hum} abre o [corre os] corre os testes, {tipo} já.")
        self.assertEqual([s.kind for s in segments], ["filler", "content", "repetition", "content", "filler", "content"])
        self.assertEqual(verbatim_text(segments), "hum abre o corre os corre os testes, tipo já.")
        self.assertEqual(clean_text(segments), "abre o corre os testes, já.")
        self.assertEqual(display_text(segments), "hum… abre o corre os… corre os testes, tipo… já.")

    def test_multiword_filler_and_strip(self):
        self.assertEqual(strip_markup("{é pá} liga a luz"), ("é pá liga a luz", "liga a luz"))

    def test_invalid_markup_errors_name_the_id_only(self):
        cases = {
            "{uau} liga a luz": "unknown filler",
            "liga {hum a luz": "unbalanced",
            "liga ] a luz": "unbalanced",
            "[desliga a] liga a luz": "repetition must be followed",
            "[liga a]": "repetition must be followed",
            "{hum}": "no content words",
            "[ ] liga": "empty repetition",
        }
        for text, message in cases.items():
            with self.assertRaises(DatasetError, msg=text) as caught:
                parse_markup(text, "dt-09")
            self.assertIn("dt-09", str(caught.exception))
            self.assertIn(message, str(caught.exception))
            self.assertNotIn("liga", str(caught.exception))

    def test_removal_counts_from_segments(self):
        segments = [(s.kind, s.text) for s in parse_markup("{hum} abre o [corre os] corre os testes")]
        full = removal_counts(segments, "abre o corre os testes")
        self.assertEqual((full.spans, full.removed, full.content_words, full.content_deleted), (2, 2, 5, 0))
        verbatim = removal_counts(segments, "hum abre o corre os corre os testes")
        self.assertEqual((verbatim.removed, verbatim.content_deleted), (0, 0))
        lossy = removal_counts(segments, "abre o testes")
        self.assertEqual((lossy.removed, lossy.content_deleted), (2, 2))


class ScriptTest(unittest.TestCase):
    def test_prefix_style_and_markup(self):
        rows = parse_script(SCRIPT, "dt", markup=True)
        self.assertEqual([r.id for r in rows], ["dt-01", "dt-02", "dt-03", "dt-04"])
        self.assertEqual(rows[0].style, "claude-code")
        self.assertEqual(rows[3].style, "vscode")

    def test_other_prefix_rejected(self):
        with self.assertRaises(DatasetError):
            parse_script(SCRIPT, "pt")
        with self.assertRaises(DatasetError):
            parse_script(SCRIPT.replace("dt-02", "dt-01"), "dt")

    def test_bad_markup_in_script(self):
        with self.assertRaises(DatasetError):
            parse_script(SCRIPT.replace("{pronto}", "{uau}"), "dt", markup=True)

    def test_committed_script(self):
        from bench.metrics import load_terms
        from bench.normalize import normalize_words
        from bench.settings import DICTATION_SCRIPT

        rows = parse_script(DICTATION_SCRIPT.read_text(encoding="utf-8"), "dt", markup=True)
        self.assertEqual([r.id for r in rows], [f"dt-{n:02d}" for n in range(1, 37)])
        self.assertEqual({r.style for r in rows}, {"claude-code", "vscode", "whatsapp", "email"})
        kinds = {s.kind for r in rows for s in parse_markup(r.text, r.id)}
        self.assertEqual(kinds, {"content", "filler", "repetition"})
        self.assertTrue(any("<projeto-" in r.text for r in rows))
        text = " ".join(normalize_words(" ".join(verbatim_text(parse_markup(r.text)) for r in rows)))
        used = [t for t in load_terms() if " " + " ".join(normalize_words(t)) + " " in f" {text} "]
        self.assertGreaterEqual(len(used), 20)
        for row in rows:
            words = len(normalize_words(verbatim_text(parse_markup(row.text))))
            self.assertTrue(12 <= words <= 75, row.id)  # about 5-30 s of speech


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.recordings = root / "dictation"
        self.script = root / "guiao.md"
        self.script.write_text(SCRIPT, encoding="utf-8")
        self.config = root / "config.toml"
        self.config.write_text("".join(f'[[projetos]]\nnome = "{n}"\n\n' for n in (*NAMES, "unused")), encoding="utf-8")
        self.entries = {}

    def settings(self, projects=None, min_takes=2):
        return Settings(
            recordings_dir=self.recordings,
            recording_script=self.script,
            reference_config=self.config,
            manifest=self.recordings / "manifesto.json",
            expected_takes=4,
            name="dictation",
            id_prefix="dt",
            min_takes=min_takes,
            markup=True,
            allow_pending=True,
            projects=projects,
        )

    def add(self, take_id, samples=None, mapping=None, file_name=None):
        self.recordings.mkdir(exist_ok=True)
        samples = samples if samples is not None else tone(1.0)
        write_wav(self.recordings / (file_name or f"{take_id}.wav"), samples)
        self.entries[take_id] = {
            "ficheiro": file_name or f"{take_id}.wav",
            "origem": "microfone",
            "projetos": mapping or {"<projeto-1>": NAMES[0], "<projeto-2>": NAMES[1]},
            "duracao_s": round(len(samples) / 16_000, 3),
        }
        (self.recordings / "manifesto.json").write_text(json.dumps({"gravacoes": self.entries}), encoding="utf-8")


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_nothing_recorded_is_all_pending(self):
        dataset = load_dataset(self.fx.settings())
        self.assertEqual((len(dataset.takes), len(dataset.invalid), dataset.script_rows), (0, 0, 4))
        self.assertEqual(dataset.pending, ("dt-01", "dt-02", "dt-03", "dt-04"))

    def test_missing_script_is_structural(self):
        self.fx.script.unlink()
        with self.assertRaises(DatasetError):
            load_dataset(self.fx.settings())

    def test_valid_pending_invalid_and_discarded(self):
        self.fx.add("dt-01")
        self.fx.add("dt-03")
        zeros = tone(1.0)
        zeros[1000:9500] = array("h", [0] * 8500)
        self.fx.add("dt-04", zeros)
        write_wav(self.fx.recordings / "dt-02.invalida-1.wav", tone(0.5))
        dataset = load_dataset(self.fx.settings())
        self.assertEqual([t.id for t in dataset.takes], ["dt-01", "dt-03"])
        self.assertEqual(dataset.pending, ("dt-02",))
        self.assertEqual(dataset.discarded, 1)
        self.assertEqual([i.id for i in dataset.invalid], ["dt-04"])
        self.assertIn("zeros", dataset.invalid[0].reason)
        first = dataset.takes[0]
        self.assertEqual(first.reference, "hum abre o painel do zeta-board e corre os corre os testes de deploy.")
        self.assertEqual(first.clean, "abre o painel do zeta-board e corre os testes de deploy.")
        self.assertEqual(first.project_names, ("zeta-board",))
        self.assertEqual(first.style, "claude-code")
        self.assertEqual([s.kind for s in first.segments].count("filler"), 1)
        self.assertEqual(dataset.names, frozenset(NAMES))
        self.assertIn(first.clean, dataset.reference_texts())

    def test_discarded_file_in_manifest_is_invalid(self):
        self.fx.add("dt-02", file_name="dt-02.invalida-1.wav")
        dataset = load_dataset(self.fx.settings())
        self.assertEqual([(i.id, i.reason) for i in dataset.invalid], [("dt-02", "manifest points to a discarded take")])
        self.assertEqual(dataset.discarded, 1)

    def test_unexpected_files_and_unknown_ids(self):
        self.fx.add("dt-01")
        self.fx.add("dt-09")
        write_wav(self.fx.recordings / "pt-01.wav", tone(0.5))
        reasons = {i.id: i.reason for i in load_dataset(self.fx.settings()).invalid}
        self.assertEqual(reasons["dt-09"], "not in recording script")
        self.assertEqual(reasons["pt-01.wav"], "unexpected audio file")

    def test_unknown_or_missing_name_is_an_error(self):
        self.fx.add("dt-01", mapping={"<projeto-1>": "invented-other"})
        with self.assertRaises(DatasetError) as caught:
            load_dataset(self.fx.settings())
        self.assertNotIn("invented-other", str(caught.exception))
        self.fx.entries.clear()
        self.fx.add("dt-03", mapping={"<projeto-1>": NAMES[0]})
        with self.assertRaises(DatasetError):
            load_dataset(self.fx.settings())

    def test_projects_from_settings_then_commands_manifest(self):
        settings = self.fx.settings(projects=(("<projeto-1>", NAMES[1]),))
        self.assertEqual(dictation_projects(settings), {"<projeto-1>": NAMES[1]})
        commands_manifest = self.fx.root / "commands.json"
        commands_manifest.write_text(json.dumps({"projetos": {"<projeto-1>": NAMES[0], "other": "x"}}), encoding="utf-8")
        commands = Settings(self.fx.root, self.fx.script, self.fx.config, commands_manifest)
        self.assertEqual(dictation_projects(self.fx.settings(), commands), {"<projeto-1>": NAMES[0]})
        with self.assertRaises(DatasetError):
            dictation_projects(self.fx.settings())
        with self.assertRaises(DatasetError):
            dictation_projects(self.fx.settings(projects=(("<projeto-1>", "invented-other"),)))

    def test_private_text_has_raw_resolved_verbatim_and_clean(self):
        settings = self.fx.settings(projects=(("<projeto-1>", NAMES[0]), ("<projeto-2>", NAMES[1])))
        texts, names = load_dictation_private_text(settings)
        self.assertEqual(names, frozenset(NAMES))
        self.assertIn("abre o painel do zeta-board e corre os testes de deploy.", texts)
        self.assertIn("hum abre o painel do <projeto-1> e corre os corre os testes de deploy.", texts)
        self.assertIn("Olá, amanhã levo o bolo verde.", texts)


class SettingsTest(unittest.TestCase):
    def write(self, root, extra=""):
        path = Path(root) / "bench.toml"
        path.write_text(
            '[paths]\nrecordings_dir = "r"\nrecording_script = "s.md"\nreference_config = "c.toml"\n' + extra,
            encoding="utf-8",
        )
        return path

    def test_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            settings = load_settings(self.write(root, '[recorder]\ndevice = "Invented Mic"\n'))
        dictation = settings.dictation
        self.assertEqual((settings.id_prefix, settings.markup, settings.allow_pending), ("pt", False, False))
        self.assertEqual((dictation.id_prefix, dictation.expected_takes, dictation.minimum_takes), ("dt", 36, 30))
        self.assertEqual(dictation.recordings_dir, DICTATION_RECORDINGS)
        self.assertTrue(dictation.markup and dictation.allow_pending)
        self.assertEqual(settings.recorder_device, "Invented Mic")
        self.assertIs(settings.for_set("dictation"), dictation)
        self.assertIs(settings.for_set("commands"), settings)
        with self.assertRaises(SettingsError):
            settings.for_set("other")

    def test_overrides_and_validation(self):
        with tempfile.TemporaryDirectory() as root:
            ok = load_settings(self.write(root, '[dictation]\nid_prefix = "dd"\nmin_takes = 3\nexpected_takes = 5\n'
                                          '[dictation.projects]\n"<projeto-1>" = "zeta-board"\n'))
            self.assertEqual((ok.dictation.id_prefix, ok.dictation.minimum_takes), ("dd", 3))
            self.assertEqual(ok.dictation.projects, (("<projeto-1>", "zeta-board"),))
            bad = (
                '[dictation]\nrecordings_dir = "elsewhere"\n',
                '[dictation]\nid_prefix = "D1"\n',
                '[dictation]\nmin_takes = 40\n',
                '[dictation.projects]\nfoo = "zeta-board"\n',
                '[recorder]\ndevice = ""\n',
            )
            for extra in bad:
                with self.assertRaises(SettingsError, msg=extra):
                    load_settings(self.write(root, extra))
            inside = load_settings(self.write(root, f'[dictation]\nrecordings_dir = "{(LOCAL_DIR / "x").as_posix()}"\n'))
            self.assertEqual(inside.dictation.recordings_dir, LOCAL_DIR / "x")


if __name__ == "__main__":
    unittest.main()
