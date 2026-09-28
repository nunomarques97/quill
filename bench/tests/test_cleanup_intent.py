"""Ollama cleanup, the intent judge and VRAM snapshots, against a fake Ollama."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from bench.cleanup import ALLOWED_CALLS, CLEANUP_MODEL, Cleaner, OllamaClient, OllamaError
from bench.engines import EngineSlot
from bench.engines.base import ResponseCache, build_hints, wav_pcm
from bench.gpu import contention, query_vram, snapshot
from bench.intent import IntentJudge, IntentRow, evaluate, missing_slots, write_table
from bench.latency import build_composites
from bench.metrics import ItemResult
from bench.run import Utterance, benchmark
from bench.tests.fakes import FakeEngine, FakeOllama, judge_yes, tone_wav


def completed(stdout: str, code: int = 0):
    return lambda *args, **kwargs: subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")


class OllamaClientTest(unittest.TestCase):
    def test_only_local_http(self) -> None:
        for url in ("http://10.0.0.5:11434", "https://127.0.0.1:11434", "http://example.com", "http://127.0.0.1:11434/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OllamaClient(url)

    def test_never_pulls_deletes_or_unloads(self) -> None:
        self.assertEqual(
            ALLOWED_CALLS, {("GET", "/api/version"), ("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/chat")}
        )
        with FakeOllama() as ollama:
            client = OllamaClient(ollama.base_url)
            for method, path in (("POST", "/api/pull"), ("DELETE", "/api/delete"), ("POST", "/api/generate")):
                with self.subTest(path=path), self.assertRaises(OllamaError):
                    client._call(method, path, {"model": "qwen3:14b"})
            with self.assertRaises(OllamaError):
                client._call("POST", "/api/chat", {"model": "qwen3:14b", "keep_alive": 0})
        self.assertEqual(ollama.requests, [])

    def test_chat_is_deterministic_without_thinking(self) -> None:
        with FakeOllama(chat=lambda payload: "<think>hidden</think> Olá, mundo.") as ollama:
            result = Cleaner(OllamaClient(ollama.base_url)).clean("olá mundo")
        self.assertEqual(result.content, "Olá, mundo.")
        payload = ollama.chat_payloads()[0]
        self.assertEqual(payload["model"], CLEANUP_MODEL)
        self.assertIs(payload["think"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertNotIn("keep_alive", payload)

    def test_vocabulary_only_when_given(self) -> None:
        client = OllamaClient()
        self.assertNotIn("spell it exactly", Cleaner(client).system_prompt())
        with_vocabulary = Cleaner(client, vocabulary=("Zorblax", "deploy"))
        self.assertIn("Zorblax, deploy.", with_vocabulary.system_prompt())
        self.assertNotEqual(Cleaner(client).params()["prompt"], with_vocabulary.params()["prompt"])

    def test_empty_text_skips_the_model(self) -> None:
        with FakeOllama() as ollama:
            self.assertEqual(Cleaner(OllamaClient(ollama.base_url)).clean("  ").content, "")
        self.assertEqual(ollama.requests, [])

    def test_errors_are_short_and_carry_no_text(self) -> None:
        with FakeOllama(chat_status=500) as ollama:
            with self.assertRaises(OllamaError) as caught:
                Cleaner(OllamaClient(ollama.base_url)).clean("frase privada de teste")
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertNotIn("privada", str(caught.exception))
        with self.assertRaises(OllamaError):
            OllamaClient("http://127.0.0.1:9", timeout_s=2).version()


class IntentTest(unittest.TestCase):
    def test_missing_slots_counts_occurrences(self) -> None:
        reference = "faz deploy do Zorblax e outro deploy"
        self.assertEqual(missing_slots(reference, "faz deploy do Zorblax e outro deploy", ["Zorblax"], ["deploy"]), [])
        self.assertEqual(missing_slots(reference, "faz deploy do zorblax", ["Zorblax"], ["deploy"]), ["deploy"])
        self.assertEqual(missing_slots(reference, "faz deploy do sorblax deploy", ["Zorblax"], ["deploy"]), ["Zorblax"])

    def test_slot_failure_never_reaches_the_judge(self) -> None:
        calls = []

        def judge(reference, hypothesis):
            calls.append(hypothesis)
            return True, "same"

        items = [
            ItemResult("pt-01", "abre o Zorblax", "abre o sorblax", ("Zorblax",)),
            ItemResult("pt-02", "faz o deploy", "faz o deploy", ()),
        ]
        rows = evaluate(items, judge, ["deploy"])
        self.assertEqual([(r.id, r.preserved) for r in rows], [("pt-01", False), ("pt-02", True)])
        self.assertIn("slot rule: missing Zorblax", rows[0].reason)
        self.assertEqual(calls, ["faz o deploy"])

    def test_judge_prompt_and_verdicts(self) -> None:
        answers = iter([{"same": "yes", "reason": "same action"}, {"same": "no", "reason": "negation lost"}])
        with FakeOllama(chat=lambda payload: json.dumps(next(answers))) as ollama:
            judge = IntentJudge(OllamaClient(ollama.base_url))
            self.assertEqual(judge.judge("não apagues", "não apagues"), (True, "same action"))
            self.assertEqual(judge.judge("não apagues", "apaga"), (False, "negation lost"))
        payload = ollama.chat_payloads()[0]
        self.assertEqual(payload["format"]["required"], ["same", "reason"])
        self.assertIn("REFERENCE: não apagues\nHYPOTHESIS: não apagues", payload["messages"][1]["content"])

    def test_invalid_verdict_raises(self) -> None:
        for content in ("maybe", json.dumps({"same": "perhaps", "reason": ""})):
            with self.subTest(content=content), FakeOllama(chat=lambda payload, c=content: c) as ollama:
                with self.assertRaises(OllamaError):
                    IntentJudge(OllamaClient(ollama.base_url)).judge("a b", "a b")

    def test_table_only_under_results(self) -> None:
        rows = [IntentRow("pt-01", True, "judge: same | ok", "abre a pasta", "abre a pasta")]
        with tempfile.TemporaryDirectory() as folder:
            results = Path(folder) / "results"
            path = write_table(results / "intent" / "x.md", "eng", "raw", rows, results)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertIn("| id | reference | hypothesis | verdict | reason | human |", lines)
            self.assertEqual(lines[-1], "| pt-01 | abre a pasta | abre a pasta | yes | judge: same \\| ok |  |")
            with self.assertRaises(ValueError):
                write_table(Path(folder) / "elsewhere.md", "eng", "raw", rows, results)


class GpuTest(unittest.TestCase):
    def test_query_vram(self) -> None:
        self.assertEqual(
            query_vram(completed("NVIDIA GeForce RTX 5060 Ti, 16311, 9000, 7311\n")),
            {"gpu": "NVIDIA GeForce RTX 5060 Ti", "total_mib": 16311, "used_mib": 9000, "free_mib": 7311},
        )
        self.assertIn("error", query_vram(completed("", 9)))
        self.assertIn("error", query_vram(completed("garbage")))

        def missing(*args, **kwargs):
            raise FileNotFoundError

        self.assertEqual(query_vram(missing), {"error": "nvidia-smi not found"})

    def test_contention(self) -> None:
        loaded = [
            {"name": CLEANUP_MODEL, "vram_mib": 6000},
            {"name": "qwen3:14b", "vram_mib": 10000},
            {"name": "tiny:1b", "vram_mib": 800},
            {"name": "big:32b", "vram_mib": 9000},
        ]
        messages = contention(loaded)
        self.assertEqual(len(messages), 2)
        self.assertIn("qwen3:14b", messages[0])
        self.assertIn("big:32b", messages[1])

    def test_snapshot_reports_other_projects_models(self) -> None:
        with FakeOllama(loaded=[("qwen3:14b", 10240)]) as ollama:
            state = snapshot("before cleanup", OllamaClient(ollama.base_url), completed("GPU, 16000, 12000, 4000"))
        self.assertEqual(state["vram"]["free_mib"], 4000)
        self.assertEqual(state["ollama"]["loaded"][0]["name"], "qwen3:14b")
        self.assertIn("VRAM contention: qwen3:14b", state["contention"][0])
        state = snapshot("x", OllamaClient("http://127.0.0.1:9", timeout_s=2), completed("GPU, 1, 1, 0"))
        self.assertIn("error", state["ollama"])
        self.assertEqual(state["contention"], [])


class OllamaPhaseTest(unittest.TestCase):
    """Cleanup variants and the judge inside benchmark()."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.utterances = [Utterance(f"pt-{n:02d}", tone_wav(n, 2.0), f"abre a pasta {n}") for n in range(1, 5)]
        self.texts = {u.wav: u.reference for u in self.utterances}
        self.composites = build_composites([(u.id, wav_pcm(u.wav)[0]) for u in self.utterances], count=2)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def run_bench(self, ollama: FakeOllama, snap=None):
        return benchmark(
            self.utterances,
            [EngineSlot("fake", FakeEngine("fake", self.texts))],
            build_hints(["Zorblax"], ["deploy"]),
            ["deploy"],
            self.composites,
            OllamaClient(ollama.base_url),
            cache=ResponseCache(self.root / "cache"),
            run_dir=self.root / "run",
            results_dir=self.root,
            snap=snap or (lambda label, client: {"label": label, "contention": []}),
            log=lambda line: None,
        )

    def test_missing_model_skips_cleanup_and_judge(self) -> None:
        with FakeOllama(installed=["qwen3:14b"]) as ollama:
            rows, environment = self.run_bench(ollama)
        by_variant = {r["variant"]: r for r in rows}
        self.assertEqual(by_variant["raw"]["status"], "ok")
        self.assertIsNone(by_variant["raw"]["intent_preserved"])
        self.assertIn("qwen3:8b is not installed", by_variant["raw"]["intent_note"])
        for variant in ("raw+cleanup", "hints+cleanup"):
            self.assertEqual(by_variant[variant]["status"], "skipped")
            self.assertIn("qwen3:8b is not installed", by_variant[variant]["skipped_reason"])
        self.assertEqual(ollama.chat_payloads(), [])
        self.assertIn("not installed", environment["ollama"]["unavailable"])

    def test_model_that_cannot_load_skips_cleanup(self) -> None:
        with FakeOllama(chat_status=500) as ollama:
            rows, _ = self.run_bench(ollama)
        reasons = {r["variant"]: r["skipped_reason"] for r in rows}
        self.assertIn("qwen3:8b cannot load", reasons["raw+cleanup"])
        self.assertIsNone(reasons["raw"])

    def test_cleanup_latency_adds_to_engine_time_and_contention_is_recorded(self) -> None:
        def snap(label, client):
            return {"label": label, "contention": ["VRAM contention: qwen3:14b is loaded by another client (10240 MiB in VRAM)"]}

        with FakeOllama(chat=judge_yes) as ollama:
            rows, environment = self.run_bench(ollama, snap)
        by_variant = {r["variant"]: r for r in rows}
        self.assertEqual(by_variant["hints+cleanup"]["status"], "ok")
        self.assertEqual(by_variant["hints+cleanup"]["latency_samples"], 4)
        self.assertGreater(by_variant["raw+cleanup"]["latency_p95_s"], by_variant["raw"]["latency_p95_s"] - 1e-9)
        self.assertEqual(len(environment["contention"]), 1)
        self.assertEqual(
            [s["label"] for s in environment["snapshots"]],
            ["start", "before cleanup", "after cleanup", "before intent judge", "after intent judge"],
        )
        systems = {p["messages"][0]["content"] for p in ollama.chat_payloads() if "format" not in p}
        self.assertTrue(any("Zorblax" in s for s in systems))
        self.assertTrue(any("Zorblax" not in s for s in systems))
        self.assertTrue(all("keep_alive" not in p for p in ollama.chat_payloads()))

    def test_judge_failure_leaves_the_variant_unjudged(self) -> None:
        def chat(payload):
            if "format" in payload:
                return "not json"
            return payload["messages"][-1]["content"]

        with FakeOllama(chat=chat) as ollama:
            rows, _ = self.run_bench(ollama)
        for row in rows:
            self.assertEqual(row["status"], "ok")
            self.assertIsNone(row["intent_preserved"])
            self.assertEqual(row["intent_judged"], 0)
            self.assertIn("intent not judged", row["intent_note"])


if __name__ == "__main__":
    unittest.main()
