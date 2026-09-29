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
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
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
            pipeline.measure(sets, "unknown", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
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

    def vocabulary_run(self, vocabulary=None, misheard="zeta bord"):
        sets = self.fx.loaded()
        engine = engine_for(sets, lambda take: take.reference)
        calls = []

        def streamer(takes, hints):  # recognition misspells the name in every take
            calls.append(hints.vocabulary())
            return [(take.clean.replace("zeta-board", misheard), 0.1) for take in takes]

        summary = pipeline.measure(sets, "vocabulary", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                                   results_dir=self.results, log=lambda line: None, streamer=streamer,
                                   vocabulary=vocabulary or pipeline.NO_VOCABULARY)
        return summary, calls

    def test_vocabulary_stage_matches_the_cleaned_text(self):
        summary, calls = self.vocabulary_run()
        self.assertEqual(summary["stages"], ["raw", "streamed", "cleanup", "vocabulary"])
        self.assertEqual(len(calls), 2)  # the baseline hints: no second streaming
        self.assertEqual(calls[0], ["omega", "zeta-board", *TERMS])
        self.assertEqual(summary["vocabulary"], {"names": 0, "terms": 0, "variants": 0, "hints": 5, "hints_dropped": 0, "restreamed": False})
        stages = summary["sets"]["dictation"]["stages"]
        self.assertEqual((stages["cleanup"]["name_error_rate"], stages["vocabulary"]["name_error_rate"]), (0.5, 0.0))
        self.assertEqual(stages["vocabulary"]["vocabulary_changes"], 1)
        self.assertLess(stages["vocabulary"]["wer_clean"], stages["cleanup"]["wer_clean"])
        self.assertEqual(summary["sets"]["commands"]["stages"]["vocabulary"]["name_error_rate"], 0.0)
        self.assertIsNone(stages["vocabulary"]["content_deleted_by_cleanup"])
        rows = json.loads((self.results / "pipeline" / "run" / "dictation" / "vocabulary.json").read_text(encoding="utf-8"))
        self.assertIn("zeta-board", rows[0]["hypothesis"])
        self.assertFalse((self.results / "pipeline" / "run" / "dictation" / "vocabulary-source.json").exists())
        self.assertEqual(pipeline._final_stage(summary["sets"]["dictation"])[0], "vocabulary")
        self.assertIn("| ditado | vocabulary | 3 |", pipeline.render_block(summary))
        # The commands set is an indicator (Sponsor decision 2026-09-29): its
        # unmeasured English-term error is reported without failing.
        results = pipeline.check_targets(summary, ["vocabulary"])
        self.assertTrue(all(met for met, _ in results))
        self.assertIn((True, "info vocabulary: commands English-term error not measured "
                             "(indicator, not a gate; target <= 10 % applies to dictation)"), results)

    def test_unrelated_words_stay_and_the_gap_is_reported(self):
        summary, _ = self.vocabulary_run(misheard="painel")  # far from the name: not a misrecognition to fix
        stages = summary["sets"]["dictation"]["stages"]
        self.assertEqual((stages["vocabulary"]["name_error_rate"], stages["vocabulary"]["vocabulary_changes"]), (0.5, 0))
        self.assertEqual(stages["vocabulary"]["wer_clean"], stages["cleanup"]["wer_clean"])
        results = pipeline.check_targets(summary, ["vocabulary"])
        failed = [line for met, line in results if not met]
        # Only the dictation set gates; the commands miss is reported as an indicator.
        self.assertEqual(failed, ["FAIL vocabulary: dictation project-name error 50.0 % (target <= 10 %)"])
        commands = summary["sets"]["commands"]["stages"]["vocabulary"]["name_error_rate"]
        self.assertGreater(commands, pipeline.MAX_NAME_ERROR)
        self.assertIn((True, f"info vocabulary: commands project-name error {100 * commands:.1f} % "
                             "(indicator, not a gate; target <= 10 % applies to dictation)"), results)

    def test_corrections_stage_learns_online_after_the_vocabulary_stage(self):
        sets = self.fx.loaded()
        engine = engine_for(sets, lambda take: take.reference)

        def streamer(takes, hints):  # the same misheard name in every take
            return [(take.clean.replace("zeta-board", "zeta bord"), 0.1) for take in takes]

        summary = pipeline.measure(sets, "corrections", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                                   results_dir=self.results, log=lambda line: None, streamer=streamer)
        self.assertEqual(summary["stages"], ["raw", "streamed", "cleanup", "vocabulary", "corrections"])
        row = summary["sets"]["dictation"]["stages"]["corrections"]
        for field in ("recurrences", "recurrences_fixed", "recurrences_fixed_rate", "recurrences_not_yet_active",
                      "new_errors", "corrections_applied", "takes_with_corrections", "learned_active",
                      "learned_pending", "learned_conflicts"):
            self.assertIn(field, row)
        self.assertEqual(row["new_errors"], 0)
        self.assertEqual(pipeline._final_stage(summary["sets"]["dictation"])[0], "corrections")
        self.assertTrue((self.results / "pipeline" / "run" / "dictation" / "corrections.json").is_file())
        block = pipeline.render_block(summary)
        self.assertIn("| ditado | corrections | 3 |", block)
        self.assertIn("| Conjunto | Erros já aprendidos que se repetem |", block)
        self.assertEqual([line for met, line in pipeline.check_targets(summary, ["corrections"]) if not met], [])

    def test_profiles_stage_applies_each_take_style_and_is_the_final_stage(self):
        sets = self.fx.loaded()
        engine = engine_for(sets, lambda take: take.reference)

        def streamer(takes, hints):
            return [(take.clean, 0.1) for take in takes]

        summary = pipeline.measure(sets, "profiles", engine, judge_yes, None, TERMS, self.results / "pipeline" / "run",
                                   results_dir=self.results, log=lambda line: None, streamer=streamer)
        self.assertEqual(summary["stages"], list(pipeline.STAGE_ORDER))
        dictation = summary["sets"]["dictation"]["stages"]
        row = dictation["profiles"]
        self.assertEqual(row["profile_takes"], {"claude-code": 1, "email": 1, "whatsapp": 1})
        self.assertEqual(row["profile_changes"], 1)  # the WhatsApp message loses its final period
        self.assertEqual(summary["sets"]["commands"]["stages"]["profiles"]["profile_takes"], {"default": 3})
        # Punctuation and capitals only: the words and every word metric stay the same.
        for key in ("wer_clean", "wer_verbatim", "filler_removal_rate", "content_deleted", "name_error_rate"):
            self.assertEqual(row[key], dictation["corrections"][key], key)
        rows = json.loads((self.results / "pipeline" / "run" / "dictation" / "profiles.json").read_text(encoding="utf-8"))
        corrected = json.loads((self.results / "pipeline" / "run" / "dictation" / "corrections.json").read_text(encoding="utf-8"))
        self.assertEqual(corrected[1]["hypothesis"], "Olá, amanhã levo o bolo verde.")
        self.assertEqual(rows[1]["hypothesis"], "Olá, amanhã levo o bolo verde")
        self.assertEqual(rows[0]["hypothesis"], corrected[0]["hypothesis"])
        self.assertEqual(pipeline._final_stage(summary["sets"]["dictation"])[0], "profiles")
        self.assertIn("| ditado | profiles | 3 |", pipeline.render_block(summary))
        failed = [line for met, line in pipeline.check_targets(summary, ["overall", "cleanup", "vocabulary", "corrections"])
                  if not met]
        self.assertEqual(failed, [])
        self.assertIn("(profiles; target", " ".join(line for _, line in pipeline.check_targets(summary, ["overall"])))

    def test_personal_vocabulary_streams_again_with_the_product_hints(self):
        from quill.vocabulary import parse_vocabulary

        personal = parse_vocabulary({"names": ["Nimbus-Deck"], "terms": ["kubectl"], "variants": {"Nimbus-Deck": ["nimbos"]}})
        summary, calls = self.vocabulary_run(personal)
        self.assertEqual(len(calls), 4)  # streamed, then streamed again for the vocabulary stage, per set
        self.assertEqual(calls[0], ["omega", "zeta-board", *TERMS])
        self.assertEqual(calls[1], ["Nimbus-Deck", "omega", "zeta-board", "kubectl", *TERMS])
        self.assertEqual(summary["vocabulary"], {"names": 1, "terms": 1, "variants": 1, "hints": 7, "hints_dropped": 0, "restreamed": True})
        self.assertEqual(summary["sets"]["dictation"]["stages"]["vocabulary"]["name_error_rate"], 0.0)
        run = self.results / "pipeline" / "run" / "dictation"
        source = json.loads((run / "vocabulary-source.json").read_text(encoding="utf-8"))
        self.assertIn("zeta bord", source[0]["hypothesis"])

    def test_product_hint_order(self):
        from quill.vocabulary import parse_vocabulary

        personal = parse_vocabulary({"names": ["Zulo"], "terms": ["deploy", "kubectl"]})
        hints = pipeline.product_hints(personal, ["zeta-board", "omega"], TERMS)
        self.assertEqual(hints.vocabulary(), ["Zulo", "omega", "zeta-board", "deploy", "kubectl", "commit", "review"])
        self.assertEqual(pipeline.product_hints(pipeline.NO_VOCABULARY, ["zeta-board", "omega"], TERMS).vocabulary(),
                         pipeline.build_hints(["zeta-board", "omega"], TERMS).vocabulary())

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
        # The vocabulary stage reads the personal vocabulary given on the command line.
        vocabulary = self.root / "vocabulary.toml"
        vocabulary.write_text("names = 1\n", encoding="utf-8")
        engine = engine_for(self.fx.loaded(), lambda take: take.clean)
        self.assertEqual(run("--stage", "vocabulary", "--vocabulary", str(vocabulary), "--summary", str(summary_path), engine=engine), 2)
        self.assertEqual(lines, ["error: vocabulary: names must be a list of strings"])
        vocabulary.write_text('names = ["Privado-Nome"]\n', encoding="utf-8")
        engine = engine_for(self.fx.loaded(), lambda take: take.clean)
        self.assertEqual(run("--stage", "vocabulary", "--vocabulary", str(vocabulary), "--summary", str(summary_path), engine=engine), 0)
        written = summary_path.read_text(encoding="utf-8")
        self.assertNotIn("Privado", written)
        self.assertEqual(json.loads(written)["vocabulary"]["names"], 1)
        # Later stages keep the personal vocabulary.
        engine = engine_for(self.fx.loaded(), lambda take: take.clean)
        self.assertEqual(run("--stage", "corrections", "--vocabulary", str(vocabulary), "--summary", str(summary_path), engine=engine), 0)
        self.assertEqual(json.loads(summary_path.read_text(encoding="utf-8"))["vocabulary"]["names"], 1)
        # No terms in the commands fixture: reported, but the commands set is not a gate.
        self.assertEqual(run("--summary", str(summary_path), "--require", "vocabulary"), 0)
        self.assertIn("ok   vocabulary: dictation project-name error 0.0 % (target <= 10 %)", lines)
        self.assertIn("info vocabulary: commands English-term error not measured "
                      "(indicator, not a gate; target <= 10 % applies to dictation)", lines)
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


def typing_result(**totals):
    return {"selftest": "typing", "version": 1, "finished_utc": "2026-09-29T19:45:55Z", "passed": True,
            "totals": {"targets": 5, "targets_passed": 5, "cases": 20, "cases_passed": 20, "characters_expected": 4190,
                       "characters_typed": 4190, "lost": 0, "extra": 0, "changed": 0, "clipboard_changed_targets": 0,
                       **totals},
            "targets": [{"name": "edit", "cases": [{"case": "short", "seconds": 0.047}]}]}


def triggers_result():
    return {"selftest": "triggers", "version": 1, "finished_utc": "2026-09-29T19:47:08Z", "passed": True,
            "duration_s": 37.5, "click_to_focus": True,
            "totals": {"starts": 2, "ends": 2, "callback_errors": 0, "handler_errors": 0, "machine_errors": 0,
                       "worker_lag_ms_p95": 0.4, "hold_s_max": 1.17},
            "signals": [{"action": "dictation", "signal": "cancel", "reason": "short_hold", "count": 1},
                        {"action": "send_claude", "signal": "stop", "reason": "release", "count": 1}],
            "ignored": [], "inputs": [{"trigger": "xbutton1", "event": "down_suppressed", "count": 1}],
            "clicks": [{"action": "send_claude", "reason": "clicked", "count": 1}]}


def indicator_result():
    return {"version": 1, "finished": "2026-09-29T19:49:11Z", "position": "pointer", "seconds": 51.7,
            "states_shown": 23, "foreground_samples": 1026, "foreground_was_indicator": 0, "frames": 1401,
            "frame_ms_p50": 1.25, "frame_ms_p95": 1.66, "passed": True}


def acceptance():
    return {"typing": pipeline.selftest_block("typing", typing_result()),
            "triggers": pipeline.selftest_block("triggers", triggers_result()),
            "indicator": pipeline.selftest_block("indicator", indicator_result())}


def rewrite_summary():
    return {"schema": 1, "kind": "rewrite", "engine": {"model": "large-v3-turbo", "step_s": 0.5},
            "rewrite_model": "qwen3:8b", "judge": "local", "takes": 20, "instruction_wer": 0.2075,
            "instruction_exact": 5, "valid": 19, "valid_rate": 0.95,
            "reasons": {"command_rewritten": 19, "command_unchanged": 1}, "invalid_details": {},
            "checks": {"changed": 15}, "checks_applicable": {"changed": 15}, "checks_passed": 16,
            "checks_passed_rate": 0.8, "judged": 16, "judge_yes": 16, "correct": 16, "correct_rate": 0.8,
            "by_kind": {"encurtar": {"takes": 3, "checks_passed": 1, "correct": 1}},
            "latency": {"total_p95_s": 1.011}, "dataset": {"recorded": 20}}


class AcceptanceTest(unittest.TestCase):
    def test_command_mode_keeps_counts_and_drops_timings(self):
        block = pipeline.command_mode_block(rewrite_summary())
        self.assertEqual(block["engine_model"], "large-v3-turbo")
        self.assertEqual((block["takes"], block["correct"], block["instruction_wer"]), (20, 16, 0.2075))
        self.assertNotIn("latency", block)
        self.assertNotIn("1.011", json.dumps(block))
        with self.assertRaises(pipeline.SettingsError):
            pipeline.command_mode_block({"kind": "pipeline"})

    def test_self_tests_keep_counts_only_and_take_the_newest_file(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            self.assertEqual(pipeline.acceptance_block(folder), {"typing": None, "triggers": None, "indicator": None})
            (folder / "typing-20260929T100000Z.json").write_text(json.dumps(typing_result(lost=3)), encoding="utf-8")
            (folder / "typing-20260929T194555Z.json").write_text(json.dumps(typing_result()), encoding="utf-8")
            (folder / "triggers-20260929T194708Z.json").write_text(json.dumps(triggers_result()), encoding="utf-8")
            (folder / "indicator-20260929T194911Z.json").write_text(json.dumps(indicator_result()), encoding="utf-8")
            block = pipeline.acceptance_block(folder)
        self.assertEqual(block, acceptance())
        self.assertEqual(block["typing"]["lost"], 0)
        self.assertEqual(block["typing"]["day"], "2026-09-29")
        text = json.dumps(block)
        for timing in ("seconds", "duration_s", "hold_s_max", "worker_lag", "frame_ms", "frames", "edit"):
            self.assertNotIn(timing, text)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "typing-1.json").write_text("not json", encoding="utf-8")
            with self.assertRaises(pipeline.SettingsError):
                pipeline.acceptance_block(Path(root))

    def test_block_renders_command_mode_and_self_tests(self):
        summary = summary_with()
        summary["command_mode"] = pipeline.command_mode_block(rewrite_summary())
        summary["acceptance"] = acceptance()
        block = pipeline.render_block(summary)
        self.assertIn("| large-v3-turbo + qwen3:8b | 20 | " + pipeline._pt_percent(0.2075)
                      + " | 5 de 20 | 19 de 20 | 16 de 20 | 16 de 16 | 16 de 20 |", block)
        self.assertIn("| encurtar | 3 | 1 | 1 |", block)
        self.assertIn("perdidos 0, a mais 0, trocados 0", block)
        self.assertIn("indicador em primeiro plano em 0 de 1026 amostras", block)
        self.assertIn("| dictation | cancel | short_hold | 1 |", block)
        self.assertNotIn("1,01", block)
        summary["acceptance"] = {"typing": None, "triggers": None, "indicator": None}
        self.assertIn("| digitação | — | não medida |", pipeline.render_block(summary))
        document = pipeline.write_doc("# Doc\n", summary)
        self.assertEqual(pipeline.check_doc(document, summary), [])

    def test_cli_merges_into_an_existing_summary(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path, rewrite, folder = root / "s.json", root / "rewrite.json", root / "selftest"
            path.write_text(json.dumps(summary_with()), encoding="utf-8")
            rewrite.write_text(json.dumps(rewrite_summary()), encoding="utf-8")
            folder.mkdir()
            (folder / "typing-20260929T194555Z.json").write_text(json.dumps(typing_result()), encoding="utf-8")
            lines = []
            code = pipeline.main(["--summary", str(path), "--add-command-mode", str(rewrite), "--add-selftests", str(folder),
                                  "--require", "desktop"], out=lines.append)
            self.assertEqual(code, 1)  # triggers and indicator results are missing
            self.assertEqual(lines[0], "summary updated (command mode, self-tests)")
            merged = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(merged["command_mode"]["correct"], 16)
            self.assertIsNone(merged["acceptance"]["indicator"])
            self.assertEqual(merged["sets"], summary_with()["sets"])
            lines = []
            self.assertEqual(pipeline.main(["--summary", str(path), "--add-command-mode", str(root / "none.json")],
                                           out=lines.append), 2)
            self.assertEqual(lines, ["error: rewrite summary not found (run bench.rewrite first)"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.main(["--stage", "raw", "--add-selftests", "local/selftest"], out=lambda line: None)


class TargetTest(unittest.TestCase):
    def failed(self, summary, *targets):
        return [line for met, line in pipeline.check_targets(summary, targets) if not met]

    def test_all_met(self):
        summary = summary_with(latency={"p95_s": 0.45, "max_audio_s": 15.0}, stages=pipeline.STAGE_ORDER)
        summary["acceptance"] = acceptance()
        self.assertEqual(self.failed(summary, *pipeline.TARGET_NAMES), [])

    def test_desktop_target_needs_every_self_test_and_zero_wrong_characters(self):
        self.assertEqual(self.failed(summary_with(), "desktop"), [
            "FAIL desktop: typing self-test not measured",
            "FAIL desktop: trigger self-test not measured",
            "FAIL desktop: indicator self-test not measured",
        ])
        for key in ("lost", "extra", "changed"):
            summary = summary_with()
            summary["acceptance"] = acceptance()
            summary["acceptance"]["typing"][key] = 1
            self.assertEqual(len(self.failed(summary, "desktop")), 1, key)
        summary = summary_with()
        summary["acceptance"] = acceptance()
        summary["acceptance"]["typing"]["extra"] = 2
        self.assertEqual(self.failed(summary, "desktop"),
                         ["FAIL desktop: typing lost 0, duplicated or extra 2, changed 0 of 4190 characters (target 0)"])
        summary["acceptance"] = acceptance()
        summary["acceptance"]["indicator"]["foreground_was_indicator"] = 3
        self.assertEqual(self.failed(summary, "desktop"),
                         ["FAIL desktop: indicator in the foreground in 3 of 1026 samples (target 0)"])
        summary["acceptance"] = acceptance()
        summary["acceptance"]["triggers"]["errors"] = 1
        summary["acceptance"]["typing"]["clipboard_changed_targets"] = 1
        self.assertEqual(len(self.failed(summary, "desktop")), 2)

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
        # Sponsor decision 2026-09-29: the dictation set gates; commands is an indicator.
        summary = summary_with(stages=("raw", "vocabulary"), commands={"name_error_rate": 0.2, "term_error_rate": 0.5})
        self.assertEqual(self.failed(summary, "vocabulary"), [])
        summary = summary_with(stages=("raw", "vocabulary"), dictation={"name_error_rate": 0.2, "term_error_rate": 0.11})
        self.assertEqual(self.failed(summary, "vocabulary"), [
            "FAIL vocabulary: dictation project-name error 20.0 % (target <= 10 %)",
            "FAIL vocabulary: dictation English-term error 11.0 % (target <= 10 %)",
        ])
        summary = summary_with(stages=("raw", "corrections"), dictation={"recurrences": 4, "recurrences_fixed_rate": 0.75})
        self.assertEqual(self.failed(summary, "corrections"),
                         ["FAIL corrections: dictation learned recurrences fixed 75.0 % of 4 (target 100 %)"])
        summary = summary_with(stages=("raw", "corrections"), dictation={"recurrences": 0, "recurrences_fixed_rate": None})
        self.assertEqual(self.failed(summary, "corrections"), [])
        self.assertIn((True, "ok   corrections: dictation no learned recurrence to fix (0 recurrences; target 100 % when present)"),
                      pipeline.check_targets(summary, ["corrections"]))
        self.assertEqual(len(self.failed(summary_with(stages=("raw", "vocabulary")), "corrections")), 2)
        self.assertEqual(len(self.failed(summary_with(latency={"p95_s": 0.6, "max_audio_s": 15.0}), "latency")), 1)
        self.assertEqual(len(self.failed(summary_with(), "unknown")), 1)


def sample(hypothesis, clean):
    return pipeline.Sample(SimpleNamespace(clean=clean), hypothesis, 0.0)


class CorrectionsStageTest(unittest.TestCase):
    """The online simulation with invented takes; ``clean`` is what the user wanted."""

    def run_takes(self, *takes):
        return pipeline.correct_samples([sample(h, c) for h, c in takes], lambda: 0.0)

    def test_active_after_two_takes_then_every_recurrence_is_fixed(self):
        takes = [(f"corre o kube control {word}", f"corre o kubectl {word}") for word in ("hoje", "agora", "logo", "já")]
        corrected, info = self.run_takes(*takes)
        self.assertEqual([s.hypothesis for s in corrected[2:]], ["corre o kubectl logo", "corre o kubectl já"])
        self.assertEqual(corrected[1].hypothesis, "corre o kube control agora")  # seen once so far: not applied
        self.assertEqual((info["recurrences"], info["recurrences_fixed"], info["recurrences_fixed_rate"]), (2, 2, 1.0))
        self.assertEqual((info["recurrences_not_yet_active"], info["new_errors"], info["corrections_applied"]), (1, 0, 2))
        self.assertEqual((info["learned_active"], info["learned_pending"], info["takes_with_corrections"]), (1, 0, 2))

    def test_a_learned_replacement_that_breaks_a_right_word_is_a_new_error(self):
        corrected, info = self.run_takes(("ram os testes", "run os testes"), ("ram outra vez", "run outra vez"),
                                         ("a ram do servidor", "a ram do servidor"))
        self.assertEqual(corrected[2].hypothesis, "a run do servidor")
        self.assertEqual((info["new_errors"], info["recurrences"], info["recurrences_fixed_rate"]), (1, 0, None))
        self.assertIn((False, "FAIL corrections: commands new word errors 1 (target 0)"),
                      pipeline.check_targets(summary_with(stages=("raw", "corrections"), commands=info), ["corrections"]))

    def test_conflicts_and_function_words_are_never_applied(self):
        takes = (("o velo branco", "o velho branco"), ("o velo branco", "o vélo branco"),
                 ("o velo branco", "o velho branco"), ("envio dos ficheiros", "envio do ficheiros"),
                 ("abre dos painéis", "abre do painéis"), ("envio dos ficheiros", "envio dos ficheiros"))
        corrected, info = self.run_takes(*takes)
        self.assertEqual([s.hypothesis for s in corrected], [hypothesis for hypothesis, _ in takes])
        self.assertEqual((info["learned_conflicts"], info["learned_active"], info["corrections_applied"]), (2, 0, 0))
        self.assertEqual((info["new_errors"], info["recurrences"]), (0, 0))

    def test_case_and_number_equivalences_are_not_errors(self):
        _, info = self.run_takes(("abre o Painel", "abre o painel"), ("abre o Painel", "abre o painel"))
        self.assertEqual((info["takes_with_corrections"], info["learned_pending"]), (0, 0))
        self.assertEqual(pipeline.error_pairs("corre o ram", "corre o run"), Counter({(("ram",), ("run",)): 1}))

    def test_correct_positions(self):
        self.assertEqual(pipeline.correct_positions(["a", "b", "c"], ["a", "x", "c"]), frozenset({0, 2}))
        self.assertEqual(pipeline.correct_positions(["a", "b"], []), frozenset())


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
