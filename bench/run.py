"""Benchmark entry point.

Usage:
    py -3.12 -m bench.run --preflight
    py -3.12 -m bench.run --dry-run [--config local/bench.toml]
    .venv\\Scripts\\python -m bench.run [--config ...] [--engines id,id] [--summary PATH]

--preflight lists every missing package, model, Ollama model and .env key
with the exact fix; it installs nothing and prints no key values.

--dry-run loads the real dataset in place and prints counts only; it never
prints spoken text, project names or paths. It exits 0 only when exactly the
expected number of valid takes (44) is available.

Without either flag the full benchmark runs: every engine and variant on all
valid takes, the latency composites, cleanup and the intent judge. Spoken
text goes only under bench/results/; the summary holds aggregates only. An
engine/variant that fails anywhere is SKIPPED as a whole with its reason.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.cleanup import CLEANUP_MODEL, Cleaner, OllamaClient, OllamaError
from bench.dataset import DatasetError, load_dataset
from bench.engines import BASE_VARIANTS, VARIANTS, EngineSlot, OptionsError, create_engines, load_engine_options
from bench.engines.base import Engine, EngineError, Hints, ResponseCache, build_hints, sha256_hex, wav_pcm
from bench.envfile import EnvFileError, load_keys, redact
from bench.gpu import snapshot
from bench.intent import IntentJudge, evaluate, write_table
from bench.latency import (
    Composite,
    EngineSample,
    LatencySample,
    add_cleanup,
    build_composites,
    measure_engine,
    save_composites,
    warm_up,
    without_cleanup,
)
from bench.metrics import ItemResult, aggregate, build_summary, load_terms, skipped, write_summary
from bench.settings import DEFAULT_CONFIG, RESULTS_DIR, SettingsError, load_settings

WARMUP_TEXT = "Olá."


def dry_run(config: Path | None) -> int:
    try:
        settings = load_settings(config)
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        dataset = load_dataset(settings)
    except DatasetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("placeholders resolved: no")
        return 1

    valid = len(dataset.takes)
    print(f"valid takes: {valid} (expected {settings.expected_takes})")
    print(f"discarded takes excluded (*.invalida-*): {dataset.discarded}")
    print(f"invalid takes excluded: {len(dataset.invalid)}")
    for reason, count in sorted(Counter(item.reason for item in dataset.invalid).items()):
        print(f"  {reason}: {count}")
    print(f"placeholders resolved: {'yes' if dataset.placeholders_resolved else 'no'}")
    origins = ", ".join(f"{origin}={count}" for origin, count in sorted(dataset.origins.items()))
    print(f"recording origin: {origins or 'none'}")
    if valid != settings.expected_takes:
        print(f"FAIL: {valid} valid takes, {settings.expected_takes} required")
        return 1
    print("OK: dataset ready")
    return 0


# ---------------------------------------------------------------- pipeline


@dataclass(frozen=True)
class Utterance:
    id: str
    wav: bytes = field(repr=False)
    reference: str = field(repr=False)
    names: tuple[str, ...] = field(default=(), repr=False)


@dataclass
class VariantRun:
    """One engine/variant: its texts and latency samples, or the SKIPPED reason."""

    engine: str
    variant: str
    texts: list[str] | None = field(default=None, repr=False)
    engine_samples: list[EngineSample] | None = field(default=None, repr=False)
    samples: list[LatencySample] | None = None
    skipped: str | None = None
    intent: list | None = field(default=None, repr=False)
    intent_note: str | None = None

    def skip(self, reason: str) -> None:
        self.skipped = reason
        self.texts = self.engine_samples = self.samples = self.intent = None


def _reason(exc: Exception, secrets: Sequence[str]) -> str:
    """Short, redacted reason. Unexpected exception types give their name only."""
    if isinstance(exc, (EngineError, OllamaError)):
        return redact(" ".join(str(exc).split()), secrets)[:180]
    return f"unexpected {type(exc).__name__}"


def transcribe_all(engine: Engine, utterances: Sequence[Utterance], hints: Hints | None, cache: ResponseCache | None) -> list[str]:
    """Text for every utterance; cloud answers come from the cache when present."""
    texts = []
    for utterance in utterances:
        key = None
        if cache is not None and engine.cacheable:
            key = cache.key(kind="take", audio=sha256_hex(utterance.wav), engine=engine.id, params=engine.params(hints))
            cached = cache.get(key)
            if cached is not None and isinstance(cached.get("text"), str):
                texts.append(cached["text"])
                continue
        engine.wait_turn()
        text = engine.transcribe(utterance.wav, hints)
        if key is not None:
            cache.put(key, {"text": text})
        texts.append(text)
    return texts


def run_engine(
    slot: EngineSlot,
    utterances: Sequence[Utterance],
    hints: Hints,
    composites: Sequence[Composite],
    cache: ResponseCache | None,
    secrets: Sequence[str],
    repetitions: int,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[dict[str, VariantRun], dict]:
    """The raw and hints variants of one engine. Returns runs by variant and warm-up info."""
    runs = {variant: VariantRun(slot.id, variant) for variant in BASE_VARIANTS}
    if slot.engine is None:
        for run in runs.values():
            run.skip(slot.unavailable or "engine unavailable")
        return runs, {}
    engine = slot.engine
    try:
        try:
            warm = warm_up(engine, composites[0], None, clock)
        except Exception as exc:  # EngineUnavailable, EngineError or a library failure
            for run in runs.values():
                run.skip(_reason(exc, secrets))
            return runs, {}
        for variant, run in runs.items():
            variant_hints = hints if variant == "hints" else None
            if variant_hints is not None:
                unsupported = engine.hints_unsupported()
                if unsupported:
                    run.skip(unsupported)
                    continue
                if not variant_hints.vocabulary():
                    run.skip("no vocabulary hints available")
                    continue
            try:
                run.texts = transcribe_all(engine, utterances, variant_hints, cache)
                run.engine_samples = measure_engine(engine, variant_hints, composites, repetitions, cache, clock)
                run.samples = without_cleanup(run.engine_samples)
            except Exception as exc:
                run.skip(_reason(exc, secrets))
        return runs, warm
    finally:
        engine.close()


def ollama_ready(client: OllamaClient) -> tuple[str | None, dict]:
    """None when qwen3:8b answers, else the reason; plus its load/warm-up times."""
    try:
        installed = client.installed()
    except OllamaError as exc:
        return f"Ollama unavailable: {exc}", {}
    if CLEANUP_MODEL not in installed:
        return f"{CLEANUP_MODEL} is not installed in Ollama", {}
    try:
        result = client.chat(CLEANUP_MODEL, "Reply with ok.", WARMUP_TEXT)
    except OllamaError as exc:
        return f"{CLEANUP_MODEL} cannot load: {exc}", {}
    return None, {"load_s": round(result.load_s, 3), "warmup_s": round(result.total_s, 3)}


def run_cleanup(base: VariantRun, cleaner: Cleaner, variant: str, secrets: Sequence[str], clock: Callable[[], float]) -> VariantRun:
    run = VariantRun(base.engine, variant)
    if base.skipped:
        run.skip(f"base variant skipped: {base.skipped}")
        return run
    try:
        run.texts = [cleaner.clean(text).content for text in base.texts]
        run.samples = add_cleanup(base.engine_samples, cleaner.clean, clock)
    except Exception as exc:
        run.skip(f"cleanup failed: {_reason(exc, secrets)}")
    return run


def judge_intent(run: VariantRun, utterances: Sequence[Utterance], judge, terms: Sequence[str], secrets: Sequence[str]) -> None:
    """Fill ``run.intent``; on a judge failure no phrase of this variant is judged."""
    items = [ItemResult(u.id, u.reference, text, u.names) for u, text in zip(utterances, run.texts)]
    try:
        run.intent = evaluate(items, judge, terms)
    except Exception as exc:
        run.intent = None
        run.intent_note = f"intent not judged: {_reason(exc, secrets)}"


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in ".-" else "_" for c in text)


def write_private(run_dir: Path, run: VariantRun, utterances: Sequence[Utterance], results_dir: Path = RESULTS_DIR) -> None:
    """Hypotheses, intent table and latency samples of one variant, under bench/results/ only."""
    if Path(results_dir).resolve() not in Path(run_dir).resolve().parents:
        raise ValueError("private outputs may only be written under bench/results/")
    if run.skipped:
        return
    stem = f"{_slug(run.engine)}__{_slug(run.variant)}"
    verdicts = {row.id: row for row in run.intent or []}
    rows = []
    for utterance, text in zip(utterances, run.texts):
        row = verdicts.get(utterance.id)
        rows.append(
            {
                "id": utterance.id,
                "reference": utterance.reference,
                "hypothesis": text,
                "intent_preserved": row.preserved if row else None,
                "intent_reason": row.reason if row else run.intent_note,
            }
        )
    (run_dir / "hypotheses").mkdir(parents=True, exist_ok=True)
    (run_dir / "hypotheses" / f"{stem}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if run.intent:
        write_table(run_dir / "intent" / f"{stem}.md", run.engine, run.variant, run.intent, results_dir)
    latency = [
        {"composite": s.composite, "repetition": s.repetition, "engine_s": s.engine_s, "cleanup_s": s.cleanup_s}
        for s in run.samples
    ]
    (run_dir / "latency").mkdir(parents=True, exist_ok=True)
    (run_dir / "latency" / f"{stem}.json").write_text(json.dumps(latency, indent=2), encoding="utf-8")


def result_row(run: VariantRun, utterances: Sequence[Utterance], terms: Sequence[str]) -> dict:
    if run.skipped:
        return skipped(run.engine, run.variant, run.skipped)
    verdicts = {row.id: row.preserved for row in run.intent or []}
    items = [ItemResult(u.id, u.reference, text, u.names, verdicts.get(u.id)) for u, text in zip(utterances, run.texts)]
    row = aggregate(run.engine, run.variant, items, terms, [s.total_s for s in run.samples])
    if run.intent_note:
        row["intent_note"] = run.intent_note
    return row


def benchmark(
    utterances: Sequence[Utterance],
    slots: Sequence[EngineSlot],
    hints: Hints,
    terms: Sequence[str],
    composites: Sequence[Composite],
    client: OllamaClient,
    *,
    cache: ResponseCache | None,
    run_dir: Path,
    results_dir: Path = RESULTS_DIR,
    secrets: Sequence[str] = (),
    repetitions: int = 2,
    snap: Callable[[str, OllamaClient], dict] = snapshot,
    log: Callable[[str], None] = print,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[list[dict], dict]:
    """Run everything; returns the aggregate rows and the environment record (no spoken text)."""
    environment: dict = {"warmup": {}, "ollama": {}, "snapshots": [], "contention": []}

    def record(label: str) -> None:
        state = snap(label, client)
        environment["snapshots"].append(state)
        for message in state.get("contention", []):
            log(f"{label}: {message}")
            if message not in environment["contention"]:
                environment["contention"].append(message)

    record("start")
    runs: dict[tuple[str, str], VariantRun] = {}
    for slot in slots:
        log(f"engine {slot.id}")
        base_runs, warm = run_engine(slot, utterances, hints, composites, cache, secrets, repetitions, clock)
        if warm:
            environment["warmup"][slot.id] = warm
        for variant, run in base_runs.items():
            runs[(slot.id, variant)] = run

    record("before cleanup")
    unavailable, ollama_warm = ollama_ready(client)
    environment["ollama"] = {"model": CLEANUP_MODEL, **ollama_warm, "unavailable": unavailable}
    plain = Cleaner(client)
    with_vocabulary = Cleaner(client, vocabulary=tuple(hints.vocabulary()))
    for slot in slots:
        for base_variant, cleaner in (("raw", plain), ("hints", with_vocabulary)):
            variant = f"{base_variant}+cleanup"
            if unavailable:
                run = VariantRun(slot.id, variant)
                run.skip(unavailable)
            else:
                run = run_cleanup(runs[(slot.id, base_variant)], cleaner, variant, secrets, clock)
            runs[(slot.id, variant)] = run
    record("after cleanup")

    record("before intent judge")
    judge = IntentJudge(client)
    for run in runs.values():
        if run.skipped:
            continue
        if unavailable:
            run.intent_note = f"intent not judged: {unavailable}"
        else:
            judge_intent(run, utterances, judge.judge, terms, secrets)
    record("after intent judge")

    rows = []
    for slot in slots:
        for variant in VARIANTS:
            run = runs[(slot.id, variant)]
            write_private(run_dir, run, utterances, results_dir)
            rows.append(result_row(run, utterances, terms))
            log(f"{slot.id} / {variant}: " + ("SKIPPED: " + run.skipped if run.skipped else f"ok, n={len(run.texts)}"))
    return rows, environment


def full_run(config: Path | None, engines_filter: set[str] | None, summary_path: Path) -> int:
    try:
        settings = load_settings(config)
        options = load_engine_options(config if config is not None else DEFAULT_CONFIG)
        keys = load_keys()
        dataset = load_dataset(settings)
    except (SettingsError, OptionsError, EnvFileError, DatasetError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if len(dataset.takes) != settings.expected_takes:
        print(f"error: {len(dataset.takes)} valid takes, {settings.expected_takes} required (see --dry-run)", file=sys.stderr)
        return 1
    secrets = keys.secret_values()
    terms = load_terms()
    hints = build_hints(dataset.names, terms)
    utterances = [Utterance(t.id, t.path.read_bytes(), t.reference, t.project_names) for t in dataset.takes]
    composites = build_composites([(u.id, wav_pcm(u.wav)[0]) for u in utterances])
    save_composites(composites, RESULTS_DIR / "composites")
    slots = create_engines(keys, options)
    if engines_filter:
        unknown = engines_filter - {slot.id for slot in slots}
        if unknown:
            print(f"error: unknown engine id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        slots = [slot for slot in slots if slot.id in engines_filter]
    run_dir = RESULTS_DIR / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    rows, environment = benchmark(
        utterances,
        slots,
        hints,
        terms,
        composites,
        OllamaClient(),
        cache=ResponseCache(RESULTS_DIR / "cache"),
        run_dir=run_dir,
        secrets=secrets,
        log=lambda line: print(redact(line, secrets)),
    )
    environment["composites"] = {
        "count": len(composites),
        "repetitions": 2,
        "duration_s": [c.duration_s for c in composites],
    }
    summary = build_summary(
        rows,
        expected_n=settings.expected_takes,
        n_valid=len(dataset.takes),
        invalid_reasons=dict(Counter(item.reason for item in dataset.invalid)),
        discarded=dataset.discarded,
        recording_origin=dict(dataset.origins),
    )
    summary["environment"] = environment
    (run_dir / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
    try:
        write_summary(summary_path, summary, dataset.reference_texts(), dataset.names)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"summary written ({len(rows)} engine/variant rows); private outputs under bench/results/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.run", description="Quill engine benchmark.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--dry-run", action="store_true", help="load the dataset and print counts only")
    parser.add_argument("--preflight", action="store_true", help="list missing packages, models and keys")
    parser.add_argument("--engines", default=None, help="comma-separated engine ids (default: all)")
    parser.add_argument(
        "--summary", type=Path, default=RESULTS_DIR / "summary.json", help="aggregate summary JSON path"
    )
    args = parser.parse_args(argv)
    if args.preflight:
        from bench.preflight import main as preflight_main

        return preflight_main()
    if args.dry_run:
        return dry_run(args.config)
    engines_filter = {part.strip() for part in args.engines.split(",") if part.strip()} if args.engines else None
    return full_run(args.config, engines_filter, args.summary)


if __name__ == "__main__":
    sys.exit(main())
