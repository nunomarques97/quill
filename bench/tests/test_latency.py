"""Latency composites and timing, with fake engines and a fake clock."""

from __future__ import annotations

import json
import tempfile
import unittest
from array import array
from pathlib import Path

from bench.engines.base import EngineError, ResponseCache, wav_pcm
from bench.latency import (
    BYTES_PER_SECOND,
    COMPOSITES,
    REPETITIONS,
    EngineSample,
    add_cleanup,
    build_composites,
    measure_engine,
    save_composites,
    timed_transcribe,
    warm_up,
    without_cleanup,
)
from bench.tests.fakes import FakeEngine, tone_wav


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.events: list[str] = []

    def __call__(self) -> float:
        self.events.append("clock")
        return self.now


class StepEngine(FakeEngine):
    """Each call advances the clock by ``step`` seconds; pacing is logged."""

    def __init__(self, clock: FakeClock, step: float = 0.5, attempts=None, **kwargs) -> None:
        super().__init__("step", {}, **kwargs)
        self.clock = clock
        self.step = step
        self.attempts = list(attempts or [])
        self._last = 1

    def wait_turn(self) -> None:
        self.clock.events.append("wait")

    @property
    def last_attempts(self) -> int:
        return self._last

    def transcribe(self, wav, hints):
        text = super().transcribe(wav, hints)
        self.clock.now += self.step
        self._last = self.attempts.pop(0) if self.attempts else 1
        return text


def takes(count: int = 12) -> list[tuple[str, bytes]]:
    return [(f"pt-{n:02d}", wav_pcm(tone_wav(n, 0.6 + (n % 6)))[0]) for n in range(1, count + 1)]


class CompositeTest(unittest.TestCase):
    def test_ten_composites_between_5_and_15_seconds(self) -> None:
        composites = build_composites(takes())
        self.assertEqual(len(composites), COMPOSITES)
        self.assertGreaterEqual(COMPOSITES, 10)
        self.assertEqual(REPETITIONS, 2)
        for composite in composites:
            self.assertGreaterEqual(composite.duration_s, 5.0)
            self.assertLessEqual(composite.duration_s, 15.0)
            self.assertTrue(set(composite.take_ids) <= {tid for tid, _ in takes()})
            self.assertGreaterEqual(len(composite.take_ids), 1)
        durations = [c.duration_s for c in composites]
        self.assertGreater(max(durations) - min(durations), 4.0)

    def test_deterministic_and_made_only_of_real_takes(self) -> None:
        source = dict(takes())
        first, second = build_composites(takes()), build_composites(takes())
        self.assertEqual([c.sha256 for c in first], [c.sha256 for c in second])
        pcm = wav_pcm(first[0].wav)[0]
        self.assertTrue(pcm.startswith(source[first[0].take_ids[0]]))
        self.assertTrue(pcm.endswith(source[first[0].take_ids[-1]]))

    def test_gaps_are_not_digital_silence(self) -> None:
        composite = build_composites(takes(), count=1)[0]
        samples = array("h", wav_pcm(composite.wav)[0])
        run = longest = 0
        for value in samples:
            run = run + 1 if value == 0 else 0
            longest = max(longest, run)
        self.assertLess(longest, BYTES_PER_SECOND // 2 // 10)

    def test_too_long_takes_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_composites([("pt-01", b"\x01\x00" * (16_000 * 16))])

    def test_saved_under_given_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            composites = build_composites(takes(), count=2)
            save_composites(composites, Path(folder))
            index = json.loads((Path(folder) / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([entry["id"] for entry in index], ["c01", "c02"])
            self.assertTrue((Path(folder) / f"{composites[0].sha256}.wav").is_file())


class TimingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.composites = build_composites(takes(), count=3)

    def test_pacing_happens_before_the_clock_starts(self) -> None:
        clock = FakeClock()
        engine = StepEngine(clock, step=0.25)
        _, elapsed = timed_transcribe(engine, self.composites[0].wav, None, clock)
        self.assertEqual(elapsed, 0.25)
        self.assertEqual(clock.events[:3], ["wait", "clock", "clock"])

    def test_retried_http_samples_are_measured_again(self) -> None:
        clock = FakeClock()
        engine = StepEngine(clock, attempts=[2, 1])
        timed_transcribe(engine, self.composites[0].wav, None, clock)
        self.assertEqual(engine.calls, 2)
        with self.assertRaises(EngineError):
            timed_transcribe(StepEngine(clock, attempts=[2, 2, 2]), self.composites[0].wav, None, clock)

    def test_every_composite_twice_and_cloud_cache_reused(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as folder:
            cache = ResponseCache(Path(folder))
            engine = StepEngine(clock, cacheable=True)
            samples = measure_engine(engine, None, self.composites, cache=cache, clock=clock)
            self.assertEqual([(s.composite, s.repetition) for s in samples][:2], [("c01", 1), ("c01", 2)])
            self.assertEqual(len(samples), 3 * REPETITIONS)
            again = StepEngine(clock, cacheable=True)
            cached = measure_engine(again, None, self.composites, cache=cache, clock=clock)
            self.assertEqual(again.calls, 0)
            self.assertEqual([s.engine_s for s in cached], [s.engine_s for s in samples])
            local = StepEngine(clock)
            measure_engine(local, None, self.composites, cache=cache, clock=clock)
            self.assertEqual(local.calls, 3 * REPETITIONS)

    def test_cleanup_time_is_added(self) -> None:
        clock = FakeClock()
        samples = [EngineSample("c01", 1, 0.4, "texto"), EngineSample("c01", 2, 0.6, "texto")]

        def clean(text):
            clock.now += 0.3

        with_cleanup = add_cleanup(samples, clean, clock)
        self.assertEqual([round(s.total_s, 6) for s in with_cleanup], [0.7, 0.9])
        self.assertEqual([s.total_s for s in without_cleanup(samples)], [0.4, 0.6])

    def test_warm_up_reported_apart(self) -> None:
        clock = FakeClock()
        local = StepEngine(clock, step=2.0)
        self.assertEqual(warm_up(local, self.composites[0], None, clock), {"load_s": 0.0, "warmup_s": 2.0})
        self.assertEqual((local.loaded, local.calls), (1, 1))
        cloud = StepEngine(clock, cacheable=True)
        self.assertEqual(warm_up(cloud, self.composites[0], None, clock)["warmup_s"], None)
        self.assertEqual(cloud.calls, 0)


if __name__ == "__main__":
    unittest.main()
