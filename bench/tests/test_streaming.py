"""bench.streaming tests with invented audio and the fake streaming model.

No GPU, model, microphone, Ollama or real recording is used; outputs go to a
temporary folder standing in for bench/results/.
"""

import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from bench import audio_mme, streaming
from bench.dataset import Take
from bench.engines import local_whisper
from bench.engines.base import EngineError, EngineUnavailable, build_hints
from bench.engines.local_whisper import LocalWhisperEngine
from quill import audio, whisper
from quill.streaming import BYTES_PER_SECOND, FinalResult, StreamingTranscriber, StreamOptions
from quill.tests.test_streaming import FakeClock, FakeModel, speech, text


def write_take(folder: Path, take_id: str, pcm: bytes, reference: str) -> Take:
    path = folder / f"{take_id}.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(pcm)
    return Take(take_id, len(pcm) / BYTES_PER_SECOND, "microfone", "caso", "intencao", path, reference, ())


def result(audio_s, hit=False, error=None, text_value="x"):
    return FinalResult(1, text_value, error, audio_s, True, 3, 1, 2, 1.0, hit, 0.0, 0.1, 0.1)


class SharedSourceTest(unittest.TestCase):
    def test_bench_uses_the_product_capture_and_loader(self):
        self.assertIs(audio_mme.Capture, audio.Capture)
        self.assertIs(audio_mme.take_problem, audio.take_problem)
        self.assertIs(local_whisper.register_cuda_dlls, whisper.register_cuda_dlls)
        self.assertIs(local_whisper.shim_requests, whisper.shim_requests)
        self.assertIsInstance(LocalWhisperEngine("large-v3").whisper, whisper.Whisper)

    def test_product_prompt_equals_the_baseline_prompt(self):
        hints = build_hints(["Zorblax"], ["pull request", "deploy", "commit"] * 3)
        engine = LocalWhisperEngine("large-v3")
        prompt, hot = whisper.hint_options(hints.vocabulary())
        self.assertEqual({"initial_prompt": prompt, "hotwords": hot}, engine._hint_options(hints))
        long = build_hints([], [f"term{i}" for i in range(200)])
        prompt, hot = whisper.hint_options(long.vocabulary())
        self.assertEqual({"initial_prompt": prompt, "hotwords": hot}, engine._hint_options(long))


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.model = FakeModel(self.clock, cost_s=0.1)
        self.transcriber = StreamingTranscriber(self.model, StreamOptions(), clock=self.clock)
        self.transcriber.start()

    def tearDown(self):
        self.transcriber.stop(timeout=5)

    def test_chunks_are_capture_sized(self):
        pieces = streaming.chunks(bytes(3300))
        self.assertEqual([len(p) for p in pieces], [1600, 1600, 100])

    def test_deterministic_replay_is_reproducible(self):
        pcm = speech(list(range(1, 11)))
        first = streaming.replay_deterministic(self.transcriber, pcm)
        calls = len(self.model.calls)
        second = streaming.replay_deterministic(self.transcriber, pcm)
        self.assertEqual(first.text, text(range(1, 11)))
        self.assertEqual(second.text, first.text)
        self.assertEqual(len(self.model.calls), 2 * calls)
        self.assertEqual((second.partials, second.committed_words), (first.partials, first.committed_words))

    def test_realtime_replay_paces_by_the_clock(self):
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.clock.advance(seconds)

        pcm = speech([1, 2, 3, 4])
        final, seconds = streaming.replay_realtime(self.transcriber, pcm, clock=self.clock, sleep=sleep)
        self.assertEqual(final.text, text([1, 2, 3, 4]))
        self.assertGreaterEqual(seconds, 0.0)
        self.assertGreaterEqual(sum(sleeps), len(pcm) / BYTES_PER_SECOND - 0.3)


    def test_deterministic_replay_follows_a_hint_source_reproducibly(self):
        before = whisper.SessionHints(prompt="Vocabulário: orchard.", hotwords="orchard")
        heard = whisper.SessionHints(prompt="Vocabulário: w2x, orchard.", hotwords="w2x orchard")
        asked = []

        def source(text_heard):
            asked.append(text_heard)
            return heard if "w2" in text_heard.split() else before

        pcm = speech([1, 2, 3, 4])
        first = streaming.replay_deterministic(self.transcriber, pcm, hints=source)
        first_asked, first_prompts = list(asked), [c.options.initial_prompt or "" for c in self.model.calls]
        self.assertEqual(first.text, text([1, 2, 3, 4]))
        self.assertEqual(first.hint_switches, 1)
        self.assertEqual(first_asked[0], "")  # asked at once, then with the replay's own partials
        self.assertGreater(len(first_asked), 2)
        self.assertTrue(first_prompts[0].startswith(before.prompt))
        self.assertTrue(first_prompts[-1].startswith(heard.prompt))  # the final decodes with the heard hints
        asked.clear()
        calls = len(self.model.calls)
        second = streaming.replay_deterministic(self.transcriber, pcm, hints=source)
        self.assertEqual((second.text, second.hint_switches), (first.text, first.hint_switches))
        self.assertEqual(asked, first_asked)
        self.assertEqual([c.options.initial_prompt or "" for c in self.model.calls[calls:]], first_prompts)


class StreamTakesTest(unittest.TestCase):
    def test_streams_every_take_and_keeps_the_model_loaded(self):
        model = FakeModel()
        with tempfile.TemporaryDirectory() as folder:
            takes = [write_take(Path(folder), f"pt-0{i}", speech([i, i + 1]), "r") for i in (1, 3)]
            out = streaming.stream_takes(model, takes, ["Zorblax"])
        self.assertEqual([t for t, _ in out], ["w1 w2", "w3 w4"])
        self.assertEqual((model.loads, model.closes), (1, 0))
        self.assertTrue(all(c.options.hotwords == "Zorblax" for c in model.calls))

    def test_session_hints_replace_the_vocabulary_hints_of_every_take(self):
        model = FakeModel()
        hints = whisper.SessionHints(prompt="Abre o VS Code no projeto.", hotwords="zorblax", language="pt")
        with tempfile.TemporaryDirectory() as folder:
            takes = [write_take(Path(folder), f"vc-0{i}", speech([i, i + 1], gap=0.6), "r") for i in (1, 3)]
            out = streaming.stream_takes(model, takes, ["Zorblax"], hints=hints)
        self.assertEqual([t for t, _ in out], ["w1 w2", "w3 w4"])
        self.assertTrue(model.calls)
        for call in model.calls:
            self.assertEqual((call.options.initial_prompt, call.options.hotwords, call.options.language),
                             ("Abre o VS Code no projeto.", "zorblax", "pt"))

    def test_failed_final_or_load_raises(self):
        model = FakeModel()
        model.fail = lambda call: RuntimeError("x") if not call.options.word_timestamps else None
        with tempfile.TemporaryDirectory() as folder:
            takes = [write_take(Path(folder), "pt-01", speech([1]), "r")]
            with self.assertRaises(EngineError):
                streaming.stream_takes(model, takes, [], StreamOptions(speculate=False))
            broken = FakeModel()
            broken.fail_load = RuntimeError("no device")
            with self.assertRaises(EngineUnavailable):
                streaming.stream_takes(broken, takes, [])


class AggregateTest(unittest.TestCase):
    def test_latency_counts_takes_up_to_15_s(self):
        with tempfile.TemporaryDirectory() as folder:
            take = write_take(Path(folder), "dt-01", b"\0\0", "a b")
        replays = [streaming.Replay(take, "dictation", result(audio_s), seconds)
                   for audio_s, seconds in ((2.0, 0.2), (15.0, 0.4), (14.0, 0.3), (18.0, 3.0))]
        replays.append(streaming.Replay(take, "dictation", result(5.0, hit=True, error="failed"), 0.1))
        block = streaming.latency_block(replays)
        self.assertEqual((block["n"], block["excluded_longer"], block["p50_s"], block["p95_s"], block["max_s"]), (4, 1, 0.2, 0.4, 0.4))
        self.assertEqual((block["speculative_hits"], block["errors"], block["max_audio_s"]), (1, 1, 15.0))
        self.assertIsNone(streaming.latency_block([])["p95_s"])

    def test_quality_and_private_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            take = write_take(root, "dt-01", b"\0\0", "abre o ficheiro")
            replays = [streaming.Replay(take, "dictation", result(1.0, text_value="abre o ficheiro"), 0.2)]
            self.assertEqual(streaming.quality_block(replays)["wer_clean"], 0.0)
            results = root / "results"
            target = streaming.write_private(results / "streaming" / "run", "deterministic",
                                             {"takes": streaming.replay_rows(replays)}, results)
            rows = json.loads(target.read_text(encoding="utf-8"))["takes"]
            self.assertEqual((rows[0]["id"], rows[0]["final"], rows[0]["release_to_final_s"]), ("dt-01", "abre o ficheiro", 0.2))
            with self.assertRaises(ValueError):
                streaming.write_private(root / "elsewhere", "x", {}, results)

    def test_gpu_state_reads_without_changing_anything(self):
        def smi(command, **kwargs):
            self.assertEqual(command, streaming.GPU_QUERY)
            return subprocess.CompletedProcess(command, 0, stdout="37, 6000, 10000\n")

        state = streaming.gpu_state(smi, loaded=lambda: [{"name": "qwen3:14b", "vram_mib": 9000}])
        self.assertEqual((state["utilization_pct"], state["free_mib"]), (37, 10000))
        self.assertEqual(len(state["contention"]), 1)

        def missing(command, **kwargs):
            raise FileNotFoundError("nvidia-smi")

        def down():
            raise OSError("refused")

        state = streaming.gpu_state(missing, loaded=down)
        self.assertEqual((state["error"], state["ollama_error"]), ("nvidia-smi unavailable", "refused"))

    def test_baseline_is_read_from_the_committed_summary(self):
        baseline = streaming.baseline_wer()
        self.assertEqual(set(baseline), {"commands", "dictation"})
        self.assertEqual(streaming.baseline_wer(Path("missing.json")), {})

    def test_bad_option_exits_before_loading_anything(self):
        lines = []
        self.assertEqual(streaming.main(["--step-s", "0"], out=lines.append), 2)
        self.assertEqual(lines, ["error: streaming option out of range: step_s"])
        lines.clear()
        self.assertEqual(streaming.main(["--pause-commit-min-s", "-1"], out=lines.append), 2)
        self.assertEqual(lines, ["error: streaming option out of range: pause_commit_min_s"])

    def test_boolean_flags_and_model_choice(self):
        args = argparse.Namespace(commit=False, pause_commit=None, step_s=1.0)
        options = streaming.options_from(args)
        self.assertEqual((options.commit, options.pause_commit, options.step_s), (False, True, 1.0))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            streaming.main(["--model", "tiny"], out=lambda line: None)


if __name__ == "__main__":
    unittest.main()
