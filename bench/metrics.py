"""Word error rate, term/name recall errors, latency percentiles and aggregation.

All text is normalized with bench.normalize before comparison. The aggregate
returned by ``aggregate`` holds numbers only, never spoken text.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.normalize import normalize, normalize_words

TERMS_FILE = Path(__file__).resolve().parent / "terms_en.txt"


@dataclass(frozen=True)
class EditCounts:
    substitutions: int
    deletions: int
    insertions: int
    reference_words: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


def word_edits(reference: Sequence[str], hypothesis: Sequence[str]) -> EditCounts:
    """Levenshtein alignment on words, returning S/D/I counts."""
    rows, cols = len(reference), len(hypothesis)
    # Each cell holds (cost, substitutions, deletions, insertions).
    previous = [(j, 0, 0, j) for j in range(cols + 1)]
    for i in range(1, rows + 1):
        current = [(i, 0, i, 0)]
        for j in range(1, cols + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                current.append(previous[j - 1])
                continue
            sub = previous[j - 1]
            dele = previous[j]
            ins = current[j - 1]
            best = min(
                (sub[0] + 1, sub[1] + 1, sub[2], sub[3]),
                (dele[0] + 1, dele[1], dele[2] + 1, dele[3]),
                (ins[0] + 1, ins[1], ins[2], ins[3] + 1),
            )
            current.append(best)
        previous = current
    _, subs, dels, ins = previous[cols]
    return EditCounts(subs, dels, ins, rows)


def corpus_wer(pairs: Iterable[tuple[str, str]]) -> float | None:
    """Sum of word edits over sum of reference words; None when empty."""
    errors = words = 0
    for reference, hypothesis in pairs:
        counts = word_edits(normalize_words(reference), normalize_words(hypothesis))
        errors += counts.errors
        words += counts.reference_words
    return errors / words if words else None


def count_occurrences(words: Sequence[str], term: Sequence[str]) -> int:
    """Non-overlapping occurrences of the word sequence ``term`` in ``words``."""
    if not term:
        return 0
    count = index = 0
    size = len(term)
    while index <= len(words) - size:
        if list(words[index : index + size]) == list(term):
            count += 1
            index += size
        else:
            index += 1
    return count


@dataclass(frozen=True)
class RecallCounts:
    expected: int
    found: int

    @property
    def error_rate(self) -> float | None:
        return 1 - self.found / self.expected if self.expected else None


def term_recall(pairs: Iterable[tuple[str, str]], terms: Iterable[str]) -> RecallCounts:
    """Per-occurrence recall: each reference occurrence is found at most once."""
    normalized_terms = sorted({tuple(normalize_words(t)) for t in terms if normalize_words(t)})
    expected = found = 0
    for reference, hypothesis in pairs:
        ref_words = normalize_words(reference)
        hyp_words = normalize_words(hypothesis)
        for term in normalized_terms:
            in_ref = count_occurrences(ref_words, term)
            if in_ref:
                expected += in_ref
                found += min(in_ref, count_occurrences(hyp_words, term))
    return RecallCounts(expected, found)


def load_terms(path: Path = TERMS_FILE) -> list[str]:
    """Committed generic English/technical terms, one per line; '#' comments."""
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            terms.append(line)
    return terms


@dataclass(frozen=True)
class RemovalCounts:
    """Filler/repetition spans removed and content words deleted in one or more texts."""

    spans: int = 0
    removed: int = 0
    content_words: int = 0
    content_deleted: int = 0

    def __add__(self, other: RemovalCounts) -> RemovalCounts:
        return RemovalCounts(
            self.spans + other.spans,
            self.removed + other.removed,
            self.content_words + other.content_words,
            self.content_deleted + other.content_deleted,
        )

    @property
    def removal_rate(self) -> float | None:
        return self.removed / self.spans if self.spans else None


def removal_counts(segments: Sequence[tuple[str, str]], hypothesis: str) -> RemovalCounts:
    """Align the verbatim reference, word by word, with ``hypothesis``.

    ``segments`` are (kind, text) pairs, kind "content", "filler" or
    "repetition". The alignment minimizes word edits and, among equal
    alignments, content-word deletions, so a repeated phrase that survives
    once is matched to its content copy. A span counts as removed when every
    one of its words is deleted; a content word counts as deleted when it has
    no counterpart (a substitution is a recognition error, not a deletion).
    """
    tagged: list[tuple[str, int]] = []  # (word, span index or -1 for content)
    spans = content_words = 0
    for kind, text in segments:
        words = normalize_words(text)
        if kind == "content":
            tagged.extend((word, -1) for word in words)
            content_words += len(words)
        elif words:
            tagged.extend((word, spans) for word in words)
            spans += 1
    hyp = normalize_words(hypothesis)
    rows, cols = len(tagged), len(hyp)
    # cost[i][j] = (edits, content deletions) aligning tagged[:i] with hyp[:j].
    cost = [[(0, 0)] * (cols + 1) for _ in range(rows + 1)]
    move = [[""] * (cols + 1) for _ in range(rows + 1)]
    for j in range(1, cols + 1):
        cost[0][j], move[0][j] = (j, 0), "I"
    for i in range(1, rows + 1):
        word, span = tagged[i - 1]
        deleted_content = 1 if span < 0 else 0
        edits, dels = cost[i - 1][0]
        cost[i][0], move[i][0] = (edits + 1, dels + deleted_content), "D"
        for j in range(1, cols + 1):
            diagonal = cost[i - 1][j - 1]
            options = [
                ((diagonal[0] + (word != hyp[j - 1]), diagonal[1]), "M"),
                ((cost[i - 1][j][0] + 1, cost[i - 1][j][1] + deleted_content), "D"),
                ((cost[i][j - 1][0] + 1, cost[i][j - 1][1]), "I"),
            ]
            cost[i][j], move[i][j] = min(options)
    kept_spans: set[int] = set()
    i, j = rows, cols
    content_deleted = 0
    while i > 0 or j > 0:
        step = move[i][j]
        if step == "M":
            if tagged[i - 1][1] >= 0:
                kept_spans.add(tagged[i - 1][1])
            i, j = i - 1, j - 1
        elif step == "D":
            if tagged[i - 1][1] < 0:
                content_deleted += 1
            i -= 1
        else:
            j -= 1
    return RemovalCounts(spans, spans - len(kept_spans), content_words, content_deleted)


def percentile_nearest_rank(values: Iterable[float], percent: float) -> float | None:
    """Nearest-rank percentile: the ceil(p/100 * n)-th smallest value."""
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


# ---------------------------------------------------------------- aggregation


@dataclass(frozen=True)
class ItemResult:
    """One transcribed take. Text fields stay in memory, never in the summary."""

    id: str
    reference: str = field(repr=False)
    hypothesis: str = field(repr=False)
    names: tuple[str, ...] = field(default=(), repr=False)
    intent_preserved: bool | None = None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def aggregate(
    engine: str,
    variant: str,
    items: Sequence[ItemResult],
    terms: Iterable[str],
    latencies_s: Sequence[float] = (),
) -> dict:
    """Aggregate numbers for one engine/variant. Contains no spoken text."""
    pairs = [(item.reference, item.hypothesis) for item in items]
    term_counts = term_recall(pairs, terms)
    name_expected = name_found = 0
    for item in items:
        counts = term_recall([(item.reference, item.hypothesis)], item.names)
        name_expected += counts.expected
        name_found += counts.found
    names = RecallCounts(name_expected, name_found)
    judged = [item.intent_preserved for item in items if item.intent_preserved is not None]
    return {
        "engine": engine,
        "variant": variant,
        "status": "ok",
        "skipped_reason": None,
        "n": len(items),
        "wer": _round(corpus_wer(pairs)),
        "term_error_rate": _round(term_counts.error_rate),
        "term_occurrences": term_counts.expected,
        "name_error_rate": _round(names.error_rate),
        "name_occurrences": names.expected,
        "intent_preserved": _round(sum(judged) / len(judged)) if judged else None,
        "intent_judged": len(judged),
        "latency_p50_s": _round(percentile_nearest_rank(latencies_s, 50)),
        "latency_p95_s": _round(percentile_nearest_rank(latencies_s, 95)),
        "latency_samples": len(latencies_s),
    }


def skipped(engine: str, variant: str, reason: str) -> dict:
    """An engine/variant that did not run. The reason must not contain spoken text."""
    reason = " ".join(str(reason).split())
    if not reason:
        raise ValueError("a skipped engine/variant needs a reason")
    return {
        "engine": engine,
        "variant": variant,
        "status": "skipped",
        "skipped_reason": reason[:200],
        "n": 0,
        "wer": None,
        "term_error_rate": None,
        "term_occurrences": 0,
        "name_error_rate": None,
        "name_occurrences": 0,
        "intent_preserved": None,
        "intent_judged": 0,
        "latency_p50_s": None,
        "latency_p95_s": None,
        "latency_samples": 0,
    }


def contains_spoken_text(serialized: str, texts: Iterable[str], names: Iterable[str] = ()) -> bool:
    """True when any name, or any 3 consecutive words of a text, leak into ``serialized``.

    Texts of two words are checked whole; single words are too generic to check.
    """
    haystack = " " + normalize(serialized) + " "
    for text in texts:
        words = normalize_words(text)
        size = min(3, len(words))
        if size < 2:
            continue
        for start in range(len(words) - size + 1):
            if " " + " ".join(words[start : start + size]) + " " in haystack:
                return True
    for name in names:
        needle = normalize(name)
        if needle and " " + needle + " " in haystack:
            return True
    return False


SUMMARY_SCHEMA = 1


def build_summary(
    results: Sequence[dict],
    *,
    expected_n: int,
    n_valid: int,
    invalid_reasons: dict[str, int],
    discarded: int,
    recording_origin: dict[str, int],
) -> dict:
    """Aggregate-only benchmark summary: counts, rates, latencies, reasons."""
    keys = [(r["engine"], r["variant"]) for r in results]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate engine/variant in results")
    return {
        "schema": SUMMARY_SCHEMA,
        "expected_n": expected_n,
        "dataset": {
            "voice": "real",
            "n_valid": n_valid,
            "invalid_reasons": dict(sorted(invalid_reasons.items())),
            "discarded_takes": discarded,
            "recording_origin": dict(sorted(recording_origin.items())),
        },
        "results": [dict(r) for r in results],
    }


def write_summary(path: Path, summary: dict, texts: Iterable[str], names: Iterable[str]) -> None:
    """Write the summary JSON after refusing any spoken text or project name."""
    serialized = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if contains_spoken_text(serialized, texts, names):
        raise ValueError("summary would contain spoken text or a project name; not written")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8", newline="\n")
