import json
import tempfile
import unittest
from pathlib import Path

from bench.metrics import (
    ItemResult,
    aggregate,
    build_summary,
    corpus_wer,
    count_occurrences,
    load_terms,
    percentile_nearest_rank,
    skipped,
    term_recall,
    word_edits,
    write_summary,
)


class WordEditsTest(unittest.TestCase):
    def test_hand_computed_alignment(self):
        # a b c d -> a x c d e: one substitution (b->x), one insertion (e).
        counts = word_edits("a b c d".split(), "a x c d e".split())
        self.assertEqual((counts.substitutions, counts.deletions, counts.insertions), (1, 0, 1))
        self.assertEqual(counts.errors, 2)
        self.assertEqual(counts.reference_words, 4)

    def test_deletions_and_empty(self):
        self.assertEqual(word_edits(["um", "dois"], []).deletions, 2)
        self.assertEqual(word_edits([], ["um"]).insertions, 1)
        self.assertEqual(word_edits([], []).errors, 0)

    def test_corpus_wer_sums_before_dividing(self):
        pairs = [("a b c d", "a x c d e"), ("um dois", "")]
        # (2 + 2) / (4 + 2)
        self.assertAlmostEqual(corpus_wer(pairs), 4 / 6)

    def test_corpus_wer_uses_normalizer(self):
        self.assertEqual(corpus_wer([("Abre o VS Code, já!", "abre o vscode já")]), 0.0)

    def test_corpus_wer_empty(self):
        self.assertIsNone(corpus_wer([]))


class RecallTest(unittest.TestCase):
    def test_count_occurrences_non_overlapping(self):
        self.assertEqual(count_occurrences("a a a".split(), ["a", "a"]), 1)
        self.assertEqual(count_occurrences("x pull request y pull request".split(), ["pull", "request"]), 2)
        self.assertEqual(count_occurrences(["a"], []), 0)

    def test_per_occurrence_recall(self):
        pairs = [
            ("faz o push e o push outra vez", "faz o push e o bush outra vez"),  # 2 expected, 1 found
            ("revê o README", "revê o read me"),  # equivalence: found
            ("sem termos aqui", "prompt a mais"),  # hypothesis-only terms do not count
        ]
        counts = term_recall(pairs, ["push", "readme", "prompt"])
        self.assertEqual((counts.expected, counts.found), (3, 2))
        self.assertAlmostEqual(counts.error_rate, 1 / 3)

    def test_multiword_names(self):
        counts = term_recall([("abre o painel zeta-board", "abre o painel zeta board")], ["Zeta-Board"])
        self.assertEqual((counts.expected, counts.found), (1, 1))

    def test_no_occurrences(self):
        self.assertIsNone(term_recall([("olá", "olá")], ["push"]).error_rate)

    def test_committed_terms_file(self):
        terms = load_terms()
        self.assertIn("vscode", terms)
        self.assertTrue(all(t == t.strip() and t for t in terms))


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self):
        values = list(range(1, 11))  # 1..10
        self.assertEqual(percentile_nearest_rank(values, 50), 5)  # ceil(5) = 5th
        self.assertEqual(percentile_nearest_rank(values, 95), 10)  # ceil(9.5) = 10th
        self.assertEqual(percentile_nearest_rank(values, 0), 1)
        self.assertEqual(percentile_nearest_rank(values, 100), 10)

    def test_unsorted_and_twenty_values(self):
        self.assertEqual(percentile_nearest_rank([0.3, 0.1, 0.2], 50), 0.2)  # ceil(1.5) = 2nd
        twenty = [i / 10 for i in range(20, 0, -1)]  # 0.1..2.0
        self.assertEqual(percentile_nearest_rank(twenty, 95), 1.9)  # ceil(19) = 19th
        self.assertEqual(percentile_nearest_rank(twenty, 50), 1.0)  # 10th

    def test_empty_and_bounds(self):
        self.assertIsNone(percentile_nearest_rank([], 50))
        with self.assertRaises(ValueError):
            percentile_nearest_rank([1], 101)


def fixture_items():
    return [
        ItemResult("pt-01", "abre o painel zeta-board e faz push", "abre o painel zeta board e faz bush", ("zeta-board",), True),
        ItemResult("pt-02", "mostra o log do quadro", "mostra o log do quadro", (), False),
    ]


class AggregateTest(unittest.TestCase):
    def test_aggregate_numbers(self):
        result = aggregate("fake", "raw", fixture_items(), ["push", "log"], [0.4, 0.2, 0.9])
        # Words: ref1 has 8 (zeta board split), 1 substitution; ref2 has 5, 0 errors.
        self.assertEqual(result["wer"], round(1 / 13, 4))
        self.assertEqual(result["term_error_rate"], 0.5)
        self.assertEqual(result["term_occurrences"], 2)
        self.assertEqual(result["name_error_rate"], 0.0)
        self.assertEqual(result["intent_preserved"], 0.5)
        self.assertEqual(result["latency_p50_s"], 0.4)
        self.assertEqual(result["latency_p95_s"], 0.9)
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["status"], "ok")

    def test_skipped_requires_reason(self):
        self.assertEqual(skipped("fake", "hints", "no hint support")["status"], "skipped")
        with self.assertRaises(ValueError):
            skipped("fake", "hints", "  ")

    def test_summary_has_no_spoken_text(self):
        items = fixture_items()
        results = [aggregate("fake", "raw", items, ["push"]), skipped("fake", "hints", "unsupported")]
        summary = build_summary(
            results, expected_n=2, n_valid=2, invalid_reasons={"x": 1}, discarded=2, recording_origin={"mic": 2}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            write_summary(path, summary, [i.reference for i in items], ["zeta-board"])
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["dataset"]["recording_origin"], {"mic": 2})
        text = json.dumps(data)
        self.assertNotIn("painel", text)
        self.assertNotIn("zeta", text)
        self.assertEqual({r["variant"] for r in data["results"]}, {"raw", "hints"})

    def test_write_summary_refuses_leaks(self):
        leaky = skipped("fake", "raw", "failed on abre o painel")
        summary = build_summary([leaky], expected_n=1, n_valid=1, invalid_reasons={}, discarded=0, recording_origin={})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            with self.assertRaises(ValueError):
                write_summary(path, summary, ["abre o painel zeta-board"], [])
            with self.assertRaises(ValueError):
                write_summary(path, build_summary([skipped("zeta-board", "raw", "x")], expected_n=1, n_valid=1,
                                                  invalid_reasons={}, discarded=0, recording_origin={}),
                              [], ["Zeta-Board"])
            self.assertFalse(path.exists())

    def test_duplicate_results_rejected(self):
        with self.assertRaises(ValueError):
            build_summary([skipped("a", "b", "r"), skipped("a", "b", "r")], expected_n=1, n_valid=1,
                          invalid_reasons={}, discarded=0, recording_origin={})


if __name__ == "__main__":
    unittest.main()
