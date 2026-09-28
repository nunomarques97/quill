"""Engine adapters, transport rules and the variant pipeline, all offline."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock
from pathlib import Path

from bench.cleanup import OllamaClient
from bench.engines import VARIANTS, EngineSlot, OptionsError, create_engines, engine_ids, load_engine_options
from bench.engines.base import (
    EngineError,
    EngineUnavailable,
    HttpResponse,
    ResponseCache,
    Transport,
    build_hints,
    check_url,
    retry_after_seconds,
    urllib_send,
)
from bench.engines.deepgram import DeepgramEngine
from bench.engines.gemini import GeminiEngine
from bench.engines.groq import GroqEngine
from bench.engines import local_whisper
from bench.engines.local_whisper import LocalWhisperEngine
from bench.envfile import Keys
from bench.latency import build_composites
from bench.engines.base import wav_pcm
from bench.run import Utterance, benchmark
from bench.tests.fakes import FAKE_KEY, FakeEngine, FakeOllama, FakeServer, json_reply, judge_yes, tone_wav

HINTS = build_hints(["Zorblax"], ["pull request", "deploy"])


def no_sleep(_seconds: float) -> None:
    pass


def transport_for(server: FakeServer, sleeps: list | None = None, **kwargs) -> Transport:
    sleep = sleeps.append if sleeps is not None else no_sleep
    return Transport([FAKE_KEY], send=server.send, sleep=sleep, **kwargs)


def multipart_fields(body: bytes) -> dict[str, str]:
    fields = {}
    for part in body.split(b"--quill-"):
        if b'name="' not in part or b"filename=" in part:
            continue
        name = part.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        fields[name] = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0].decode()
    return fields


class TransportTest(unittest.TestCase):
    def test_only_https_on_allowed_hosts(self) -> None:
        for url in (
            "http://api.groq.com/x",
            "https://example.com/x",
            "https://api.groq.com.evil.test/x",
            "https://user:pw@api.groq.com/x",
            "https://api.groq.com:8443/x",
            "https://127.0.0.1/x",
        ):
            with self.subTest(url=url), self.assertRaises(EngineError):
                check_url(url)
        for host in ("api.groq.com", "generativelanguage.googleapis.com", "api.deepgram.com"):
            self.assertEqual(check_url(f"https://{host}/v1"), host)

    def test_refused_url_sends_nothing(self) -> None:
        sent = []
        transport = Transport([FAKE_KEY], send=lambda r, t: sent.append(r))
        with self.assertRaises(EngineError):
            transport.request("POST", "https://example.com/v1", {}, b"")
        self.assertEqual(sent, [])

    def test_429_waits_retry_after_then_succeeds(self) -> None:
        replies = [json_reply({"error": "slow down"}, 429, {"Retry-After": "7"}), json_reply({"text": "ok"})]
        sleeps: list[float] = []
        with FakeServer(lambda request: replies.pop(0)) as server:
            response = transport_for(server, sleeps).request("POST", "https://api.groq.com/v1", {}, b"x")
        self.assertEqual(response.status, 200)
        self.assertEqual(sleeps, [7.0])

    def test_429_without_retry_after_backs_off_then_gives_up(self) -> None:
        sleeps: list[float] = []
        with FakeServer(lambda request: json_reply({}, 429)) as server:
            transport = transport_for(server, sleeps, max_attempts=3, backoff_s=1.0)
            with self.assertRaises(EngineError) as caught:
                transport.request("POST", "https://api.groq.com/v1", {}, b"x")
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertIn("429", str(caught.exception))
        self.assertEqual(len(server.requests), 3)

    def test_retry_after_beyond_limit_stops_without_waiting(self) -> None:
        sleeps: list[float] = []
        with FakeServer(lambda request: json_reply({}, 429, {"Retry-After": "3600"})) as server:
            with self.assertRaises(EngineError) as caught:
                transport_for(server, sleeps).request("POST", "https://api.groq.com/v1", {}, b"x")
        self.assertEqual(sleeps, [])
        self.assertIn("free-tier limit", str(caught.exception))

    def test_retry_after_http_date(self) -> None:
        from datetime import datetime, timezone

        now = lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731
        self.assertEqual(retry_after_seconds({"retry-after": "Mon, 28 Sep 2026 12:00:30 GMT"}, now), 30.0)
        self.assertIsNone(retry_after_seconds({"retry-after": "soon"}, now))

    def test_redirects_are_not_followed(self) -> None:
        with FakeServer(lambda request: (302, {"Location": "http://127.0.0.1:9/steal"}, b"")) as server:
            response = urllib_send(__import__("urllib.request").request.Request(server.base_url + "/x"), 5)
        self.assertEqual(response.status, 302)
        self.assertEqual(len(server.requests), 1)

    def test_error_body_echoing_the_key_is_redacted(self) -> None:
        reply = json_reply({"error": {"message": f"invalid key {FAKE_KEY}"}}, 401)
        with FakeServer(lambda request: reply) as server:
            with self.assertRaises(EngineError) as caught:
                transport_for(server).request("POST", "https://api.groq.com/v1", {}, b"x")
        self.assertIn("401", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    def test_network_error_message_has_no_key(self) -> None:
        def fail(request, timeout):
            raise OSError(f"connection reset while sending {FAKE_KEY}")

        transport = Transport([FAKE_KEY], send=fail, sleep=no_sleep, max_attempts=2)
        with self.assertRaises(EngineError) as caught:
            transport.request("POST", "https://api.deepgram.com/v1/listen", {}, b"x")
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


class AdapterTest(unittest.TestCase):
    wav = tone_wav(1)

    def test_groq_raw_and_hints(self) -> None:
        with FakeServer(lambda request: json_reply({"text": " olá mundo "})) as server:
            engine = GroqEngine("whisper-large-v3-turbo", FAKE_KEY, transport_for(server))
            self.assertEqual(engine.transcribe(self.wav, None), "olá mundo")
            engine.transcribe(self.wav, HINTS)
        raw, hinted = server.requests
        self.assertEqual(raw["path"], "/openai/v1/audio/transcriptions")
        self.assertEqual(raw["headers"]["x-original-host"], "api.groq.com")
        self.assertEqual(raw["headers"]["authorization"], f"Bearer {FAKE_KEY}")
        fields = multipart_fields(raw["body"])
        self.assertEqual((fields["model"], fields["language"], fields["temperature"]), ("whisper-large-v3-turbo", "pt", "0"))
        self.assertNotIn("prompt", fields)
        self.assertIn(self.wav, raw["body"])
        prompt = multipart_fields(hinted["body"])["prompt"]
        self.assertTrue(prompt.startswith("Zorblax"))
        self.assertIn("pull request", prompt)

    def test_gemini_key_in_header_and_vocabulary_in_prompt(self) -> None:
        reply = json_reply(
            {"candidates": [{"content": {"parts": [{"text": "pensei", "thought": True}, {"text": "olá  mundo"}]}}]}
        )
        with FakeServer(lambda request: reply) as server:
            engine = GeminiEngine(FAKE_KEY, transport_for(server), "gemini-2.5-flash", 0)
            self.assertEqual(engine.transcribe(self.wav, HINTS), "olá mundo")
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertNotIn(FAKE_KEY, request["path"])
        self.assertEqual(request["headers"]["x-goog-api-key"], FAKE_KEY)
        payload = json.loads(request["body"])
        self.assertEqual(payload["generationConfig"], {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}})
        self.assertIn("Zorblax", payload["contents"][0]["parts"][0]["text"])
        self.assertEqual(payload["contents"][0]["parts"][1]["inline_data"]["mime_type"], "audio/wav")

    def test_gemini_without_text_fails(self) -> None:
        reply = json_reply({"candidates": [{"finishReason": "SAFETY"}]})
        with FakeServer(lambda request: reply) as server:
            with self.assertRaises(EngineError) as caught:
                GeminiEngine(FAKE_KEY, transport_for(server)).transcribe(self.wav, None)
        self.assertIn("SAFETY", str(caught.exception))

    def test_gemini_transcribe_model_uses_interactions_without_storage(self) -> None:
        reply = json_reply(
            {
                "status": "completed",
                "steps": [
                    {"type": "thought", "content": [{"type": "text", "text": "pensei"}]},
                    {"type": "model_output", "content": [{"type": "text", "text": "olá  mundo"}]},
                ],
            }
        )
        with FakeServer(lambda request: reply) as server:
            engine = GeminiEngine(FAKE_KEY, transport_for(server), "gemini-3.5-transcribe", None)
            self.assertEqual(engine.transcribe(self.wav, None), "olá mundo")
            self.assertEqual(engine.transcribe(self.wav, HINTS), "olá mundo")
        raw, hinted = server.requests
        self.assertEqual(raw["path"], "/v1beta/interactions")
        self.assertNotIn(FAKE_KEY, raw["path"])
        self.assertEqual(raw["headers"]["x-goog-api-key"], FAKE_KEY)
        payload = json.loads(raw["body"])
        self.assertEqual(payload["model"], "gemini-3.5-transcribe")
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["generation_config"], {"transcription_config": {"language_codes": ["pt-PT"]}})
        self.assertEqual(payload["input"][0]["mime_type"], "audio/wav")
        vocabulary = json.loads(hinted["body"])["generation_config"]["transcription_config"]["custom_vocabulary"]
        self.assertEqual(vocabulary, HINTS.vocabulary())
        self.assertNotEqual(engine.params(None), engine.params(HINTS))

    def test_gemini_transcribe_without_text_fails(self) -> None:
        reply = json_reply({"status": "failed", "steps": [{"type": "model_output", "content": []}]})
        with FakeServer(lambda request: reply) as server:
            with self.assertRaises(EngineError) as caught:
                GeminiEngine(FAKE_KEY, transport_for(server), "gemini-3.5-transcribe", None).transcribe(self.wav, None)
        self.assertIn("status failed", str(caught.exception))

    def test_deepgram_language_and_keyterms(self) -> None:
        reply = json_reply({"results": {"channels": [{"alternatives": [{"transcript": "olá mundo"}]}]}})
        with FakeServer(lambda request: reply) as server:
            engine = DeepgramEngine(FAKE_KEY, transport_for(server))
            self.assertEqual(engine.transcribe(self.wav, None), "olá mundo")
            engine.transcribe(self.wav, HINTS)
        raw, hinted = server.requests
        raw_query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw["path"]).query)
        self.assertEqual(raw_query["model"], ["nova-3"])
        self.assertEqual(raw_query["language"], ["pt-PT"])
        self.assertNotIn("keyterm", raw_query)
        self.assertEqual(raw["headers"]["authorization"], f"Token {FAKE_KEY}")
        self.assertEqual(raw["body"], self.wav)
        hinted_query = urllib.parse.parse_qs(urllib.parse.urlsplit(hinted["path"]).query)
        self.assertEqual(hinted_query["keyterm"], ["Zorblax", "pull request", "deploy"])

    def test_deepgram_keyterm_disabled_is_a_documented_skip(self) -> None:
        engine = DeepgramEngine(FAKE_KEY, Transport([FAKE_KEY], send=lambda r, t: None), keyterm=False)
        self.assertIn("keyterm", engine.hints_unsupported())

    def test_local_whisper_needs_local_model_and_imports_lazily(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            engine = LocalWhisperEngine("large-v3", Path(folder))
            with self.assertRaises(EngineUnavailable):
                engine.load()
        self.assertNotIn("faster_whisper", sys.modules)
        params = engine.params(HINTS)
        self.assertEqual((params["device"], params["compute_type"]), ("cuda", "float16"))
        self.assertIn("Zorblax", params["initial_prompt"])
        self.assertIsNone(engine.params(None)["hotwords"])

    def test_requests_shim_only_when_absent(self) -> None:
        real_find_spec = local_whisper.importlib.util.find_spec
        with mock.patch.dict(sys.modules):
            sys.modules.pop("requests", None)
            sys.modules.pop("requests.exceptions", None)
            with mock.patch.object(
                local_whisper.importlib.util,
                "find_spec",
                side_effect=lambda name, *a: None if name == "requests" else real_find_spec(name, *a),
            ):
                self.assertTrue(local_whisper.shim_requests())
                self.assertTrue(issubclass(sys.modules["requests"].exceptions.ConnectionError, OSError))
                self.assertIs(sys.modules["requests.exceptions"], sys.modules["requests"].exceptions)
                self.assertFalse(local_whisper.shim_requests())
        # mock.patch.dict restored sys.modules: the shim does not leak into other tests.
        self.assertTrue("requests" not in sys.modules or getattr(sys.modules["requests"], "__file__", None))

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL search only")
    def test_cuda_folders_go_on_process_path_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "torch" / "lib"
            lib.mkdir(parents=True)
            spec = mock.Mock(origin=str(Path(tmp) / "torch" / "__init__.py"))
            specs = {"torch": spec, "nvidia": None}
            with mock.patch.object(local_whisper.importlib.util, "find_spec", side_effect=specs.get), \
                    mock.patch.dict(local_whisper.os.environ, {"PATH": "C:\\other"}), \
                    mock.patch.object(local_whisper.os, "add_dll_directory", return_value=None), \
                    mock.patch.object(local_whisper, "_dll_handles", []):
                self.assertEqual(local_whisper.register_cuda_dlls(), ["lib"])
                local_whisper.register_cuda_dlls()
                parts = local_whisper.os.environ["PATH"].split(local_whisper.os.pathsep)
        self.assertEqual(parts, [str(lib), "C:\\other"])

    def test_registry_reports_missing_keys(self) -> None:
        slots = create_engines(Keys({"GROQ_API_KEY": FAKE_KEY}), send=lambda r, t: None)
        self.assertEqual([slot.id for slot in slots], engine_ids())
        by_id = {slot.id: slot for slot in slots}
        self.assertIsNotNone(by_id["groq-whisper-large-v3"].engine)
        self.assertIn("GEMINI_API_KEY", by_id["gemini-gemini-2.5-flash"].unavailable)
        self.assertIn("DEEPGRAM_API_KEY", by_id["deepgram-nova-3"].unavailable)
        self.assertNotIn(FAKE_KEY, repr(slots))

    def test_engine_options(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bench.toml"
            path.write_text('[engines.gemini]\nmodel = "gemini-9-flash"\n[engines.deepgram]\nlanguage = "pt"\n', encoding="utf-8")
            options = load_engine_options(path)
            self.assertEqual((options.gemini_model, options.gemini_thinking_budget, options.deepgram_language), ("gemini-9-flash", None, "pt"))
            path.write_text('[engines.gemini]\nmodel = "../evil"\n', encoding="utf-8")
            with self.assertRaises(OptionsError):
                load_engine_options(path)

    def test_skip_by_decision(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bench.toml"
            path.write_text('[engines.skip]\n"deepgram-nova-3" = "by  decision"\n', encoding="utf-8")
            options = load_engine_options(path)
            self.assertEqual(options.skip, (("deepgram-nova-3", "by decision"),))
            keys = Keys({"GROQ_API_KEY": FAKE_KEY, "DEEPGRAM_API_KEY": FAKE_KEY})
            slots = {slot.id: slot for slot in create_engines(keys, options, send=lambda r, t: None)}
            self.assertIsNone(slots["deepgram-nova-3"].engine)
            self.assertEqual(slots["deepgram-nova-3"].unavailable, "skipped: by decision")
            self.assertIsNotNone(slots["groq-whisper-large-v3"].engine)
            for bad in ('"nope-engine" = "x"', '"deepgram-nova-3" = ""', '"deepgram-nova-3" = "   "',
                        '"deepgram-nova-3" = "ok\\u0007"', '"deepgram-nova-3" = 3', f'"deepgram-nova-3" = "{"x" * 121}"'):
                path.write_text(f"[engines.skip]\n{bad}\n", encoding="utf-8")
                with self.assertRaises(OptionsError, msg=bad):
                    load_engine_options(path)


class PipelineTest(unittest.TestCase):
    """benchmark() with fake engines, a fake Ollama and no GPU."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.utterances = [
            Utterance(f"pt-{n:02d}", tone_wav(n, 2.0), f"abre o Zorblax e faz deploy {n}", ("Zorblax",)) for n in range(1, 7)
        ]
        self.texts = {u.wav: u.reference for u in self.utterances}
        self.composites = build_composites([(u.id, wav_pcm(u.wav)[0]) for u in self.utterances], count=3)
        self.hints = build_hints(["Zorblax"], ["deploy"])

    def tearDown(self) -> None:
        self.folder.cleanup()

    def run_bench(self, slots, ollama: FakeOllama, secrets=()):
        return benchmark(
            self.utterances,
            slots,
            self.hints,
            ["deploy"],
            self.composites,
            OllamaClient(ollama.base_url),
            cache=ResponseCache(self.root / "cache"),
            run_dir=self.root / "run",
            results_dir=self.root,
            secrets=secrets,
            snap=lambda label, client: {"label": label, "contention": []},
            log=lambda line: None,
        )

    def test_every_variant_ok_or_skipped_as_a_whole(self) -> None:
        good = FakeEngine("good", self.texts)
        failing = FakeEngine("failing", self.texts, fail_on=4, error=f"HTTP 500 from host with {FAKE_KEY}")
        no_hints = FakeEngine("no-hints", self.texts, hints_reason="no hint feature for Portuguese")
        slots = [
            EngineSlot("good", good),
            EngineSlot("failing", failing),
            EngineSlot("no-hints", no_hints),
            EngineSlot("keyless", None, "KEYLESS_API_KEY missing from .env"),
        ]
        with FakeOllama(chat=judge_yes) as ollama:
            rows, environment = self.run_bench(slots, ollama, secrets=[FAKE_KEY])
        by_key = {(r["engine"], r["variant"]): r for r in rows}
        self.assertEqual(len(rows), 4 * len(VARIANTS))
        for variant in VARIANTS:
            row = by_key[("good", variant)]
            self.assertEqual((row["status"], row["n"], row["wer"], row["intent_preserved"]), ("ok", 6, 0.0, 1.0))
            self.assertEqual(row["latency_samples"], 6)
            self.assertEqual(by_key[("keyless", variant)]["skipped_reason"].count("KEYLESS_API_KEY"), 1)
        for variant in VARIANTS:
            row = by_key[("failing", variant)]
            self.assertEqual((row["status"], row["n"], row["wer"]), ("skipped", 0, None))
            self.assertNotIn(FAKE_KEY, row["skipped_reason"])
        self.assertEqual(by_key[("no-hints", "raw")]["status"], "ok")
        self.assertEqual(by_key[("no-hints", "hints")]["skipped_reason"], "no hint feature for Portuguese")
        self.assertIn("no hint feature", by_key[("no-hints", "hints+cleanup")]["skipped_reason"])
        self.assertEqual(good.closed, 1)
        self.assertEqual(environment["ollama"]["unavailable"], None)
        self.assertEqual({s["label"] for s in environment["snapshots"]} >= {"before cleanup", "after intent judge"}, True)
        tables = sorted(p.name for p in (self.root / "run" / "intent").iterdir())
        self.assertIn("good__hints_cleanup.md", tables)
        self.assertNotIn(FAKE_KEY, json.dumps(rows))

    def test_cloud_rerun_resends_nothing(self) -> None:
        with FakeOllama(chat=judge_yes) as ollama:
            first = FakeEngine("cloud", self.texts, cacheable=True)
            rows_1, _ = self.run_bench([EngineSlot("cloud", first)], ollama)
            second = FakeEngine("cloud", self.texts, cacheable=True)
            rows_2, _ = self.run_bench([EngineSlot("cloud", second)], ollama)
        self.assertEqual(first.calls, 2 * (6 + 3 * 2))
        self.assertEqual(second.calls, 0)
        self.assertEqual([r["wer"] for r in rows_1], [r["wer"] for r in rows_2])
        cached = "".join(p.read_text(encoding="utf-8") for p in (self.root / "cache").rglob("*.json"))
        self.assertNotIn(FAKE_KEY, cached)

    def test_cache_files_from_a_real_adapter_hold_no_key(self) -> None:
        reply = json_reply({"text": "abre o Zorblax"})
        with FakeServer(lambda request: reply) as server, FakeOllama(chat=judge_yes) as ollama:
            engine = GroqEngine("whisper-large-v3", FAKE_KEY, transport_for(server))
            rows, _ = self.run_bench([EngineSlot(engine.id, engine)], ollama, secrets=[FAKE_KEY])
        self.assertEqual(rows[0]["status"], "ok")
        files = list((self.root / "cache").rglob("*.json")) + list((self.root / "run").rglob("*.*"))
        self.assertTrue(files)
        for path in files:
            self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"), path.name)

    def test_engine_load_failure_skips_all_variants(self) -> None:
        class Broken(FakeEngine):
            def load(self) -> None:
                raise EngineUnavailable("broken: model files missing")

        with FakeOllama(chat=judge_yes) as ollama:
            rows, _ = self.run_bench([EngineSlot("broken", Broken("broken", self.texts))], ollama)
        self.assertEqual([r["status"] for r in rows], ["skipped"] * 4)
        self.assertTrue(all("model files missing" in r["skipped_reason"] for r in rows))


class HttpResponseTest(unittest.TestCase):
    def test_repr_hides_body(self) -> None:
        self.assertNotIn("secret-body", repr(HttpResponse(200, {}, b"secret-body")))


if __name__ == "__main__":
    unittest.main()
