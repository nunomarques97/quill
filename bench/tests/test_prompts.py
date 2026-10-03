"""Claude Code prompts harness tests with invented data.

Project names, domain terms, folders, context packs, transcriptions and
recordings are invented: the recordings are generated tones in a temporary
folder that stands in for the ignored local/ folder, the streamer is a fake
engine and the local model is a scripted fake client. The text pipeline and
the rewriter are the app's own. No GPU, microphone, sound or Ollama is used.
"""

import contextlib
import io
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
from quill.whisper import SessionHints

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


def make_product(root, model, packs=PACKS, projects=PROJECTS, likely_terms=autorewrite.LIKELY_TERMS, names=(),
                 common_sense=None):
    """The app's text pipeline, project detection and rewriter with invented folders and packs.

    ``names`` are the personal-vocabulary names the rewriter's name fixes read; ``common_sense`` overrides the
    example config's common-sense fixes (None: the config's, off).
    """
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
    rewriter = autorewrite.AutoRewriter(model, "fake-model", settings, likely_terms=likely_terms,
                                        names=lambda: names, common_sense_fixes=common_sense)
    by_folder = {str(folder): packs.get(name) for name, folder in folders.items()}

    def lookup(folder):
        pack = by_folder.get(str(folder))
        return pack, "found" if pack is not None else "not_git"

    info = {"model": "fake-model", "timeout_s": config.autorewrite.timeout_s,
            "enrich_timeout_s": config.autorewrite.enrich_timeout_s, "bench_timeout_s": P.BENCH_TIMEOUT_S,
            "cleanup": config.cleanup_mode}
    hints_for = P.app_hints(pipeline, lambda folder: by_folder.get(str(folder)), _vocabulary(), ())
    return P.Product(pipeline, holder, rewriter, tuple(names), lookup, info, hints_for=hints_for)


def _vocabulary():
    from quill.vocabulary import Vocabulary

    return Vocabulary()


class FakeStreamer:
    """The engine: ``texts`` with the vocabulary hints, ``hinted`` (when given) with a take's own project hints and
    ``heard`` (when given) with its heard-term hint source (otherwise as with its project hints).

    A hint source is asked as the product's session asks it: at once with nothing heard, then after each partial
    (here each growing prefix of the words that pass hears); the switches it chooses are returned with the text.
    """

    def __init__(self, texts, hinted=None, heard=None):
        self.texts = texts
        self.hinted = hinted or {}
        self.heard = heard or {}
        self.hints = None
        self.closed = False
        self.calls = []  # (take ids, session hints or None) of each stream call
        self.asked = {}  # take id -> what its hint source was asked, in order
        self.chosen = {}  # take id -> the hints its source chose last

    def factory(self, hints):
        self.hints = hints

        def stream(takes, session_hints=None):
            self.calls.append(([take.id for take in takes], session_hints))
            if session_hints is None:
                return [(self.texts[take.id], 0.5) for take in takes]
            out = []
            for take, own in zip(takes, session_hints, strict=True):
                if own is None:
                    out.append((self.texts[take.id], 0.5))
                elif isinstance(own, SessionHints):
                    out.append((self.hinted.get(take.id, self.texts[take.id]), 0.6))
                else:
                    heard = self.heard.get(take.id, self.hinted.get(take.id, self.texts[take.id]))
                    words = heard.split()
                    self.asked[take.id] = [" ".join(words[:n]) for n in range(len(words) + 1)]
                    chosen = [own(text) for text in self.asked[take.id]]
                    self.chosen[take.id] = chosen[-1]
                    out.append((heard, 0.7, sum(a != b for a, b in zip(chosen, chosen[1:]))))
            return out

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
        self.assertEqual(block["tidied"], {"replies": 0, "enriched": 0})
        self.assertEqual(block["invented"]["new"], 0)
        self.assertEqual(block["term_errors"]["new"], 0)

    def test_a_reply_tidied_before_the_guard_is_counted(self):
        self.record()
        model = ScriptedModel(enrich_reply=lambda text, user: f"Pedido: {text}\nRestrições: nenhuma\nCritérios de "
                                                               "aceitação:")
        code, lines, _, summary_path, _ = self.run_main(self.heard(), model=model)
        self.assertEqual(code, 0)
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        self.assertEqual((block["enriched"], block["enrichment_requests"]), (3, 3))
        self.assertEqual(block["tidied"], {"replies": 3, "enriched": 3})
        self.assertEqual((block["lost"]["new"], block["invented"]["new"], block["pack_outside_context"]), (0, 0, 0))
        self.assertTrue(any("enriched 3 of 3" in line for line in lines))
        run = next((self.results / "prompts").iterdir())
        takes = json.loads((run / "takes.json").read_text(encoding="utf-8"))
        self.assertEqual([t["enrich_tidied"] for t in takes], [2, 2, 2])
        self.assertTrue(all("Restrições" not in t["final"] for t in takes))

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

    def run_hinted(self, hinted, model=None, packs=PACKS, heard=None):
        model = model or ScriptedModel()
        streamer = FakeStreamer(self.heard(), hinted, heard)
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
        # Warm-up and today's run with the vocabulary hints, then each take with its Phase 7 project hints,
        # then with the app's heard-term hint source.
        self.assertEqual(len(streamer.calls), 4)
        self.assertEqual([hints for _, hints in streamer.calls[:2]], [None, None])
        ids, hints = streamer.calls[2]
        self.assertEqual(ids, ["pp-01", "pp-02", "pp-03"])
        self.assertTrue(all(isinstance(h, SessionHints) for h in hints))
        self.assertIn("nimbus-deck", hints[0].prompt)
        self.assertIn("Kwartz", hints[0].hotwords)
        self.assertIn("ledgerly", hints[1].hotwords)
        self.assertNotIn("Kwartz", hints[1].hotwords)  # another project's term never helps this take
        ids, sources = streamer.calls[3]
        self.assertEqual(ids, ["pp-01", "pp-02", "pp-03"])
        self.assertTrue(all(callable(h) and not isinstance(h, SessionHints) for h in sources))
        # The relevance pass is the source's hints with nothing heard: the Phase 7 hints (a used source
        # remembers what its session heard, so its initial hints).
        self.assertEqual([source.initial for source in sources], hints)
        self.assertEqual(streamer.asked["pp-01"][0], "")
        self.assertTrue(streamer.chosen["pp-01"].hotwords.startswith("Kwartz"))  # heard, so first
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        block = summary["sets"]["prompts"]
        self.assertEqual(block["hints"], {"project_hints": 3})
        # The target still compares the new mouse 5 with today's.
        self.assertEqual(block["term_errors"], {"pipeline": 0, "today": 3, "new": 0})
        self.assertEqual(block["relevance_hints"]["term_errors"], {"pipeline": 0, "new": 0})
        self.assertEqual(block["today_hints"]["term_errors"], {"pipeline": 3, "new": 0})
        for name in ("relevance_hints", "today_hints"):
            self.assertEqual((block[name]["lost"], block[name]["invented"], block[name]["pack_outside_context"]),
                             (0, 0, 0))
            self.assertEqual((block[name]["enrichment_requests"], block[name]["enriched"],
                              block[name]["enrichment_refused"]), (3, 3, 0))
        self.assertEqual((block["enrichment_requests"], block["enriched"], block["enrichment_refused"]), (3, 3, 0))
        # Kwartz in both nimbus-deck takes; ledgerly, then florin in the orchard take.
        self.assertEqual(block["heard_hints"], {"sources": 3, "takes_switched": 3, "switches": 4, "heard_changed": 0})
        self.assertEqual(block["latency"]["transcription_p50_s"], 0.7)
        self.assertEqual(block["relevance_hints"]["latency"]["transcription_p50_s"], 0.6)
        self.assertEqual(block["today_hints"]["latency"]["transcription_p50_s"], 0.5)
        self.assertIsNotNone(block["relevance_hints"]["latency"]["total_p95_s"])
        self.assertEqual(summary["engine"]["project_hints"]["takes"], 3)
        self.assertTrue(summary["meets_targets"]["term_errors"])
        # Today's mouse 5 is asked once per take, with today's text; the new one again for each changed take,
        # and not again for the heard pass, which hears each take as the relevance pass did.
        corrections = [call for call in model.calls if not call.enrich]
        self.assertEqual(sum("<project_terms>" not in call.user for call in corrections), 3)
        self.assertEqual(sum("<project_terms>" in call.user for call in corrections), 6)
        report = "\n".join(lines)
        self.assertIn("  before (relevance hints): domain-term errors pipeline 0, final 0; lost 0, invented 0, pack "
                      "words outside context 0; enrichment requested 3, accepted 3, refused 0; p50/p95 s: "
                      "transcription 0.6 / 0.6, correction ", report)
        self.assertIn("  after (heard hints): domain-term errors pipeline 0, final 0;", report)
        self.assertIn("  with today's hints: domain-term errors pipeline 3, final 0;", report)
        self.assertIn("  heard-term hints: switched in 3 of 3 takes (4 switches); heard otherwise than before 0",
                      lines)
        self.assertIn("  decoding hints: project_hints 3", lines)
        serialized = summary_path.read_text(encoding="utf-8")
        for secret in ("Kwartz", "ledgerly", "florin", "quartz", "nimbus", "orchard", "scheduler", "invoice"):
            self.assertNotIn(secret.casefold(), serialized.casefold())
            self.assertNotIn(secret.casefold(), report.casefold())
        run = next((self.results / "prompts").iterdir())
        today = json.loads((run / "takes-today.json").read_text(encoding="utf-8"))
        before = json.loads((run / "takes-before.json").read_text(encoding="utf-8"))
        after = json.loads((run / "takes.json").read_text(encoding="utf-8"))
        self.assertIn("quartz", today[0]["heard"])
        self.assertIn("Kwartz", before[0]["heard"])
        self.assertIn("Kwartz", after[0]["heard"])
        self.assertEqual((before[0]["hints"], before[0]["hint_switches"]), ("project_hints", 0))
        self.assertEqual([t["hint_switches"] for t in after], [1, 2, 1])
        self.assertIn("quartz", after[0]["today"])  # today's mouse 5 kept today's transcription
        examples = (run / "exemplos.md").read_text(encoding="utf-8")
        self.assertIn("Ouvido com as dicas de hoje", examples)
        self.assertNotIn("Ouvido com os termos do projeto da fase 7", examples)  # heard the same

    def test_the_heard_pass_is_measured_against_the_relevance_pass(self):
        self.record()
        right = {row.id: spoken(row) for row in self.rows()}
        # The Phase 7 hints hear as today; only the heard-term hints hear the terms.
        code, lines, streamer, summary_path, model = self.run_hinted({}, heard=right)
        self.assertEqual(code, 0)
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        self.assertEqual(block["term_errors"], {"pipeline": 0, "today": 3, "new": 0})
        self.assertEqual(block["relevance_hints"]["term_errors"], {"pipeline": 3, "new": 0})
        self.assertEqual(block["heard_hints"]["heard_changed"], 3)
        # The relevance pass reuses today's run (heard the same); the heard pass asks the new mouse 5 again.
        corrections = [call for call in model.calls if not call.enrich]
        self.assertEqual(sum("<project_terms>" not in call.user for call in corrections), 3)
        self.assertEqual(sum("<project_terms>" in call.user for call in corrections), 6)
        self.assertEqual(sum(call.enrich for call in model.calls), 6)
        self.assertIn("  heard-term hints: switched in 3 of 3 takes (4 switches); heard otherwise than before 3",
                      lines)
        run = next((self.results / "prompts").iterdir())
        self.assertIn("Ouvido com os termos do projeto da fase 7", (run / "exemplos.md").read_text(encoding="utf-8"))

    def test_a_heard_pass_heard_as_today_reuses_todays_run(self):
        self.record()
        right = {row.id: spoken(row) for row in self.rows()}
        code, _, _, summary_path, model = self.run_hinted(right, heard=self.heard())
        self.assertEqual(code, 0)
        corrections = [call for call in model.calls if not call.enrich]
        self.assertEqual(len(corrections), 9)  # today's, then the new one for today's and the relevance pass only
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        self.assertEqual(block["term_errors"], {"pipeline": 3, "today": 3, "new": 0})
        self.assertEqual(block["relevance_hints"]["term_errors"], {"pipeline": 0, "new": 0})
        self.assertEqual(block["heard_hints"]["heard_changed"], 3)
        run = next((self.results / "prompts").iterdir())
        after = json.loads((run / "takes.json").read_text(encoding="utf-8"))
        self.assertEqual([t["asr_s"] for t in after], [0.7, 0.7, 0.7])  # this pass's time, the earlier mouse 5

    def test_a_take_heard_the_same_is_not_asked_again(self):
        self.record()
        code, _, streamer, summary_path, model = self.run_hinted({})
        self.assertEqual(code, 0)
        self.assertEqual(len(streamer.calls), 4)
        corrections = [call for call in model.calls if not call.enrich]
        self.assertEqual(len(corrections), 6)  # today's and the new mouse 5 once per take
        self.assertEqual(sum(call.enrich for call in model.calls), 3)
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        self.assertEqual(block["term_errors"], {"pipeline": 3, "today": 3, "new": 0})
        self.assertEqual(block["relevance_hints"]["term_errors"], {"pipeline": 3, "new": 0})
        self.assertEqual(block["today_hints"]["term_errors"], {"pipeline": 3, "new": 0})
        self.assertEqual(block["heard_hints"]["heard_changed"], 0)

    def test_without_a_pack_every_take_keeps_todays_hints_and_transcription(self):
        self.record()
        code, _, streamer, summary_path, _ = self.run_hinted({row.id: "never heard" for row in self.rows()}, packs={})
        self.assertEqual(code, 0)
        self.assertEqual([hints for _, hints in streamer.calls], [None, None])  # no replay with other hints
        block = json.loads(summary_path.read_text(encoding="utf-8"))["sets"]["prompts"]
        self.assertEqual(block["hints"], {"no_pack": 3})
        self.assertEqual(block["heard_hints"], {"sources": 0, "takes_switched": 0, "switches": 0, "heard_changed": 0})
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


    def test_a_summary_leaking_a_term_only_the_heard_hints_can_hold_is_refused(self):
        self.record()
        # Distinctive terms enough to fill the project part: the last one is never in the Phase 7 hints.
        filler = tuple(f"QuasarSync{chr(65 + n)}" for n in range(12))
        packs = {**PACKS, "nimbus-deck": ContextPack("A board.", ("Kwartz", *filler, "ZephyrLink"))}
        right = {row.id: spoken(row) for row in self.rows()}
        built = []

        def factory(vocab, generic):
            built.append(make_product(self.root, ScriptedModel(), packs=packs))
            built[-1].info["model"] = "ZephyrLink"  # a pack term only a heard-term source may put in the hints
            return built[-1]

        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        lines = []
        code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       "--set", "prompts"], product_factory=factory,
                      streamer_factory=FakeStreamer(self.heard(), right).factory, results_dir=self.results,
                      out=lines.append)
        source, _ = built[0].hints_for(P.window("nimbus-deck"))
        self.assertNotIn("ZephyrLink", source.initial.hotwords)
        self.assertEqual(code, 1)
        self.assertFalse(summary.exists())
        self.assertIn("not written", lines[-1])

    def test_relevance_and_heard_hints_of_a_take(self):
        product = make_product(self.root, ScriptedModel())
        source, _ = product.hints_for(P.window("orchard"))
        self.assertIs(P.heard_source(source), source)
        self.assertEqual(P.relevance_hints(source), source.initial)
        plain = SessionHints(prompt="Vocabulário: orchard.", hotwords="orchard")
        self.assertIs(P.relevance_hints(plain), plain)
        self.assertIsNone(P.heard_source(plain))
        self.assertIsNone(P.relevance_hints(None))
        self.assertIsNone(P.heard_source(None))
        self.assertEqual(P.relevance_hints(lambda heard: plain), plain)  # a source without initial hints
        self.assertEqual(P.hint_terms(source), {"orchard", "ledgerly", "florin", "invoice"})

    def test_streamed_output_with_and_without_switches(self):
        self.assertEqual(P.split_streamed([("a", 0.5), ("b", 0.7, 2)]), ([("a", 0.5), ("b", 0.7)], [0, 2]))
        self.assertEqual(P.split_streamed([]), ([], []))


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


class LikelyModel(ScriptedModel):
    """The local model: fixes a misheard term only when the correction prompt lists it as sounding like the text."""

    def chat(self, model, system, user, max_tokens=None, history=(), timeout_s=None):
        if system.startswith("You turn one dictated request"):
            return super().chat(model, system, user, max_tokens, history, timeout_s)
        text = re.search(r"<dictation>\n(.*)\n</dictation>", user, re.S).group(1)
        self.calls.append(SimpleNamespace(enrich=False, user=user, timeout_s=timeout_s))
        listed = re.search(r"<likely_terms>\n(.*)\n</likely_terms>", user)
        for term in (listed.group(1).split(", ") if listed else ()):
            if term in MISHEARD:
                text = text.replace(MISHEARD[term], term)
        return SimpleNamespace(content=text)


class CorrectionCandidatesTest(Case):
    """The terms the correction prompt lists as sounding like the text: off as in the app, on as an ablation."""

    def run_main(self, *args):
        model = LikelyModel()
        asked = []

        def factory(vocab, generic, **options):
            asked.append(options)
            return make_product(self.root, model,
                                likely_terms=options.get("correction_candidates", autorewrite.LIKELY_TERMS))

        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        lines = []
        code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                       "--set", "prompts", *args], product_factory=factory,
                      streamer_factory=FakeStreamer({row.id: spoken(row, heard=True) for row in self.rows()}).factory,
                      results_dir=self.results, out=lines.append)
        self.assertEqual(code, 0)
        return json.loads(summary.read_text(encoding="utf-8")), asked, model, lines

    def test_off_by_default_as_in_the_app(self):
        self.record()
        summary, asked, model, lines = self.run_main()
        self.assertEqual(asked, [{}])
        self.assertIs(summary["rewrite"]["correction_candidates"], False)
        block = summary["sets"]["prompts"]
        self.assertEqual(block["term_errors"], {"pipeline": 3, "today": 3, "new": 3})
        self.assertEqual(block["likely_terms"], {"takes": 0, "terms": 0})
        self.assertFalse(any("<likely_terms>" in call.user for call in model.calls))
        self.assertFalse(any(line.startswith("correction candidates:") for line in lines))

    def test_on_the_listed_terms_are_fixed_and_counted(self):
        self.record()
        summary, asked, model, lines = self.run_main("--correction-candidates")
        self.assertEqual(asked, [{"correction_candidates": True}])
        self.assertIs(summary["rewrite"]["correction_candidates"], True)
        block = summary["sets"]["prompts"]
        # Three misheard terms, one per take, each listed (written terms are not): all fixed.
        self.assertEqual(block["term_errors"], {"pipeline": 3, "today": 3, "new": 0})
        self.assertEqual(block["likely_terms"], {"takes": 3, "terms": 3})
        self.assertEqual(block["relevance_hints"]["likely_terms"], {"takes": 3, "terms": 3})
        self.assertEqual((block["lost"]["new"], block["invented"]["new"]), (0, 0))
        new = [call for call in model.calls if not call.enrich and "<project_terms>" in call.user]
        self.assertEqual(sum("<likely_terms>" in call.user for call in new), 3)
        # Today's correction (no context mode) never lists them.
        today = [call for call in model.calls if not call.enrich and "<project_terms>" not in call.user]
        self.assertTrue(today and not any("<likely_terms>" in call.user for call in today))
        self.assertIn("  correction prompts listing terms that sound like the dictation: 3 (3 terms)", lines)
        self.assertIn("correction candidates: on (ablation)", lines)
        serialized = json.dumps(summary)
        for secret in ("Kwartz", "ledgerly", "quartz", "nimbus", "orchard"):
            self.assertNotIn(secret.casefold(), serialized.casefold())

    def test_off_explicitly_is_the_default_and_no_ablation(self):
        self.record()
        summary, asked, model, lines = self.run_main("--no-correction-candidates")
        self.assertEqual(asked, [{"correction_candidates": False}])
        self.assertIs(summary["rewrite"]["correction_candidates"], False)
        self.assertFalse(any("<likely_terms>" in call.user for call in model.calls))
        self.assertFalse(any(line.startswith("correction candidates:") for line in lines))

    def test_both_switches_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            P.main(["--correction-candidates", "--no-correction-candidates"], out=lambda line: None)
        self.assertEqual(raised.exception.code, 2)

    def test_the_ablation_combines_with_product_timeouts(self):
        self.record()
        _, asked, _, _ = self.run_main("--correction-candidates", "--timeouts", "product")
        self.assertEqual(asked, [{"product_timeouts": True, "correction_candidates": True}])

    def test_the_report_names_only_a_switch_away_from_the_app(self):
        for value, expected in ((True, ["correction candidates: on (ablation)"]), (False, []), (None, [])):
            with self.subTest(value=value):
                summary = {"sets": {}, "rewrite": {"correction_candidates": value}}
                self.assertEqual([line for line in P.report_lines(summary)
                                  if line.startswith("correction candidates:")], expected)


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


# ---------------------------------------------------------------- Phase 9: correction variants and the safety set


VOCABULARY_NAME = "verja"  # an invented lower-case vocabulary name; the engine writes it "Verza"
# Generic misheard groups (what was said, what the engine wrote): fixed only by common sense.
COMMON_GROUPS = {"tu decides": "tudo cedo", "modelo local": "museu local"}

VARIANT_SCRIPT = """# Invented prompts script with a vocabulary name and a misheard group

| id | caso | frase | intenção | projeto | termos | estilo |
|----|------|-------|----------|---------|--------|--------|
| pp-01 | termo | Abre no <projeto-1> o repositório do verja e corre os testes do <termo-1> antes de publicar. | pedir | <projeto-1> | <termo-1> | claude-code |
| pp-02 | termo | Olha, tu decides no <projeto-2> qual é a melhor opção para o <termo-2> de testes. | perguntar | <projeto-2> | <termo-2> | claude-code |
| pp-03 | restrição | Revê a documentação do <termo-1> no <projeto-1> sem tocares nos ficheiros de configuração. | perguntar | <projeto-1> | <termo-1> | claude-code |
"""


def misheard(text):
    """``text`` as the fake engine writes it: its terms, the vocabulary name and the common groups misheard."""
    for term, wrong in MISHEARD.items():
        text = re.sub(rf"\b{term}\b", wrong, text)
    text = re.sub(rf"\b{VOCABULARY_NAME}\b", "Verza", text)
    for said, wrong in COMMON_GROUPS.items():
        text = text.replace(said, wrong)
    return text


class SenseModel(ScriptedModel):
    """The local model: fixes the pack's terms as ScriptedModel, and a misheard group only under the common-sense
    rule. It never fixes a name itself: only the rewriter's name pre-step does."""

    def chat(self, model, system, user, max_tokens=None, history=(), timeout_s=None):
        reply = super().chat(model, system, user, max_tokens, history, timeout_s)
        if self.calls[-1].enrich:
            return reply
        self.calls[-1].system = system
        text = reply.content
        if autorewrite.COMMON_SENSE_RULE in system:
            for said, wrong in COMMON_GROUPS.items():
                text = text.replace(wrong, said)
        return SimpleNamespace(content=text)


class VariantsCase(Case):
    script = VARIANT_SCRIPT

    def run_main(self, *args, model=None, load=None):
        model = model or SenseModel()
        asked = []

        def factory(vocab, generic, **options):
            asked.append(options)
            return make_product(self.root, model, names=(VOCABULARY_NAME,), common_sense=options.get("common_sense"))

        streamer = FakeStreamer({**{row.id: misheard(spoken(row)) for row in self.rows()}, **self.extra_heard()})
        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        lines = []
        patch = mock.patch.object(P, "load_sets", load) if load is not None else contextlib.nullcontext()
        with patch, mock.patch("bench.audio_mme.WinMM", side_effect=AssertionError("the microphone was opened")):
            code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                           "--set", "prompts", *args], product_factory=factory, streamer_factory=streamer.factory,
                          results_dir=self.results, out=lines.append)
        data = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else None
        return code, data, lines, model, asked, streamer

    def extra_heard(self):
        return {}

    def run_dir(self):
        return next((self.results / "prompts").iterdir())


class VariantsTest(VariantsCase):
    """--variants: the Phase 8, names-only and common-sense corrections on the transcripts of the app's hint pass."""

    def test_each_variant_on_the_same_transcripts(self):
        self.record()
        code, summary, lines, model, asked, streamer = self.run_main("--variants")
        self.assertEqual((code, asked), (0, [{}]))
        block = summary["sets"]["prompts"]
        variants = block["variants"]
        self.assertEqual(list(variants), [P.PHASE8, P.NAMES, P.COMMON_SENSE])
        # The app's settings (example config): name fixes on, common sense off; that variant is the main run.
        self.assertEqual(block["variant_app"], P.NAMES)
        self.assertEqual((summary["rewrite"]["name_fixes"], summary["rewrite"]["common_sense_fixes"]), (True, False))
        # The vocabulary name once and the project name in each take: the Phase 8 correction keeps the name wrong.
        self.assertEqual([variants[v]["name_occurrences"] for v in variants], [4, 4, 4])
        self.assertEqual([variants[v]["name_errors"] for v in variants],
                         [{"source": 1, "corrected": 1}, {"source": 1, "corrected": 0}, {"source": 1, "corrected": 0}])
        self.assertEqual([variants[v]["names_fixed"] for v in variants], [0, 1, 1])
        self.assertEqual([variants[v]["names_prestep"] for v in variants], [0, 1, 1])
        self.assertEqual([variants[v]["term_errors"] for v in variants], [{"pipeline": 3, "new": 0}] * 3)
        # Only common sense fixes the misheard group: fewer word errors against the reference, nothing invented.
        source = variants[P.PHASE8]["word_errors"]["source"]
        corrected = [variants[v]["word_errors"]["corrected"] for v in variants]
        self.assertEqual(source, 7)  # the name, two misheard groups of the terms, the misheard group of two words
        self.assertEqual(corrected, [3, 2, 0])
        self.assertEqual([variants[v]["common_sense_fixes"] for v in variants], [0, 0, 1])
        # The ordinary word the common-sense fix brought is the one the reference holds; nothing beyond it.
        self.assertEqual([variants[v]["invented_fixes"] for v in variants], [0, 0, 1])
        self.assertLess(variants[P.COMMON_SENSE]["wer"]["corrected"], variants[P.NAMES]["wer"]["corrected"])
        for name, view in variants.items():
            with self.subTest(name):
                self.assertEqual((view["lost"], view["invented_beyond_fixes"], view["invented_reference"],
                                  view["pack_outside_context"]), (0, 0, 0, 0))
                self.assertEqual(view["invented"], view["invented_fixes"])
                self.assertEqual((view["takes"], view["enrichment_requests"], view["enriched"]), (3, 3, 3))
                self.assertIsNotNone(view["latency"]["correction_p95_s"])
        # The main block carries the same reference counts as its variant.
        self.assertEqual(block["against_reference"]["word_errors"], variants[P.NAMES]["word_errors"])
        self.assertEqual(block["against_reference"]["names_fixed"], 1)
        self.assertEqual(summary["common_sense_rule"]["met"], True)
        self.assertEqual(summary["common_sense_rule"]["word_errors"], {"off": 2, "on": 0})
        self.assertEqual(summary["meets_targets"]["lost"], True)
        # The names variant is the main run, not asked again: the Phase 8 and common-sense ones ask once per take.
        context = [call for call in model.calls if not call.enrich and "<project_terms>" in call.user]
        self.assertEqual(len(context), 9)
        self.assertEqual(sum(autorewrite.NAME_RULE in call.system for call in context), 6)
        self.assertEqual(sum(autorewrite.COMMON_SENSE_RULE in call.system for call in context), 3)
        # Each variant runs on the heard pass's transcripts (the streamer replays nothing more).
        self.assertEqual(len(streamer.calls), 4)
        # Counts only in the summary and the report; per-take text under results/.
        serialized = json.dumps(summary).casefold()
        report = "\n".join(lines).casefold()
        for secret in ("verja", "verza", "tudo cedo", "decides", "kwartz", "quartz", "nimbus", "orchard"):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, report)
        self.assertTrue(any(line.startswith("  variant names: names fixed 1 of 4") and
                            line.endswith("(the app's settings)") for line in lines))
        self.assertIn("correction settings: name fixes on, common-sense fixes off", lines)
        self.assertTrue(any(line.startswith("common-sense rule: met") for line in lines))
        run = self.run_dir()
        phase8 = json.loads((run / "takes-phase8.json").read_text(encoding="utf-8"))
        sense = json.loads((run / "takes-common_sense.json").read_text(encoding="utf-8"))
        self.assertIn("Verza", phase8[0]["corrected"])
        self.assertIn("tu decides", sense[1]["corrected"])
        self.assertEqual([t["variant"] for t in sense], [P.COMMON_SENSE] * 3)
        self.assertNotIn("safety", summary)

    def test_without_the_option_no_variant_runs(self):
        self.record()
        code, summary, lines, model, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("variants", summary["sets"]["prompts"])
        self.assertNotIn("common_sense_rule", summary)
        self.assertEqual(len([call for call in model.calls if not call.enrich]), 6)
        self.assertFalse(any((self.run_dir() / f"takes-{v}.json").exists() for v in P.VARIANTS))
        # The main run still fixes the name with the app's settings and reports it against the reference.
        self.assertEqual(summary["sets"]["prompts"]["against_reference"]["names_fixed"], 1)

    def test_common_sense_overrides_the_main_run(self):
        self.record()
        code, summary, lines, model, asked, _ = self.run_main("--common-sense", "--variants")
        self.assertEqual((code, asked), (0, [{"common_sense": True}]))
        block = summary["sets"]["prompts"]
        self.assertEqual(block["variant_app"], P.COMMON_SENSE)
        self.assertIs(summary["rewrite"]["common_sense_fixes"], True)
        self.assertEqual(block["against_reference"]["common_sense_fixes"], 1)
        self.assertEqual(block["against_reference"]["word_errors"], block["variants"][P.COMMON_SENSE]["word_errors"])
        # The main run is the common-sense variant: the other two ask again.
        context = [call for call in model.calls if not call.enrich and "<project_terms>" in call.user]
        self.assertEqual(sum(autorewrite.COMMON_SENSE_RULE in call.system for call in context), 3)
        self.assertEqual(len(context), 9)
        self.assertIn("correction settings: name fixes on, common-sense fixes on", lines)

    def test_no_common_sense_is_the_config_default(self):
        self.record()
        code, summary, _, model, asked, _ = self.run_main("--no-common-sense")
        self.assertEqual((code, asked), (0, [{"common_sense": False}]))
        self.assertIs(summary["rewrite"]["common_sense_fixes"], False)
        self.assertFalse(any(autorewrite.COMMON_SENSE_RULE in getattr(call, "system", "") for call in model.calls))

    def test_both_common_sense_switches_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            P.main(["--common-sense", "--no-common-sense"], out=lambda line: None)
        self.assertEqual(raised.exception.code, 2)

    def test_the_rewriter_gets_its_own_settings_back(self):
        product = make_product(self.root, SenseModel(), names=(VOCABULARY_NAME,))
        rewriter = product.rewriter
        self.assertEqual(P.app_variant(rewriter), P.NAMES)
        for variant, state in P.VARIANTS.items():
            with self.subTest(variant), P.rewriter_variant(rewriter, variant):
                self.assertEqual((rewriter.name_fixes, rewriter.common_sense_fixes), state)
                self.assertEqual(P.app_variant(rewriter), variant)
        with self.assertRaises(RuntimeError), P.rewriter_variant(rewriter, P.PHASE8):
            raise RuntimeError("a failed take")
        self.assertEqual((rewriter.name_fixes, rewriter.common_sense_fixes), (True, False))
        rewriter.name_fixes, rewriter.common_sense_fixes = False, True
        self.assertIsNone(P.app_variant(rewriter))


class SafetyTest(VariantsCase):
    """--safety: every valid dictation take, each correction variant, into Claude Code without a project."""

    DICTATION = (("dt-01", "Quero usar o modelo local para corrigir o texto ditado amanhã.", CLAUDE_CODE),
                 ("dt-02", "Olá, amanhã falamos com calma sobre isto e o verja.", "default"),
                 ("dt-03", "Diz ao nimbus-deck que o painel fica para amanhã.", "default"))

    def takes(self):
        return tuple(Take(take_id, 3.0, "dictation", "", "", Path(f"{take_id}.wav"), text, ("nimbus-deck",), style)
                     for take_id, text, style in self.DICTATION)

    def extra_heard(self):
        heard = {take_id: misheard(text) for take_id, text, _ in self.DICTATION}
        heard["dt-03"] = " "  # no speech
        return heard

    def load(self):
        real = P.load_sets
        takes = self.takes()
        dataset = SimpleNamespace(takes=takes, script_rows=4, pending=("dt-04",), invalid=(),
                                  names=frozenset({"nimbus-deck"}), reference_texts=lambda: [t.clean for t in takes])

        def load_sets(settings, chosen, safety=False):
            loaded = real(settings, chosen)
            if safety:
                loaded.dictation, loaded.safety_takes = dataset, takes
            return loaded

        return load_sets

    def test_every_take_with_each_variant_counts_only(self):
        self.record()
        code, summary, lines, model, _, streamer = self.run_main("--safety", load=self.load())
        self.assertEqual(code, 0)
        safety = summary["safety"]
        self.assertEqual((safety["status"], safety["takes"], safety["enrichment"]), ("measured", 3, False))
        self.assertEqual(safety["dataset"], {"script_rows": 4, "recorded": 3, "pending": 1, "invalid": 0})
        self.assertEqual(safety["variant_app"], P.NAMES)
        variants = safety["variants"]
        self.assertEqual(list(variants), [P.PHASE8, P.NAMES, P.COMMON_SENSE])
        for name, view in variants.items():
            with self.subTest(name):
                self.assertEqual((view["takes"], view["no_speech"]), (3, 1))
                self.assertIsNone(view["term_errors"])
                self.assertEqual((view["enrichment_requests"], view["enriched"]), (0, 0))
                self.assertEqual((view["lost"], view["invented_beyond_fixes"], view["invented_reference"]),
                                 (0, 0, 0))
        # The vocabulary name in dt-02 is fixed by the name pre-step without a project; the group by common sense.
        self.assertEqual([variants[v]["names_fixed"] for v in variants], [0, 1, 1])
        self.assertEqual([variants[v]["common_sense_fixes"] for v in variants], [0, 0, 1])
        words = [variants[v]["word_errors"]["corrected"] for v in variants]
        self.assertEqual(words, [2, 1, 0])
        self.assertEqual([variants[v]["invented_fixes"] for v in variants], [0, 0, 1])
        # The safety set counts in the rule but never in the targets.
        self.assertEqual(set(summary["common_sense_rule"]["sets"]), {"safety"})
        self.assertTrue(summary["common_sense_rule"]["met"])
        self.assertEqual(set(summary["meets_targets"]), {"complete", "term_errors", "lost", "invented",
                                                        "pack_outside_context", "all"})
        # Transcribed with today's hints (no project) and never enriched; no today's mouse 5 for the safety set.
        self.assertIn((["dt-01", "dt-02", "dt-03"], None), streamer.calls)
        safety_calls = [call for call in model.calls if "Quero usar" in call.user or "Olá" in call.user]
        self.assertEqual(len(safety_calls), 6)  # two spoken takes, three variants, the correction only
        self.assertTrue(all(not call.enrich and "<project_terms>" not in call.user for call in safety_calls))
        serialized = json.dumps(summary).casefold()
        report = "\n".join(lines).casefold()
        for secret in ("museu", "modelo local", "verja", "verza", "nimbus", "amanhã"):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, report)
        self.assertIn("safety: 3 dictation takes into Claude Code without a project, no enrichment", lines)
        run = self.run_dir()
        rows = json.loads((run / "takes-safety-common_sense.json").read_text(encoding="utf-8"))
        self.assertIn("modelo local", rows[0]["corrected"])
        self.assertTrue(all(row["set"] == P.SAFETY and row["today_reason"] == "" for row in rows))

    def test_a_summary_leaking_safety_text_is_refused(self):
        self.record()
        product = []

        def factory(vocab, generic, **options):
            product.append(make_product(self.root, SenseModel(), names=(VOCABULARY_NAME,)))
            product[-1].info["model"] = "o museu local"  # spoken text of a safety take in an aggregate field
            return product[-1]

        summary = self.results / "committed-summary.json"
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("", encoding="utf-8")
        lines = []
        streamer = FakeStreamer({**{row.id: misheard(spoken(row)) for row in self.rows()}, **self.extra_heard()})
        with mock.patch.object(P, "load_sets", self.load()):
            code = P.main(["--config", str(self.config), "--summary", str(summary), "--vocabulary", str(vocabulary),
                           "--set", "prompts", "--safety"], product_factory=factory, streamer_factory=streamer.factory,
                          results_dir=self.results, out=lines.append)
        self.assertEqual(code, 1)
        self.assertFalse(summary.exists())
        self.assertIn("not written", lines[-1])

    def test_dry_run_counts_the_safety_set_without_gpu_microphone_or_ollama(self):
        lines = []
        forbidden = mock.Mock(side_effect=AssertionError("the dry run loaded a model or Ollama"))
        with mock.patch.object(P, "load_sets", self.load()), \
                mock.patch("bench.audio_mme.WinMM", side_effect=AssertionError("the dry run opened the microphone")):
            code = P.main(["--dry-run", "--config", str(self.config), "--set", "prompts", "--safety"],
                          out=lines.append, product_factory=forbidden, streamer_factory=forbidden,
                          fixture=lambda rows, projects, terms: P.Fixture(2, 2, 2, 2, 2, True))
        self.assertEqual(code, 0)
        self.assertIn("safety set: 3 valid dictation takes, each correction variant into Claude Code without a project",
                      lines)
        self.assertFalse(any("Quero" in line or "verja" in line for line in lines))


class ReferenceCountsTest(unittest.TestCase):
    """Word errors and invented words against the clean reference, independently of the product guard."""

    def test_invented_against_the_reference(self):
        reference = "Olha, tu decides qual é a melhor opção para o ficheiro de testes."
        heard = "Olha, tudo cedo qual é a melhor opção para o ficheiro de testes."
        self.assertEqual(P.invented_reference(reference, heard, reference), 0)  # a fix to what was said
        self.assertEqual(P.invented_reference(reference, heard, heard), 0)  # nothing brought
        # A sound-close fix the reference does not hold is invented, whatever the guard said.
        self.assertEqual(P.invented_reference(reference, heard, heard.replace("tudo cedo", "tudo certo")), 1)
        # Function words never count; a number always does; reordering brings nothing.
        self.assertEqual(P.invented_reference(reference, heard, heard.replace("Olha,", "Olha, a")), 0)
        self.assertEqual(P.invented_reference(reference, heard, heard + " 3"), 1)
        self.assertEqual(P.invented_reference(reference, heard, "Testes de ficheiro o para opção melhor a é qual "
                                                                "cedo tudo, olha."), 0)

    def test_invented_against_the_input_with_names_and_beyond_the_fixes(self):
        heard = "Abre o repositório do Verza e corre os testes do museu local."
        fixed = "Abre o repositório do verja e corre os testes do modelo local."
        # A vocabulary name written as listed is known, as a pack term; an ordinary word is invented by this rule.
        self.assertEqual(P.invented_words(heard, fixed)[0], 2)
        self.assertEqual(P.invented_words(heard, fixed, names=("verja",))[0], 1)
        # Beyond the correction's own words: only what the output adds to them.
        self.assertEqual(P.invented_beyond_fixes(heard, fixed, fixed, names=("verja",)), 0)
        self.assertEqual(P.invented_beyond_fixes(heard, fixed, "Pedido: " + fixed, names=("verja",)), 0)
        self.assertEqual(P.invented_beyond_fixes(heard, fixed, fixed + " Usa Docker.", names=("verja",)), 2)
        self.assertEqual(P.invented_beyond_fixes(heard, heard, heard + " Em 3 dias."), 2)

    def test_metric_helpers(self):
        from bench.metrics import invented_against, text_edits

        self.assertEqual(invented_against(["a", "b", "b"], ["a", "b", "x"], ["a", "b", "b"]), 0)
        self.assertEqual(invented_against(["a", "b"], ["a", "x"], ["a", "y"]), 1)
        self.assertEqual(invented_against(["a"], ["a", "a"], ["a", "a"]), 0)
        counts = text_edits("Tu decides, já.", "tudo cedo já")
        self.assertEqual((counts.errors, counts.reference_words), (2, 3))

    def test_the_rule_needs_fewer_word_errors_and_zero_lost_and_invented_everywhere(self):
        def block(off, on, lost=0, invented_beyond_fixes=0, invented_reference=0):
            view = {"word_errors": {"corrected": off}, "lost": 0, "invented_beyond_fixes": 0, "invented_fixes": 0,
                    "invented_reference": 0}
            return {"variants": {P.NAMES: view, P.COMMON_SENSE: {
                "word_errors": {"corrected": on}, "lost": lost, "invented_beyond_fixes": invented_beyond_fixes,
                "invented_fixes": 2, "invented_reference": invented_reference}}}

        self.assertIsNone(P.common_sense_rule({"prompts": {"status": "measured"}}))
        self.assertTrue(P.common_sense_rule({"prompts": block(5, 3), "safety": block(2, 2)})["met"])
        self.assertFalse(P.common_sense_rule({"prompts": block(5, 5)})["met"])  # no fewer errors
        self.assertFalse(P.common_sense_rule({"prompts": block(5, 2), "safety": block(2, 3)})["met"])  # one set worse
        for bad in ({"lost": 1}, {"invented_beyond_fixes": 1}, {"invented_reference": 1}):
            with self.subTest(**bad):
                self.assertFalse(P.common_sense_rule({"prompts": block(5, 2), "safety": block(2, 2, **bad)})["met"])


if __name__ == "__main__":
    unittest.main()
