"""Phase 2 pipeline evaluation on the real-voice sets.

Usage:
    py -3.12 -m bench.pipeline --set all --dry-run
    .venv\\Scripts\\python -m bench.pipeline --set all --stage raw --summary PATH
    py -3.12 -m bench.pipeline --summary PATH --require complete --require overall
    py -3.12 -m bench.pipeline --summary PATH --write-doc DOC.md
    py -3.12 -m bench.pipeline --summary PATH --check-doc DOC.md

Two sets are measured: ``commands`` (the 44 short takes of the reference
project) and ``dictation`` (the dictation script recorded with bench.record).
Stages are cumulative; this module implements ``raw`` (warm faster-whisper
large-v3 float16 with vocabulary hints) and later stages extend it.

``--dry-run`` prints counts only and needs no GPU. Measurement writes per-take
text only under bench/results/pipeline/<run>/; the summary JSON holds
aggregates only and is refused when a spoken phrase or name would leak into
it. ``--require TARGET`` exits 1 and prints measured-vs-target aggregates when
a target is unmet or a set is incomplete. Nothing spoken is printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.dataset import Dataset, DatasetError, Take, load_dataset
from bench.engines.base import Engine, EngineError, EngineUnavailable, Hints, build_hints
from bench.intent import evaluate
from bench.metrics import (
    ItemResult,
    RemovalCounts,
    corpus_wer,
    percentile_nearest_rank,
    removal_counts,
    term_recall,
    load_terms,
    write_summary,
)
from bench.settings import RESULTS_DIR, Settings, SettingsError, load_settings

SETS = ("commands", "dictation")
# Cumulative stages, in order. Only the ones in IMPLEMENTED_STAGES can run.
STAGE_ORDER = ("raw", "cleanup", "vocabulary", "corrections", "profiles")
IMPLEMENTED_STAGES = ("raw",)
TARGET_NAMES = ("complete", "latency", "cleanup", "vocabulary", "corrections", "overall")
SUMMARY_SCHEMA = 1
DEFAULT_SUMMARY = RESULTS_DIR / "pipeline" / "summary.json"
ENGINE_MODEL = "large-v3"
ENGINE_COMPUTE = "float16"

# Phase 2 targets (docs/PRODUCT.md and the Phase 2 goal). Never lowered here.
MAX_LATENCY_P95_S = 1.5
MAX_LATENCY_AUDIO_S = 15.0
MIN_REMOVAL_RATE = 0.95
MAX_CONTENT_DELETED = 0
MAX_NAME_ERROR = 0.10
MAX_TERM_ERROR = 0.10
MAX_FINAL_WER = 0.10
MIN_INTENT = 0.95

START_MARKER = "<!-- pipeline:summary:start -->"
END_MARKER = "<!-- pipeline:summary:end -->"
BLOCK = re.compile(re.escape(START_MARKER) + r"\n.*?" + re.escape(END_MARKER), re.DOTALL)


# ---------------------------------------------------------------- datasets


def load_sets(settings: Settings, names: Sequence[str]) -> dict[str, tuple[Settings, Dataset]]:
    return {name: (settings.for_set(name), load_dataset(settings.for_set(name))) for name in names}


def dataset_block(settings: Settings, dataset: Dataset) -> dict:
    """Counts and completeness of one set; no text, names or paths."""
    valid = len(dataset.takes)
    if settings.allow_pending:
        complete = valid >= settings.minimum_takes
    else:
        complete = valid == settings.expected_takes
    return {
        "voice": "real",
        "script_rows": dataset.script_rows or valid + len(dataset.invalid),
        "expected": settings.expected_takes,
        "minimum": settings.minimum_takes,
        "n_valid": valid,
        "pending": len(dataset.pending),
        "invalid_reasons": dict(sorted(Counter(item.reason for item in dataset.invalid).items())),
        "discarded_takes": dataset.discarded,
        "recording_origin": dict(sorted(dataset.origins.items())),
        "complete": complete,
    }


def dry_run(settings: Settings, names: Sequence[str], out: Callable[[str], None] = print) -> int:
    code = 0
    for name in names:
        try:
            set_settings = settings.for_set(name)
            block = dataset_block(set_settings, load_dataset(set_settings))
        except (DatasetError, SettingsError) as exc:
            out(f"{name}: error: {exc}")
            code = 1
            continue
        if set_settings.allow_pending:
            out(
                f"{name}: script rows {block['script_rows']}, recorded {block['n_valid']} "
                f"(minimum {block['minimum']}), pending {block['pending']}"
            )
        else:
            out(f"{name}: valid takes {block['n_valid']} (expected {block['expected']})")
        out(f"  discarded takes excluded (*.invalida-*): {block['discarded_takes']}")
        out(f"  invalid takes excluded: {sum(block['invalid_reasons'].values())}")
        for reason, count in block["invalid_reasons"].items():
            out(f"    {reason}: {count}")
        origins = ", ".join(f"{origin}={count}" for origin, count in block["recording_origin"].items())
        out(f"  recording origin: {origins or 'none'}")
        out(f"  complete: {'yes' if block['complete'] else 'no'}")
    return code


# ---------------------------------------------------------------- measurement


@dataclass(frozen=True)
class Sample:
    """One take through one stage. Holds spoken text: never printed or summarized."""

    take: Take = field(repr=False)
    hypothesis: str = field(repr=False)
    seconds: float


def transcribe_set(engine: Engine, hints: Hints, takes: Sequence[Take], clock: Callable[[], float]) -> list[Sample]:
    samples = []
    for take in takes:
        wav = take.path.read_bytes()
        started = clock()
        text = engine.transcribe(wav, hints)
        samples.append(Sample(take, text, clock() - started))
    return samples


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def stage_metrics(samples: Sequence[Sample], terms: Sequence[str], markup: bool, intent: dict[str, bool] | None, intent_note: str | None) -> dict:
    """Aggregate numbers for one set and stage. Contains no spoken text."""
    verbatim = [(s.take.reference, s.hypothesis) for s in samples]
    clean = [(s.take.clean, s.hypothesis) for s in samples]
    term_counts = term_recall(clean, terms)
    name_expected = name_found = 0
    for sample in samples:
        counts = term_recall([(sample.take.clean, sample.hypothesis)], sample.take.project_names)
        name_expected += counts.expected
        name_found += counts.found
    judged = [value for value in (intent or {}).values() if value is not None]
    seconds = [s.seconds for s in samples]
    row = {
        "n": len(samples),
        "wer_verbatim": _round(corpus_wer(verbatim)),
        "wer_clean": _round(corpus_wer(clean)),
        "term_error_rate": _round(term_counts.error_rate),
        "term_occurrences": term_counts.expected,
        "name_error_rate": _round(1 - name_found / name_expected) if name_expected else None,
        "name_occurrences": name_expected,
        "intent_preserved": _round(sum(judged) / len(judged)) if judged else None,
        "intent_judged": len(judged),
        "intent_note": intent_note,
        "filler_spans": None,
        "filler_removal_rate": None,
        "content_words": None,
        "content_deleted": None,
        "transcribe_p50_s": _round(percentile_nearest_rank(seconds, 50)),
        "transcribe_p95_s": _round(percentile_nearest_rank(seconds, 95)),
        "max_audio_s": _round(max((s.take.duration_s for s in samples), default=None)),
    }
    if markup:
        total = RemovalCounts()
        for sample in samples:
            total = total + removal_counts([(seg.kind, seg.text) for seg in sample.take.segments], sample.hypothesis)
        row.update(
            filler_spans=total.spans,
            filler_removal_rate=_round(total.removal_rate),
            content_words=total.content_words,
            content_deleted=total.content_deleted,
        )
    return row


def judge_samples(samples: Sequence[Sample], judge: Callable | None, terms: Sequence[str], unavailable: str | None) -> tuple[dict[str, bool] | None, str | None, list]:
    """Intent verdicts against the clean reference; on any failure none are counted."""
    if not samples:
        return None, None, []
    if judge is None or unavailable:
        return None, f"intent not judged: {unavailable or 'no judge'}", []
    items = [ItemResult(s.take.id, s.take.clean, s.hypothesis, s.take.project_names) for s in samples]
    try:
        rows = evaluate(items, judge, terms)
    except Exception as exc:  # OllamaError or an unexpected client failure
        reason = " ".join(str(exc).split())[:160] if isinstance(exc, (EngineError, OSError)) or type(exc).__name__ == "OllamaError" else type(exc).__name__
        return None, f"intent not judged: {reason}", []
    return {row.id: row.preserved for row in rows}, None, rows


def write_private(run_dir: Path, set_name: str, stage: str, samples: Sequence[Sample], intent_rows: Sequence, results_dir: Path = RESULTS_DIR) -> Path:
    """Per-take text of one set and stage, only under bench/results/."""
    target = Path(run_dir).resolve() / set_name / f"{stage}.json"
    if Path(results_dir).resolve() not in target.parents:
        raise ValueError("per-take outputs may only be written under bench/results/")
    verdicts = {row.id: row for row in intent_rows}
    rows = []
    for sample in samples:
        verdict = verdicts.get(sample.take.id)
        rows.append(
            {
                "id": sample.take.id,
                "verbatim": sample.take.reference,
                "clean": sample.take.clean,
                "hypothesis": sample.hypothesis,
                "seconds": round(sample.seconds, 3),
                "intent_preserved": verdict.preserved if verdict else None,
                "intent_reason": verdict.reason if verdict else None,
            }
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def default_engine() -> Engine:
    from bench.engines.local_whisper import LocalWhisperEngine

    return LocalWhisperEngine(ENGINE_MODEL, compute_type=ENGINE_COMPUTE)


def default_judge() -> tuple[Callable | None, str | None]:
    from bench.cleanup import OllamaClient
    from bench.intent import IntentJudge
    from bench.run import ollama_ready

    client = OllamaClient()
    unavailable, _ = ollama_ready(client)
    return (None if unavailable else IntentJudge(client).judge), unavailable


def measure(
    sets: dict[str, tuple[Settings, Dataset]],
    stage: str,
    engine: Engine,
    judge: Callable | None,
    unavailable: str | None,
    terms: Sequence[str],
    run_dir: Path,
    *,
    results_dir: Path = RESULTS_DIR,
    clock: Callable[[], float] = time.perf_counter,
    log: Callable[[str], None] = print,
) -> dict:
    """Run ``stage`` on every set with the engine kept warm; returns the summary."""
    if stage not in IMPLEMENTED_STAGES:
        raise ValueError(f"stage not implemented yet: {stage}")
    names = sorted({name for _, dataset in sets.values() for name in dataset.names})
    hints = build_hints(names, terms)
    first = next((take for _, dataset in sets.values() for take in dataset.takes), None)
    warm: dict = {}
    started = clock()
    engine.load()
    warm["load_s"] = round(clock() - started, 3)
    if first is not None:
        started = clock()
        engine.transcribe(first.path.read_bytes(), hints)
        warm["warmup_s"] = round(clock() - started, 3)
    summary: dict = {
        "schema": SUMMARY_SCHEMA,
        "kind": "pipeline",
        "engine": {"model": ENGINE_MODEL, "compute_type": ENGINE_COMPUTE, "hints": True, **warm},
        "stages": list(STAGE_ORDER[: STAGE_ORDER.index(stage) + 1]),
        "sets": {},
        "latency": None,
    }
    for set_name, (set_settings, dataset) in sets.items():
        samples = transcribe_set(engine, hints, dataset.takes, clock)
        intent, note, rows = judge_samples(samples, judge, terms, unavailable)
        write_private(run_dir, set_name, stage, samples, rows, results_dir)
        summary["sets"][set_name] = {
            "dataset": dataset_block(set_settings, dataset),
            "stages": {stage: stage_metrics(samples, terms, set_settings.markup, intent, note)},
        }
        log(f"{set_name} / {stage}: n={len(samples)}" + (f" ({note})" if note else ""))
    return summary


# ---------------------------------------------------------------- targets


def _final_stage(set_block: dict) -> tuple[str, dict] | tuple[None, None]:
    stages = set_block.get("stages") or {}
    for stage in reversed(STAGE_ORDER):
        if isinstance(stages.get(stage), dict):
            return stage, stages[stage]
    return None, None


def _pct(value: float | None) -> str:
    return "not measured" if value is None else f"{100 * value:.1f} %"


def check_targets(summary: dict, targets: Sequence[str]) -> list[tuple[bool, str]]:
    """(met, line) for each check of each target; lines hold aggregates only."""
    results: list[tuple[bool, str]] = []
    sets = summary.get("sets") if isinstance(summary.get("sets"), dict) else {}

    def add(met: bool, target: str, text: str) -> None:
        results.append((met, f"{'ok  ' if met else 'FAIL'} {target}: {text}"))

    def per_set(target: str, need_stage: str | None = None):
        for set_name in SETS:
            block = sets.get(set_name)
            if not isinstance(block, dict):
                add(False, target, f"{set_name} not measured")
                continue
            data = block.get("dataset", {})
            stage, row = _final_stage(block)
            if not data.get("complete") or row is None or row.get("n") != data.get("n_valid"):
                measured = row.get("n") if row else 0
                add(False, target, f"{set_name} incomplete: n {measured}, valid {data.get('n_valid')}, minimum {data.get('minimum')}")
                continue
            if need_stage and need_stage not in block.get("stages", {}):
                add(False, target, f"{set_name}: {need_stage} stage not measured (final stage {stage})")
                continue
            yield set_name, stage, row

    for target in targets:
        if target not in TARGET_NAMES:
            add(False, target, "unknown target")
        elif target == "complete":
            for set_name, stage, row in per_set(target):
                add(True, target, f"{set_name} n {row['n']} ({stage})")
        elif target == "latency":
            latency = summary.get("latency")
            p95 = latency.get("p95_s") if isinstance(latency, dict) else None
            longest = latency.get("max_audio_s") if isinstance(latency, dict) else None
            if p95 is None:
                add(False, target, "key-release latency not measured")
            else:
                add(p95 <= MAX_LATENCY_P95_S, target, f"p95 {p95:.2f} s (target <= {MAX_LATENCY_P95_S:.1f} s, utterances up to {longest} s)")
        elif target == "cleanup":
            for set_name, stage, row in per_set(target, "cleanup"):
                if set_name != "dictation":
                    continue
                rate, deleted = row.get("filler_removal_rate"), row.get("content_deleted")
                add(rate is not None and rate >= MIN_REMOVAL_RATE, target, f"dictation filler/repetition removal {_pct(rate)} (target >= {100 * MIN_REMOVAL_RATE:.0f} %)")
                add(deleted is not None and deleted <= MAX_CONTENT_DELETED, target, f"dictation content words deleted {deleted} (target {MAX_CONTENT_DELETED})")
        elif target == "vocabulary":
            for set_name, stage, row in per_set(target, "vocabulary"):
                for key, label, limit in (("name_error_rate", "project-name error", MAX_NAME_ERROR), ("term_error_rate", "English-term error", MAX_TERM_ERROR)):
                    value = row.get(key)
                    add(value is not None and value <= limit, target, f"{set_name} {label} {_pct(value)} (target <= {100 * limit:.0f} %)")
        elif target == "corrections":
            for set_name, stage, row in per_set(target, "corrections"):
                fixed, new = row.get("recurrences_fixed_rate"), row.get("new_errors")
                add(fixed is not None and fixed >= 1.0, target, f"{set_name} learned recurrences fixed {_pct(fixed)} (target 100 %)")
                add(new is not None and new == 0, target, f"{set_name} new word errors {new} (target 0)")
        elif target == "overall":
            for set_name, stage, row in per_set(target):
                wer, intent = row.get("wer_clean"), row.get("intent_preserved")
                add(wer is not None and wer <= MAX_FINAL_WER, target, f"{set_name} final-text WER {_pct(wer)} ({stage}; target <= {100 * MAX_FINAL_WER:.0f} %)")
                add(intent is not None and intent >= MIN_INTENT, target, f"{set_name} intent preserved {_pct(intent)} ({stage}; target >= {100 * MIN_INTENT:.0f} %)")
    return results


# ---------------------------------------------------------------- document block

HEADER = (
    "Conjunto", "Etapa", "n", "WER literal", "WER limpo", "Erro termos EN", "Erro nomes",
    "Intenção preservada", "Hesitações removidas", "Palavras apagadas", "p95 transcrição (s)",
)
SET_LABELS = {"commands": "comandos", "dictation": "ditado"}
EMPTY = "—"


def _pt_percent(value: float | None) -> str:
    return EMPTY if value is None else f"{100 * value:.1f}".replace(".", ",") + " %"


def _pt_seconds(value: float | None) -> str:
    return EMPTY if value is None else f"{value:.2f}".replace(".", ",")


def render_block(summary: dict) -> str:
    lines = [START_MARKER, "| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    for set_name in SETS:
        block = (summary.get("sets") or {}).get(set_name)
        if not isinstance(block, dict):
            continue
        for stage in STAGE_ORDER:
            row = (block.get("stages") or {}).get(stage)
            if not isinstance(row, dict):
                continue
            cells = (
                SET_LABELS[set_name], stage, str(row.get("n")),
                _pt_percent(row.get("wer_verbatim")), _pt_percent(row.get("wer_clean")),
                _pt_percent(row.get("term_error_rate")), _pt_percent(row.get("name_error_rate")),
                _pt_percent(row.get("intent_preserved")), _pt_percent(row.get("filler_removal_rate")),
                EMPTY if row.get("content_deleted") is None else str(row["content_deleted"]),
                _pt_seconds(row.get("transcribe_p95_s")),
            )
            lines.append("| " + " | ".join(cells) + " |")
    latency = summary.get("latency")
    if isinstance(latency, dict) and latency.get("p95_s") is not None:
        lines.append("")
        lines.append(f"Latência da aplicação (largar a tecla até ao texto), p95: {_pt_seconds(latency['p95_s'])} s")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def write_doc(document: str, summary: dict) -> str:
    document = document.replace("\r\n", "\n")
    block = render_block(summary).rstrip("\n")
    if BLOCK.search(document):
        return BLOCK.sub(lambda _: block, document, count=1)
    return document.rstrip("\n") + "\n\n" + block + "\n"


def check_doc(document: str, summary: dict) -> list[str]:
    match = BLOCK.search(document.replace("\r\n", "\n"))
    if match is None:
        return ["pipeline summary block not found"]
    if match.group(0) != render_block(summary).rstrip("\n"):
        return ["pipeline summary block differs from the summary (run --write-doc)"]
    return []


# ---------------------------------------------------------------- CLI


def _read_summary(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError("summary not found (run a stage first)") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SettingsError("summary is not valid JSON") from None
    if not isinstance(data, dict) or data.get("kind") != "pipeline":
        raise SettingsError("not a pipeline summary")
    return data


def main(
    argv: list[str] | None = None,
    *,
    engine_factory: Callable[[], Engine] = default_engine,
    judge_factory: Callable[[], tuple[Callable | None, str | None]] = default_judge,
    results_dir: Path = RESULTS_DIR,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(prog="bench.pipeline", description="Phase 2 pipeline evaluation on the real-voice sets.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--set", choices=(*SETS, "all"), default="all", help="dataset set (default all)")
    parser.add_argument("--dry-run", action="store_true", help="dataset counts only; no GPU")
    parser.add_argument("--stage", choices=IMPLEMENTED_STAGES, default=None, help="measure up to this stage")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY, help="aggregate summary JSON")
    parser.add_argument("--require", action="append", choices=TARGET_NAMES, default=[], help="exit 1 unless met")
    parser.add_argument("--check-doc", type=Path, default=None, help="exit 1 when DOC's block differs from the summary")
    parser.add_argument("--write-doc", type=Path, default=None, help="insert or replace the block in DOC")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    names = SETS if args.set == "all" else (args.set,)

    if args.dry_run:
        try:
            settings = load_settings(args.config)
        except SettingsError as exc:
            out(f"error: {exc}")
            return 2
        return dry_run(settings, names, out)
    if not (args.stage or args.require or args.check_doc or args.write_doc):
        parser.error("nothing to do: give --dry-run, --stage, --require, --check-doc or --write-doc")

    try:
        if args.stage:
            settings = load_settings(args.config)
            sets = load_sets(settings, names)
            engine = engine_factory()
            judge, unavailable = judge_factory()
            run_dir = Path(results_dir) / "pipeline" / time.strftime("%Y%m%d-%H%M%S")
            try:
                summary = measure(sets, args.stage, engine, judge, unavailable, load_terms(), run_dir, results_dir=results_dir, log=out)
            finally:
                engine.close()
            texts = [text for _, dataset in sets.values() for text in dataset.reference_texts()]
            spoken_names = [name for _, dataset in sets.values() for name in dataset.names]
            write_summary(args.summary, summary, texts, spoken_names)
            out(f"summary written ({', '.join(summary['sets'])}); per-take outputs under bench/results/")
        else:
            summary = _read_summary(args.summary)
    except (SettingsError, DatasetError) as exc:
        out(f"error: {exc}")
        return 2
    except (EngineUnavailable, EngineError) as exc:
        out(f"error: {' '.join(str(exc).split())[:200]}")
        return 2
    except ValueError as exc:
        out(f"error: {exc}")
        return 1

    code = 0
    if args.write_doc:
        document = args.write_doc.read_text(encoding="utf-8") if args.write_doc.is_file() else ""
        args.write_doc.write_text(write_doc(document, summary), encoding="utf-8", newline="\n")
        out("document block written")
    if args.check_doc:
        if not args.check_doc.is_file():
            out("FAIL: document not found")
            code = 1
        else:
            problems = check_doc(args.check_doc.read_text(encoding="utf-8"), summary)
            for problem in problems:
                out(f"FAIL: {problem}")
            code = 1 if problems else code
    if args.require:
        results = check_targets(summary, args.require)
        for _, line in results:
            out(line)
        if not all(met for met, _ in results):
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
