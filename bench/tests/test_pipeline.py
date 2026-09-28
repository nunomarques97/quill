"""bench.pipeline tests with invented phrases, generated tones and a fake engine.

No GPU, microphone, Ollama or real recording is used; per-take outputs go to
a temporary folder standing in for bench/results/.
"""

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

from bench import pipeline
from bench.cleanup import OllamaError
from bench.dataset import load_dataset
from bench.settings import load_settings
from bench.tests import test_dataset, test_dictation_dataset
from bench.tests.fakes import FakeEngine

TERMS = ["deploy", "commit", "review"]


def distinct(index, seconds=1.0):
    level = 400 + 10 * index
    return array("h", [level if i % 2 else -level for i in range(int(seconds * 16_000))])


class Sets:
    """Commands (pt-01..03) and dictation (dt-01..04, three recorded) fixtures."""

    def __init__(self, root: Path):
        (root / "cmd").mkdir()
        self.commands = test_dataset.Fixture(root / "cmd")
        for index, take_id in enumerate(("pt-01", "pt-02", "pt-03")):
            self.commands.add(take_id, distinct(index))
        self.commands.write_manifest()
        self.dictation = test_dictation_dataset.Fixture(root)
        for index, take_id in enumerate(("dt-01", "dt-02", "dt-03"), start=10):
            self.dictation.add(take_id, distinct(index))
        self.settings = dataclasses.replace(
            load_settings(self.commands.settings_path), dictation=self.dictation.settings(min_takes=3)
        )

    def loaded(self, names=pipeline.SETS):
        return pipeline.load_sets(self.settings, names)


def engine_for(sets, choose):
    """A fake engine answering ``choose(take)`` for every take of both sets."""
    texts = {}
    for _, dataset in sets.values():
        for take in dataset.takes:
            texts[take.path.read_bytes()] = choose(take)
    return FakeEngine("fake-whisper", texts)


def judge_yes(reference, hypothesis):
    return True, "same"


class MeasureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fx = Sets(self.root)
        self.results = self.root / "results"

    def tearDown(self):
        self._tmp.cleanup()

    def measure(self, choose, judge=judge_yes, unavailable=None):
        sets = self.fx.loaded()
        engine = engine_for(sets, choose)
        summary = pipeline.measure(sets, "raw", engine, judge, unavailable, TERMS, self.results / "pipeline" / "run",
                                   results_dir=self.results, log=lambda line: None)
        return sets, engine, summary

    def test_clean_hypotheses(self):
        sets, engine, summary = self.measure(lambda take: take.clean)
        self.assertEqual(engine.loaded, 1)
        self.assertEqual(engine.calls, 1 + 3 + 3)  # one warm-up, then every take once
        commands = summary["sets"]["commands"]
        dictation = summary["sets"]["dictation"]
        self.assertTrue(commands["dataset"]["complete"])
        self.assertEqual((dictation["dataset"]["n_valid"], dictation["dataset"]["pending"]), (3, 1))
        raw = dictation["stages"]["raw"]
        self.assertEqual((raw["n"], raw["wer_clean"], raw["filler_removal_rate"], raw["content_deleted"]), (3, 0.0, 1.0, 0))
        self.assertGreater(raw["wer_verbatim"], 0)
        self.assertEqual((raw["name_error_rate"], raw["term_error_rate"], raw["intent_preserved"]), (0.0, 0.0, 1.0))
        self.assertEqual(raw["filler_spans"], 5)
        cmd = commands["stages"]["raw"]
        self.assertEqual((cmd["wer_verbatim"], cmd["wer_clean"], cmd["filler_removal_rate"]), (0.0, 0.0, None))
        self.assertEqual(summary["stages"], ["raw"])

    def test_verbatim_hypotheses_remove_nothing(self):
        _, _, summary = self.measure(lambda take: take.reference)
        raw = summary["sets"]["dictation"]["stages"]["raw"]
        self.assertEqual((raw["wer_verbatim"], raw["filler_removal_rate"], raw["content_deleted"]), (0.0, 0.0, 0))

    def test_lost_name_and_term(self):
        _, _, summary = self.measure(lambda take: take.clean.replace("zeta-board", "zeta").replace("deploy", "de ploi"))
        raw = summary["sets"]["dictation"]["stages"]["raw"]
        self.assertEqual((raw["name_error_rate"], raw["term_error_rate"]), (0.5, 0.3333))  # 1 of 2 names, 1 of 3 terms
        self.assertGreater(raw["content_deleted"], 0)

    def test_private_outputs_and_aggregate_summary(self):
        sets, _, summary = self.measure(lambda take: take.clean)
        rows = json.loads((self.results / "pipeline" / "run" / "dictation" / "raw.json").read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in rows], ["dt-01", "dt-02", "dt-03"])
        self.assertTrue(rows[0]["verbatim"].startswith("hum abre"))
        texts = [t for _, d in sets.values() for t in d.reference_texts()]
        names = [n for _, d in sets.values() for n in d.names]
        target = self.root / "summary.json"
        pipeline.write_summary(target, summary, texts, names)
        serialized = target.read_text(encoding="utf-8")
        for text in texts + names:
            self.assertNotIn(text, serialized)
        with self.assertRaises(ValueError):
            pipeline.write_private(self.root / "elsewhere", "dictation", "raw", [], [], self.results)

    def test_judge_failure_and_unavailable(self):
        def broken(reference, hypothesis):
            raise OllamaError("judge timed out")

        _, _, summary = self.measure(lambda take: take.clean, judge=broken)
        raw = summary["sets"]["dictation"]["stages"]["raw"]
        self.assertEqual((raw["intent_preserved"], raw["intent_judged"]), (None, 0))
        self.assertIn("judge timed out", raw["intent_note"])
        _, _, summary = self.measure(lambda take: take.clean, judge=None, unavailable="Ollama unavailable: invented")
        self.assertIn("Ollama unavailable", summary["sets"]["commands"]["stages"]["raw"]["intent_note"])

    def test_dry_run(self):
        lines = []
        self.assertEqual(pipeline.dry_run(self.fx.settings, pipeline.SETS, lines.append), 0)
        self.assertIn("commands: valid takes 3 (expected 3)", lines)
        self.assertIn("dictation: script rows 4, recorded 3 (minimum 3), pending 1", lines)
        empty = dataclasses.replace(self.fx.settings.dictation, recordings_dir=self.root / "none", manifest=self.root / "none" / "m.json")
        lines.clear()
        self.assertEqual(pipeline.dry_run(dataclasses.replace(self.fx.settings, dictation=empty), ("dictation",), lines.append), 0)
        self.assertIn("dictation: script rows 4, recorded 0 (minimum 3), pending 4", lines)
        self.assertIn("  complete: no", lines)
        self.fx.dictation.script.unlink()
        self.assertEqual(pipeline.dry_run(self.fx.settings, ("dictation",), lines.append), 1)

    def test_cli_stage_require_and_doc(self):
        summary_path = self.root / "out" / "summary.json"
        doc = self.root / "FASE.md"
        doc.write_text("# Relatório\n", encoding="utf-8")
        lines = []

        def run(*argv, engine=None):
            lines.clear()
            with mock.patch.object(pipeline, "load_settings", return_value=self.fx.settings):
                return pipeline.main(
                    list(argv),
                    engine_factory=lambda: engine,
                    judge_factory=lambda: (judge_yes, None),
                    results_dir=self.results,
                    out=lines.append,
                )

        engine = engine_for(self.fx.loaded(), lambda take: take.clean)
        self.assertEqual(run("--stage", "raw", "--summary", str(summary_path), engine=engine), 0)
        self.assertEqual(engine.closed, 1)
        self.assertTrue(summary_path.is_file())
        self.assertEqual(run("--summary", str(summary_path), "--require", "complete", "--require", "overall"), 0)
        self.assertTrue(all(line.startswith("ok") for line in lines))
        self.assertEqual(run("--summary", str(summary_path), "--require", "latency"), 1)
        self.assertEqual(run("--summary", str(summary_path), "--require", "vocabulary"), 1)
        self.assertIn("vocabulary stage not measured", "\n".join(lines))
        self.assertEqual(run("--summary", str(summary_path), "--check-doc", str(doc)), 1)
        self.assertEqual(run("--summary", str(summary_path), "--write-doc", str(doc)), 0)
        self.assertEqual(run("--summary", str(summary_path), "--check-doc", str(doc)), 0)
        self.assertIn("# Relatório", doc.read_text(encoding="utf-8"))
        # Only the commands set measured: every target reports dictation as missing.
        engine = engine_for(self.fx.loaded(), lambda take: take.clean)
        self.assertEqual(run("--set", "commands", "--stage", "raw", "--summary", str(summary_path), "--require", "complete", engine=engine), 1)
        self.assertIn("FAIL complete: dictation not measured", lines)
        self.assertEqual(run("--summary", str(summary_path), "--check-doc", str(doc)), 1)


def summary_with(commands=None, dictation=None, latency=None, stages=("raw",)):
    def block(n, valid, complete, row):
        return {"dataset": {"n_valid": valid, "minimum": valid, "complete": complete},
                "stages": {stage: {"n": n, **row} for stage in stages}}

    good = {"wer_clean": 0.05, "intent_preserved": 0.97, "name_error_rate": 0.05, "term_error_rate": 0.08,
            "filler_removal_rate": 0.96, "content_deleted": 0, "recurrences_fixed_rate": 1.0, "new_errors": 0}
    return {
        "kind": "pipeline",
        "sets": {
            "commands": block(44, 44, True, {**good, **(commands or {})}),
            "dictation": block(32, 32, True, {**good, **(dictation or {})}),
        },
        "latency": latency,
    }


class TargetTest(unittest.TestCase):
    def failed(self, summary, *targets):
        return [line for met, line in pipeline.check_targets(summary, targets) if not met]

    def test_all_met(self):
        summary = summary_with(latency={"p95_s": 1.2, "max_audio_s": 15.0}, stages=pipeline.STAGE_ORDER)
        self.assertEqual(self.failed(summary, *pipeline.TARGET_NAMES), [])

    def test_overall_gap_reports_measured_and_target(self):
        failed = self.failed(summary_with(dictation={"wer_clean": 0.283, "intent_preserved": 0.523}), "overall")
        self.assertEqual(failed, [
            "FAIL overall: dictation final-text WER 28.3 % (raw; target <= 10 %)",
            "FAIL overall: dictation intent preserved 52.3 % (raw; target >= 95 %)",
        ])

    def test_incomplete_sets(self):
        summary = summary_with()
        summary["sets"]["dictation"]["dataset"]["complete"] = False
        self.assertIn("dictation incomplete", self.failed(summary, "overall")[0])
        summary = summary_with()
        summary["sets"]["commands"]["stages"]["raw"]["n"] = 43
        self.assertIn("commands incomplete", self.failed(summary, "complete")[0])
        summary = summary_with()
        del summary["sets"]["dictation"]
        self.assertEqual(self.failed(summary, "complete"), ["FAIL complete: dictation not measured"])

    def test_stage_specific_targets(self):
        summary = summary_with(stages=("raw", "cleanup"), dictation={"filler_removal_rate": 0.9, "content_deleted": 1})
        self.assertEqual(len(self.failed(summary, "cleanup")), 2)
        self.assertEqual(len(self.failed(summary, "vocabulary")), 2)
        summary = summary_with(stages=("raw", "vocabulary"), commands={"name_error_rate": 0.2})
        self.assertEqual(self.failed(summary, "vocabulary"), ["FAIL vocabulary: commands project-name error 20.0 % (target <= 10 %)"])
        self.assertEqual(len(self.failed(summary_with(latency={"p95_s": 1.6, "max_audio_s": 15.0}), "latency")), 1)
        self.assertEqual(len(self.failed(summary_with(), "unknown")), 1)


class DocTest(unittest.TestCase):
    def test_render_check_and_replace(self):
        summary = summary_with(latency={"p95_s": 1.234, "max_audio_s": 15.0})
        block = pipeline.render_block(summary)
        self.assertIn("| comandos | raw | 44 |", block)
        self.assertIn("5,0 %", block)
        self.assertIn("p95: 1,23 s", block)
        document = pipeline.write_doc("# Doc\n\ntexto\n", summary)
        self.assertEqual(pipeline.check_doc(document, summary), [])
        self.assertEqual(pipeline.check_doc(document.replace("\n", "\r\n"), summary), [])
        changed = summary_with(dictation={"wer_clean": 0.5})
        self.assertEqual(len(pipeline.check_doc(document, changed)), 1)
        rewritten = pipeline.write_doc(document, changed)
        self.assertEqual(rewritten.count(pipeline.START_MARKER), 1)
        self.assertEqual(pipeline.check_doc("# Doc\n", summary), ["pipeline summary block not found"])


class CliTest(unittest.TestCase):
    def test_nothing_to_do_and_bad_summary(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.main([], out=lambda line: None)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "s.json"
            path.write_text("{}", encoding="utf-8")
            lines = []
            self.assertEqual(pipeline.main(["--summary", str(path), "--require", "complete"], out=lines.append), 2)
            self.assertEqual(lines, ["error: not a pipeline summary"])


class SplitTimingsTest(unittest.TestCase):
    def test_moves_wall_clock_fields_out_of_the_summary(self):
        summary = {
            "engine": {"model": "m", "load_s": 8.2, "warmup_s": 1.7},
            "sets": {"dictation": {"stages": {"raw": {"wer_clean": 0.13, "transcribe_p50_s": 1.1, "transcribe_p95_s": 2.2, "max_audio_s": 18.0}}}},
        }
        timings = pipeline.split_timings(summary)
        self.assertEqual(summary["engine"], {"model": "m"})
        self.assertEqual(summary["sets"]["dictation"]["stages"]["raw"], {"wer_clean": 0.13, "max_audio_s": 18.0})
        self.assertEqual(timings["engine"], {"load_s": 8.2, "warmup_s": 1.7})
        self.assertEqual(timings["sets"]["dictation"]["raw"], {"transcribe_p50_s": 1.1, "transcribe_p95_s": 2.2})

    def test_rendered_block_has_no_timing_column(self):
        self.assertNotIn("p95 transcrição (s)", pipeline.render_block({"sets": {}}))


if __name__ == "__main__":
    unittest.main()
