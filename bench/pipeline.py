"""Phase 2 pipeline evaluation on the real-voice sets.

Usage:
    py -3.12 -m bench.pipeline --set all --dry-run
    .venv\\Scripts\\python -m bench.pipeline --set all --stage raw --summary PATH
    .venv\\Scripts\\python -m bench.pipeline --set all --stage streamed --summary PATH
    py -3.12 -m bench.pipeline --summary PATH --require complete --require overall
    py -3.12 -m bench.pipeline --summary PATH --write-doc DOC.md
    py -3.12 -m bench.pipeline --summary PATH --check-doc DOC.md
    py -3.12 -m bench.pipeline --compare-cleanup bench/results/pipeline/<run>
    .venv\\Scripts\\python -m bench.pipeline --set all --stage vocabulary --summary PATH [--vocabulary FILE]
    .venv\\Scripts\\python -m bench.pipeline --set all --stage corrections --summary PATH [--vocabulary FILE]
    .venv\\Scripts\\python -m bench.pipeline --set all --stage profiles --summary PATH [--vocabulary FILE]
    .venv\\Scripts\\python -m bench.pipeline --set all --stage rewrite --summary PATH [--vocabulary FILE]

Two sets are measured: ``commands`` (the 44 short takes of the reference
project) and ``dictation`` (the dictation script recorded with bench.record).
Stages are cumulative. ``raw`` transcribes each whole take once (warm
faster-whisper large-v3 float16 with vocabulary hints): the Phase 2 baseline.
``streamed`` is the product's source text: the take replayed through
``quill.streaming`` with the product's engine model and its tuning
(large-v3-turbo by default, Sponsor decision 2026-09-29) on the
deterministic audio-time schedule (``bench.streaming``), so its final text is
reproducible. Later stages start from the ``streamed`` text. ``cleanup``
applies the product's deterministic cleanup rules (``quill.cleanup``) to the
``streamed`` text and adds ``content_deleted_by_cleanup``: content words that
the streamed text had right and the cleaned text lost (deleted or changed).
``vocabulary`` applies the product's post-recognition matcher
(``quill.vocabulary``) to the cleaned text, with the personal vocabulary
(``local/vocabulary.toml``, or ``--vocabulary FILE``) merged with the resolved
project names and ``bench/terms_en.txt``. When the personal vocabulary changes
the Whisper hints (``quill.vocabulary.hint_list`` priority order), the takes
are streamed and cleaned again with the product hints before matching, so the
row shows the whole effect of the vocabulary; the earlier stages keep the
baseline hints (resolved names sorted, then the generic terms).
``corrections`` is an online simulation of learning from corrections on the
``vocabulary`` text, in the fixed take order of each set: each take first
gets the replacements active so far (``quill.corrections``, the product rule:
active once seen in two distinct dictations, conflicts never applied), then
the simulated user corrects the typed text to the clean reference and the
product derives and counts the replacements from that correction. A learned
recurrence is an error in a take whose replacement was already active; the
row adds ``recurrences_fixed_rate`` (target 100 %) and ``new_errors``
(reference words that the take had right and the applied replacements made
wrong; target 0).
``profiles`` applies the active-window profile rules (``quill.profiles``) to
the ``corrections`` text: each dictation take gets the profile of its script
``estilo`` column (claude-code, vscode, whatsapp or email) and every take
without one (the commands set) the ``default`` profile. The rules change
punctuation and sentence-start capitals only; the row adds
``profile_changes`` (takes whose text changed) and ``profile_takes`` (takes
per profile). It is the final text of the application while the automatic
rewrite is off.
``rewrite`` applies the product's automatic rewrite of long dictations
(``quill.autorewrite``, the local Ollama model of quill's config) to the
``profiles`` text: a take over the ``[autorewrite]`` thresholds of
quill.example.toml (audio seconds or words) is rewritten with its profile,
the product's words to keep (personal vocabulary, resolved names, generic
terms) and, in the editor profiles (claude-code, vscode), the take's project
standing in for the project read from the window title; every other take is
unchanged. The measurement ignores the product timeout, so the counts do not
depend on the machine's load; the calls slower than it are reported with the
timings. The row adds the takes rewritten, unchanged and refused (with the
guard's reasons), ``content_deleted_by_rewrite`` (content words that the
``profiles`` text had right and the rewrite lost; target 0),
``content_fixed_by_rewrite`` and the clean WER of the long takes before and
after. The rewrite is the final text only when quill.example.toml turns it
on (``summary["rewrite"]["product_default"]``).

``--compare-cleanup RUN`` needs no GPU: it reads the streamed outputs saved by
an earlier run and compares the rules with the local qwen3:8b cleanup
(quality and time per take); its outputs stay under bench/results/cleanup/.

``--dry-run`` prints counts only and needs no GPU. Measurement writes per-take
text only under bench/results/pipeline/<run>/; the summary JSON holds
aggregates only and is refused when a spoken phrase or name would leak into
it. ``--add-command-mode RUN_SUMMARY`` (the aggregate summary of a
``bench.rewrite`` run) and ``--add-selftests DIR`` (the newest typing,
triggers and indicator results of the Sponsor's manual desktop self-tests,
``local/selftest/``) merge whitelisted counts into ``--summary``: one-off
manual steps, never a check; wall-clock timings are left out. Trigger
results of version 1 and 2 are accepted; version 2 adds per-trigger press
durations and hook callback delays as bucket counts (the millisecond p95 and
maximum stay in the local file). A later
``--stage`` run writes a fresh summary, so the merges are repeated after it.
``--require TARGET`` exits 1 and prints measured-vs-target aggregates when
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
    matched_content,
    percentile_nearest_rank,
    removal_counts,
    term_recall,
    load_terms,
    write_summary,
)
from bench.normalize import normalize_words
from bench.settings import RESULTS_DIR, Settings, SettingsError, load_settings
from quill import autorewrite
from quill.autorewrite import AutoRewriter, is_long
from quill.config import AutoRewrite
from quill.cleanup import Cleanup, CleanupResult, clean_text
from quill.corrections import ACTIVE, Corrections, Replacement, derive, phrase_key
from quill.profiles import CLAUDE_CODE, DEFAULT as DEFAULT_PROFILE, apply_profile
from quill.vocabulary import EMPTY as NO_VOCABULARY, LOCAL_VOCABULARY, Matcher, Vocabulary, VocabularyError, hint_list, hints_within_limit, load_vocabulary, whisper_hints
from quill.whisper import DEFAULT_MODEL

SETS = ("commands", "dictation")
# Cumulative stages, in order. Only the ones in IMPLEMENTED_STAGES can run.
STAGE_ORDER = ("raw", "streamed", "cleanup", "vocabulary", "corrections", "profiles", "rewrite")
IMPLEMENTED_STAGES = STAGE_ORDER
TARGET_NAMES = ("complete", "latency", "cleanup", "vocabulary", "corrections", "overall", "desktop", "rewrite")
SUMMARY_SCHEMA = 1
DEFAULT_SUMMARY = RESULTS_DIR / "pipeline" / "summary.json"
# The baseline engine of the raw stage; the streamed stage uses the product's.
ENGINE_MODEL = "large-v3"
ENGINE_COMPUTE = "float16"
STREAM_MODEL = DEFAULT_MODEL

# Phase 2 targets (docs/PRODUCT.md and the Phase 2 goal). Never lowered here.
# Latency: release of the trigger to the final text (Sponsor target).
MAX_LATENCY_P95_S = 0.5
MAX_LATENCY_AUDIO_S = 15.0
MIN_REMOVAL_RATE = 0.95
MAX_CONTENT_DELETED = 0
MAX_NAME_ERROR = 0.10
MAX_TERM_ERROR = 0.10
# Sponsor decision 2026-09-29: the name and term targets gate the dictation
# set (how the product is used); the short commands set is reported as an
# indicator, never as a gate.
VOCABULARY_GATED_SETS = ("dictation",)
MAX_FINAL_WER = 0.10
MIN_INTENT = 0.95
# Automatic rewrite of long dictations (Phase 3): it becomes the default only
# with 0 content words deleted, a clean WER no worse than the profiles stage
# and a warm p95 of at most 3 s (a timing: under bench/results/ only).
MAX_REWRITE_P95_S = 3.0
# In these profiles the window title names the open project; the take's
# project stands in for it.
EDITOR_PROFILES = (CLAUDE_CODE, "vscode")
# The measurement's own timeout: the counts must not depend on the machine's load.
REWRITE_BENCH_TIMEOUT_S = 120.0

# Command mode and the manual desktop self-tests: deterministic counts only.
COMMAND_MODE_KEYS = (
    "takes", "instruction_wer", "instruction_exact", "valid", "reasons", "checks", "checks_applicable",
    "checks_passed", "judged", "judge_yes", "correct", "by_kind", "second_attempts", "second_attempt_reasons",
)
SELFTESTS = ("typing", "triggers", "indicator")
TYPING_KEYS = (
    "targets", "targets_passed", "cases", "cases_passed", "characters_expected", "characters_typed",
    "lost", "extra", "changed", "clipboard_changed_targets",
)
TRIGGER_FIELDS = {
    "signals": ("action", "signal", "reason"), "ignored": ("trigger", "reason"),
    "inputs": ("trigger", "event"), "clicks": ("action", "reason"),
}
TRIGGER_VERSIONS = (1, 2)
# Version 2: per trigger input, press durations and hook callback delays as bucket counts.
TRIGGER_BUCKET_FIELDS = {"holds": ("trigger", "bucket"), "event_ages": ("trigger", "bucket")}
INDICATOR_KEYS = ("position", "states_shown", "foreground_samples", "foreground_was_indicator")

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


def stage_metrics(samples: Sequence[Sample], terms: Sequence[str], markup: bool, intent: dict[str, bool] | None,
                  intent_note: str | None, before: Sequence[Sample] | None = None) -> dict:
    """Aggregate numbers for one set and stage. Contains no spoken text.

    ``before`` is the previous stage of a cleanup stage, in the same take
    order: with markup, the content words that ``before`` had right and the
    cleaned text no longer has (deleted or changed) are counted as
    ``content_deleted_by_cleanup``.
    """
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
        "content_deleted_by_cleanup": None,
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
        if before is not None:
            row["content_deleted_by_cleanup"] = sum(
                len(_matched(previous) - _matched(sample)) for sample, previous in zip(samples, before, strict=True)
            )
    return row


def _matched(sample: Sample) -> frozenset[int]:
    return matched_content([(seg.kind, seg.text) for seg in sample.take.segments], sample.hypothesis)


def clean_samples(samples: Sequence[Sample], keep: Sequence[str], clock: Callable[[], float]) -> list[Sample]:
    """The cleanup stage: the product's rules on each previous-stage text."""
    cleaned = []
    for sample in samples:
        started = clock()
        text = clean_text(sample.hypothesis, keep)
        cleaned.append(Sample(sample.take, text, sample.seconds + clock() - started))
    return cleaned


def match_samples(samples: Sequence[Sample], matcher: Matcher, clock: Callable[[], float]) -> tuple[list[Sample], int]:
    """The vocabulary stage: the product matcher on each previous-stage text; also the spans changed."""
    matched, changes = [], 0
    for sample in samples:
        started = clock()
        changes += len(matcher.replacements(sample.hypothesis))
        text = matcher.apply(sample.hypothesis)
        matched.append(Sample(sample.take, text, sample.seconds + clock() - started))
    return matched, changes


def correct_positions(reference: Sequence[str], hypothesis: Sequence[str]) -> frozenset[int]:
    """Reference word indexes aligned to an equal hypothesis word (one minimum-cost alignment)."""
    n, m = len(reference), len(hypothesis)
    cost = [[i + j if i == 0 or j == 0 else 0 for j in range(m + 1)] for i in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = min(cost[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]), cost[i - 1][j] + 1, cost[i][j - 1] + 1)
    found: set[int] = set()
    i, j = n, m
    while i > 0 and j > 0:
        if reference[i - 1] == hypothesis[j - 1] and cost[i][j] == cost[i - 1][j - 1]:
            found.add(i - 1)
            i, j = i - 1, j - 1
        elif cost[i][j] == cost[i - 1][j - 1] + 1:
            i, j = i - 1, j - 1
        elif cost[i][j] == cost[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return frozenset(found)


def _measurable(replacements: Sequence[Replacement]) -> list[Replacement]:
    """Replacements the WER sees: the normalizer's equivalences (case, numbers) are not errors."""
    return [r for r in replacements if normalize_words(r.source) != normalize_words(r.target)]


def error_pairs(hypothesis: str, reference: str) -> Counter:
    """Every word error of ``hypothesis`` as a (source, target) key, without the rewrite guard."""
    found = _measurable(derive(hypothesis, reference, case_sensitive=False, strict=False))
    return Counter((r.key, phrase_key(r.target)) for r in found)


def correct_samples(samples: Sequence[Sample], clock: Callable[[], float]) -> tuple[list[Sample], dict]:
    """The corrections stage: apply what is active, then learn from the take's correction.

    The simulated user corrects every typed take to its clean reference; the
    product rule (``quill.corrections``) decides what is derived, counted and
    activated. Returns the corrected samples and aggregate counts only.
    """
    corrections = Corrections.empty(0.0)
    corrected: list[Sample] = []
    recurrences = fixed = pending = new_errors = applications = learning_takes = 0
    for index, sample in enumerate(samples):
        reference = sample.take.clean
        statuses = corrections.statuses()
        active = {(e.key, e.target_key) for i, e in enumerate(corrections.entries) if statuses[i] == ACTIVE}
        known = {(e.key, e.target_key) for e in corrections.entries}
        started = clock()
        text, applied = corrections.apply(sample.hypothesis)
        corrected.append(Sample(sample.take, text, sample.seconds + clock() - started))
        applications += applied
        before, after = error_pairs(sample.hypothesis, reference), error_pairs(text, reference)
        for pair, count in before.items():
            if pair in active:
                recurrences += count
                fixed += max(0, count - after[pair])
            elif pair in known:
                pending += count
        words = normalize_words(reference)
        new_errors += len(correct_positions(words, normalize_words(sample.hypothesis)) - correct_positions(words, normalize_words(text)))
        replacements = _measurable(derive(text, reference, case_sensitive=False))
        learning_takes += bool(replacements)
        corrections.learn(f"take-{index}", replacements, float(index))
    counts = corrections.counts()
    info = {
        "recurrences": recurrences,
        "recurrences_fixed": fixed,
        "recurrences_fixed_rate": _round(fixed / recurrences) if recurrences else None,
        "recurrences_not_yet_active": pending,
        "new_errors": new_errors,
        "corrections_applied": applications,
        "takes_with_corrections": learning_takes,
        "learned_active": counts["active"],
        "learned_pending": counts["pending"],
        "learned_conflicts": counts["conflict"],
    }
    return corrected, info


def profile_samples(samples: Sequence[Sample], keep: Sequence[str], clock: Callable[[], float]) -> tuple[list[Sample], dict]:
    """The profiles stage: each take's script ``estilo`` profile (``default`` without one)."""
    shaped: list[Sample] = []
    changed = 0
    takes: Counter = Counter()
    for sample in samples:
        profile = sample.take.style or DEFAULT_PROFILE
        started = clock()
        text = apply_profile(sample.hypothesis, profile, keep)
        shaped.append(Sample(sample.take, text, sample.seconds + clock() - started))
        changed += text != sample.hypothesis
        takes[profile] += 1
    return shaped, {"profile_changes": changed, "profile_takes": dict(sorted(takes.items()))}


def rewrite_samples(samples: Sequence[Sample], rewriter: AutoRewriter, keep: Sequence[str], markup: bool,
                    product_timeout_s: float | None = None) -> tuple[list[Sample], dict]:
    """The rewrite stage: the product's automatic rewrite on each long ``profiles`` text.

    A take is long by its audio length or its word count, as in the product.
    Returns the samples and aggregate counts only; ``rewrite_p50_s``,
    ``rewrite_p95_s`` (the model calls of long takes) and
    ``rewrite_over_timeout`` (calls slower than ``product_timeout_s``) are
    wall-clock timings that ``split_timings`` moves out of the summary.
    """
    rewritten: list[Sample] = []
    outcomes: Counter = Counter()
    refusals: Counter = Counter()
    seconds: list[float] = []
    changes = 0
    long_pairs: list[tuple[str, str, str]] = []  # clean reference, before, after
    for sample in samples:
        take = sample.take
        profile = take.style or DEFAULT_PROFILE
        project = take.project_names[0] if profile in EDITOR_PROFILES and len(take.project_names) == 1 else ""
        result = rewriter.rewrite(sample.hypothesis, audio_s=take.duration_s, profile=profile, keep=keep,
                                  project=project)
        rewritten.append(Sample(take, result.text, sample.seconds + result.seconds))
        if result.called:
            seconds.append(result.seconds)
        if is_long(sample.hypothesis, take.duration_s, rewriter.settings):
            outcomes[result.reason] += 1
            long_pairs.append((take.clean, sample.hypothesis, result.text))
            changes += result.changes if result.rewritten else 0
            if result.reason == autorewrite.REFUSED:
                refusals[result.detail] += 1
    info = {
        "long_takes": len(long_pairs),
        "rewritten": outcomes[autorewrite.REWRITTEN],
        "unchanged": outcomes[autorewrite.UNCHANGED],
        "refused": outcomes[autorewrite.REFUSED],
        "refusal_reasons": dict(sorted(refusals.items())),
        "failed": outcomes[autorewrite.FAILED],
        "timeouts": outcomes[autorewrite.TIMEOUT],
        "word_changes": changes,
        "content_deleted_by_rewrite": None,
        "content_fixed_by_rewrite": None,
        "wer_clean_long_before": _round(corpus_wer([(clean, before) for clean, before, _ in long_pairs])) if long_pairs else None,
        "wer_clean_long": _round(corpus_wer([(clean, after) for clean, _, after in long_pairs])) if long_pairs else None,
        "rewrite_p50_s": _round(percentile_nearest_rank(seconds, 50)),
        "rewrite_p95_s": _round(percentile_nearest_rank(seconds, 95)),
        "rewrite_over_timeout": None if product_timeout_s is None else sum(s > product_timeout_s for s in seconds),
    }
    if markup:
        info["content_deleted_by_rewrite"] = sum(
            len(_matched(previous) - _matched(sample)) for sample, previous in zip(rewritten, samples, strict=True))
        info["content_fixed_by_rewrite"] = sum(
            len(_matched(sample) - _matched(previous)) for sample, previous in zip(rewritten, samples, strict=True))
    return rewritten, info


def product_hints(vocabulary: Vocabulary, names: Sequence[str], terms: Sequence[str]) -> Hints:
    """The product's hints: personal names, resolved names, generic terms, personal terms, within the cap."""
    return Hints(names=tuple(whisper_hints(vocabulary, sorted(names), terms)))


def saved_samples(run_dir: Path, set_name: str, stage: str, dataset: Dataset) -> list[Sample]:
    """A stage's texts saved by an earlier run, matched to the dataset's takes by id."""
    path = Path(run_dir) / set_name / f"{stage}.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError(f"{set_name}: no saved {stage} outputs in the run folder") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SettingsError(f"{set_name}: saved {stage} outputs are not valid JSON") from None
    texts = {row.get("id"): row.get("hypothesis") for row in rows if isinstance(row, dict)}
    missing = [take.id for take in dataset.takes if not isinstance(texts.get(take.id), str)]
    if missing:
        raise SettingsError(f"{set_name}: {len(missing)} takes missing from the saved {stage} outputs")
    return [Sample(take, texts[take.id], 0.0) for take in dataset.takes]


def compare_cleanup(
    sets: dict[str, tuple[Settings, Dataset]],
    run_dir: Path,
    cleaners: dict[str, Callable[[str], CleanupResult]],
    judge: Callable | None,
    unavailable: str | None,
    terms: Sequence[str],
    *,
    stage: str = "streamed",
) -> tuple[dict, dict]:
    """Each cleaner on the saved ``stage`` texts: (aggregates and times, per-take texts).

    The aggregates hold no spoken text; the per-take texts stay under
    bench/results/. A cleaner's time is only its own (the LLM call).
    """
    report: dict = {"source_stage": stage, "sets": {}}
    texts: dict = {}
    for set_name, (set_settings, dataset) in sets.items():
        before = saved_samples(run_dir, set_name, stage, dataset)
        report["sets"][set_name] = {}
        for mode, cleaner in cleaners.items():
            samples, fallbacks, seconds = [], Counter(), []
            for sample in before:
                result = cleaner(sample.hypothesis)
                samples.append(Sample(sample.take, result.text, result.llm_s or 0.0))
                if result.fallback:
                    fallbacks[result.fallback] += 1
                if result.llm_s is not None:
                    seconds.append(result.llm_s)
            intent, note, rows = judge_samples(samples, judge, terms, unavailable)
            row = stage_metrics(samples, terms, set_settings.markup, intent, note, before)
            for key in ("transcribe_p50_s", "transcribe_p95_s"):
                row.pop(key, None)
            row["fallbacks"] = dict(sorted(fallbacks.items()))
            row["cleanup_p50_s"] = _round(percentile_nearest_rank(seconds, 50))
            row["cleanup_p95_s"] = _round(percentile_nearest_rank(seconds, 95))
            report["sets"][set_name][mode] = row
            verdicts = {r.id: r for r in rows}
            texts.setdefault(set_name, {})[mode] = [
                {"id": s.take.id, "before": b.hypothesis, "after": s.hypothesis, "clean": s.take.clean,
                 "intent_preserved": verdicts[s.take.id].preserved if s.take.id in verdicts else None}
                for s, b in zip(samples, before, strict=True)
            ]
    return report, texts


def default_cleaners(keep: Sequence[str]) -> tuple[dict[str, Callable[[str], CleanupResult]], str | None]:
    """The product's rules and the local qwen3:8b cleanup (skipped when Ollama is not ready)."""
    from bench.cleanup import OllamaClient
    from bench.run import ollama_ready

    cleaners: dict = {"rules": Cleanup("rules", keep=keep)}
    client = OllamaClient()
    unavailable, _ = ollama_ready(client)
    if unavailable is None:
        cleaners["llm"] = Cleanup("llm", client=client, keep=keep)
    return cleaners, unavailable


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


# Streams every take of a set and returns (text, seconds) per take, in order.
Streamer = Callable[[Sequence[Take], Hints], list[tuple[str, float]]]


def default_streamer(engine: Engine) -> tuple[Streamer, dict]:
    """The product streaming path (its engine model and tuning), and those settings.

    The model is the raw engine's when they are the same, otherwise a second
    local model loaded once; ``stream.close`` releases it.
    """
    from dataclasses import asdict

    from bench.streaming import stream_takes
    from quill.streaming import options_for
    from quill.whisper import Whisper

    options = options_for(STREAM_MODEL)
    shared = getattr(engine, "model", None) == STREAM_MODEL
    model = engine.whisper if shared else Whisper(STREAM_MODEL, compute_type=ENGINE_COMPUTE)

    def stream(takes: Sequence[Take], hints: Hints) -> list[tuple[str, float]]:
        return stream_takes(model, takes, hints.vocabulary(), options)

    stream.close = (lambda: None) if shared else model.close
    return stream, {"model": STREAM_MODEL, **asdict(options)}


def default_judge() -> tuple[Callable | None, str | None]:
    from bench.cleanup import OllamaClient
    from bench.intent import IntentJudge
    from bench.run import ollama_ready

    client = OllamaClient()
    unavailable, _ = ollama_ready(client)
    return (None if unavailable else IntentJudge(client).judge), unavailable


def rewrite_info(settings: AutoRewrite, model: str) -> dict:
    """The product's automatic-rewrite settings, as recorded in the summary."""
    return {"model": model, "min_audio_s": settings.min_audio_s, "min_words": settings.min_words,
            "timeout_s": settings.timeout_s, "product_default": settings.enabled}


def default_rewriter() -> tuple[AutoRewriter, dict]:
    """The product's automatic rewrite and its settings, warmed up.

    The Ollama URL and model come from quill's config, the thresholds and
    whether it is on by default from quill.example.toml. It is measured on
    whatever the default, with ``REWRITE_BENCH_TIMEOUT_S``. The local model
    is only listed and asked one short warm-up turn (the p95 is of a warm
    model, as in a working session); nothing is pulled, loaded on purpose or
    unloaded. Raises SettingsError when it cannot answer.
    """
    from dataclasses import replace

    from quill.config import load_config
    from quill.ollama import OllamaClient, OllamaError

    config = load_config()
    product = load_config(local=None).autorewrite
    client = OllamaClient(config.ollama_url, timeout_s=REWRITE_BENCH_TIMEOUT_S)
    try:
        if config.ollama_model not in client.installed():
            raise SettingsError(f"rewrite stage: {config.ollama_model} is not installed in Ollama")
        client.chat(config.ollama_model, "Reply with ok.", "ok", max_tokens=8)
    except OllamaError as exc:
        raise SettingsError(f"rewrite stage: Ollama unavailable: {' '.join(str(exc).split())[:160]}") from None
    settings = replace(product, enabled=True, timeout_s=REWRITE_BENCH_TIMEOUT_S)
    return AutoRewriter(client, config.ollama_model, settings), rewrite_info(product, config.ollama_model)


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
    streamer: Streamer | None = None,
    stream_options: dict | None = None,
    vocabulary: Vocabulary = NO_VOCABULARY,
    rewriter: AutoRewriter | None = None,
    rewrite_settings: dict | None = None,
) -> dict:
    """Run the stages up to ``stage`` on every set with the engine kept warm; returns the summary.

    The rewrite stage needs ``rewriter`` (enabled, with the measurement's
    timeout) and ``rewrite_settings`` (``rewrite_info`` of the product's).
    """
    if stage not in IMPLEMENTED_STAGES:
        raise ValueError(f"stage not implemented yet: {stage}")
    stages = STAGE_ORDER[: STAGE_ORDER.index(stage) + 1]
    if "streamed" in stages and streamer is None:
        raise ValueError("the streamed stage needs a streamer")
    if "rewrite" in stages and (rewriter is None or rewrite_settings is None or not rewriter.settings.enabled):
        raise ValueError("the rewrite stage needs an enabled rewriter and the product's rewrite settings")
    names = sorted({name for _, dataset in sets.values() for name in dataset.names})
    hints = build_hints(names, terms)
    personal = product_hints(vocabulary, names, terms)
    restream = personal.vocabulary() != hints.vocabulary()
    matcher = Matcher(vocabulary, sorted(names), terms)
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
        "stages": list(stages),
        "sets": {},
        "latency": None,
    }
    if "streamed" in stages and stream_options is not None:
        summary["engine"]["streaming"] = dict(stream_options)
    if "vocabulary" in stages:
        kept, dropped = hints_within_limit(hint_list(vocabulary, sorted(names), terms))
        summary["vocabulary"] = {**vocabulary.counts(), "hints": kept, "hints_dropped": dropped, "restreamed": restream}
    if "rewrite" in stages:
        summary["rewrite"] = dict(rewrite_settings)
    # The product keeps every vocabulary word through the rewrite, not only the ones within the hint cap.
    lexicon = hint_list(vocabulary, sorted(names), terms)
    for set_name, (set_settings, dataset) in sets.items():
        block: dict = {"dataset": dataset_block(set_settings, dataset), "stages": {}}
        summary["sets"][set_name] = block
        samples: list[Sample] = []
        for current in stages:
            before = None
            if current == "raw":
                samples = transcribe_set(engine, hints, dataset.takes, clock)
            elif current == "streamed":  # the product source for every later stage
                streamed = streamer(dataset.takes, hints)
                samples = [Sample(take, text, seconds) for take, (text, seconds) in zip(dataset.takes, streamed, strict=True)]
            elif current == "cleanup":
                before = samples
                samples = clean_samples(before, hints.vocabulary(), clock)
            elif current == "corrections":  # online learning on the vocabulary text
                samples, corrections_info = correct_samples(samples, clock)
            elif current == "profiles":  # the window profile rules on the corrected text
                samples, profiles_info = profile_samples(samples, personal.vocabulary(), clock)
            elif current == "rewrite":  # the automatic rewrite of long takes on the profiles text
                samples, rewrite_stage = rewrite_samples(samples, rewriter, lexicon, set_settings.markup,
                                                         rewrite_settings.get("timeout_s"))
            else:  # vocabulary: the cleaned text, streamed again first when the hints change
                if restream:
                    streamed = streamer(dataset.takes, personal)
                    source = [Sample(take, text, seconds) for take, (text, seconds) in zip(dataset.takes, streamed, strict=True)]
                    samples = clean_samples(source, personal.vocabulary(), clock)
                    write_private(run_dir, set_name, "vocabulary-source", samples, [], results_dir)
                samples, changes = match_samples(samples, matcher, clock)
            intent, note, rows = judge_samples(samples, judge, terms, unavailable)
            write_private(run_dir, set_name, current, samples, rows, results_dir)
            block["stages"][current] = stage_metrics(samples, terms, set_settings.markup, intent, note, before)
            if current == "vocabulary":
                block["stages"][current]["vocabulary_changes"] = changes
            if current == "corrections":
                block["stages"][current].update(corrections_info)
            if current == "profiles":
                block["stages"][current].update(profiles_info)
            if current == "rewrite":
                block["stages"][current].update(rewrite_stage)
            log(f"{set_name} / {current}: n={len(samples)}" + (f" ({note})" if note else ""))
    return summary


TIMING_KEYS = ("load_s", "warmup_s", "transcribe_p50_s", "transcribe_p95_s", "rewrite_p50_s", "rewrite_p95_s",
               "rewrite_over_timeout")


def split_timings(summary: dict) -> dict:
    """Move wall-clock timings out of ``summary`` (in place) and return them.

    Timings vary from run to run, so the committed summary keeps only
    deterministic quality metrics; timings go to the ignored results folder.
    """
    timings: dict = {"engine": {}, "sets": {}}
    engine = summary.get("engine") or {}
    for key in TIMING_KEYS:
        if key in engine:
            timings["engine"][key] = engine.pop(key)
    for set_name, block in (summary.get("sets") or {}).items():
        for stage, row in (block.get("stages") or {}).items():
            moved = {key: row.pop(key) for key in TIMING_KEYS if key in row}
            if moved:
                timings["sets"].setdefault(set_name, {})[stage] = moved
    return timings


# ---------------------------------------------------------------- command mode and self-tests


def command_mode_block(rewrite: dict) -> dict:
    """The deterministic part of a ``bench.rewrite`` summary: counts and rates, no latency."""
    if not isinstance(rewrite, dict) or rewrite.get("kind") != "rewrite":
        raise SettingsError("not a rewrite summary")
    block = {"engine_model": (rewrite.get("engine") or {}).get("model"), "rewrite_model": rewrite.get("rewrite_model"),
             "judge": rewrite.get("judge")}
    block.update({key: rewrite.get(key) for key in COMMAND_MODE_KEYS})
    return block


def _count_rows(rows: object, fields: Sequence[str]) -> list[dict]:
    """Rows of a self-test result reduced to the named fields and an integer count."""
    kept = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("count"), int):
            kept.append({**{name: str(row.get(name, "")) for name in fields}, "count": row["count"]})
    return kept


def _day(value: object) -> str | None:
    return value[:10] if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", value) else None


def selftest_block(name: str, data: dict) -> dict:
    """Whitelisted counts of one manual self-test result; durations and frame times are left out."""
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    if name == "typing":
        return {"day": _day(data.get("finished_utc")), "passed": data.get("passed") is True,
                **{key: totals.get(key) for key in TYPING_KEYS}}
    if name == "triggers":
        version = data.get("version")
        if version not in TRIGGER_VERSIONS:
            raise SettingsError(f"triggers self-test result version {version!r} is not supported")
        errors = sum(totals.get(key) or 0 for key in ("callback_errors", "handler_errors", "machine_errors"))
        block = {"day": _day(data.get("finished_utc")), "passed": data.get("passed") is True,
                 "click_to_focus": data.get("click_to_focus") is True, "starts": totals.get("starts"),
                 "ends": totals.get("ends"), "errors": errors,
                 **{key: _count_rows(data.get(key), fields) for key, fields in TRIGGER_FIELDS.items()}}
        if version >= 2:
            min_hold = data.get("min_hold_ms")
            block["min_hold_ms"] = min_hold if isinstance(min_hold, int) and not isinstance(min_hold, bool) else None
            block.update({key: _count_rows(data.get(key), fields) for key, fields in TRIGGER_BUCKET_FIELDS.items()})
        return block
    if name == "indicator":
        return {"day": _day(data.get("finished")), "passed": data.get("passed") is True,
                **{key: data.get(key) for key in INDICATOR_KEYS}}
    raise ValueError(f"unknown self-test: {name}")


def acceptance_block(folder: Path) -> dict:
    """The newest result of each manual self-test under ``folder``; a missing one is None."""
    block: dict = {}
    for name in SELFTESTS:
        files = sorted(Path(folder).glob(f"{name}-*.json"))
        if not files:
            block[name] = None
            continue
        try:
            data = json.loads(files[-1].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SettingsError(f"{name} self-test result is not valid JSON") from None
        if not isinstance(data, dict):
            raise SettingsError(f"{name} self-test result is not a JSON object")
        block[name] = selftest_block(name, data)
    return block


# ---------------------------------------------------------------- targets


def _rewrite_default(summary: dict) -> bool:
    rewrite = summary.get("rewrite")
    return isinstance(rewrite, dict) and rewrite.get("product_default") is True


def _final_stage(set_block: dict, rewrite_default: bool = False) -> tuple[str, dict] | tuple[None, None]:
    """The last measured stage that the product types; the rewrite only when it is on by default."""
    stages = set_block.get("stages") or {}
    for stage in reversed(STAGE_ORDER):
        if stage == "rewrite" and not rewrite_default:
            continue
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
            stage, row = _final_stage(block, _rewrite_default(summary))
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
            # Fillers must stay removed in the final text; deletions are the
            # cleanup's own, against the stage before it (recognition
            # omissions stay visible in content_deleted).
            for set_name, stage, row in per_set(target, "cleanup"):
                if set_name != "dictation":
                    continue
                rate = row.get("filler_removal_rate")
                deleted = sets[set_name]["stages"]["cleanup"].get("content_deleted_by_cleanup")
                add(rate is not None and rate >= MIN_REMOVAL_RATE, target, f"dictation filler/repetition removal {_pct(rate)} ({stage}; target >= {100 * MIN_REMOVAL_RATE:.0f} %)")
                add(deleted is not None and deleted <= MAX_CONTENT_DELETED, target, f"dictation content words deleted by cleanup {deleted} (target {MAX_CONTENT_DELETED})")
        elif target == "vocabulary":
            for set_name, stage, row in per_set(target, "vocabulary"):
                gated = set_name in VOCABULARY_GATED_SETS
                for key, label, limit in (("name_error_rate", "project-name error", MAX_NAME_ERROR), ("term_error_rate", "English-term error", MAX_TERM_ERROR)):
                    value = row.get(key)
                    if gated:
                        add(value is not None and value <= limit, target, f"{set_name} {label} {_pct(value)} (target <= {100 * limit:.0f} %)")
                    else:
                        results.append((True, f"info {target}: {set_name} {label} {_pct(value)} (indicator, not a gate; target <= {100 * limit:.0f} % applies to dictation)"))
        elif target == "corrections":
            for set_name, stage, row in per_set(target, "corrections"):
                row = sets[set_name]["stages"]["corrections"]
                fixed, new, recurrences = row.get("recurrences_fixed_rate"), row.get("new_errors"), row.get("recurrences")
                if recurrences == 0:
                    # Nothing learned was repeated: nothing left unfixed, and no evidence either.
                    add(True, target, f"{set_name} no learned recurrence to fix (0 recurrences; target 100 % when present)")
                else:
                    add(fixed is not None and fixed >= 1.0, target,
                        f"{set_name} learned recurrences fixed {_pct(fixed)} of {recurrences} (target 100 %)")
                add(new is not None and new == 0, target, f"{set_name} new word errors {new} (target 0)")
        elif target == "rewrite":
            # The gate to make the automatic rewrite the default; its p95 is a
            # timing and is read from the run's timings file, not here.
            for set_name, stage, row in per_set(target, "rewrite"):
                row = sets[set_name]["stages"]["rewrite"]
                before = sets[set_name]["stages"].get("profiles") or {}
                deleted, long_takes = row.get("content_deleted_by_rewrite"), row.get("long_takes")
                if deleted is not None:
                    add(deleted <= MAX_CONTENT_DELETED, target,
                        f"{set_name} content words deleted by the rewrite {deleted} (target {MAX_CONTENT_DELETED})")
                wer, previous = row.get("wer_clean"), before.get("wer_clean")
                add(wer is not None and previous is not None and wer <= previous, target,
                    f"{set_name} clean WER {_pct(wer)} (profiles {_pct(previous)}; target no worse)")
                if not long_takes:
                    results.append((True, f"info {target}: {set_name} has no long take (nothing rewritten)"))
                    continue
                wer, previous = row.get("wer_clean_long"), row.get("wer_clean_long_before")
                add(wer is not None and previous is not None and wer <= previous, target,
                    f"{set_name} clean WER of the {long_takes} long takes {_pct(wer)} (before {_pct(previous)}; target no worse)")
        elif target == "overall":
            for set_name, stage, row in per_set(target):
                wer, intent = row.get("wer_clean"), row.get("intent_preserved")
                add(wer is not None and wer <= MAX_FINAL_WER, target, f"{set_name} final-text WER {_pct(wer)} ({stage}; target <= {100 * MAX_FINAL_WER:.0f} %)")
                add(intent is not None and intent >= MIN_INTENT, target, f"{set_name} intent preserved {_pct(intent)} ({stage}; target >= {100 * MIN_INTENT:.0f} %)")
        elif target == "desktop":
            acceptance = summary.get("acceptance") if isinstance(summary.get("acceptance"), dict) else {}
            typing, triggers, indicator = (acceptance.get(name) for name in SELFTESTS)
            if not isinstance(typing, dict):
                add(False, target, "typing self-test not measured")
            else:
                lost, extra, changed = (typing.get(key) for key in ("lost", "extra", "changed"))
                add(typing.get("passed") is True and lost == extra == changed == 0, target,
                    f"typing lost {lost}, duplicated or extra {extra}, changed {changed} "
                    f"of {typing.get('characters_expected')} characters (target 0)")
                add(typing.get("clipboard_changed_targets") == 0, target,
                    f"clipboard changed in {typing.get('clipboard_changed_targets')} of {typing.get('targets')} targets (target 0)")
            if not isinstance(triggers, dict):
                add(False, target, "trigger self-test not measured")
            else:
                add(triggers.get("passed") is True and triggers.get("errors") == 0, target,
                    f"trigger self-test errors {triggers.get('errors')}, starts {triggers.get('starts')}, ends {triggers.get('ends')}")
            if not isinstance(indicator, dict):
                add(False, target, "indicator self-test not measured")
            else:
                stolen = indicator.get("foreground_was_indicator")
                add(indicator.get("passed") is True and stolen == 0, target,
                    f"indicator in the foreground in {stolen} of {indicator.get('foreground_samples')} samples (target 0)")
    return results


# ---------------------------------------------------------------- document block

HEADER = (
    "Conjunto", "Etapa", "n", "WER literal", "WER limpo", "Erro termos EN", "Erro nomes",
    "Intenção preservada", "Hesitações removidas", "Palavras apagadas", "Apagadas pela limpeza",
)
CORRECTIONS_HEADER = (
    "Conjunto", "Erros já aprendidos que se repetem", "Corrigidos", "Taxa corrigida", "Repetições antes de ativar",
    "Erros novos", "Substituições aplicadas", "Ativas / pendentes / em conflito no fim",
)
COMMAND_HEADER = (
    "Modo comando", "n", "WER da instrução", "Instruções sem erros", "Aceites pelo produto", "Verificações passadas",
    "Juiz sim", "Corretas", "Segundas tentativas",
)
KIND_HEADER = ("Caso", "n", "Verificações passadas", "Corretas")
REWRITE_HEADER = (
    "Reescrita automática", "Ditados longos", "Reescritos", "Sem alterações", "Recusados pela guarda",
    "Falhas ou tempo esgotado", "Palavras de conteúdo apagadas", "Palavras de conteúdo corrigidas",
    "WER limpo dos longos (antes → depois)", "WER limpo do conjunto (profiles → rewrite)",
)
ACCEPTANCE_HEADER = ("Verificação manual no desktop", "Data", "Resultado")
TRIGGER_HEADER = ("Ação", "Sinal", "Motivo", "n")
HOLD_HEADER = ("Gatilho", "Duração do toque", "n")
EVENT_AGE_HEADER = ("Gatilho", "Atraso do callback do hook", "n")
BUCKET_LABELS = {
    "under_100ms": "menos de 100 ms", "100_249ms": "100 a 249 ms", "250_499ms": "250 a 499 ms",
    "500_999ms": "500 a 999 ms", "1000_2999ms": "1 a 3 s", "3000ms_plus": "3 s ou mais",
    "under_16ms": "menos de 16 ms", "16_49ms": "16 a 49 ms", "50_99ms": "50 a 99 ms",
    "250ms_plus": "250 ms ou mais", "unknown": "desconhecido",
}
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
                EMPTY if row.get("content_deleted_by_cleanup") is None else str(row["content_deleted_by_cleanup"]),
            )
            lines.append("| " + " | ".join(cells) + " |")
    corrections = [(name, row) for name in SETS
                   if isinstance(row := (((summary.get("sets") or {}).get(name) or {}).get("stages") or {}).get("corrections"), dict)]
    if corrections:
        lines += ["", "| " + " | ".join(CORRECTIONS_HEADER) + " |", "|" + "---|" * len(CORRECTIONS_HEADER)]
        for set_name, row in corrections:
            cells = (
                SET_LABELS[set_name], str(row.get("recurrences")), str(row.get("recurrences_fixed")),
                _pt_percent(row.get("recurrences_fixed_rate")), str(row.get("recurrences_not_yet_active")),
                str(row.get("new_errors")), str(row.get("corrections_applied")),
                f"{row.get('learned_active')} / {row.get('learned_pending')} / {row.get('learned_conflicts')}",
            )
            lines.append("| " + " | ".join(cells) + " |")
    lines += _rewrite_lines(summary)
    lines += _command_lines(summary.get("command_mode"))
    lines += _acceptance_lines(summary.get("acceptance"))
    latency = summary.get("latency")
    if isinstance(latency, dict) and latency.get("p95_s") is not None:
        lines.append("")
        lines.append(f"Latência da aplicação (largar a tecla até ao texto), p95: {_pt_seconds(latency['p95_s'])} s")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _of(count: object, total: object) -> str:
    return f"{EMPTY if count is None else count} de {EMPTY if total is None else total}"


def _table(header: Sequence[str]) -> list[str]:
    return ["", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]


def _count(value: object) -> str:
    return EMPTY if value is None else str(value)


def _rewrite_lines(summary: dict) -> list[str]:
    """The automatic rewrite of long takes: outcomes, content kept and WER before and after."""
    settings = summary.get("rewrite")
    rows = [(name, row) for name in SETS
            if isinstance(row := (((summary.get("sets") or {}).get(name) or {}).get("stages") or {}).get("rewrite"), dict)]
    if not isinstance(settings, dict) or not rows:
        return []
    lines = _table(REWRITE_HEADER)
    for set_name, row in rows:
        profiles = ((summary["sets"][set_name].get("stages") or {}).get("profiles") or {})
        reasons = row.get("refusal_reasons") or {}
        refused = _count(row.get("refused"))
        if reasons:
            refused += " (" + ", ".join(f"{reason} {count}" for reason, count in reasons.items()) + ")"
        cells = (
            SET_LABELS[set_name], _count(row.get("long_takes")), _count(row.get("rewritten")),
            _count(row.get("unchanged")), refused, _count((row.get("failed") or 0) + (row.get("timeouts") or 0)),
            _count(row.get("content_deleted_by_rewrite")), _count(row.get("content_fixed_by_rewrite")),
            f"{_pt_percent(row.get('wer_clean_long_before'))} → {_pt_percent(row.get('wer_clean_long'))}",
            f"{_pt_percent(profiles.get('wer_clean'))} → {_pt_percent(row.get('wer_clean'))}",
        )
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        f"Reescrita automática ligada por omissão: {'sim' if settings.get('product_default') is True else 'não'} "
        f"(modelo {settings.get('model')}; longo = mais de {settings.get('min_audio_s'):g} s de áudio ou mais de "
        f"{settings.get('min_words')} palavras; limite do produto {settings.get('timeout_s'):g} s)"
    )
    return lines


def _command_lines(block: object) -> list[str]:
    """Command-mode counts: the instruction transcription, the product's validation, checks and judge."""
    if not isinstance(block, dict):
        return []
    takes = block.get("takes")
    judge_yes = block.get("judge_yes")
    cells = (
        f"{block.get('engine_model')} + {block.get('rewrite_model')}", str(takes),
        _pt_percent(block.get("instruction_wer")), _of(block.get("instruction_exact"), takes),
        _of(block.get("valid"), takes), _of(block.get("checks_passed"), takes),
        EMPTY if judge_yes is None else _of(judge_yes, block.get("judged")),
        EMPTY if block.get("correct") is None else _of(block.get("correct"), takes),
        EMPTY if block.get("second_attempts") is None else _of(block.get("second_attempts"), takes),
    )
    lines = _table(COMMAND_HEADER) + ["| " + " | ".join(cells) + " |"]
    kinds = block.get("by_kind") if isinstance(block.get("by_kind"), dict) else {}
    if kinds:
        lines += _table(KIND_HEADER)
        for kind, row in kinds.items():
            correct = row.get("correct")
            lines.append(f"| {kind} | {row.get('takes')} | {row.get('checks_passed')} | {EMPTY if correct is None else correct} |")
    return lines


def _acceptance_lines(block: object) -> list[str]:
    """The Sponsor's manual desktop self-tests: typing, triggers and indicator counts."""
    if not isinstance(block, dict):
        return []
    typing, triggers, indicator = (block.get(name) for name in SELFTESTS)
    lines = _table(ACCEPTANCE_HEADER)
    if isinstance(typing, dict):
        lines.append(
            f"| digitação | {typing.get('day')} | {_of(typing.get('characters_typed'), typing.get('characters_expected'))} "
            f"caracteres em {typing.get('targets')} janelas ({_of(typing.get('cases_passed'), typing.get('cases'))} casos certos); "
            f"perdidos {typing.get('lost')}, a mais {typing.get('extra')}, trocados {typing.get('changed')}; "
            f"clipboard alterado em {_of(typing.get('clipboard_changed_targets'), typing.get('targets'))} janelas |")
    else:
        lines.append(f"| digitação | {EMPTY} | não medida |")
    if isinstance(triggers, dict):
        lines.append(
            f"| gatilhos | {triggers.get('day')} | {triggers.get('starts')} inícios, {triggers.get('ends')} fins, "
            f"{triggers.get('errors')} erros; clicar para focar {'ligado' if triggers.get('click_to_focus') else 'desligado'} |")
    else:
        lines.append(f"| gatilhos | {EMPTY} | não medidos |")
    if isinstance(indicator, dict):
        place = "ponteiro" if indicator.get("position") == "pointer" else "fundo do ecrã"
        lines.append(
            f"| indicador | {indicator.get('day')} | {indicator.get('states_shown')} estados mostrados junto ao {place}; "
            f"indicador em primeiro plano em {_of(indicator.get('foreground_was_indicator'), indicator.get('foreground_samples'))} amostras |")
    else:
        lines.append(f"| indicador | {EMPTY} | não medido |")
    if isinstance(triggers, dict) and triggers.get("signals"):
        lines += _table(TRIGGER_HEADER)
        for row in triggers["signals"]:
            lines.append(f"| {row['action']} | {row['signal']} | {row['reason'] or EMPTY} | {row['count']} |")
    for key, header in (("holds", HOLD_HEADER), ("event_ages", EVENT_AGE_HEADER)):
        rows = triggers.get(key) if isinstance(triggers, dict) else None
        if rows:  # version 2 results only
            lines += _table(header)
            for row in rows:
                lines.append(f"| {row['trigger']} | {BUCKET_LABELS.get(row['bucket'], row['bucket'])} | {row['count']} |")
    return lines


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


def run_compare(config: Path | None, names: Sequence[str], run_dir: Path, judge_factory: Callable, cleaners_factory: Callable,
                results_dir: Path, out: Callable[[str], None]) -> int:
    """--compare-cleanup: aggregates on screen; per-take texts and times under bench/results/cleanup/."""
    run_dir = Path(run_dir).resolve()
    if Path(results_dir).resolve() not in run_dir.parents:
        out("error: the run folder must be under bench/results/")
        return 2
    try:
        sets = load_sets(load_settings(config), names)
        spoken_names = sorted({name for _, dataset in sets.values() for name in dataset.names})
        cleaners, cleaner_note = cleaners_factory(build_hints(spoken_names, load_terms()).vocabulary())
        judge, unavailable = judge_factory()
        report, texts = compare_cleanup(sets, run_dir, cleaners, judge, unavailable, load_terms())
    except (SettingsError, DatasetError) as exc:
        out(f"error: {exc}")
        return 2
    report["run"] = run_dir.name
    report["llm_note"] = cleaner_note
    target = Path(results_dir) / "cleanup" / time.strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    (target / "compare.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "texts.json").write_text(json.dumps(texts, ensure_ascii=False, indent=2), encoding="utf-8")
    if cleaner_note:
        out(f"llm cleanup not measured: {cleaner_note}")
    for set_name, modes in report["sets"].items():
        for mode, row in modes.items():
            p50, p95 = row["cleanup_p50_s"], row["cleanup_p95_s"]
            timing = "" if p95 is None else f", p50 {p50:.2f} s, p95 {p95:.2f} s"
            fallbacks = sum(row["fallbacks"].values())
            out(
                f"{set_name} / {mode}: WER clean {_pct(row['wer_clean'])}, intent {_pct(row['intent_preserved'])}, "
                f"removal {_pct(row['filler_removal_rate'])}, deleted by cleanup {row['content_deleted_by_cleanup']}"
                f"{timing}, fallbacks {fallbacks}" + (f" ({row['intent_note']})" if row["intent_note"] else "")
            )
    out("comparison written under bench/results/cleanup/")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    engine_factory: Callable[[], Engine] = default_engine,
    judge_factory: Callable[[], tuple[Callable | None, str | None]] = default_judge,
    streamer_factory: Callable[[Engine], tuple[Streamer, dict]] = default_streamer,
    cleaners_factory: Callable[[Sequence[str]], tuple[dict, str | None]] = default_cleaners,
    rewriter_factory: Callable[[], tuple[AutoRewriter, dict]] = default_rewriter,
    results_dir: Path = RESULTS_DIR,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(prog="bench.pipeline", description="Phase 2 pipeline evaluation on the real-voice sets.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--set", choices=(*SETS, "all"), default="all", help="dataset set (default all)")
    parser.add_argument("--dry-run", action="store_true", help="dataset counts only; no GPU")
    parser.add_argument("--stage", choices=IMPLEMENTED_STAGES, default=None, help="measure up to this stage")
    parser.add_argument("--compare-cleanup", type=Path, default=None, metavar="RUN",
                        help="compare rules and qwen3:8b cleanup on RUN's saved streamed texts; no GPU")
    parser.add_argument("--vocabulary", type=Path, default=LOCAL_VOCABULARY,
                        help="personal vocabulary for the vocabulary and later stages (default local/vocabulary.toml; missing is empty)")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY, help="aggregate summary JSON")
    parser.add_argument("--require", action="append", choices=TARGET_NAMES, default=[], help="exit 1 unless met")
    parser.add_argument("--check-doc", type=Path, default=None, help="exit 1 when DOC's block differs from the summary")
    parser.add_argument("--write-doc", type=Path, default=None, help="insert or replace the block in DOC")
    parser.add_argument("--add-command-mode", type=Path, default=None, metavar="RUN_SUMMARY",
                        help="merge the counts of a bench.rewrite summary into --summary (manual step)")
    parser.add_argument("--add-selftests", type=Path, default=None, metavar="DIR",
                        help="merge the newest manual self-test counts from DIR (local/selftest) into --summary")
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
    if args.compare_cleanup:
        if args.stage:
            parser.error("--compare-cleanup and --stage are separate runs")
        return run_compare(args.config, names, args.compare_cleanup, judge_factory, cleaners_factory, results_dir, out)
    merging = bool(args.add_command_mode or args.add_selftests)
    if not (args.stage or args.require or args.check_doc or args.write_doc or merging):
        parser.error("nothing to do: give --dry-run, --stage, --compare-cleanup, --require, --check-doc, --write-doc, "
                     "--add-command-mode or --add-selftests")
    if args.stage and merging:
        parser.error("--add-command-mode and --add-selftests merge into an existing summary: run them after --stage")

    try:
        if args.stage:
            settings = load_settings(args.config)
            sets = load_sets(settings, names)
            # Every stage from vocabulary on uses the personal vocabulary.
            later = STAGE_ORDER.index(args.stage) >= STAGE_ORDER.index("vocabulary")
            vocabulary = load_vocabulary(args.vocabulary) if later else NO_VOCABULARY
            rewriter, rewrite_settings = rewriter_factory() if args.stage == "rewrite" else (None, None)
            engine = engine_factory()
            judge, unavailable = judge_factory()
            run_dir = Path(results_dir) / "pipeline" / time.strftime("%Y%m%d-%H%M%S")
            streamer, stream_options = streamer_factory(engine) if args.stage != "raw" else (None, None)
            try:
                summary = measure(sets, args.stage, engine, judge, unavailable, load_terms(), run_dir, results_dir=results_dir,
                                  log=out, streamer=streamer, stream_options=stream_options, vocabulary=vocabulary,
                                  rewriter=rewriter, rewrite_settings=rewrite_settings)
            finally:
                engine.close()
                close = getattr(streamer, "close", None)
                if close is not None:
                    close()
            run_dir.mkdir(parents=True, exist_ok=True)
            timings = split_timings(summary)
            (run_dir / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
            for set_name, stages in timings["sets"].items():
                rewrite = stages.get("rewrite") or {}
                if rewrite.get("rewrite_p95_s") is not None:
                    out(f"{set_name} / rewrite model calls: p50 {rewrite['rewrite_p50_s']:.2f} s, "
                        f"p95 {rewrite['rewrite_p95_s']:.2f} s (target <= {MAX_REWRITE_P95_S:.0f} s), "
                        f"slower than the product timeout {rewrite['rewrite_over_timeout']}")
            texts = [text for _, dataset in sets.values() for text in dataset.reference_texts()]
            spoken_names = [name for _, dataset in sets.values() for name in dataset.names]
            spoken_names += [entry.text for entry in vocabulary.names]
            write_summary(args.summary, summary, texts, spoken_names)
            out(f"summary written ({', '.join(summary['sets'])}); per-take outputs under bench/results/")
        else:
            summary = _read_summary(args.summary)
        if merging:
            if args.add_command_mode:
                try:
                    rewrite = json.loads(args.add_command_mode.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    raise SettingsError("rewrite summary not found (run bench.rewrite first)") from None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise SettingsError("rewrite summary is not valid JSON") from None
                summary["command_mode"] = command_mode_block(rewrite)
            if args.add_selftests:
                if not args.add_selftests.is_dir():
                    raise SettingsError("self-test folder not found")
                summary["acceptance"] = acceptance_block(args.add_selftests)
            # The merged blocks hold counts and reason codes only; no text to check against.
            write_summary(args.summary, summary, [], [])
            merged = [name for name, given in (("command mode", args.add_command_mode), ("self-tests", args.add_selftests)) if given]
            out(f"summary updated ({', '.join(merged)})")
    except (SettingsError, DatasetError, VocabularyError) as exc:
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
