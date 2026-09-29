"""Intent preserved: hard slot and cut-off rules AND a local same-meaning judge.

A phrase counts as preserved only when
(a) every project name and every listed English term in the reference is
    still present in the hypothesis (per-occurrence, after normalization),
(b) the hypothesis is not cut off: the last word of the reference has a
    counterpart in every minimum-edit word alignment, and
(c) the local judge (Ollama qwen3:8b, thinking off, temperature 0) answers
    "yes" to the fixed JUDGE_SYSTEM prompt.
The judge only runs when (a) and (b) hold. The human-checkable table is written only
under bench/results/ because it contains spoken text.

Review of the judge (Phase 2, task T4): a manual assessment of a pipeline
run's outputs lives in an ignored JSON file under bench/results/. The review
command writes, per set, a table with the reference, the hypothesis, the judge
verdict and reason, the manual verdict and reason and an empty Sponsor column,
and prints the judge-versus-review agreement as aggregates only:

    py -3.12 -m bench.intent --run bench/results/pipeline/<run> --reviews PATH
    py -3.12 -m bench.intent --run bench/results/pipeline/<run> --reviews PATH --rejudge

``--rejudge`` judges the saved outputs again with the current judge (needs the
local Ollama) instead of reading the verdicts stored in the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.cleanup import CLEANUP_MODEL, OllamaClient, OllamaError
from bench.metrics import ItemResult, count_occurrences
from bench.normalize import normalize_words
from bench.settings import RESULTS_DIR

SETS = ("commands", "dictation")

# Revised in Phase 2 (T4) after the manual review of the streamed outputs:
# the first prompt accepted a request turned into a statement, a changed
# tense, a dropped or replaced informative word and a cut-off ending as small
# wording differences. One sentence names those cases; a stricter, itemized
# prompt made qwen3:8b reject fillers, articles and misspellings instead.
JUDGE_SYSTEM = (
    "You compare two short Portuguese texts: a REFERENCE (what the speaker said) and a "
    "HYPOTHESIS (what a dictation system wrote). Answer yes only if someone acting on the "
    "hypothesis would do the same action with the same meaning as the reference: same "
    "command, same target, same names and terms, same negation and numbers. A request that "
    "becomes a statement about the speaker, a changed tense, a missing or replaced word that "
    "carries information, or a text cut off before its end is not the same meaning. Ignore "
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


def cut_off(reference: str, hypothesis: str) -> bool:
    """True when every minimum-edit alignment deletes the reference's last word.

    A hypothesis that ends with a different word (a substitution) is left to
    the judge; only a missing ending counts. Ties are not cut off.
    """
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    if not ref:
        return False
    if not hyp:
        return True
    # distance[i][j]: word edits between ref[:i] and hyp[:j].
    distance = [list(range(len(hyp) + 1))]
    for i in range(1, len(ref) + 1):
        row = [i]
        for j in range(1, len(hyp) + 1):
            row.append(min(distance[i - 1][j] + 1, row[j - 1] + 1, distance[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1])))
        distance.append(row)
    last, total = len(ref), distance[-1][-1]
    # Cheapest alignment that keeps the last reference word on some hypothesis
    # word j, with every later hypothesis word inserted.
    kept = min(distance[last - 1][j - 1] + (ref[-1] != hyp[j - 1]) + len(hyp) - j for j in range(1, len(hyp) + 1))
    return kept > total


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
        elif cut_off(item.reference, item.hypothesis):
            preserved, reason = False, "cut-off rule: the end of the reference is missing"
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


# ---------------------------------------------------------------- judge review


@dataclass(frozen=True)
class Review:
    """One manual verdict, tied to the exact hypothesis it was given for."""

    preserved: bool
    reason: str = field(repr=False)
    hypothesis: str = field(repr=False)


def load_reviews(path: Path) -> dict[str, dict[str, Review]]:
    """Manual verdicts per set and take id from ``{"sets": {set: {id: {...}}}}``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("review file not found") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("review file is not valid JSON") from None
    sets = data.get("sets") if isinstance(data, dict) else None
    if not isinstance(sets, dict):
        raise ValueError("review file has no sets")
    reviews: dict[str, dict[str, Review]] = {}
    for set_name, entries in sets.items():
        if set_name not in SETS or not isinstance(entries, dict):
            raise ValueError(f"review file: unknown set {set_name!r}")
        reviews[set_name] = {}
        for take_id, entry in entries.items():
            if (not isinstance(entry, dict) or not isinstance(entry.get("preserved"), bool)
                    or not isinstance(entry.get("reason"), str) or not isinstance(entry.get("hypothesis"), str)):
                raise ValueError(f"review file: invalid entry {set_name}/{take_id}")
            reviews[set_name][take_id] = Review(entry["preserved"], entry["reason"], entry["hypothesis"])
    return reviews


@dataclass(frozen=True)
class ReviewRow:
    id: str
    judge_preserved: bool | None
    judge_reason: str = field(repr=False)
    review: Review | None = field(repr=False)
    reference: str = field(repr=False)
    hypothesis: str = field(repr=False)


def review_rows(stage_rows: Sequence[dict], reviews: dict[str, Review], judged: dict[str, IntentRow] | None = None) -> list[ReviewRow]:
    """Join a run's per-take rows with the manual verdicts and the judge verdicts.

    ``stage_rows`` are the rows bench.pipeline writes (id, clean, hypothesis,
    intent_preserved, intent_reason); ``judged`` replaces the stored verdicts.
    A review given for a different hypothesis is stale and left out.
    """
    rows = []
    for row in stage_rows:
        take_id = row["id"]
        if judged is not None:
            verdict = judged.get(take_id)
            preserved, reason = (verdict.preserved, verdict.reason) if verdict else (None, "")
        else:
            preserved, reason = row.get("intent_preserved"), row.get("intent_reason") or ""
        review = reviews.get(take_id)
        if review is not None and review.hypothesis != row["hypothesis"]:
            review = None
        rows.append(ReviewRow(take_id, preserved, reason, review, row["clean"], row["hypothesis"]))
    return rows


def agreement(rows: Sequence[ReviewRow]) -> dict:
    """Judge-versus-review counts; numbers only."""
    both = [row for row in rows if row.review is not None and row.judge_preserved is not None]
    agree = sum(row.judge_preserved == row.review.preserved for row in both)
    return {
        "n": len(rows),
        "compared": len(both),
        "agree": agree,
        "agreement": round(agree / len(both), 4) if both else None,
        "judge_yes_review_no": sum(row.judge_preserved and not row.review.preserved for row in both),
        "judge_no_review_yes": sum(row.review.preserved and not row.judge_preserved for row in both),
        "judge_preserved": sum(bool(row.judge_preserved) for row in both),
        "review_preserved": sum(row.review.preserved for row in both),
    }


def _yes_no(value: bool | None) -> str:
    return "—" if value is None else ("sim" if value else "não")


REVIEW_RULE = (
    "`sim` quer dizer que quem lê a transcrição recebe a mesma mensagem que a referência "
    "(o texto que se queria escrever, sem hesitações). Conta como diferente: um pedido que passa "
    "a afirmação ou muda de tempo, uma palavra com informação que falta, sobra ou é trocada "
    "(ação, objeto, nome, termo, qualificador, número, hora, negação) e uma frase cortada. "
    "Não conta: pontuação, maiúsculas, hesitações, gralhas que se leem como a mesma palavra, "
    "`corrige`/`corrija`, algarismos e artigos que não mudam o sentido."
)


def write_review_table(path: Path, set_name: str, title: str, rows: Sequence[ReviewRow], results_dir: Path = RESULTS_DIR) -> Path:
    """Sponsor-facing Markdown table (European Portuguese); only under bench/results/."""
    target = Path(path).resolve()
    if Path(results_dir).resolve() not in target.parents:
        raise ValueError("intent review tables may only be written under bench/results/")
    counts = agreement(rows)
    lines = [
        f"# Revisão do juiz de intenção: {set_name} ({title})",
        "",
        "Privado: contém texto falado. Não copiar para ficheiros versionados.",
        "",
        REVIEW_RULE,
        "",
        f"Juiz e revisão concordam em {counts['agree']} de {counts['compared']}; "
        f"juiz sim e revisão não: {counts['judge_yes_review_no']}; juiz não e revisão sim: {counts['judge_no_review_yes']}.",
        "",
        "Para validar: leia cada linha e escreva `sim` ou `não` na coluna Sponsor, pelo menos nas "
        "linhas em que o juiz e a revisão discordam.",
        "",
        "| id | referência | transcrição | juiz | razão do juiz | revisão | razão da revisão | Sponsor |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        review = row.review
        lines.append(
            f"| {row.id} | {_cell(row.reference)} | {_cell(row.hypothesis)} | {_yes_no(row.judge_preserved)} | "
            f"{_cell(row.judge_reason)} | {_yes_no(review.preserved if review else None)} | "
            f"{_cell(review.reason) if review else 'sem revisão'} |  |"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target


def read_stage_rows(run_dir: Path, set_name: str, stage: str) -> list[dict] | None:
    """A set's per-take rows of one stage from a pipeline run, or None when absent."""
    path = Path(run_dir) / set_name / f"{stage}.json"
    if not path.is_file():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        rows = None
    if not isinstance(rows, list) or not all(isinstance(r, dict) and {"id", "clean", "hypothesis"} <= r.keys() for r in rows):
        raise ValueError(f"{set_name}/{stage}.json is not a pipeline output")
    return rows


def dataset_names() -> dict[str, dict[str, tuple[str, ...]]]:
    """Project names spoken in each take, from the local datasets (for the slot rule)."""
    from bench.dataset import load_dataset
    from bench.settings import load_settings

    settings = load_settings()
    return {name: {take.id: take.project_names for take in load_dataset(settings.for_set(name)).takes} for name in SETS}


def default_judge() -> tuple[Callable | None, str | None]:
    from bench.run import ollama_ready

    client = OllamaClient()
    unavailable, _ = ollama_ready(client)
    return (None if unavailable else IntentJudge(client).judge), unavailable


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f} %"


def main(
    argv: list[str] | None = None,
    *,
    judge_factory: Callable[[], tuple[Callable | None, str | None]] = default_judge,
    names_factory: Callable[[], dict[str, dict[str, tuple[str, ...]]]] = dataset_names,
    terms: Sequence[str] | None = None,
    results_dir: Path = RESULTS_DIR,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(prog="bench.intent", description="Review the intent judge against a manual assessment.")
    parser.add_argument("--run", type=Path, required=True, help="a bench/results/pipeline/<run> folder")
    parser.add_argument("--stage", default="streamed", help="stage outputs to review (default streamed)")
    parser.add_argument("--reviews", type=Path, required=True, help="manual verdicts JSON under bench/results/")
    parser.add_argument("--rejudge", action="store_true", help="judge the saved outputs again with the current judge")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out_dir = Path(results_dir) / "intent-review"
    try:
        reviews = load_reviews(args.reviews)
        stage_rows = {name: read_stage_rows(args.run, name, args.stage) for name in SETS}
    except (ValueError, OSError) as exc:
        out(f"error: {exc}")
        return 2
    judge = None
    if args.rejudge:
        judge, unavailable = judge_factory()
        if judge is None:
            out(f"error: {unavailable}")
            return 2
        names = names_factory()
        if terms is None:
            from bench.metrics import load_terms

            terms = load_terms()
    label = f"{Path(args.run).name}, {args.stage}" + (", julgado de novo com o juiz atual" if args.rejudge else "")
    for set_name in SETS:
        rows = stage_rows[set_name]
        if rows is None:
            out(f"{set_name}: no {args.stage} outputs in the run")
            continue
        judged = None
        if judge is not None:
            items = [ItemResult(r["id"], r["clean"], r["hypothesis"], names.get(set_name, {}).get(r["id"], ())) for r in rows]
            try:
                judged = {row.id: row for row in evaluate(items, judge, terms)}
            except OllamaError as exc:
                out(f"error: {exc}")
                return 2
        joined = review_rows(rows, reviews.get(set_name, {}), judged)
        suffix = "-rejudged" if args.rejudge else ""
        write_review_table(out_dir / f"{Path(args.run).name}-{args.stage}-{set_name}{suffix}.md", set_name, label, joined, results_dir)
        counts = agreement(joined)
        preserved = [row.judge_preserved for row in joined if row.judge_preserved is not None]
        out(
            f"{set_name}: n {counts['n']}, compared {counts['compared']}, agree {counts['agree']} ({_pct(counts['agreement'])}), "
            f"judge yes/review no {counts['judge_yes_review_no']}, judge no/review yes {counts['judge_no_review_yes']}, "
            f"judge preserved {_pct(sum(preserved) / len(preserved) if preserved else None)}, "
            f"review preserved {_pct(counts['review_preserved'] / counts['compared'] if counts['compared'] else None)}"
        )
    out("tables written under bench/results/intent-review/ (private)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
