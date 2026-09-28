"""Latency on 5-15 s composites built from the real takes.

The real takes are short commands, so typical dictation lengths are made by
concatenating real takes (real voice, never synthetic) with a short silence
between them. Latency is the time from the moment the whole utterance audio
is in memory (simulated key release) to the final text: the engine call,
including the network for cloud engines, plus cleanup for cleanup variants.
Model load and warm-up are measured and reported separately.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.engines.base import Engine, EngineError, Hints, ResponseCache, pcm16_wav, sha256_hex

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
GAP_S = 0.3
MIN_S = 5.0
MAX_S = 15.0
COMPOSITES = 10
REPETITIONS = 2
SEED = 20260928
# A sample whose HTTP call was retried includes a wait, so it is measured again.
MAX_CLEAN_TRIES = 3
# Low-level noise instead of digital silence between takes (deterministic).
GAP_AMPLITUDE = 3


@dataclass(frozen=True)
class Composite:
    id: str
    take_ids: tuple[str, ...]
    duration_s: float
    wav: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        return sha256_hex(self.wav)


def _gap(seconds: float, rng: random.Random) -> bytes:
    count = int(seconds * SAMPLE_RATE)
    from array import array

    return array("h", (rng.randint(-GAP_AMPLITUDE, GAP_AMPLITUDE) for _ in range(count))).tobytes()


def build_composites(
    takes: Sequence[tuple[str, bytes]],
    count: int = COMPOSITES,
    seed: int = SEED,
    min_s: float = MIN_S,
    max_s: float = MAX_S,
    gap_s: float = GAP_S,
) -> list[Composite]:
    """Deterministic composites whose durations spread across [min_s, max_s].

    ``takes`` are (id, raw PCM16 at 16 kHz). Takes are drawn in a seeded
    order and reused only after every take has been used once.
    """
    usable = [(tid, pcm) for tid, pcm in takes if 0 < len(pcm) / BYTES_PER_SECOND <= max_s]
    if not usable:
        raise ValueError("no takes short enough to build composites")
    rng = random.Random(seed)
    order = list(usable)
    rng.shuffle(order)
    position = 0
    composites: list[Composite] = []
    attempts = 0
    while len(composites) < count:
        attempts += 1
        if attempts > count * len(order) + 100:
            raise ValueError("cannot build composites in the requested duration range")
        index = len(composites)
        target = min_s + 0.5 + (max_s - min_s - 1.0) * (index / max(1, count - 1))
        parts: list[tuple[str, bytes]] = []
        duration = 0.0
        tried = 0
        while duration < target and tried < len(order):
            tid, pcm = order[position % len(order)]
            position += 1
            tried += 1
            length = len(pcm) / BYTES_PER_SECOND
            extra = length + (gap_s if parts else 0.0)
            if duration + extra > max_s:
                continue
            parts.append((tid, pcm))
            duration += extra
        if duration < min_s:
            continue
        chunks: list[bytes] = []
        for number, (_, pcm) in enumerate(parts):
            if number:
                chunks.append(_gap(gap_s, rng))
            chunks.append(pcm)
        audio = b"".join(chunks)
        composites.append(
            Composite(
                id=f"c{index + 1:02d}",
                take_ids=tuple(tid for tid, _ in parts),
                duration_s=round(len(audio) / BYTES_PER_SECOND, 3),
                wav=pcm16_wav(audio, SAMPLE_RATE),
            )
        )
    return composites


def save_composites(composites: Sequence[Composite], folder: Path) -> None:
    """Write the composites and their index under the ignored results folder."""
    folder.mkdir(parents=True, exist_ok=True)
    index = []
    for composite in composites:
        (folder / f"{composite.sha256}.wav").write_bytes(composite.wav)
        index.append({"id": composite.id, "sha256": composite.sha256, "takes": list(composite.take_ids), "duration_s": composite.duration_s})
    (folder / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class EngineSample:
    composite: str
    repetition: int
    engine_s: float
    text: str = field(repr=False)


@dataclass(frozen=True)
class LatencySample:
    composite: str
    repetition: int
    engine_s: float
    cleanup_s: float | None

    @property
    def total_s(self) -> float:
        return self.engine_s + (self.cleanup_s or 0.0)


def timed_transcribe(engine: Engine, wav: bytes, hints: Hints | None, clock: Callable[[], float]) -> tuple[str, float]:
    """One clean timed call: pacing happens before the clock starts; retried calls are redone."""
    for _ in range(MAX_CLEAN_TRIES):
        engine.wait_turn()
        started = clock()
        text = engine.transcribe(wav, hints)
        elapsed = clock() - started
        if engine.last_attempts <= 1:
            return text, elapsed
    raise EngineError(f"{engine.id}: every latency sample needed an HTTP retry")


def warm_up(engine: Engine, composite: Composite, hints: Hints | None, clock: Callable[[], float] = time.perf_counter) -> dict:
    """Model load and first-call times, reported apart from the latency samples.

    Cloud engines skip the warm-up call so no extra audio is sent; each of
    their requests opens a new HTTPS connection anyway.
    """
    started = clock()
    engine.load()
    load_s = clock() - started
    if engine.cacheable:
        return {"load_s": round(load_s, 3), "warmup_s": None}
    _, first_s = timed_transcribe(engine, composite.wav, hints, clock)
    return {"load_s": round(load_s, 3), "warmup_s": round(first_s, 3)}


def measure_engine(
    engine: Engine,
    hints: Hints | None,
    composites: Sequence[Composite],
    repetitions: int = REPETITIONS,
    cache: ResponseCache | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> list[EngineSample]:
    """Every composite ``repetitions`` times. Cloud samples are cached, so a rerun resends nothing."""
    samples: list[EngineSample] = []
    for composite in composites:
        for repetition in range(1, repetitions + 1):
            key = None
            if cache is not None and engine.cacheable:
                key = cache.key(
                    kind="latency", audio=composite.sha256, engine=engine.id, params=engine.params(hints), repetition=repetition
                )
                cached = cache.get(key)
                if cached and isinstance(cached.get("text"), str) and isinstance(cached.get("engine_s"), (int, float)):
                    samples.append(EngineSample(composite.id, repetition, float(cached["engine_s"]), cached["text"]))
                    continue
            text, elapsed = timed_transcribe(engine, composite.wav, hints, clock)
            if key is not None:
                cache.put(key, {"text": text, "engine_s": elapsed})
            samples.append(EngineSample(composite.id, repetition, elapsed, text))
    return samples


def add_cleanup(samples: Sequence[EngineSample], clean: Callable[[str], object], clock: Callable[[], float] = time.perf_counter) -> list[LatencySample]:
    """Cleanup time measured now on each engine sample's own output."""
    result = []
    for sample in samples:
        started = clock()
        clean(sample.text)
        result.append(LatencySample(sample.composite, sample.repetition, sample.engine_s, clock() - started))
    return result


def without_cleanup(samples: Sequence[EngineSample]) -> list[LatencySample]:
    return [LatencySample(s.composite, s.repetition, s.engine_s, None) for s in samples]
