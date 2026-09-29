"""Command-mode harness tests with invented data.

The script rows, selections, instructions, transcriptions and rewrites are
invented; recordings are generated tones in a temporary folder. The
streamer, the product rewriter's Ollama client and the judge are fakes: no
GPU, microphone, sound or Ollama is used.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bench import rewrite as R
from bench import settings as S
from bench.dataset import DatasetError, Take
from bench.settings import REWRITE_SCRIPT, SettingsError, load_settings
from bench.tests.test_dataset import tone, write_wav
from quill import command

SCRIPT = """# Invented rewrite script

| id | caso | frase | seleção | língua | preservar |
|----|------|-------|---------|--------|-----------|
| rw-01 | formal | {hum} põe isto mais formal. | olá, a entrega do bolo verde passa para quinta | pt | quinta |
| rw-02 | inglês | Traduz para inglês. | O deploy do painel azul correu bem ontem. | en | deploy |
| rw-03 | encurtar | Encurta isto. | A reunião do clube de xadrez foi adiada porque a sala grande ficou ocupada com obras até sexta. | pt | — |
| rw-04 | lista | Faz uma lista. | Leva maçãs, peras e uvas. | pt | maçãs; peras |
"""

REPLIES = {
    "olá, a entrega do bolo verde passa para quinta": "Informo que a entrega do bolo verde fica adiada para quinta-feira.",
    "O deploy do painel azul correu bem ontem.": "The deploy of the blue panel went well yesterday.",
    "A reunião do clube de xadrez foi adiada porque a sala grande ficou ocupada com obras até sexta.":
        "Reunião do clube de xadrez adiada até sexta por obras.",
    "Leva maçãs, peras e uvas.": "- maçãs\n- peras\n- uvas",
}


def rows_by_id(text=SCRIPT):
    return {row.id: row for row in R.parse_rewrite_script(text)}


class FakeOllama:
    """The product client's interface: chat(model, system, user, max_tokens, history)."""

    def __init__(self, replies=None, error=None):
        self.replies = dict(REPLIES if replies is None else replies)
        self.error = error
        self.calls = []
        self.histories = []

    def chat(self, model, system, user, max_tokens=None, history=()):
        self.calls.append(user)
        self.histories.append(tuple(history))
        if self.error is not None:
            raise self.error
        selection = user.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
        return SimpleNamespace(content=self.replies.get(selection, "Texto de aquecimento, mais formal."))


class FakeJudge:
    def __init__(self, verdicts=None, fail_at=None):
        self.verdicts = verdicts or {}
        self.fail_at = fail_at
        self.calls = []

    def __call__(self, instruction, selection, rewrite):
        self.calls.append((instruction, selection, rewrite))
        if self.fail_at is not None and len(self.calls) >= self.fail_at:
            raise R.JudgeError("rewrite judge unavailable (URLError)")
        return self.verdicts.get(selection, True), "follows the instruction"


def take(row_id, clean, path=Path("x.wav")):
    return Take(row_id, 1.0, "microfone", "", "", path, clean, (), clean_reference=clean)


def rewriter(client=None):
    ticks = iter(range(0, 1000))
    return command.CommandRewriter(R.RecordingClient(client or FakeOllama()), "qwen3:8b",
                                   clock=lambda: next(ticks) * 0.5)


class ScriptTest(unittest.TestCase):
    def test_committed_script(self):
        rows = R.parse_rewrite_script(REWRITE_SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual([row.id for row in rows], [f"rw-{n:02d}" for n in range(1, 21)])
        self.assertEqual(len(rows), S.REWRITE_EXPECTED_TAKES)
        self.assertEqual({row.language for row in rows}, {"pt", "en"})
        kinds = {row.kind for row in rows}
        self.assertTrue({"formal", "inglês", "encurtar", "lista", "corrigir", "português"} <= kinds)
        for row in rows:
            with self.subTest(row=row.id):
                self.assertTrue(row.instruction and row.selection)
                if row.kind != R.CORRECT:
                    self.assertTrue(all(R.contains_term(row.selection, term) for term in row.preserve))
                # The selection is in the other language for a translation, in Portuguese otherwise.
                translation = row.kind in ("inglês", "português")
                expected = ({"pt": "en", "en": "pt"}[row.language]) if translation else row.language
                self.assertEqual(R.detect_language(row.selection), expected)

    def test_invented_script(self):
        rows = rows_by_id()
        self.assertEqual(list(rows), ["rw-01", "rw-02", "rw-03", "rw-04"])
        self.assertEqual((rows["rw-02"].kind, rows["rw-02"].language, rows["rw-02"].preserve), ("inglês", "en", ("deploy",)))
        self.assertEqual(rows["rw-03"].preserve, ())
        self.assertEqual(rows["rw-04"].preserve, ("maçãs", "peras"))
        self.assertNotIn("olá", repr(rows["rw-01"]))

    def test_invalid_scripts(self):
        header = "| id | caso | frase | seleção | língua | preservar |\n|---|---|---|---|---|---|\n"
        cases = {
            "| id | caso | frase | seleção | língua |\n|---|---|---|---|---|\n| rw-01 | formal | põe formal. | um texto | pt |\n":
                "missing columns preserve",
            header + "| rw-01 | formal | põe formal. | um texto | fr | — |\n": "língua",
            header + "| rw-01 | formal | põe formal. | — | pt | — |\n": "empty selection",
            header + "| rw-01 | formal | põe formal. | um texto | pt | outro |\n": "preservar term",
            header + "| rw-01 | | põe formal. | um texto | pt | — |\n": "empty caso",
            header + "| rw-01 | formal | {uau} põe formal. | um texto | pt | — |\n": "unknown filler",
            header + "| xx-01 | formal | põe formal. | um texto | pt | — |\n": "invalid id",
        }
        for text, message in cases.items():
            with self.subTest(message=message):
                with self.assertRaises(DatasetError) as caught:
                    R.parse_rewrite_script(text)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("um texto", str(caught.exception))


class SettingsTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            local = root / "local"
            config = root / "bench.toml"
            config.write_text('[paths]\nrecordings_dir = "r"\nrecording_script = "s"\nreference_config = "c"\n',
                              encoding="utf-8")
            settings = load_settings(config)
            self.assertEqual(settings.for_set("rewrite"), settings.rewrite)
            self.assertEqual((settings.rewrite.id_prefix, settings.rewrite.expected_takes, settings.rewrite.minimum_takes),
                             ("rw", 20, 16))
            self.assertEqual(settings.rewrite.recording_script, REWRITE_SCRIPT)
            self.assertTrue(settings.rewrite.markup and settings.rewrite.allow_pending)
            self.assertIsNone(settings.rewrite.projects)
            with mock.patch.object(S, "LOCAL_DIR", local):
                config.write_text(config.read_text(encoding="utf-8")
                                  + f'[dictation]\nrecordings_dir = "{(local / "dt").as_posix()}"\n'
                                  + f'[rewrite]\nrecordings_dir = "{(local / "rw").as_posix()}"\n'
                                  'expected_takes = 4\nmin_takes = 3\n', encoding="utf-8")
                settings = load_settings(config)
                self.assertEqual((settings.rewrite.recordings_dir, settings.rewrite.minimum_takes), (local / "rw", 3))
            # Outside the ignored local/ folder.
            config.write_text('[paths]\nrecordings_dir = "r"\nrecording_script = "s"\nreference_config = "c"\n'
                              f'[rewrite]\nrecordings_dir = "{(root / "rw").as_posix()}"\n', encoding="utf-8")
            with self.assertRaises(SettingsError) as caught:
                load_settings(config)
            self.assertIn("[rewrite].recordings_dir", str(caught.exception))
            with self.assertRaises(SettingsError):
                settings.for_set("unknown")


class ChecksTest(unittest.TestCase):
    def test_language(self):
        self.assertEqual(R.detect_language("The report is ready and we can send it."), "en")
        self.assertEqual(R.detect_language("O relatório está pronto e já o podemos enviar."), "pt")
        self.assertEqual(R.detect_language("Ok."), "?")

    def test_terms(self):
        self.assertTrue(R.contains_term("Temos 10% a mais em iOS.", "10%"))
        self.assertTrue(R.contains_term("Rever o Pull  Request hoje", "pull request"))
        self.assertFalse(R.contains_term("redeployed", "deploy"))
        self.assertFalse(R.contains_term("qualquer", ""))

    def test_checks_per_kind(self):
        rows = rows_by_id()
        self.assertEqual(R.rewrite_checks(rows["rw-01"], REPLIES[rows["rw-01"].selection]),
                         ({"language": True, "preserved": True, "changed": True}, ()))
        self.assertEqual(R.rewrite_checks(rows["rw-01"], "Olá, a entrega passa para amanhã."),
                         ({"language": True, "preserved": False, "changed": True}, ("quinta",)))
        self.assertEqual(R.rewrite_checks(rows["rw-02"], "O deploy correu bem."),
                         ({"language": False, "preserved": True, "changed": True}, ()))
        self.assertEqual(R.rewrite_checks(rows["rw-03"], REPLIES[rows["rw-03"].selection])[0]["shorter"], True)
        self.assertEqual(R.rewrite_checks(rows["rw-03"], rows["rw-03"].selection)[0]["shorter"], False)
        self.assertEqual(R.rewrite_checks(rows["rw-04"], "- maçãs\n- peras e uvas")[0]["list"], True)
        self.assertEqual(R.rewrite_checks(rows["rw-04"], "Leva maçãs e peras.")[0]["list"], False)


class JudgeTest(unittest.TestCase):
    def client(self, content=None, error=None):
        def chat(model, system, user, schema=None):
            self.seen = (model, system, user, schema)
            if error is not None:
                raise error
            return SimpleNamespace(content=content)

        return SimpleNamespace(chat=chat)

    def test_verdicts(self):
        judge = R.RewriteJudge(self.client('{"ok": "yes", "reason": " keeps   the meaning "}'), "qwen3:8b")
        self.assertEqual(judge.judge("põe formal", "texto", "Texto."), (True, "keeps the meaning"))
        self.assertEqual(self.seen[0], "qwen3:8b")
        self.assertEqual(self.seen[3], R.JUDGE_SCHEMA)
        self.assertIn("INSTRUCTION: põe formal\nORIGINAL:\ntexto\nREWRITE:\nTexto.", self.seen[2])
        judge = R.RewriteJudge(self.client('{"ok": "no", "reason": "adds facts"}'), "m")
        self.assertEqual(judge.judge("a", "b", "c"), (False, "adds facts"))

    def test_failures(self):
        for client in (self.client("not json"), self.client('{"ok": "maybe"}'), self.client(error=OSError("down"))):
            with self.assertRaises(R.JudgeError):
                R.RewriteJudge(client, "m").judge("a", "b", "c")


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self.rows = rows_by_id()
        self.takes = [take("rw-01", "põe isto mais formal."), take("rw-02", "Traduz para inglês."),
                      take("rw-03", "Encurta isto."), take("rw-04", "Faz uma lista.")]
        self.heard = [("põe isto mais formal", 0.3), ("traduz para inglês", 0.2), ("encurta", 0.4),
                      ("faz uma lista", 0.1)]

    def test_all_correct(self):
        judge = FakeJudge()
        results = R.measure(self.takes, self.rows, self.heard, rewriter(), judge, log=lambda line: None)
        self.assertEqual([r.reason for r in results], [command.REWRITTEN] * 4)
        self.assertEqual([r.correct for r in results], [True] * 4)
        self.assertEqual(judge.calls[0][0], "põe isto mais formal.")  # the script instruction, not the heard one
        self.assertEqual(results[3].rewrite, "- maçãs\n- peras\n- uvas")
        summary = R.aggregate(results)
        self.assertEqual((summary["takes"], summary["valid"], summary["checks_passed"], summary["correct"]), (4, 4, 4, 4))
        self.assertEqual(summary["correct_rate"], 1.0)
        self.assertEqual(summary["instruction_wer"], round(1 / 12, 4))  # "isto" missing in rw-03
        self.assertEqual(summary["instruction_exact"], 3)
        self.assertEqual(summary["checks"], {"changed": 2, "language": 4, "list": 1, "preserved": 4, "shorter": 1})
        self.assertEqual(summary["latency"], {
            "instruction_p50_s": 0.2, "instruction_p95_s": 0.4, "rewrite_p50_s": 0.5, "rewrite_p95_s": 0.5,
            "total_p50_s": 0.7, "total_p95_s": 0.9})
        self.assertEqual(summary["by_kind"]["lista"], {"takes": 1, "checks_passed": 1, "correct": 1})

    def test_failures_are_counted_not_hidden(self):
        replies = dict(REPLIES)
        replies[self.rows["rw-01"].selection] = "Aqui está o texto reescrito: Informo que a entrega passa para quinta."
        replies[self.rows["rw-02"].selection] = "O deploy do painel azul correu muito bem ontem."  # not English
        replies[self.rows["rw-04"].selection] = self.rows["rw-04"].selection  # unchanged
        judge = FakeJudge({self.rows["rw-03"].selection: False})
        results = R.measure(self.takes, self.rows, self.heard, rewriter(FakeOllama(replies)), judge,
                            log=lambda line: None)
        self.assertEqual([r.reason for r in results],
                         [command.INVALID_REWRITE, command.REWRITTEN, command.REWRITTEN, command.UNCHANGED])
        self.assertEqual(results[0].detail, command.PREAMBLE)
        self.assertIn("Aqui está", results[0].reply)  # kept for the manual table
        self.assertEqual([r.correct for r in results], [False, False, False, False])
        self.assertEqual(len(judge.calls), 2)  # every accepted rewrite is judged (shown in the manual table)
        self.assertEqual(results[1].judged, True)
        summary = R.aggregate(results)
        self.assertEqual(summary["reasons"], {command.INVALID_REWRITE: 1, command.REWRITTEN: 2, command.UNCHANGED: 1})
        self.assertEqual(summary["invalid_details"], {command.PREAMBLE: 1})
        self.assertEqual((summary["checks_passed"], summary["judge_yes"], summary["correct"]), (1, 0, 0))
        # The product asked the model twice for the refused and the unchanged reply, never more.
        self.assertEqual([r.attempts for r in results], [2, 1, 1, 2])
        self.assertEqual((summary["second_attempts"], summary["second_attempt_reasons"]),
                         (2, {command.RETRY_INVALID: 1, command.RETRY_UNCHANGED: 1}))

    def test_ollama_down_is_counted(self):
        results = R.measure(self.takes, self.rows, self.heard, rewriter(FakeOllama(error=OSError("down"))),
                            FakeJudge(), log=lambda line: None)
        self.assertEqual({r.reason for r in results}, {command.OLLAMA_UNAVAILABLE})
        self.assertEqual(R.aggregate(results)["valid"], 0)

    def test_judge_failure_never_mixes_judged_and_unjudged(self):
        lines = []
        results = R.measure(self.takes, self.rows, self.heard, rewriter(), FakeJudge(fail_at=3), log=lines.append)
        self.assertEqual([r.judged for r in results], [None] * 4)
        self.assertEqual([r.correct for r in results], [None] * 4)
        summary = R.aggregate(results)
        self.assertEqual((summary["correct"], summary["correct_rate"], summary["checks_passed"]), (None, None, 4))
        self.assertEqual(lines, ["judge stopped: rewrite judge unavailable (URLError)"])

    def test_without_judge(self):
        results = R.measure(self.takes, self.rows, self.heard, rewriter(), None, log=lambda line: None)
        self.assertEqual(R.aggregate(results)["correct"], None)
        self.assertEqual(R.report_lines(R.aggregate(results))[1].split("; ")[-1], "correct: not judged")


class OutputTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.results_dir = Path(self._tmp.name) / "results"
        rows = rows_by_id()
        takes = [take("rw-01", "põe isto mais formal.")]
        self.results = R.measure(takes, rows, [("põe isto mais | formal", 0.3)], rewriter(), FakeJudge(),
                                 log=lambda line: None)

    def test_private_outputs_only_under_results(self):
        takes_path, table_path = R.write_private(self.results_dir / "rewrite" / "run", self.results, self.results_dir)
        data = json.loads(takes_path.read_text(encoding="utf-8"))
        self.assertEqual((data[0]["id"], data[0]["correct"], data[0]["heard"]), ("rw-01", True, "põe isto mais | formal"))
        table = table_path.read_text(encoding="utf-8")
        self.assertIn("| Sponsor |", table)
        self.assertIn("mais \\| formal", table)  # pipes escaped
        self.assertTrue(table.splitlines()[-1].endswith("| sim |  |"))
        with self.assertRaises(ValueError):
            R.write_private(Path(self._tmp.name) / "elsewhere", self.results, self.results_dir)

    def test_summary_never_holds_text(self):
        summary = R.aggregate(self.results)
        serialized = json.dumps(summary, ensure_ascii=False)
        for text in ("põe isto", "entrega do bolo", "Informo que"):
            self.assertNotIn(text, serialized)
        self.assertNotIn("olá", repr(self.results[0]))


class MainTest(unittest.TestCase):
    """bench.rewrite main with a temporary settings file, generated takes and fake factories."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.local = self.root / "local"
        patcher = mock.patch.object(S, "LOCAL_DIR", self.local)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recordings = self.local / "recordings" / "rewrite"
        self.script = self.root / "guiao.md"
        self.script.write_text(SCRIPT, encoding="utf-8")
        reference = self.root / "reference.toml"
        reference.write_text('[[projetos]]\nnome = "invented"\n', encoding="utf-8")
        self.config = self.root / "bench.toml"
        self.config.write_text(
            f'[paths]\nrecordings_dir = "r"\nrecording_script = "s"\nreference_config = "{reference.as_posix()}"\n'
            f'[dictation]\nrecordings_dir = "{(self.local / "dictation").as_posix()}"\n'
            f'[rewrite]\nrecordings_dir = "{self.recordings.as_posix()}"\nrecording_script = "{self.script.as_posix()}"\n'
            'expected_takes = 4\nmin_takes = 3\n', encoding="utf-8")
        self.results_dir = self.root / "results"
        self.lines = []
        self.closed = False

    def record(self, ids):
        self.recordings.mkdir(parents=True, exist_ok=True)
        entries = {}
        for take_id in ids:
            write_wav(self.recordings / f"{take_id}.wav", tone(1.0))
            entries[take_id] = {"ficheiro": f"{take_id}.wav", "origem": "microfone", "duracao_s": 1.0, "projetos": {}}
        (self.recordings / "manifesto.json").write_text(json.dumps({"gravacoes": entries}), encoding="utf-8")

    def streamer_factory(self, hints):
        heard = {"rw-01": "põe isto mais formal", "rw-02": "traduz para inglês", "rw-03": "encurta isto",
                 "rw-04": "faz uma lista"}

        def stream(takes):
            return [(heard[t.id], 0.25) for t in takes]

        stream.close = lambda: setattr(self, "closed", True)
        self.hints = hints
        return stream, {"model": "fake"}

    def rewriter_factory(self, terms):
        self.kept_terms = list(terms)
        return rewriter(self.client), "qwen3:8b"

    def run_main(self, *extra, client=None):
        self.client = client or FakeOllama()
        return R.main(["--config", str(self.config), "--vocabulary", str(self.root / "none.toml"), *extra],
                      streamer_factory=self.streamer_factory,
                      rewriter_factory=self.rewriter_factory,
                      judge_factory=lambda: (FakeJudge(), None), results_dir=self.results_dir, out=self.lines.append)

    def test_measures_and_writes_aggregates_only(self):
        self.record(["rw-01", "rw-02", "rw-03", "rw-04"])
        self.assertEqual(self.run_main(), 0)
        self.assertTrue(self.closed)
        self.assertIn("deploy", [h.casefold() for h in self.hints])  # the product hints include the English terms
        self.assertIn("deploy", self.kept_terms)  # and the rewriter keeps the product's terms
        run = next((self.results_dir / "rewrite").iterdir())
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["kind"], summary["takes"], summary["correct"], summary["rewrite_model"]),
                         ("rewrite", 4, 4, "qwen3:8b"))
        self.assertEqual(summary["dataset"], {"script_rows": 4, "recorded": 4, "pending": 0, "invalid": 0})
        self.assertEqual((summary["second_attempts"], summary["second_attempt_reasons"]), (0, {}))
        self.assertTrue((run / "table.md").is_file() and (run / "takes.json").is_file())
        printed = "\n".join(self.lines)
        self.assertIn("correct 4/4", printed)
        for text in ("põe isto", "bolo verde", "Informo", "deploy do painel"):
            self.assertNotIn(text, printed)
        self.assertEqual(len(self.client.calls), 5)  # a warm-up, then one per take

    def test_too_few_takes(self):
        self.record(["rw-01", "rw-02"])
        self.assertEqual(self.run_main(), 2)
        self.assertIn("at least 3 needed", self.lines[-1])
        self.assertFalse((self.results_dir / "rewrite").exists())

    def test_ollama_down(self):
        self.record(["rw-01", "rw-02", "rw-03"])
        self.assertEqual(self.run_main(client=FakeOllama(error=OSError("down"))), 2)
        self.assertEqual(self.lines[-1], "error: Ollama unavailable for the rewrite model")
        self.assertTrue(self.closed)

    def test_summary_outside_results_is_refused(self):
        self.record(["rw-01", "rw-02", "rw-03"])
        self.assertEqual(self.run_main("--summary", str(self.root / "summary.json")), 1)
        self.assertFalse((self.root / "summary.json").exists())

    def test_dry_run(self):
        self.record(["rw-01"])
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertEqual(self.lines, [
            "rewrite script: 4 rows; kinds encurtar 1, formal 1, inglês 1, lista 1; languages en 1, pt 3",
            "recorded 1, pending 3, invalid 0, discarded 0; minimum 3",
        ])

    def test_missing_script(self):
        self.script.unlink()
        self.assertEqual(self.run_main("--dry-run"), 2)
        self.assertTrue(self.lines[-1].startswith("error: "))


if __name__ == "__main__":
    unittest.main()
