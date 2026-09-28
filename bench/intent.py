"""Intent preserved: hard slot rule AND a local same-meaning judge.

A phrase counts as preserved only when
(a) every project name and every listed English term in the reference is
    still present in the hypothesis (per-occurrence, after normalization), and
(b) the local judge (Ollama qwen3:8b, thinking off, temperature 0) answers
    "yes" to the fixed JUDGE_SYSTEM prompt.
The judge only runs when (a) holds. The human-checkable table is written only
under bench/results/ because it contains spoken text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.cleanup import CLEANUP_MODEL, OllamaClient, OllamaError
from bench.metrics import count_occurrences
from bench.normalize import normalize_words
from bench.settings import RESULTS_DIR

JUDGE_SYSTEM = (
    "You compare two short Portuguese texts: a REFERENCE (what the speaker said) and a "
    "HYPOTHESIS (what a dictation system wrote). Answer yes only if someone acting on the "
    "hypothesis would do the same action with the same meaning as the reference: same "
    "command, same target, same names and terms, same negation and numbers. Ignore "
    "punctuation, capitalization, filler words and small wording differences that keep the "
    "meaning. Reply in JSON with the fields same (yes or no) and reason (at most 15 English words)."
)
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "string", "enum": ["yes", "no"]}, "reason": {"type": "string"}},
    "required": ["same", "reason"],
}


def missing_slots(reference: str, hypothesis: str, names: Iterable[str], terms: Iterable[str]) -> list[str]:
    """Names and terms that occur in the reference more often than in the hypothesis."""
    ref_words = normalize_words(reference)
    hyp_words = normalize_words(hypothesis)
    missing: list[str] = []
    for label, words in [(n, normalize_words(n)) for n in names] + [(t, normalize_words(t)) for t in terms]:
        if not words:
            continue
        expected = count_occurrences(ref_words, words)
        if expected and count_occurrences(hyp_words, words) < expected and label not in missing:
            missing.append(label)
    return missing


class IntentJudge:
    def __init__(self, client: OllamaClient, model: str = CLEANUP_MODEL) -> None:
        self.client = client
        self.model = model

    def judge(self, reference: str, hypothesis: str) -> tuple[bool, str]:
        user = f"REFERENCE: {reference}\nHYPOTHESIS: {hypothesis}"
        result = self.client.chat(self.model, JUDGE_SYSTEM, user, schema=JUDGE_SCHEMA)
        try:
            data = json.loads(result.content)
        except json.JSONDecodeError:
            raise OllamaError("intent judge returned invalid JSON") from None
        same = data.get("same") if isinstance(data, dict) else None
        if same not in ("yes", "no"):
            raise OllamaError("intent judge returned no yes/no verdict")
        reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
        return same == "yes", " ".join(reason.split())[:200]


@dataclass(frozen=True)
class IntentRow:
    id: str
    preserved: bool
    reason: str
    reference: str = field(repr=False)
    hypothesis: str = field(repr=False)


def evaluate(items: Sequence, judge, terms: Sequence[str]) -> list[IntentRow]:
    """Judge every item (metrics.ItemResult-like: id, reference, hypothesis, names).

    ``judge`` is any callable (reference, hypothesis) -> (bool, reason). Raises
    OllamaError on the first judge failure, so a variant never mixes judged and
    unjudged phrases.
    """
    rows: list[IntentRow] = []
    for item in items:
        missing = missing_slots(item.reference, item.hypothesis, item.names, terms)
        if missing:
            preserved, reason = False, "slot rule: missing " + ", ".join(missing)
        else:
            preserved, reason = judge(item.reference, item.hypothesis)
            reason = "judge: " + reason
        rows.append(IntentRow(item.id, preserved, reason, item.reference, item.hypothesis))
    return rows


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def write_table(path: Path, engine: str, variant: str, rows: Sequence[IntentRow], results_dir: Path = RESULTS_DIR) -> Path:
    """Markdown table with a blank human column. Refuses paths outside bench/results/."""
    target = Path(path).resolve()
    root = Path(results_dir).resolve()
    if root not in target.parents:
        raise ValueError("intent tables may only be written under bench/results/")
    lines = [
        f"# Intent check: {engine} / {variant}",
        "",
        "Fill the human column with yes or no. Private: contains spoken text.",
        "",
        "| id | reference | hypothesis | verdict | reason | human |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        verdict = "yes" if row.preserved else "no"
        lines.append(f"| {row.id} | {_cell(row.reference)} | {_cell(row.hypothesis)} | {verdict} | {_cell(row.reason)} |  |")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target
