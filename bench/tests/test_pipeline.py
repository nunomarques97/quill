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

    def test_streamed_stage_follows_raw_and_becomes_the_final_stage(self):
        sets = self.fx.loaded()
        engine = engine_for(sets, lambda take: take.reference)
        seen = []

        def streamer(takes, hints):
            seen.append((len(takes), hints.vocabulary()[:1]))
            return [(take.clean, 0.1) for take in takes]

        summary = pipeline.measure(sets, "streamed", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                                   results_dir=self.results, log=lambda line: None, streamer=streamer,
                                   stream_options={"step_s": 0.5})
        self.assertEqual(summary["stages"], ["raw", "streamed"])
        self.assertEqual(summary["engine"]["streaming"], {"step_s": 0.5})
        self.assertEqual([count for count, _ in seen], [3, 3])
        dictation = summary["sets"]["dictation"]["stages"]
        self.assertEqual((dictation["raw"]["filler_removal_rate"], dictation["streamed"]["filler_removal_rate"]), (0.0, 1.0))
        self.assertEqual(dictation["streamed"]["wer_clean"], 0.0)
        self.assertTrue((self.results / "pipeline" / "run" / "commands" / "streamed.json").is_file())
        self.assertEqual(pipeline._final_stage(summary["sets"]["commands"])[0], "streamed")
        self.assertIn("| ditado | streamed | 3 |", pipeline.render_block(summary))
        timings = pipeline.split_timings(summary)
        self.assertIn("transcribe_p95_s", timings["sets"]["dictation"]["streamed"])
        self.assertNotIn("transcribe_p95_s", dictation["streamed"])
        with self.assertRaises(ValueError):
            pipeline.measure(sets, "streamed", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                             results_dir=self.results, log=lambda line: None)
        with self.assertRaises(ValueError):
            pipeline.measure(sets, "vocabulary", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                             results_dir=self.results, log=lambda line: None, streamer=streamer)

    def test_cleanup_stage_cleans_the_streamed_text(self):
        sets = self.fx.loaded()
        engine = engine_for(sets, lambda take: take.reference)

        def streamer(takes, hints):
            return [(take.reference, 0.1) for take in takes]

        summary = pipeline.measure(sets, "cleanup", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                                   results_dir=self.results, log=lambda line: None, streamer=streamer)
        self.assertEqual(summary["stages"], ["raw", "streamed", "cleanup"])
        stages = summary["sets"]["dictation"]["stages"]
        self.assertEqual((stages["streamed"]["filler_removal_rate"], stages["streamed"]["content_deleted_by_cleanup"]), (0.0, None))
        cleanup = stages["cleanup"]
        self.assertEqual((cleanup["filler_removal_rate"], cleanup["content_deleted"], cleanup["content_deleted_by_cleanup"]), (1.0, 0, 0))
        self.assertEqual((cleanup["name_error_rate"], cleanup["term_error_rate"]), (0.0, 0.0))
        self.assertLess(cleanup["wer_clean"], stages["streamed"]["wer_clean"])
        self.assertIsNone(summary["sets"]["commands"]["stages"]["cleanup"]["content_deleted_by_cleanup"])  # no markup
        rows = json.loads((self.results / "pipeline" / "run" / "dictation" / "cleanup.json").read_text(encoding="utf-8"))
        self.assertTrue(rows[0]["hypothesis"].startswith("Abre o painel"))
        self.assertEqual(pipeline._final_stage(summary["sets"]["dictation"])[0], "cleanup")
        self.assertIn("| ditado | cleanup | 3 |", pipeline.render_block(summary))
        self.assertEqual(self.failed_cleanup(summary), [])

    def failed_cleanup(self, summary):
        return [line for met, line in pipeline.check_targets(summary, ["cleanup"]) if not met]

    def test_content_deleted_by_cleanup_counts_only_new_deletions(self):
        sets = self.fx.loaded()
        engine = engine_for(sets, lambda take: take.reference)

        def streamer(takes, hints):  # recognition loses "painel" in dt-01: not the cleanup's deletion
            return [(take.reference.replace(" painel", ""), 0.1) for take in takes]

        real_clean = pipeline.clean_text

        def dropping(text, keep=(), final_mark=True):  # a cleanup that also deletes "bolo"
            return real_clean(text.replace("bolo", ""), keep, final_mark)

        with mock.patch.object(pipeline, "clean_text", dropping):
            summary = pipeline.measure(sets, "cleanup", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                                       results_dir=self.results, log=lambda line: None, streamer=streamer)
        stages = summary["sets"]["dictation"]["stages"]
        self.assertEqual(stages["streamed"]["content_deleted"], 1)
        self.assertEqual((stages["cleanup"]["content_deleted"], stages["cleanup"]["content_deleted_by_cleanup"]), (2, 1))
        self.assertEqual(self.failed_cleanup(summary), ["FAIL cleanup: dictation content words deleted by cleanup 1 (target 0)"])

    def streamed_run(self):
        sets = self.fx.loaded()
        engine = engine_for(sets, lambda take: take.reference)
        run = self.results / "pipeline" / "run"
        pipeline.measure(sets, "streamed", engine, judge_yes, None, TERMS, run, results_dir=self.results,
                         log=lambda line: None, streamer=lambda takes, hints: [(t.reference, 0.1) for t in takes])
        return sets, run

    def test_compare_cleanup_reads_saved_texts(self):
        from quill.cleanup import Cleanup, CleanupResult

        sets, run = self.streamed_run()

        def llm(text):  # a fake LLM cleanup that fails on one take and takes 0.4 s otherwise
            if "bolo" in text:
                return CleanupResult(pipeline.clean_text(text), "rules", "llm failed: OllamaError")
            return CleanupResult(pipeline.clean_text(text), "llm", None, 0.4)

        report, texts = pipeline.compare_cleanup(sets, run, {"rules": Cleanup("rules"), "llm": llm}, judge_yes, None, TERMS)
        rules, fake = report["sets"]["dictation"]["rules"], report["sets"]["dictation"]["llm"]
        self.assertEqual((rules["filler_removal_rate"], rules["content_deleted_by_cleanup"], rules["cleanup_p95_s"]), (1.0, 0, None))
        self.assertEqual(rules["fallbacks"], {})
        self.assertEqual((fake["cleanup_p50_s"], fake["cleanup_p95_s"], fake["fallbacks"]), (0.4, 0.4, {"llm failed: OllamaError": 1}))
        self.assertNotIn("transcribe_p95_s", rules)
        self.assertEqual([row["id"] for row in texts["dictation"]["rules"]], ["dt-01", "dt-02", "dt-03"])
        (run / "commands" / "streamed.json").unlink()
        with self.assertRaises(pipeline.SettingsError):
            pipeline.compare_cleanup(sets, run, {"rules": Cleanup("rules")}, judge_yes, None, TERMS)

    def test_compare_cleanup_cli_writes_only_under_results(self):
        from quill.cleanup import Cleanup

        _, run = self.streamed_run()
        lines = []
        with mock.patch.object(pipeline, "load_settings", return_value=self.fx.settings), \
                mock.patch.object(pipeline, "load_terms", return_value=TERMS):
            code = pipeline.main(["--compare-cleanup", str(run)], judge_factory=lambda: (judge_yes, None),
                                 cleaners_factory=lambda keep: ({"rules": Cleanup("rules", keep=keep)}, "Ollama unavailable: test"),
                                 results_dir=self.results, out=lines.append)
            self.assertEqual(code, 0)
            self.assertEqual(lines[0], "llm cleanup not measured: Ollama unavailable: test")
            self.assertTrue(any(line.startswith("dictation / rules: WER clean") for line in lines))
            outputs = list((self.results / "cleanup").glob("*/compare.json"))
            self.assertEqual(len(outputs), 1)
            self.assertEqual(json.loads(outputs[0].read_text(encoding="utf-8"))["llm_note"], "Ollama unavailable: test")
            lines.clear()
            self.assertEqual(pipeline.main(["--compare-cleanup", str(self.root)], results_dir=self.results, out=lines.append), 2)
            self.assertEqual(lines, ["error: the run folder must be under bench/results/"])

    def test_default_streamer_uses_the_product_engine_and_tuning(self):
        from quill import streaming, whisper

        self.assertEqual(pipeline.STREAM_MODEL, whisper.DEFAULT_MODEL)
        self.assertEqual(pipeline.ENGINE_MODEL, "large-v3")  # the raw baseline is unchanged
        seen = []
        fake_stream = lambda model, takes, vocabulary, options: seen.append((model, options)) or []
        baseline = mock.Mock(model="large-v3", whisper=object())
        with mock.patch("bench.streaming.stream_takes", fake_stream):
            stream, options = pipeline.default_streamer(baseline)
            stream([], mock.Mock(vocabulary=lambda: []))
            self.assertEqual(options["model"], "large-v3-turbo")
            self.assertEqual({k: v for k, v in options.items() if k != "model"},
                             dataclasses.asdict(streaming.options_for("large-v3-turbo")))
            model, tuning = seen[-1]
            self.assertIsInstance(model, whisper.Whisper)
            self.assertEqual((model.model, model.loaded), ("large-v3-turbo", False))
            self.assertEqual(tuning, streaming.options_for("large-v3-turbo"))
            stream.close()
            same = mock.Mock(model="large-v3-turbo", whisper=object())
            stream, _ = pipeline.default_streamer(same)
            stream([], mock.Mock(vocabulary=lambda: []))
            self.assertIs(seen[-1][0], same.whisper)
            stream.close()  # the shared model stays with the engine

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
        streamers_closed = []

        def streamer_factory(engine):
            def stream(takes, hints):
                return [(t.clean, 0.2) for t in takes]

            stream.close = lambda: streamers_closed.append(1)
            return stream, {"step_s": 0.5}

        def run(*argv, engine=None):
            lines.clear()
            with mock.patch.object(pipeline, "load_settings", return_value=self.fx.settings):
                return pipeline.main(
                    list(argv),
                    engine_factory=lambda: engine,
                    judge_factory=lambda: (judge_yes, None),
                    streamer_factory=streamer_factory,
                    results_dir=self.results,
                    out=lines.append,
                )

        engine = engine_for(self.fx.loaded(), lambda take: take.reference)
        self.assertEqual(run("--stage", "streamed", "--summary", str(summary_path), engine=engine), 0)
        self.assertEqual(engine.closed, 1)
        self.assertEqual(streamers_closed, [1])
        streamed = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(streamed["stages"], ["raw", "streamed"])
        self.assertNotIn("transcribe_p95_s", streamed["sets"]["dictation"]["stages"]["streamed"])
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
            "filler_removal_rate": 0.96, "content_deleted": 0, "content_deleted_by_cleanup": 0, "recurrences_fixed_rate": 1.0, "new_errors": 0}
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
        summary = summary_with(latency={"p95_s": 0.45, "max_audio_s": 15.0}, stages=pipeline.STAGE_ORDER)
        self.assertEqual(self.failed(summary, *pipeline.TARGET_NAMES), [])

    def test_latency_target_is_half_a_second(self):
        self.assertEqual(pipeline.MAX_LATENCY_P95_S, 0.5)
        self.assertEqual(self.failed(summary_with(latency={"p95_s": 0.5, "max_audio_s": 15.0}), "latency"), [])
        self.assertEqual(self.failed(summary_with(latency={"p95_s": 0.51, "max_audio_s": 15.0}), "latency"),
                         ["FAIL latency: p95 0.51 s (target <= 0.5 s, utterances up to 15.0 s)"])

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
        summary = summary_with(stages=("raw", "cleanup"), dictation={"filler_removal_rate": 0.9, "content_deleted_by_cleanup": 1})
        self.assertEqual(self.failed(summary, "cleanup"), [
            "FAIL cleanup: dictation filler/repetition removal 90.0 % (cleanup; target >= 95 %)",
            "FAIL cleanup: dictation content words deleted by cleanup 1 (target 0)",
        ])
        # Recognition omissions stay in content_deleted but are not the cleanup's.
        summary = summary_with(stages=("raw", "cleanup"), dictation={"content_deleted": 19})
        self.assertEqual(self.failed(summary, "cleanup"), [])
        self.assertEqual(len(self.failed(summary_with(stages=("raw", "streamed")), "cleanup")), 2)
        self.assertEqual(len(self.failed(summary, "vocabulary")), 2)
        summary = summary_with(stages=("raw", "vocabulary"), commands={"name_error_rate": 0.2})
        self.assertEqual(self.failed(summary, "vocabulary"), ["FAIL vocabulary: commands project-name error 20.0 % (target <= 10 %)"])
        self.assertEqual(len(self.failed(summary_with(latency={"p95_s": 0.6, "max_audio_s": 15.0}), "latency")), 1)
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
