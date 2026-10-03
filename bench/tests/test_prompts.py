"""Claude Code prompts harness tests with invented data.

Project names, domain terms, folders, context packs, transcriptions and
recordings are invented: the recordings are generated tones in a temporary
folder that stands in for the ignored local/ folder, the streamer is a fake
engine and the local model is a scripted fake client. The text pipeline and
the rewriter are the app's own. No GPU, microphone, sound or Ollama is used.
"""

import json
import re
import tempfile
import tomllib
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bench import prompts as P
from bench import record
from bench import settings as S
from bench.dataset import DatasetError, Take, load_dataset
from bench.metrics import write_summary
from bench.settings import PROMPTS_SCRIPT, SettingsError, load_settings
from bench.tests.test_record import FakeWaveIn, ScriptedConsole, samples
from quill import autorewrite, enrich
from quill.app import TextPipeline
from quill.config import EXAMPLE_CONFIG, load_config
from quill.context_pack import ContextPack
from quill.profiles import CLAUDE_CODE, Profiles
from quill.projects import ProjectDetector, ProjectFolders

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "bench" / "bench.example.toml"
SUMMARY = REPO / "docs" / "research" / "prompts-summary.json"
STEPS = REPO / "docs" / "research" / "GRAVAR-PROMPTS.md"

PROJECTS = {"<projeto-1>": "nimbus-deck", "<projeto-2>": "orchard"}
TERMS = {"<termo-1>": "Kwartz", "<termo-2>": "ledgerly", "<termo-3>": "florin"}
# How the fake engine mishears each term; the fake model fixes it only with the pack.
MISHEARD = {"Kwartz": "quartz", "ledgerly": "ledger lee", "florin": "florin"}

SMALL_SCRIPT = """# Invented prompts script

| id | caso | frase | intenção | projeto | termos | estilo |
|----|------|-------|----------|---------|--------|--------|
| pp-01 | termo | Ajusta no <projeto-1> a rotina do <termo-1> para arrancar só depois das oito horas. | pedir | <projeto-1> | <termo-1> | claude-code |
| pp-02 | números | Garante no <projeto-2> que o <termo-2> guarda 12 linhas por página e o <termo-3> fica igual. | pedir | <projeto-2> | <termo-2>, <termo-3> | claude-code |
| pp-03 | restrição | Revê a documentação do <termo-1> no <projeto-1> sem tocares nos ficheiros de configuração. | perguntar | <projeto-1> | <termo-1> | claude-code |
"""

PACKS = {
    "nimbus-deck": ContextPack("A board that plans cloud jobs.", ("Kwartz", "scheduler", "board")),
    "orchard": ContextPack("An accounting ledger for small shops.", ("ledgerly", "florin", "invoice")),
}


def toml_table(mapping):
    return "".join(f'"{key}" = "{value}"\n' for key, value in mapping.items())


def spoken(row, projects=PROJECTS, terms=TERMS, heard=False):
    """The phrase as read (``heard``: as the fake engine mishears its terms)."""
    text = P._fill(row.text, {**projects, **terms})
    if heard:
        for term, wrong in MISHEARD.items():
            text = re.sub(rf"\b{term}\b", wrong, text)
    return text


class ScriptedModel:
    """The local model: fixes a misheard term only when it sees the project's terms; enrichment sorts into parts."""

    def __init__(self, enrich_reply=None):
        self.calls = []
        self.enrich_reply = enrich_reply

    def chat(self, model, system, user, max_tokens=None, history=(), timeout_s=None):
        text = re.search(r"<dictation>\n(.*)\n</dictation>", user, re.S).group(1)
        enriching = system.startswith("You turn one dictated request")
        self.calls.append(SimpleNamespace(enrich=enriching, user=user, timeout_s=timeout_s))
        if enriching:
            reply = self.enrich_reply(text, user) if self.enrich_reply else f"Pedido: {text}"
            return SimpleNamespace(content=reply)
        if "<project_terms>" in user:
            for term, wrong in MISHEARD.items():
                if term in user:
                    text = text.replace(wrong, term)
        return SimpleNamespace(content=text)


def make_product(root, model, packs=PACKS, projects=PROJECTS):
    """The app's text pipeline, project detection and rewriter with invented folders and packs."""
    config = load_config(None, EXAMPLE_CONFIG)
    folders = {}
    for name in projects.values():
        folder = root / "repos" / name
        folder.mkdir(parents=True, exist_ok=True)
        folders[name] = folder
    holder = P.Window()
    detector = ProjectDetector(ProjectFolders(tuple(folders.items())))
    pipeline = TextPipeline(config, vocabulary=_vocabulary(),
                            generic_terms=(), describe=holder.describe, projects=detector)
    settings = replace(config.autorewrite, timeout_s=P.BENCH_TIMEOUT_S, enrich_timeout_s=P.BENCH_TIMEOUT_S)
    rewriter = autorewrite.AutoRewriter(model, "fake-model", settings)
    by_folder = {str(folder): packs.get(name) for name, folder in folders.items()}

    def lookup(folder):
        pack = by_folder.get(str(folder))
        return pack, "found" if pack is not None else "not_git"

    info = {"model": "fake-model", "timeout_s": config.autorewrite.timeout_s,
            "enrich_timeout_s": config.autorewrite.enrich_timeout_s, "bench_timeout_s": P.BENCH_TIMEOUT_S,
            "cleanup": config.cleanup_mode}
    hints_for = P.app_hints(pipeline, lambda folder: by_folder.get(str(folder)), _vocabulary(), ())
    return P.Product(pipeline, holder, rewriter, (), lookup, info, hints_for=hints_for)


def _vocabulary():
    from quill.vocabulary import Vocabulary

    return Vocabulary()


class FakeStreamer:
    """The engine: ``texts`` with the vocabulary hints, ``hinted`` (when given) with a take's own session hints."""

    def __init__(self, texts, hinted=None):
        self.texts = texts
        self.hinted = hinted or {}
        self.hints = None
        self.closed = False
        self.calls = []  # (take ids, session hints or None) of each stream call

    def factory(self, hints):
        self.hints = hints

        def stream(takes, session_hints=None):
            self.calls.append(([take.id for take in takes], session_hints))
            if session_hints is None:
                return [(self.texts[take.id], 0.5) for take in takes]
            return [(self.hinted.get(take.id, self.texts[take.id]) if own is not None else self.texts[take.id], 0.6)
                    for take, own in zip(takes, session_hints, strict=True)]

        stream.close = self.close
        return stream, {"model": "fake-engine"}

    def close(self):
        self.closed = True


class Case(unittest.TestCase):
    """A temporary root with local/ patched in and a bench config with the prompts set."""

    script = SMALL_SCRIPT
    projects = PROJECTS
    terms = TERMS

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.local = self.root / "local"
        for module in (record, S):
            patcher = mock.patch.object(module, "LOCAL_DIR", self.local)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.recordings = self.local / "recordings" / "prompts"
        self.results = self.root / "results"
        script_line = ""
        if self.script is not None:
            path = self.root / "prompts.md"
            path.write_text(self.script, encoding="utf-8")
            script_line = f'recording_script = "{path.as_posix()}"\nexpected_takes = 3\nmin_takes = 3\n'
        self.config = self.root / "bench.toml"
        self.config.write_text(
            '[paths]\nrecordings_dir = "r"\nrecording_script = "s"\nreference_config = "c"\n'
            f'[dictation]\nrecordings_dir = "{(self.local / "dictation").as_posix()}"\n'
            f'[rewrite]\nrecordings_dir = "{(self.local / "rewrite").as_posix()}"\n'
            f'[voice]\nrecordings_dir = "{(self.local / "voice").as_posix()}"\n'
            f'[prompts]\nrecordings_dir = "{self.recordings.as_posix()}"\n{script_line}'
            + '[prompts.projects]\n' + toml_table(self.projects)
            + '[prompts.terms]\n' + toml_table(self.terms) + '[recorder]\ndevice = "Invented Mic"\n',
            encoding="utf-8")
        self.fake = FakeWaveIn()

    def prompts(self):
        return load_settings(self.config).for_set("prompts")

    def rows(self):
        return P.load_prompt_rows(self.prompts())

    def record(self, take_ids=None):
        """Save generated takes through the recorder's own save path (no microphone)."""
        chosen = self.prompts()
        rows = [row for row in record.load_script(chosen) if take_ids is None or row.id in take_ids]
        session = record.Session(rows=rows, mapping=dict(chosen.projects), known=frozenset(self.projects.values()),
                                 recordings_dir=chosen.recordings_dir, manifest_path=chosen.manifest,
                                 capture_factory=None, console=ScriptedConsole([]),
                                 now=lambda: datetime(2026, 1, 2, 3, 4, 5), terms=dict(chosen.terms))
        for row in rows:
            session.save(row, samples(1.2), 1.2)


# ---------------------------------------------------------------- the committed script


class ScriptTest(unittest.TestCase):
    def test_committed_script_shape(self):
        rows = P.parse_prompt_script(PROMPTS_SCRIPT.read_text(encoding="utf-8"))
        self.assertTrue(13 <= len(rows) <= 17)
        self.assertEqual([row.id for row in rows], [f"pp-{n:02d}" for n in range(1, len(rows) + 1)])
        self.assertGreaterEqual(len({row.project for row in rows}), 3)
        self.assertEqual({row.case for row in rows}, set(P.CASES))
        self.assertTrue(all(row.terms for row in rows))
        self.assertGreaterEqual(sum(bool(re.search(r"\d", row.text)) for row in rows), 3)
        # Only placeholders: every term and project is a <...-N>, nothing else between angle brackets.
        for row in rows:
            self.assertEqual(re.findall(r"<[^>]*>", row.text), re.findall(r"<(?:projeto|termo)-\d+>", row.text))
        # Every term placeholder is numbered once for the whole script and belongs to one project.
        owners = {}
        for row in rows:
            for term in row.terms:
                owners.setdefault(term, set()).add(row.project)
        self.assertTrue(all(len(projects) == 1 for projects in owners.values()))

    def test_example_config_documents_the_prompts_set_commented_out(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("prompts", tomllib.loads(text))
        self.assertIn("# [prompts.terms]", text)

    def test_invalid_rows(self):
        header = "| id | caso | frase | intenção | projeto | termos | estilo |\n|--|--|--|--|--|--|--|\n"
        bad = {
            "caso": "| pp-01 | outro | Olha o <termo-1> no <projeto-1>. | pedir | <projeto-1> | <termo-1> | claude-code |",
            "estilo": "| pp-01 | termo | Olha o <termo-1> no <projeto-1>. | pedir | <projeto-1> | <termo-1> | default |",
            "no term": "| pp-01 | termo | Olha o painel no <projeto-1>. | pedir | <projeto-1> | — | claude-code |",
            "two projects": "| pp-01 | termo | Olha o <termo-1> no <projeto-1> e <projeto-2>. | pedir | <projeto-1> "
                            "| <termo-1> | claude-code |",
            "termos": "| pp-01 | termo | Olha o <termo-1> e o <termo-2> no <projeto-1>. | pedir | <projeto-1> "
                      "| <termo-2>, <termo-1> | claude-code |",
        }
        for label, line in bad.items():
            with self.subTest(label), self.assertRaises(DatasetError) as caught:
                P.parse_prompt_script(header + line + "\n")
            self.assertNotIn("Olha", str(caught.exception))
        rows = P.parse_prompt_script(header + "| pp-01 | termo | Olha o <termo-1> e o <termo-2> no <projeto-1>. "
                                     "| pedir | <projeto-1> | <termo-1>, <termo-2> | claude-code |\n")
        self.assertEqual(rows[0].terms, ("<termo-1>", "<termo-2>"))


# ---------------------------------------------------------------- settings, recorder and dataset


class SettingsTest(Case):
    script = None

    def test_prompts_set_defaults_names_and_terms(self):
        chosen = self.prompts()
        self.assertEqual((chosen.name, chosen.id_prefix, chosen.expected_takes, chosen.minimum_takes),
                         ("prompts", "pp", 15, 15))
        self.assertEqual(chosen.recording_script, PROMPTS_SCRIPT)
        self.assertEqual(dict(chosen.projects), PROJECTS)
        self.assertEqual(dict(chosen.terms), TERMS)
        self.assertTrue(chosen.own_names)
        self.assertNotIn("Kwartz", repr(chosen))

    def test_bad_term_mapping_is_refused_without_its_value(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace(
            '"<termo-1>" = "Kwartz"', '"termo-x" = "Kwartz"'), encoding="utf-8")
        with self.assertRaises(SettingsError) as caught:
            load_settings(self.config)
        self.assertNotIn("Kwartz", str(caught.exception))

    def test_recordings_must_stay_under_local(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace(
            self.recordings.as_posix(), (self.root / "elsewhere").as_posix()), encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.config)


class RecordTest(Case):
    def speak(self, seconds):
        def answer():
            for _ in range(round(seconds / 0.05)):
                self.fake.advance(0.05)
                self.capture.pump()
            return ""
        return answer

    def test_records_a_prompt_showing_the_resolved_phrase(self):
        console = ScriptedConsole(["", self.speak(1.5), "q"])
        original = record.Capture

        def capture(api, index):
            self.capture = original(api, index, background=False, clock=self.fake.clock)
            return self.capture

        with mock.patch.object(record, "Capture", capture):
            code = record.main(["--config", str(self.config), "--set", "prompts"], api=self.fake, console=console)
        self.assertEqual(code, 0)
        shown = "\n".join(console.lines)
        self.assertIn("pp-01", shown)
        self.assertIn("rotina do Kwartz para arrancar", shown)
        self.assertIn("no nimbus-deck", shown)
        self.assertNotIn("<termo-", shown)
        self.assertNotIn("<projeto-", shown)
        self.assertEqual(self.fake.opened, self.fake.closed)
        entry = json.loads((self.recordings / "manifesto.json").read_text(encoding="utf-8"))["gravacoes"]["pp-01"]
        self.assertEqual(entry["termos"], TERMS)
        self.assertEqual(entry["projetos"], PROJECTS)
        self.assertTrue((self.recordings / "pp-01.wav").is_file())

    def test_missing_terms_stop_before_the_microphone(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('"<termo-3>" = "florin"\n', ""),
                               encoding="utf-8")
        console = ScriptedConsole([""])
        code = record.main(["--config", str(self.config), "--set", "prompts"], api=self.fake, console=console)
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.opened, 0)
        shown = "\n".join(console.lines)
        self.assertIn("[prompts.terms]", shown)
        self.assertNotIn("Kwartz", shown)


class DatasetTest(Case):
    def test_takes_resolve_names_and_terms_from_their_manifest(self):
        self.record()
        dataset = load_dataset(self.prompts())
        self.assertEqual([take.id for take in dataset.takes], ["pp-01", "pp-02", "pp-03"])
        second = dataset.takes[1]
        self.assertEqual(second.terms, ("ledgerly", "florin"))
        self.assertEqual(second.project_names, ("orchard",))
        self.assertIn("o ledgerly guarda 12 linhas", second.clean)
        self.assertNotIn("<termo-", second.clean)

    def test_a_take_without_its_term_mapping_is_refused(self):
        self.record(["pp-01"])
        path = self.recordings / "manifesto.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["gravacoes"]["pp-01"]["termos"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(DatasetError) as caught:
            load_dataset(self.prompts())
        self.assertIn("pp-01", str(caught.exception))

    def test_private_text_has_raw_and_resolved_phrases_names_and_terms(self):
        self.record(["pp-01"])
        # A take recorded with an older term: its manifest mapping is covered too.
        path = self.recordings / "manifesto.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["gravacoes"]["pp-01"]["termos"]["<termo-1>"] = "Quorbit"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        texts, names, terms = P.private_text(self.prompts())
        self.assertEqual(names, frozenset(PROJECTS.values()))
        self.assertEqual(terms, frozenset(TERMS.values()) | {"Quorbit"})
        joined = "\n".join(texts)
        self.assertIn("rotina do <termo-1> para", joined)
        self.assertIn("rotina do Kwartz para", joined)
        self.assertIn("rotina do Quorbit para", joined)
        self.assertEqual(P.private_text(None), ([], frozenset(), frozenset()))


# ---------------------------------------------------------------- the counts


class WordsTest(unittest.TestCase):
    def test_lost_words_ignore_reordering_and_the_context_part(self):
        reference = "Revê o painel do Kwartz e não mexas nos testes."
        source = "Revê o painel do quartz e não mexas nos testes."
        enriched = "Contexto: painel\nPedido: Revê o painel do Kwartz.\nRestrições: não mexas nos testes."
        self.assertEqual(P.lost_words(reference, source, enriched), 0)
        # "painel" only in the context part: lost. The misheard term was never right, so it is not lost.
        self.assertEqual(P.lost_words(reference, source, "Contexto: painel\nPedido: Revê o do quartz. Não mexas "
                                                         "nos testes."), 1)
        # A negation dropped is a loss.
        self.assertEqual(P.lost_words(reference, source, "Revê o painel do Kwartz e mexe nos testes."), 2)

    def test_invented_words_counted_without_the_product_guard(self):
        source = "Revê o painel do Kwartz e limpa a cache velha."
        pack = ContextPack("A board that plans cloud jobs.", ("scheduler",))
        self.assertEqual(P.invented_words(source, "Pedido: revê o painel do Kwartz e limpa a cache velha.", pack), (0, 0))
        # Structure words and pack words in the context part are allowed.
        self.assertEqual(P.invented_words(source, "Contexto: projeto nimbus-deck, scheduler de cloud jobs.\n"
                                                  "Pedido: revê o painel do Kwartz e limpa a cache velha.", pack,
                                          "nimbus-deck"), (0, 0))
        # A new word, a new number, and a pack word outside the context part.
        self.assertEqual(P.invented_words(source, source + " Usa Docker.", pack), (2, 0))
        self.assertEqual(P.invented_words(source, source + " Em 3 dias.", pack), (2, 0))
        self.assertEqual(P.invented_words(source, "Pedido: revê o painel do Kwartz e limpa a cache velha do "
                                                  "scheduler.", pack), (0, 1))
        # A misheard word the correction replaced with a pack term is not a requirement from the pack.
        heard = "Revê o painel do quartz e limpa a cache velha."
        pack = ContextPack("A board.", ("Kwartz", "scheduler"))
        self.assertEqual(P.invented_words(heard, "Pedido: revê o painel do Kwartz e limpa a cache velha.", pack,
                                          corrected=source), (0, 0))
        self.assertEqual(P.invented_words(heard, "Pedido: revê o painel do Kwartz e limpa a cache velha do scheduler.",
                                          pack, corrected=source), (0, 1))
        # A repeated number beyond its count is invented too.
        self.assertEqual(P.invented_words("Guarda 12 linhas.", "Guarda 12 linhas. Critérios de aceitação: 12.",
                                          None), (1, 0))

    def test_term_errors(self):
        reference = "O Kwartz e o ledgerly e outra vez o Kwartz."
        self.assertEqual(P.term_errors(reference, "O quartz e o ledgerly e outra vez o Kwartz.",
                                       ("Kwartz", "ledgerly", "Kwartz")), (3, 1))
        self.assertEqual(P.term_errors(reference, reference, ("Kwartz", "ledgerly")), (3, 0))

    def test_outside_context(self):
        text = "Objetivo: rever.\nContexto: painel de jobs\n- nuvem\nPedido: limpa a cache velha."
        self.assertEqual(P.outside_context(text), "Objetivo: rever.\nPedido: limpa a cache velha.")
        self.assertEqual(P.outside_context("Sem etiquetas: nada.\nlinha"), "Sem etiquetas: nada.\nlinha")


# ---------------------------------------------------------------- the measurement


class MeasureTest(Case):
    def run_main(self, texts, *args, model=None):
        model = model or ScriptedModel()
        streamer = FakeStreamer(texts)
        lines = []
        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        with mock.patch("bench.audio_mme.WinMM", side_effect=AssertionError("the microphone was opened")):
            code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                           "--set", "prompts", *args], product_factory=lambda vocab, generic: make_product(
                               self.root, model), streamer_factory=streamer.factory, results_dir=self.results,
                          out=lines.append)
        return code, lines, streamer, summary, model

    def heard(self):
        return {row.id: spoken(row, heard=True) for row in self.rows()}

    def test_context_fixes_the_terms_and_enrichment_keeps_every_word(self):
        self.record()
        code, lines, streamer, summary_path, model = self.run_main(self.heard(), "--require")
        self.assertTrue(streamer.closed)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        block = summary["sets"]["prompts"]
        self.assertEqual(block["status"], "measured")
        self.assertEqual((block["takes"], block["projects_detected"], block["packs_found"]), (3, 3, 3))
        # Four term occurrences, three misheard: today's mouse 5 keeps them wrong, the pack fixes them.
        self.assertEqual(block["term_occurrences"], 4)
        self.assertEqual(block["term_errors"], {"pipeline": 3, "today": 3, "new": 0})
        self.assertEqual(block["lost"], {"today": 0, "new": 0})
        self.assertEqual(block["invented"], {"today": 0, "new": 0})
        self.assertEqual(block["pack_outside_context"], 0)
        self.assertEqual(block["enriched"], 3)
        self.assertEqual(block["reasons"]["enrichment"], {enrich.ENRICHED: 3})
        self.assertIsNotNone(block["latency"]["total_p95_s"])
        self.assertEqual(summary["meets_targets"], {"complete": True, "term_errors": True, "lost": True,
                                                    "invented": True, "pack_outside_context": True, "all": True})
        self.assertEqual(code, 0)
        # Today's call never saw a pack or context; the new one did, then asked for the enrichment.
        corrections = [call for call in model.calls if not call.enrich]
        self.assertEqual(sum("<project_terms>" in call.user for call in corrections), 3)
        self.assertEqual(sum(call.enrich for call in model.calls), 3)
        self.assertTrue(all(call.timeout_s == P.BENCH_TIMEOUT_S for call in model.calls))
        # Aggregates only in the summary; text, names and terms only under results/.
        serialized = summary_path.read_text(encoding="utf-8")
        for secret in ("Kwartz", "ledgerly", "florin", "quartz", "nimbus", "orchard", "rotina", "linhas"):
            self.assertNotIn(secret.casefold(), serialized.casefold())
        output = "\n".join(lines)
        for secret in ("Kwartz", "ledgerly", "nimbus", "orchard", "rotina"):
            self.assertNotIn(secret, output)
        run = next((self.results / "prompts").iterdir())
        takes = json.loads((run / "takes.json").read_text(encoding="utf-8"))
        self.assertEqual([t["id"] for t in takes], ["pp-01", "pp-02", "pp-03"])
        self.assertIn("quartz", takes[0]["today"])
        self.assertIn("Kwartz", takes[0]["corrected"])
        self.assertTrue(takes[0]["final"].startswith("Pedido: "))
        examples = (run / "exemplos.md").read_text(encoding="utf-8")
        self.assertIn("Enviado (novo):", examples)
        self.assertIn("Sponsor:", examples)
        self.assertEqual(json.loads((run / "summary.json").read_text(encoding="utf-8"))["sets"], summary["sets"])

    def test_an_invented_enrichment_is_refused_and_counted_zero(self):
        self.record()
        model = ScriptedModel(enrich_reply=lambda text, user: f"Pedido: {text}\nRestrições: usa sempre Docker.")
        code, lines, _, summary_path, _ = self.run_main(self.heard(), model=model)
        self.assertEqual(code, 0)
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        # The product guard refuses it: the corrected text is sent, nothing invented reaches the output.
        self.assertEqual(block["reasons"]["enrichment"], {enrich.REFUSED: 3})
        self.assertEqual(block["enriched"], 0)
        self.assertEqual(block["invented"]["new"], 0)
        self.assertEqual(block["term_errors"]["new"], 0)

    def test_missing_takes_fail_the_requirement(self):
        self.record(["pp-01", "pp-02"])
        code, lines, _, summary_path, _ = self.run_main(self.heard(), "--require")
        self.assertEqual(code, 1)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertFalse(summary["meets_targets"]["complete"])
        self.assertTrue(any(line.startswith("NOT met: prompts set complete") for line in lines))

    def test_no_takes_measure_nothing(self):
        code, lines, streamer, summary_path, _ = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIsNone(streamer.hints)
        self.assertFalse(summary_path.exists())
        self.assertIn("bench.record --set prompts", lines[-1])

    def test_a_summary_that_would_leak_is_refused(self):
        self.record()
        texts = self.heard()
        product = []

        def factory(vocab, generic):
            built = make_product(self.root, ScriptedModel())
            built.info["model"] = "Kwartz"  # a real term in an aggregate field
            product.append(built)
            return built

        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        lines = []
        code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       "--set", "prompts"], product_factory=factory, streamer_factory=FakeStreamer(texts).factory,
                      results_dir=self.results, out=lines.append)
        self.assertEqual(code, 1)
        self.assertFalse(summary.exists())
        self.assertIn("not written", lines[-1])

    def test_check_applies_the_targets_to_a_saved_summary(self):
        self.record()
        _, _, _, summary_path, _ = self.run_main(self.heard())
        lines = []
        self.assertEqual(P.main(["--check", str(summary_path)], out=lines.append), 0)
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        data["sets"]["prompts"]["term_errors"]["new"] = 2  # more than half of today's 3
        data["sets"]["prompts"]["invented"]["new"] = 1
        summary_path.write_text(json.dumps(data), encoding="utf-8")
        lines = []
        self.assertEqual(P.main(["--check", str(summary_path)], out=lines.append), 1)
        self.assertTrue(any(line.startswith("NOT met: domain-term errors") for line in lines))
        self.assertTrue(any(line.startswith("NOT met: invented") for line in lines))


class ProjectHintsTest(Case):
    """The same takes replayed with the decoding hints the app now gives mouse 5, reported before and after."""

    def heard(self):
        return {row.id: spoken(row, heard=True) for row in self.rows()}

    def run_hinted(self, hinted, model=None, packs=PACKS):
        model = model or ScriptedModel()
        streamer = FakeStreamer(self.heard(), hinted)
        lines = []
        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       "--set", "prompts"], product_factory=lambda vocab, generic: make_product(
                           self.root, model, packs=packs), streamer_factory=streamer.factory,
                      results_dir=self.results, out=lines.append)
        return code, lines, streamer, summary, model

    def test_the_project_hints_reach_the_engine_and_the_summary_reports_before_and_after(self):
        self.record()
        right = {row.id: spoken(row) for row in self.rows()}  # with its project's hints the engine hears the terms
        code, lines, streamer, summary_path, model = self.run_hinted(right)
        self.assertEqual(code, 0)
        # Warm-up and today's run with the vocabulary hints, then each take with its own project hints.
        self.assertEqual([hints for _, hints in streamer.calls[:2]], [None, None])
        ids, hints = streamer.calls[2]
        self.assertEqual(ids, ["pp-01", "pp-02", "pp-03"])
        self.assertIn("nimbus-deck", hints[0].prompt)
        self.assertIn("Kwartz", hints[0].hotwords)
        self.assertIn("ledgerly", hints[1].hotwords)
        self.assertNotIn("Kwartz", hints[1].hotwords)  # another project's term never helps this take
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        block = summary["sets"]["prompts"]
        self.assertEqual(block["hints"], {"project_hints": 3})
        self.assertEqual(block["term_errors"], {"pipeline": 0, "today": 3, "new": 0})
        self.assertEqual(block["before_hints"]["term_errors"], {"pipeline": 3, "new": 0})
        self.assertEqual((block["before_hints"]["lost"], block["before_hints"]["invented"]), (0, 0))
        self.assertEqual(block["latency"]["transcription_p50_s"], 0.6)
        self.assertEqual(block["before_hints"]["latency"]["transcription_p50_s"], 0.5)
        self.assertEqual(summary["engine"]["project_hints"]["takes"], 3)
        self.assertTrue(summary["meets_targets"]["term_errors"])
        # Today's mouse 5 is asked once per take, with today's text; the new one again for each changed take.
        corrections = [call for call in model.calls if not call.enrich]
        self.assertEqual(sum("<project_terms>" not in call.user for call in corrections), 3)
        self.assertEqual(sum("<project_terms>" in call.user for call in corrections), 6)
        self.assertTrue(any(line.startswith("  before the project hints:") for line in lines))
        self.assertIn("  decoding hints: project_hints 3", lines)
        serialized = summary_path.read_text(encoding="utf-8")
        for secret in ("Kwartz", "ledgerly", "florin", "quartz", "nimbus", "orchard", "scheduler", "invoice"):
            self.assertNotIn(secret.casefold(), serialized.casefold())
        run = next((self.results / "prompts").iterdir())
        before = json.loads((run / "takes-before.json").read_text(encoding="utf-8"))
        after = json.loads((run / "takes.json").read_text(encoding="utf-8"))
        self.assertIn("quartz", before[0]["heard"])
        self.assertIn("Kwartz", after[0]["heard"])
        self.assertEqual(after[0]["hints"], "project_hints")
        self.assertIn("quartz", after[0]["today"])  # today's mouse 5 kept today's transcription
        self.assertIn("Ouvido com as dicas de hoje", (run / "exemplos.md").read_text(encoding="utf-8"))

    def test_a_take_heard_the_same_is_not_asked_again(self):
        self.record()
        code, _, streamer, summary_path, model = self.run_hinted({})
        self.assertEqual(code, 0)
        self.assertEqual(len(streamer.calls), 3)
        corrections = [call for call in model.calls if not call.enrich]
        self.assertEqual(len(corrections), 6)  # today's and the new mouse 5 once per take
        self.assertEqual(sum(call.enrich for call in model.calls), 3)
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        self.assertEqual(block["term_errors"], {"pipeline": 3, "today": 3, "new": 0})
        self.assertEqual(block["before_hints"]["term_errors"], {"pipeline": 3, "new": 0})

    def test_without_a_pack_every_take_keeps_todays_hints_and_transcription(self):
        self.record()
        code, _, streamer, summary_path, _ = self.run_hinted({row.id: "never heard" for row in self.rows()}, packs={})
        self.assertEqual(code, 0)
        self.assertEqual([hints for _, hints in streamer.calls], [None, None])  # no replay with other hints
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        self.assertEqual(block["hints"], {"no_pack": 3})
        self.assertEqual(block["takes"], 3)
        run = next((self.results / "prompts").iterdir())
        self.assertNotIn("never heard", (run / "takes.json").read_text(encoding="utf-8"))

    def test_take_hints_follow_the_window_of_each_take(self):
        product = make_product(self.root, ScriptedModel())
        takes = [Take("t1", 1.0, "dictation", "", "", Path("t1.wav"), "x", ("orchard",), CLAUDE_CODE),
                 Take("t2", 1.0, "dictation", "", "", Path("t2.wav"), "x", (), CLAUDE_CODE),
                 Take("t3", 1.0, "dictation", "", "", Path("t3.wav"), "x", ("nimbus-deck", "orchard"), CLAUDE_CODE)]
        hints = P.take_hints(takes, product)
        self.assertEqual([reason for _, reason in hints], ["project_hints", "no_project", "no_project"])
        self.assertIn("florin", hints[0][0].hotwords)
        self.assertIsNone(hints[1][0])
        self.assertEqual(P.take_hints(takes, replace(product, hints_for=None)), [(None, "")] * 3)

    def test_a_summary_leaking_a_project_hint_term_is_refused(self):
        self.record()
        packs = {**PACKS, "nimbus-deck": ContextPack("A board.", ("Kwartz", "QuasarSync", "board"))}
        right = {row.id: spoken(row) for row in self.rows()}

        def factory(vocab, generic):
            built = make_product(self.root, ScriptedModel(), packs=packs)
            built.info["model"] = "QuasarSync"  # a pack term that only reached the hints
            return built

        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        lines = []
        code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       "--set", "prompts"], product_factory=factory,
                      streamer_factory=FakeStreamer(self.heard(), right).factory, results_dir=self.results,
                      out=lines.append)
        self.assertEqual(code, 1)
        self.assertFalse(summary.exists())
        self.assertIn("not written", lines[-1])


class DictationTakesTest(unittest.TestCase):
    """The existing claude-code dictation takes: lost, invented and latency, no domain terms."""

    def take(self, take_id, text, style, project="nimbus-deck"):
        return Take(take_id, 3.0, "dictation", "", "", Path(f"{take_id}.wav"), text, (project,), style)

    def test_only_claude_code_takes_are_measured_without_term_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            takes = (self.take("dt-01", "Revê o painel do nimbus-deck e limpa a cache velha todos.", CLAUDE_CODE),
                     self.take("dt-02", "Olá, amanhã falamos com calma sobre isto.", "default"))
            dataset = SimpleNamespace(takes=takes)
            chosen = P.claude_code_takes(dataset)
            self.assertEqual([take.id for take in chosen], ["dt-01"])
            product = make_product(Path(tmp), ScriptedModel())
            results = P.measure("dictation", chosen, {}, [(chosen[0].clean, 0.3)], product)
        block = P.set_block(results, {"script_rows": 1, "recorded": 1, "pending": 0, "invalid": 0},
                            product.info, terms=False)
        self.assertIsNone(block["term_errors"])
        self.assertEqual((block["lost"]["new"], block["invented"]["new"], block["enriched"]), (0, 0, 1))
        self.assertEqual(block["projects_detected"], 1)
        summary = P.build_summary({"dictation": block}, {}, {})
        # Without the prompts set the domain-term target is not measured: never reported as met.
        self.assertFalse(summary["meets_targets"]["term_errors"])
        self.assertFalse(summary["meets_targets"]["all"])
        self.assertTrue(summary["meets_targets"]["lost"])

    def test_an_empty_transcription_types_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            product = make_product(Path(tmp), ScriptedModel())
            take = self.take("dt-01", "Revê o painel do nimbus-deck e limpa a cache velha todos.", CLAUDE_CODE)
            result = P.run_take(take, "dictation", "", "  ", 0.2, product)
        self.assertFalse(result.spoken)
        self.assertEqual(product.rewriter.client.calls, [])


class DryRunTest(Case):
    def dry_run(self, *args, fixture=None):
        lines = []
        forbidden = mock.Mock(side_effect=AssertionError("the dry run loaded a model or Ollama"))
        with mock.patch("bench.audio_mme.WinMM", side_effect=AssertionError("the dry run opened the microphone")):
            code = P.main(["--dry-run", "--config", str(self.config), "--set", "prompts", *args], out=lines.append,
                          product_factory=forbidden, streamer_factory=forbidden,
                          fixture=fixture or self.fixture)
        return code, "\n".join(lines)

    def fixture(self, rows, projects, terms):
        folders = {name: Path(name) for name in projects.values()}
        return P.check_fixture(rows, projects, terms, folders.get, lambda folder: PACKS.get(folder.name),
                               Profiles(load_config(None, EXAMPLE_CONFIG).profiles).select)

    def test_counts_only(self):
        code, shown = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("prompts script: 3 rows; cases termo 1, restrição 1, números 1; 2 projects, 3 domain terms",
                      shown)
        self.assertIn("local mapping: 2 of 2 projects and 3 of 3 terms named", shown)
        self.assertIn("recorded 0 of 3, pending 3", shown)
        self.assertIn("projects: 2 of 2 with a local folder, 2 with a context pack; terms in their project's pack "
                      "3 of 3; simulated window profile claude-code", shown)
        for secret in ("Kwartz", "ledgerly", "florin", "nimbus", "orchard", "rotina"):
            self.assertNotIn(secret, shown)

    def test_require_fails_on_missing_takes_and_passes_when_complete(self):
        self.assertEqual(self.dry_run("--require")[0], 1)
        self.record()
        code, shown = self.dry_run("--require")
        self.assertEqual(code, 0)
        self.assertIn("complete: yes", shown)

    def test_fixture_problems_name_placeholders_only(self):
        def fixture(rows, projects, terms):
            return P.check_fixture(rows, projects, terms, lambda name: Path(name) if name == "orchard" else None,
                                   lambda folder: ContextPack("An accounting ledger.", ("ledgerly",)),
                                   lambda info: CLAUDE_CODE)

        code, shown = self.dry_run(fixture=fixture)
        self.assertEqual(code, 0)
        self.assertIn("fixture problems (placeholders): <projeto-1>, <termo-1>, <termo-3>", shown)
        self.assertNotIn("florin", shown)

    def test_missing_mapping_is_reported_by_count(self):
        self.config.write_text(self.config.read_text(encoding="utf-8").replace('"<termo-2>" = "ledgerly"\n', ""),
                               encoding="utf-8")
        code, shown = self.dry_run()
        self.assertEqual(code, 0)
        self.assertIn("local mapping: 2 of 2 projects and 2 of 3 terms named", shown)
        self.assertNotIn("fixture", shown)


class WindowTest(unittest.TestCase):
    def test_the_simulated_window_is_the_claude_code_panel_in_the_hub_format(self):
        profiles = Profiles(load_config(None, EXAMPLE_CONFIG).profiles)
        info = P.window("nimbus-deck")
        self.assertEqual(info.title, "nimbus-deck | Claude Code - Visual Studio Code [Claude Code]")
        self.assertEqual(profiles.select(info), CLAUDE_CODE)
        self.assertEqual(profiles.select(P.window("")), CLAUDE_CODE)
        # Today's hint takes the whole left part of a hub title.
        self.assertEqual(autorewrite.project_hint(info), "nimbus-deck | Claude Code")


class PrivateOutputTest(unittest.TestCase):
    def test_outputs_with_text_stay_under_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                P.write_private(Path(tmp) / "elsewhere", [], Path(tmp) / "results")

    def test_summary_refuses_a_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                write_summary(Path(tmp) / "s.json", {"note": "o Kwartz"}, [], ["Kwartz"])


class CommittedFilesTest(unittest.TestCase):
    def test_recording_steps_are_numbered_and_name_the_commands(self):
        text = STEPS.read_text(encoding="utf-8")
        steps = re.findall(r"^(\d+)\. ", text, re.MULTILINE)
        self.assertGreaterEqual(len(steps), 5)
        self.assertEqual(steps, [str(n) for n in range(1, len(steps) + 1)])
        for command in ("bench.record --set prompts", "bench.prompts --dry-run", "local/bench.toml",
                        "[prompts.terms]", "[prompts.projects]"):
            self.assertIn(command, text)
        self.assertNotIn("\\Users\\", text)


class ProductTimeoutsTest(Case):
    """--timeouts product: the app's timeouts, the warm-up at each mouse 5 hold, the model left as it is."""

    def test_product_mode_warms_at_each_hold_and_reports_the_first_call(self):
        self.record()
        model = ScriptedModel()
        events = []
        chat = model.chat

        def chat_logged(*args, **kwargs):
            events.append("chat")
            return chat(*args, **kwargs)

        model.chat = chat_logged
        asked = []

        def factory(vocab, generic, **options):
            asked.append(options)
            built = make_product(self.root, model)
            built.info.update(timeouts="product", bench_timeout_s=None, keep_alive="30m", load_wait_s=8.0)
            return replace(built, hold_start=lambda: events.append("hold"), model_loaded=lambda: False)

        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        lines = []
        code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       "--set", "prompts", "--timeouts", "product"], product_factory=factory,
                      streamer_factory=FakeStreamer({row.id: spoken(row, heard=True) for row in self.rows()}).factory,
                      results_dir=self.results, out=lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(asked, [{"product_timeouts": True}])
        # One warm-up per take, before its first model call (today's correction, the new one, the enrichment).
        self.assertEqual(events, ["hold", "chat", "chat", "chat"] * 3)
        data = json.loads(summary.read_text(encoding="utf-8"))
        rewrite = data["rewrite"]
        self.assertEqual((rewrite["timeouts"], rewrite["keep_alive"], rewrite["load_wait_s"], rewrite["bench_timeout_s"]),
                         ("product", "30m", 8.0, None))
        self.assertEqual(rewrite["first_call"]["model_loaded_before"], False)
        self.assertEqual(rewrite["first_call"]["reason"], autorewrite.UNCHANGED)  # the take had no term to fix
        over = data["sets"]["prompts"]["over_product_timeout"]
        self.assertEqual((over["correction_calls"], over["correction"], over["enrichment"]), (6, 0, 0))
        self.assertTrue(any(line.startswith("first model call: ") for line in lines))

    def test_bench_mode_is_unchanged(self):
        self.record()
        asked = []

        def factory(vocab, generic, **options):
            asked.append(options)
            return make_product(self.root, ScriptedModel())

        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       "--set", "prompts"], product_factory=factory,
                      streamer_factory=FakeStreamer({row.id: spoken(row, heard=True) for row in self.rows()}).factory,
                      results_dir=self.results, out=[].append)
        self.assertEqual((code, asked), (0, [{}]))
        rewrite = json.loads(summary.read_text(encoding="utf-8"))["rewrite"]
        self.assertEqual(rewrite["bench_timeout_s"], P.BENCH_TIMEOUT_S)
        self.assertNotIn("keep_alive", rewrite)
        self.assertEqual(rewrite["first_call"]["model_loaded_before"], None)  # not known without model_loaded

    def test_an_unreadable_model_state_is_reported_as_unknown(self):
        product = SimpleNamespace(model_loaded=mock.Mock(side_effect=OSError("down")))
        self.assertIsNone(P.model_state(product))
        self.assertIsNone(P.first_call([], True))


if __name__ == "__main__":
    unittest.main()
