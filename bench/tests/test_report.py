import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bench import report
from bench.metrics import build_summary, skipped


def ok_result(engine, variant, n=44, wer=0.1234):
    return {
        "engine": engine, "variant": variant, "status": "ok", "skipped_reason": None, "n": n,
        "wer": wer, "term_error_rate": 0.05, "term_occurrences": 20, "name_error_rate": 0.0,
        "name_occurrences": 30, "intent_preserved": 0.95, "intent_judged": n,
        "latency_p50_s": 0.42, "latency_p95_s": 1.234, "latency_samples": 20,
    }


def summary_with(*results):
    return build_summary(list(results), expected_n=44, n_valid=44, invalid_reasons={},
                         discarded=44, recording_origin={"mic": 44})


class RenderTest(unittest.TestCase):
    def test_render_rows(self):
        block = report.render_block(summary_with(ok_result("fake", "raw"), skipped("fake", "hints", "no hints")))
        lines = block.splitlines()
        self.assertEqual(lines[0], "<!-- bench:summary:start lang=pt -->")
        self.assertEqual(lines[-1], report.END_MARKER)
        self.assertIn("| fake | raw | 44 | 12,3\u00a0% | 5,0\u00a0% | 0,0\u00a0% | 95,0\u00a0% | 0,42 | 1,23 | ok |", lines)
        self.assertIn("| fake | hints | 0 | — | — | — | — | — | — | SKIPPED: no hints |", lines)

    def test_render_english_and_pipe_escape(self):
        block = report.render_block(summary_with(ok_result("a|b", "raw")), "en")
        self.assertIn(r"| a\|b | raw | 44 | 12.3% |", block)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.summary = summary_with(ok_result("fake", "raw"), skipped("fake", "hints", "no hints"))
        self.document = report.write_block("# Relatório\n\nTexto.\n", self.summary)

    def test_in_sync(self):
        self.assertEqual(report.check(self.document, self.summary), [])
        self.assertEqual(report.check(self.document.replace("\n", "\r\n"), self.summary), [])

    def test_edited_block_fails(self):
        edited = self.document.replace("12,3", "11,3")
        self.assertTrue(any("differs" in p for p in report.check(edited, self.summary)))

    def test_changed_summary_fails(self):
        changed = summary_with(ok_result("fake", "raw", wer=0.2), skipped("fake", "hints", "no hints"))
        self.assertTrue(report.check(self.document, changed))

    def test_missing_block_fails(self):
        self.assertTrue(any("not found" in p for p in report.check("# nada\n", self.summary)))

    def test_n_mismatch_without_skip_fails(self):
        summary = summary_with(ok_result("fake", "raw", n=43))
        document = report.write_block("", summary)
        problems = report.check(document, summary)
        self.assertEqual(len(problems), 1)
        self.assertIn("n=43", problems[0])

    def test_skipped_without_reason_fails(self):
        result = skipped("fake", "raw", "x")
        result["skipped_reason"] = ""
        summary = summary_with(result)
        self.assertTrue(any("without a reason" in p for p in report.check(report.write_block("", summary), summary)))

    def test_empty_results_fail(self):
        summary = summary_with()
        self.assertIn("summary has no results", report.check(report.write_block("", summary), summary))

    def test_write_block_replaces_in_place(self):
        changed = summary_with(ok_result("fake", "raw", wer=0.2), skipped("fake", "hints", "no hints"))
        updated = report.write_block(self.document + "\nDepois.\n", changed)
        self.assertEqual(updated.count("bench:summary:start"), 1)
        self.assertTrue(updated.startswith("# Relatório\n"))
        self.assertTrue(updated.endswith("\nDepois.\n"))
        self.assertEqual(report.check(updated, changed), [])
        self.assertEqual(report.write_block(updated, changed), updated)


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report.main(list(args))
        return code, out.getvalue()

    def test_write_then_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.json"
            doc = Path(tmp) / "doc.md"
            summary_path.write_text(json.dumps(summary_with(ok_result("fake", "raw"))), encoding="utf-8")
            doc.write_text("# Doc\n", encoding="utf-8")
            self.assertEqual(self.run_cli("--summary", str(summary_path), "--write", str(doc))[0], 0)
            self.assertEqual(self.run_cli("--summary", str(summary_path), "--check", str(doc))[0], 0)
            summary_path.write_text(json.dumps(summary_with(ok_result("fake", "raw", n=40))), encoding="utf-8")
            code, output = self.run_cli("--summary", str(summary_path), "--check", str(doc))
            self.assertEqual(code, 1)
            self.assertIn("FAIL", output)


if __name__ == "__main__":
    unittest.main()
