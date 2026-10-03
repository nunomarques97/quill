"""bench.asr_errors tests with invented text, generated tones in temporary folders and fake models.

No GPU, model, microphone, Ollama or real recording is used; per-take outputs
go to a temporary folder standing in for bench/results/.
"""

import json
import random
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from bench import asr_errors
from bench.asr_errors import CONFIGS, Config, Context, Loaded, classify
from bench.dataset import Take
from bench.metrics import word_edits
from bench.normalize import normalize_words
from quill.heard import HeardHints
from quill.streaming import BYTES_PER_SECOND, StreamOptions
from quill.tests.test_streaming import FakeModel, silence, speech, text
from quill.vocabulary import Entry, Vocabulary, whisper_hints
from quill.whisper import PRECISE_MODEL, Transcript, WhisperUnavailable

APP_MODEL = "large-v3-turbo"
PROJECT = "Zorblax"
TERMS = ("NimbusDeck", "quorbit", "Flarn-Kit")


def write_take(folder: Path, take_id: str, pcm: bytes, reference: str, *, project: str = "",
               terms: tuple[str, ...] = (), style: str = "") -> Take:
    path = folder / f"{take_id}.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(pcm)
    names = (project,) if project else ()
    return Take(take_id, len(pcm) / BYTES_PER_SECOND, "microfone", "caso", "intencao", path, reference, names,
                style, terms=terms)


def vocabulary() -> Vocabulary:
    return Vocabulary(names=(Entry("Quessa", "name"),), terms=(Entry("blorptest", "term"),))


class ModelHub:
    """Fake models by name: tracks loads and closes so only one is ever loaded at a time."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.models: dict[str, FakeModel] = {}
        self.loaded = 0
        self.max_loaded = 0
        self.order: list[str] = []

    def __call__(self, name: str) -> object:
        hub = self
        model = FakeModel()
        self.models[name] = model

        def load() -> None:
            if hub.fail == name:
                raise WhisperUnavailable("fake: no memory")
            if name in hub.order and hub.loaded:
                return  # loaded already, as quill.whisper.Whisper.load
            hub.loaded += 1
            hub.max_loaded = max(hub.max_loaded, hub.loaded)
            hub.order.append(name)

        def close() -> None:
            if name in hub.order:
                hub.loaded -= 1

        transcribe = model.transcribe

        def decode(pcm, options):
            result = transcribe(pcm, options)
            # The larger model and a wider beam hear "w2" right; otherwise it is split into "v2 x".
            words = result.text.split()
            if not (name == PRECISE_MODEL or options.beam_size > 5):
                words = ["v2 x" if w == "w2" else w for w in words]
            language = "en" if options.detect_language else "pt"
            return Transcript(" ".join(words), result.words, language)

        model.load, model.close, model.transcribe = load, close, decode
        return model


def context(hints_for=lambda project: None, pipeline=lambda text, project: text.upper()) -> Context:
    vocab = vocabulary()
    return Context(vocab, (), whisper_hints(vocab, (), ()), APP_MODEL, lambda name: StreamOptions(commit=False),
                   hints_for, pipeline, {"app_model": APP_MODEL, "compute_type": "float16"})


class ClassifyTest(unittest.TestCase):
    def test_totals_equal_word_edits(self) -> None:
        rng = random.Random(7)
        alphabet = ["a", "b", "c", "d", "ab"]
        for _ in range(300):
            ref = [rng.choice(alphabet) for _ in range(rng.randint(0, 8))]
            hyp = [rng.choice(alphabet) for _ in range(rng.randint(0, 8))]
            counts = classify(" ".join(ref), " ".join(hyp))
            edits = word_edits(normalize_words(" ".join(ref)), normalize_words(" ".join(hyp)))
            self.assertEqual((counts.substitutions, counts.deletions, counts.insertions, counts.reference_words),
                             (edits.substitutions, edits.deletions, edits.insertions, edits.reference_words))
            self.assertEqual(counts.term_errors + counts.other_errors, counts.errors)

    def test_one_word_heard_as_two_is_a_split(self) -> None:
        counts = classify("então o teste falha sempre aqui", "então o teste file é sempre aqui")
        self.assertEqual((counts.splits, counts.split_errors, counts.merges), (1, 2, 0))
        self.assertEqual((counts.substitutions, counts.insertions), (1, 1))
        self.assertEqual(counts.edge_errors, 0)

    def test_two_words_heard_as_one_is_a_merge(self) -> None:
        counts = classify("corre o auto rewrite agora mesmo", "corre o autorewrite agora mesmo")
        self.assertEqual((counts.merges, counts.merge_errors, counts.splits), (1, 2, 0))

    def test_edges_are_the_first_and_last_two_words(self) -> None:
        reference = "um dois três quatro cinco seis sete"
        self.assertEqual(classify(reference, "dois três quatro cinco seis sete").edge_errors, 1)
        self.assertEqual(classify(reference, "um dois três quatro cinco seis").edge_errors, 1)
        self.assertEqual(classify(reference, "ah um dois três quatro cinco seis sete").edge_errors, 1)
        self.assertEqual(classify(reference, "um dois três quatro cinco seis sete ah").edge_errors, 1)
        middle = classify(reference, "um dois três catorze cinco seis sete")
        self.assertEqual((middle.edge_errors, middle.errors, middle.edge_words), (0, 1, 4))
        self.assertEqual(classify("sim", "não").edge_words, 1)

    def test_term_errors_against_other_errors(self) -> None:
        counts = classify("abre o nimbus deck agora e corre", "abre o nimbos deck agora a corre", ["Nimbus Deck"])
        self.assertEqual((counts.term_words, counts.term_errors, counts.other_errors), (2, 1, 1))
        clean = classify("abre o nimbus deck agora", "abre o nimbus deck agora", ["Nimbus Deck"])
        self.assertEqual((clean.errors, clean.term_words, clean.splits, clean.merges), (0, 2, 0, 0))

    def test_errors_add_up(self) -> None:
        total = classify("a b c", "a x c") + classify("d e", "d e f")
        self.assertEqual((total.reference_words, total.substitutions, total.insertions), (5, 1, 1))


class TrimTest(unittest.TestCase):
    def test_trim_keeps_lead_and_tail_pad_around_speech(self) -> None:
        options = StreamOptions(commit=False)
        pcm = silence(1.0) + speech([1, 2], lead=0.0, trail=0.0) + silence(1.0)
        start, end = asr_errors.trim_range(pcm, options)
        speech_start = int(1.0 * BYTES_PER_SECOND)
        speech_end = speech_start + int(0.8 * BYTES_PER_SECOND)
        self.assertAlmostEqual(start / BYTES_PER_SECOND, (speech_start / BYTES_PER_SECOND) - options.lead_s, places=2)
        self.assertAlmostEqual(end / BYTES_PER_SECOND, speech_end / BYTES_PER_SECOND + options.tail_pad_s, places=2)

    def test_silence_has_no_range(self) -> None:
        self.assertIsNone(asr_errors.trim_range(silence(2.0), StreamOptions()))


class HintsTest(unittest.TestCase):
    def test_app_words_at_the_app_budget_equal_a_fresh_heard_source(self) -> None:
        vocab = vocabulary()
        for heard in ("", "abre o nimbos deck", "o quorbit e o flarn kit"):
            source = HeardHints(PROJECT, TERMS, vocab, ("deploy",))
            expected = HeardHints(PROJECT, TERMS, vocab, ("deploy",)).words(heard)
            self.assertEqual(asr_errors.app_words(source, vocab, ("deploy",), heard), expected)
        self.assertEqual(asr_errors.app_words(None, vocab, ("deploy",), "x"), whisper_hints(vocab, (), ("deploy",)))

    def test_budget_cuts_the_list(self) -> None:
        vocab = Vocabulary(names=tuple(Entry(f"Nome{i}", "name") for i in range(60)))
        small = asr_errors.app_words(None, vocab, (), "", 150)
        large = asr_errors.app_words(None, vocab, (), "", 600)
        self.assertLess(len(small), len(asr_errors.app_words(None, vocab, (), "")))
        self.assertLessEqual(sum(map(len, small)) + 2 * (len(small) - 1), 150)
        self.assertGreater(len(large), len(asr_errors.app_words(None, vocab, (), "")))

    def test_config_hints_and_decode(self) -> None:
        vocab = vocabulary()
        by_name = {config.name: config for config in CONFIGS}
        self.assertIsNone(asr_errors.config_hints(by_name["hints_none"], None, vocab, (), ""))
        app = asr_errors.config_hints(by_name["whole_app"], None, vocab, (), "")
        self.assertIn("Quessa", app.hotwords)
        options = StreamOptions()
        both = asr_errors.whole_decode(by_name["whole_app"], app, 4.0, options)
        self.assertEqual((both.initial_prompt, both.hotwords, both.beam_size), (app.prompt, app.hotwords, 5))
        self.assertEqual(both.max_new_tokens, 40 + options.min_new_tokens)
        self.assertTrue(both.without_timestamps)
        self.assertEqual((both.detect_language, both.temperature_fallback, both.condition_on_previous_text,
                          both.vad_filter), (False, False, False, False))
        hot = asr_errors.whole_decode(by_name["hotwords_only"], app, 4.0, options)
        self.assertEqual((hot.initial_prompt, hot.hotwords), (None, app.hotwords))
        prompt = asr_errors.whole_decode(by_name["prompt_only"], app, 4.0, options)
        self.assertEqual((prompt.initial_prompt, prompt.hotwords), (app.prompt, None))
        none = asr_errors.whole_decode(by_name["hints_none"], None, 4.0, options)
        self.assertEqual((none.initial_prompt, none.hotwords), (None, None))
        instructed = asr_errors.whole_decode(by_name["prompt_instruction"], app, 4.0, options)
        self.assertEqual((instructed.initial_prompt, instructed.hotwords),
                         (f"{asr_errors.INSTRUCTION_PROMPT} {app.prompt}", app.hotwords))
        alone = asr_errors.whole_decode(replace(by_name["prompt_instruction"], hints="none"), None, 4.0, options)
        self.assertEqual((alone.initial_prompt, alone.hotwords), (asr_errors.INSTRUCTION_PROMPT, None))
        for name, attribute in (("language_auto", "detect_language"), ("temperature_fallback", "temperature_fallback"),
                                ("condition_previous", "condition_on_previous_text"),
                                ("whole_vad_filter", "vad_filter")):
            self.assertTrue(getattr(asr_errors.whole_decode(by_name[name], app, 4.0, options), attribute), name)


class MatrixTest(unittest.TestCase):
    def test_every_cause_is_isolated(self) -> None:
        by_name = {config.name: config for config in CONFIGS}
        self.assertEqual(len(by_name), len(CONFIGS))
        for config in CONFIGS:
            if config.compared_with is not None:
                self.assertIn(config.compared_with, by_name)
        causes = {config.cause for config in CONFIGS}
        self.assertTrue({"baseline", "segmentation", "vad_trimming", "engine", "hints", "hint_budget", "language",
                         "beam", "temperature_fallback", "condition_on_previous_text", "initial_prompt"} <= causes)
        self.assertTrue(by_name["stream_app"].stream and by_name["stream_app"].hints == "app")
        whole = by_name["whole_app"]
        self.assertEqual((whole.stream, whole.model, whole.trim, whole.beam), (False, asr_errors.APP, True, 5))
        self.assertFalse(by_name["whole_untrimmed"].trim)
        self.assertEqual(by_name["whole_large_v3"].model, PRECISE_MODEL)
        self.assertEqual({by_name["hints_none"].hints, by_name["hints_vocabulary"].hints}, {"none", "vocabulary"})
        budgets = {config.budget for config in CONFIGS if config.cause == "hint_budget"}
        self.assertEqual(len(budgets), 2)
        self.assertNotIn(asr_errors.HINT_BUDGET, budgets)
        beams = {config.beam for config in CONFIGS if config.cause == "beam"}
        self.assertIn(1, beams)
        self.assertTrue(any(beam > 5 for beam in beams))
        self.assertEqual({config.use for config in CONFIGS if config.cause == "initial_prompt"},
                         {"hotwords", "prompt", "both"})
        self.assertEqual([config.name for config in CONFIGS if config.instruction], ["prompt_instruction"])

    def test_app_model_loads_first(self) -> None:
        self.assertEqual(asr_errors.model_order(CONFIGS, APP_MODEL), [APP_MODEL, PRECISE_MODEL])
        self.assertEqual(asr_errors.model_order(CONFIGS, PRECISE_MODEL), [PRECISE_MODEL])


class Fixture:
    """Generated-tone takes of the four sets in a temporary folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        terms = ("Flarn-Kit",)
        self.prompt = write_take(root, "pp-01", speech([1, 2, 3]), f"{text([1, 2, 3])} Flarn-Kit", project=PROJECT,
                                 terms=terms)
        self.prompt2 = write_take(root, "pp-02", speech([1, 2, 3]) + silence(0.5), text([1, 2, 3]), terms=terms)
        self.claude = write_take(root, "dt-01", speech([2, 4]), text([2, 4]), style="claude-code")
        self.other = write_take(root, "dt-02", speech([3, 4, 5]), text([3, 4, 5]))
        self.rewrite = write_take(root, "rw-01", speech([4, 5]), text([4, 5]))
        self.loaded = Loaded(members={"prompts": (self.prompt, self.prompt2), "claude_code": (self.claude,),
                                      "dictation": (self.claude, self.other), "rewrite": (self.rewrite,)})


def gpu() -> dict:
    return {"used_mib": 1000, "free_mib": 15000, "ollama_loaded": []}


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.fx = Fixture(self.root)
        self.lines: list[str] = []

    def run_configs(self, hub: ModelHub, ctx: Context | None = None):
        return asr_errors.run_configs(CONFIGS, ctx or context(), self.fx.loaded.unique(), hub, gpu, self.lines.append)

    def test_every_configuration_runs_with_one_model_at_a_time(self) -> None:
        hub = ModelHub()
        rows, snapshots = self.run_configs(hub)
        self.assertEqual(set(rows), set(asr_errors.CONFIG_NAMES))
        for name, config_rows in rows.items():
            self.assertEqual(sorted(row.id for row in config_rows), sorted(t.id for t in self.fx.loaded.unique()))
            self.assertTrue(all(row.error is None for row in config_rows), name)
        self.assertEqual(hub.order, [APP_MODEL, PRECISE_MODEL])
        self.assertEqual((hub.max_loaded, hub.loaded), (1, 0))
        self.assertEqual([s["label"] for s in snapshots][0], "start")
        self.assertEqual(snapshots[-1]["label"], "end")
        self.assertIn(f"{PRECISE_MODEL} loaded", [s["label"] for s in snapshots])
        stream = {row.id: row for row in rows["stream_app"]}
        self.assertEqual(stream["pp-01"].text, "w1 v2 x w3")
        self.assertEqual(stream["pp-01"].pipeline, "W1 V2 X W3")
        self.assertEqual({row.id: row.text for row in rows["whole_large_v3"]}["pp-01"], "w1 w2 w3")
        self.assertEqual({row.language for row in rows["language_auto"]}, {"en"})
        decodes = [call.options for call in hub.models[APP_MODEL].calls if call.options.without_timestamps]
        self.assertTrue(any(options.beam_size == 1 for options in decodes))
        self.assertTrue(any(options.vad_filter for options in decodes))

    def test_untrimmed_audio_is_longer(self) -> None:
        hub = ModelHub()
        self.run_configs(hub)
        whole = [call.seconds for call in hub.models[APP_MODEL].calls if call.options.without_timestamps]
        self.assertIn(round(self.fx.prompt2.duration_s, 2), [round(seconds, 2) for seconds in whole])

    def test_app_hints_ask_a_fresh_source_with_the_streaming_text(self) -> None:
        asked: list[str] = []
        vocab = vocabulary()

        def hints_for(project: str):
            if project != PROJECT:
                return None
            source = HeardHints(PROJECT, TERMS, vocab, ())
            real = source.matcher.closest

            def closest(heard):
                asked.append(heard)
                return real(heard)

            source.matcher.closest = closest
            return source

        rows, _ = self.run_configs(ModelHub(), context(hints_for=hints_for))
        self.assertIn("w1 v2 x w3", asked)
        self.assertEqual(len(rows["hints_none"]), len(self.fx.loaded.unique()))

    def test_summary_blocks_causes_and_check(self) -> None:
        rows, snapshots = self.run_configs(ModelHub())
        sets = asr_errors.set_blocks(rows, self.fx.loaded.members, CONFIGS)
        summary = asr_errors.build_summary(sets, CONFIGS, {"app_model": APP_MODEL}, snapshots)
        prompts = summary["sets"]["prompts"]["configs"]
        base = prompts["stream_app"]
        self.assertEqual(base["reference_words"], 8)
        self.assertEqual(base["word_errors"], {"substitutions": 2, "deletions": 2, "insertions": 2, "total": 6})
        self.assertEqual(base["splits"], {"events": 2, "errors": 4})
        self.assertEqual(base["merges"], {"events": 0, "errors": 0})
        self.assertEqual(base["edges"], {"reference_words": 7, "errors": 6})
        self.assertEqual(base["term_words"], {"reference_words": 2, "errors": 2})
        self.assertEqual(base["other_words"], {"reference_words": 6, "errors": 4})
        self.assertEqual(base["domain_terms"], {"occurrences": 1, "errors": 1})
        self.assertEqual(base["pipeline"]["word_errors"], 6)
        self.assertEqual(base["languages"], {"pt": 2})
        self.assertEqual(base["latency_s"]["n"], 2)
        self.assertEqual(prompts["whole_large_v3"]["word_errors"]["total"], 2)
        causes = summary["causes"]["prompts"]
        self.assertEqual({entry["cause"] for entry in causes[:2]}, {"beam", "engine"})
        self.assertEqual([entry["word_errors_removed"] for entry in causes[:2]], [4, 4])
        self.assertEqual(summary["sets"]["claude_code"]["takes"], 1)
        self.assertEqual(summary["configurations"]["whole_large_v3"]["model"], PRECISE_MODEL)
        self.assertTrue(all(ok for ok, _ in asr_errors.check_summary(summary)))
        broken = json.loads(json.dumps(summary))
        del broken["sets"]["rewrite"]["configs"]["beam_10"]["splits"]
        lines = asr_errors.check_summary(broken)
        self.assertFalse(all(ok for ok, _ in lines))
        self.assertTrue(any("beam_10" in line for ok, line in lines if not ok))

    def test_a_model_that_cannot_load_fails_its_blocks(self) -> None:
        rows, snapshots = self.run_configs(ModelHub(fail=PRECISE_MODEL))
        self.assertTrue(all(row.error == "model unavailable" for row in rows["whole_large_v3"]))
        self.assertTrue(all(row.error is None for row in rows["whole_app"]))
        self.assertIn(f"{PRECISE_MODEL} failed to load", [s["label"] for s in snapshots])
        sets = asr_errors.set_blocks(rows, self.fx.loaded.members, CONFIGS)
        summary = asr_errors.build_summary(sets, CONFIGS, {"app_model": APP_MODEL}, snapshots)
        self.assertEqual(summary["sets"]["prompts"]["configs"]["whole_large_v3"]["failed"], 2)
        self.assertFalse(all(ok for ok, _ in asr_errors.check_summary(summary)))


def forbidden(*args, **kwargs):
    raise AssertionError("the dry run must not open a model, the GPU or Ollama")


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.fx = Fixture(self.root)
        self.results = self.root / "results"
        self.summary = self.root / "summary.json"
        self.lines: list[str] = []

    def main(self, *argv, ctx=None, **kwargs) -> int:
        return asr_errors.main(
            [*argv, "--vocabulary", str(self.root / "missing.toml"), "--summary", str(self.summary)],
            context_factory=lambda vocab: ctx or context(), model_factory=kwargs.pop("model_factory", ModelHub()),
            gpu=kwargs.pop("gpu", gpu), sets_loader=lambda config, chosen: (None, self.fx.loaded),
            results_dir=self.results, out=self.lines.append)

    def test_dry_run_prints_counts_only(self) -> None:
        code = asr_errors.main(["--dry-run"], context_factory=forbidden, model_factory=forbidden, gpu=forbidden,
                               sets_loader=lambda config, chosen: (None, self.fx.loaded), results_dir=self.results,
                               out=self.lines.append)
        self.assertEqual(code, 0)
        self.assertIn("prompts: 2 takes", self.lines)
        self.assertTrue(any(line.startswith("unique takes: 5; configurations: 20") for line in self.lines))
        self.assertFalse(self.results.exists())
        joined = "\n".join(self.lines)
        for take in self.fx.loaded.unique():
            self.assertNotIn(take.clean, joined)

    def test_run_writes_summary_and_private_rows(self) -> None:
        self.assertEqual(self.main(), 0)
        summary = json.loads(self.summary.read_text(encoding="utf-8"))
        self.assertEqual(summary["kind"], "asr_errors")
        private = list(self.results.glob("asr/*/takes.json"))
        self.assertEqual(len(private), 1)
        rows = json.loads(private[0].read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 5 * len(CONFIGS))
        serialized = self.summary.read_text(encoding="utf-8")
        self.assertNotIn("w1 w2", serialized)
        self.assertNotIn(PROJECT, serialized)
        check = []
        self.assertEqual(asr_errors.main(["--check", str(self.summary)], out=check.append), 0)

    def test_a_name_in_the_summary_refuses_it(self) -> None:
        leaky = context()
        leaky.info["note"] = f"project {PROJECT}"
        self.assertEqual(self.main(ctx=leaky), 1)
        self.assertFalse(self.summary.exists())
        self.assertTrue(any("not written" in line for line in self.lines))
        self.assertEqual(len(list(self.results.glob("asr/*/takes.json"))), 1)

    def test_a_spoken_phrase_in_the_summary_refuses_it(self) -> None:
        leaky = context()
        leaky.info["note"] = self.fx.other.clean
        self.assertEqual(self.main(ctx=leaky), 1)
        self.assertFalse(self.summary.exists())

    def test_pack_terms_are_private_but_measured_model_names_are_not(self) -> None:
        ctx = context(hints_for=lambda project: HeardHints(PROJECT, (*TERMS, PRECISE_MODEL), vocabulary(), ()))
        _, names = asr_errors.private_names(None, self.fx.loaded, vocabulary(), ctx)
        self.assertIn("NimbusDeck", names)
        self.assertIn(PROJECT, names)
        self.assertNotIn(PRECISE_MODEL, names)
        ctx.info["model"] = PRECISE_MODEL
        self.assertEqual(self.main(ctx=ctx), 0)
        self.assertIn(PRECISE_MODEL, self.summary.read_text(encoding="utf-8"))
        ctx.info["note"] = "NimbusDeck"
        self.assertEqual(self.main(ctx=ctx), 1)

    def test_check_refuses_other_files(self) -> None:
        other = self.root / "other.json"
        other.write_text(json.dumps({"kind": "mouse5_prompts"}), encoding="utf-8")
        lines: list[str] = []
        self.assertEqual(asr_errors.main(["--check", str(other)], out=lines.append), 1)
        (self.root / "bad.json").write_text("{", encoding="utf-8")
        self.assertEqual(asr_errors.main(["--check", str(self.root / "bad.json")], out=lines.append), 2)

    def test_gpu_snapshot_is_read_only(self) -> None:
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(stdout="5, 14000, 2000\n")

        from bench.streaming import gpu_state

        state = gpu_state(run=run, loaded=lambda: [{"name": "fake:1b", "vram_mib": 900}])
        self.assertEqual((state["used_mib"], state["free_mib"]), (14000, 2000))
        self.assertEqual(calls[0][0], "nvidia-smi")
        self.assertTrue(all("--query-gpu" in part or not part.startswith("--") or part.startswith("--format")
                            for part in calls[0][1:]))


if __name__ == "__main__":
    unittest.main()
