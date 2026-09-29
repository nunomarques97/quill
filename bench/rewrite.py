"""Command-mode measurement on the Sponsor's spoken rewrite instructions.

Usage:
    py -3.12 -m bench.rewrite --dry-run
    .venv\\Scripts\\python -m bench.rewrite [--no-judge] [--vocabulary FILE] [--summary PATH]

The ``rewrite`` set pairs each spoken instruction with an invented selected
text (``bench/dictation/guiao-reescrita-pt.md``, recorded with
``py -3.12 -m bench.record --set rewrite``). Each recorded take goes through
the product path: the instruction is replayed through ``quill.streaming``
(the product's engine model and tuning, on the deterministic audio-time
schedule, with the product hints), and the transcribed instruction and the
selection go to ``quill.command.CommandRewriter`` with the product's Ollama
model, strict prompt and validation (``local/quill.toml`` or the example).

Measured:

- instruction WER against the script instruction without fillers;
- rewrite correctness. The deterministic checks: the product accepted the
  rewrite (``valid``), the target language by function words (``língua``;
  a text without function words, such as a list of nouns, is not refused),
  every ``preservar`` term present, and per ``caso`` a text with at most
  80 % of the words for ``encurtar``, two or more hyphen items for ``lista``
  and a changed text otherwise. The local judge (qwen3:8b through Ollama,
  JSON yes/no) reads the script instruction, the selection and the rewrite.
  A take is correct when the checks pass and the judge says yes; without the
  judge, correctness is reported as not judged;
- second attempts: how many takes the product asked the model twice, and why
  (codes of ``quill.command``: unchanged, invalid, not shorter, not a list,
  a kept term changed); the rewrite time includes both calls;
- latency p50/p95: release to final instruction text (streamed), the model's
  rewrite, and their sum. The command's two clipboard copies and the typing
  need the desktop and are not part of it (the app logs them per command).

Spoken and script text goes only under ``bench/results/rewrite/<run>/``: the
per-take JSON and a human-checkable table with an empty Sponsor column. The
summary JSON holds aggregates only (also under bench/results/, timings
included) and is refused when a script text, a transcription or a name
would leak into it. Nothing spoken is printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from bench.dataset import Dataset, DatasetError, Take, clean_text, load_dataset, parse_markup, parse_script
from bench.metrics import corpus_wer, percentile_nearest_rank, write_summary
from bench.normalize import normalize_words
from bench.settings import RESULTS_DIR, Settings, SettingsError, load_settings
from quill import command
from quill.command import CommandRewriter, same_text

SET_NAME = "rewrite"
SUMMARY_SCHEMA = 1
LANGUAGES = ("pt", "en")
NONE_MARK = "—"
SHORTEN = "encurtar"
LIST = "lista"
CORRECT = "corrigir"  # its preservar terms are the corrected words, absent from the selection
MAX_SHORTEN_RATIO = 0.8
MIN_LIST_ITEMS = 2
WARMUP_SELECTION = "Texto de aquecimento sem importância nenhuma."
WARMUP_INSTRUCTION = "põe isto mais formal"

# Extra columns of the rewrite script (the script is written in Portuguese).
REWRITE_COLUMNS = {"id": "id", "caso": "kind", "frase": "instruction", "seleção": "selection", "língua": "language",
                   "preservar": "preserve"}

# Function words that tell the two languages apart (words present in both are left out).
PT_WORDS = frozenset("""o os as de do da dos das que não é para com um uma em no na nos nas se por mais isto
ao às foi está estão são também mas ou já muito seu sua há até pelo pela quando depois ainda porque
este esta esse essa eu tu ele ela nós vocês eles elas me te lhe""".split())
EN_WORDS = frozenset("""the an of to and is are in on for with that this it be by you we they he she
after before not can will was were have has from at or but please your our their its if then there
what which who when do does did""".split())
_ITEM = re.compile(r"^\s*[-–•]\s+\S")


class RewriteScriptError(DatasetError):
    """The rewrite script has a missing column or an invalid value."""


@dataclass(frozen=True)
class RewriteRow:
    """One script row: the kind, the spoken instruction and the checks of its rewrite."""

    id: str
    kind: str
    instruction: str = field(repr=False)
    selection: str = field(repr=False)
    language: str = "pt"
    preserve: tuple[str, ...] = field(default=(), repr=False)


def parse_rewrite_script(text: str, id_prefix: str = "rw") -> list[RewriteRow]:
    """The rewrite script's rows, with the columns ``parse_script`` does not keep."""
    ids = [row.id for row in parse_script(text, id_prefix, markup=True)]  # ids, duplicates and markup
    header: list[str] | None = None
    rows: list[RewriteRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            if cells and cells[0].casefold() == "id" and "frase" in [c.casefold() for c in cells]:
                header = [REWRITE_COLUMNS.get(c.casefold(), c.casefold()) for c in cells]
                missing = sorted(set(REWRITE_COLUMNS.values()) - set(header))
                if missing:
                    raise RewriteScriptError("rewrite script: missing columns " + ", ".join(missing))
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        record = dict(zip(header, cells))
        take_id = record["id"]
        if not record["selection"] or record["selection"] == NONE_MARK:
            raise RewriteScriptError(f"{take_id}: empty selection")
        if not record["kind"]:
            raise RewriteScriptError(f"{take_id}: empty caso")
        if record["language"] not in LANGUAGES:
            raise RewriteScriptError(f"{take_id}: língua must be pt or en")
        preserve = () if record["preserve"] in ("", NONE_MARK) else tuple(
            term.strip() for term in record["preserve"].split(";") if term.strip())
        for term in preserve:
            if record["kind"] != CORRECT and not contains_term(record["selection"], term):
                raise RewriteScriptError(f"{take_id}: a preservar term is not in the selection")
        rows.append(RewriteRow(take_id, record["kind"], record["instruction"], record["selection"], record["language"],
                               preserve))
    if [row.id for row in rows] != ids:
        raise RewriteScriptError("rewrite script: rows could not be read")
    return rows


def load_rewrite_rows(settings: Settings) -> list[RewriteRow]:
    if not settings.recording_script.is_file():
        raise DatasetError(f"{settings.name}: recording script not found")
    return parse_rewrite_script(settings.recording_script.read_text(encoding="utf-8"), settings.id_prefix)


# ---------------------------------------------------------------- deterministic checks


def detect_language(text: str) -> str:
    """"pt" or "en" by function words; "?" when neither wins."""
    words = [word.strip(".,;:!?\"'«»“”()-").casefold() for word in text.split()]
    pt = sum(word in PT_WORDS for word in words)
    en = sum(word in EN_WORDS for word in words)
    if pt == en:
        return "?"
    return "pt" if pt > en else "en"


def contains_term(text: str, term: str) -> bool:
    """``term`` as whole words in ``text``, ignoring case and spacing."""
    needle = r"\s+".join(re.escape(part) for part in term.casefold().split())
    return bool(needle) and re.search(rf"(?<!\w){needle}(?!\w)", text.casefold()) is not None


def rewrite_checks(row: RewriteRow, text: str) -> tuple[dict[str, bool], tuple[str, ...]]:
    """Deterministic checks of an accepted rewrite and the preserved terms it lost."""
    missing = tuple(term for term in row.preserve if not contains_term(text, term))
    # Undecided (no function words, such as a list of nouns) is not a wrong language.
    checks = {"language": detect_language(text) in (row.language, "?"), "preserved": not missing}
    if row.kind == SHORTEN:
        checks["shorter"] = len(text.split()) <= MAX_SHORTEN_RATIO * len(row.selection.split())
    elif row.kind == LIST:
        checks["list"] = sum(bool(_ITEM.match(line)) for line in text.splitlines()) >= MIN_LIST_ITEMS
    else:
        checks["changed"] = not same_text(text, row.selection)
    return checks, missing


# ---------------------------------------------------------------- judge

JUDGE_SYSTEM = (
    "You check a text rewrite. You get an INSTRUCTION spoken in European Portuguese, the ORIGINAL text and the "
    "REWRITE. Answer yes only if the rewrite does what the instruction asks, keeps the meaning, facts, names and "
    "numbers of the original unless the instruction asks to change them, adds no new information, and contains "
    "only the rewritten text, without comments. Portuguese means European Portuguese. Reply in JSON with the "
    "fields ok (yes or no) and reason (at most 15 English words)."
)
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "string", "enum": ["yes", "no"]}, "reason": {"type": "string"}},
    "required": ["ok", "reason"],
}


class JudgeError(Exception):
    """The judge gave no usable verdict. Carries no text."""


class RewriteJudge:
    """``client`` is a ``bench.cleanup.OllamaClient`` (or a fake with ``chat(..., schema=)``)."""

    def __init__(self, client: object, model: str) -> None:
        self.client = client
        self.model = model

    def judge(self, instruction: str, selection: str, rewrite: str) -> tuple[bool, str]:
        user = f"INSTRUCTION: {instruction}\nORIGINAL:\n{selection}\nREWRITE:\n{rewrite}"
        try:
            result = self.client.chat(self.model, JUDGE_SYSTEM, user, schema=JUDGE_SCHEMA)
            data = json.loads(result.content)
        except json.JSONDecodeError:
            raise JudgeError("rewrite judge returned invalid JSON") from None
        except Exception as exc:  # noqa: BLE001 - Ollama down or failing: no verdict
            raise JudgeError(f"rewrite judge unavailable ({type(exc).__name__})") from None
        verdict = data.get("ok") if isinstance(data, dict) else None
        if verdict not in ("yes", "no"):
            raise JudgeError("rewrite judge returned no yes/no verdict")
        reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
        return verdict == "yes", " ".join(reason.split())[:200]


# ---------------------------------------------------------------- measurement


class RecordingClient:
    """Wraps the product's Ollama client and keeps the last reply, for the human table only."""

    def __init__(self, client: object) -> None:
        self.client = client
        self.last = ""

    def chat(self, model: str, system: str, user: str, max_tokens: int | None = None,
             history: Sequence[tuple[str, str]] = ()) -> object:
        self.last = ""
        reply = self.client.chat(model, system, user, max_tokens=max_tokens, history=history)
        self.last = reply.content
        return reply


@dataclass(frozen=True)
class TakeResult:
    """One take through command mode. Holds spoken and script text: never printed or summarized."""

    id: str
    kind: str
    language: str
    instruction: str = field(repr=False)  # the script instruction without fillers
    heard: str = field(repr=False)  # the streamed transcription
    selection: str = field(repr=False)
    reply: str = field(repr=False)  # the model's raw reply
    rewrite: str | None = field(repr=False)  # what the product would type
    reason: str = ""
    detail: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    missing: tuple[str, ...] = field(default=(), repr=False)
    judged: bool | None = None
    judge_reason: str = field(default="", repr=False)
    asr_s: float = 0.0
    rewrite_s: float = 0.0
    attempts: int = 1
    retry: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.reason == command.REWRITTEN

    @property
    def checks_passed(self) -> bool:
        return self.valid and all(self.checks.values())

    @property
    def correct(self) -> bool | None:
        """Checks and judge; None when the judge gave no verdict on a take that passed the checks."""
        if not self.checks_passed:
            return False
        return self.judged


def measure(
    takes: Sequence[Take],
    rows: dict[str, RewriteRow],
    transcripts: Sequence[tuple[str, float]],
    rewriter: CommandRewriter,
    judge: Callable[[str, str, str], tuple[bool, str]] | None,
    *,
    log: Callable[[str], None] = print,
) -> list[TakeResult]:
    """Rewrite each take's selection with its transcribed instruction, then check and judge the rewrite."""
    recorder = rewriter.client if isinstance(rewriter.client, RecordingClient) else None
    results = []
    judge_failed = False
    for take, (heard, asr_s) in zip(takes, transcripts, strict=True):
        row = rows[take.id]
        outcome = rewriter.rewrite(row.selection, heard)
        reply = recorder.last if recorder is not None else ""
        checks: dict[str, bool] = {}
        missing: tuple[str, ...] = ()
        judged, judge_reason = None, ""
        if outcome.ok:
            checks, missing = rewrite_checks(row, outcome.text.strip())
            if judge is not None and not judge_failed:
                try:
                    judged, judge_reason = judge(take.clean, row.selection, outcome.text.strip())
                except JudgeError as exc:
                    judge_failed = True  # never mix judged and unjudged takes
                    log(f"judge stopped: {exc}")
        results.append(TakeResult(
            take.id, row.kind, row.language, take.clean, heard, row.selection, reply, outcome.text, outcome.reason,
            outcome.detail, checks, missing, judged, judge_reason, asr_s, outcome.seconds, outcome.attempts,
            outcome.retry))
    if judge_failed:
        results = [replace(r, judged=None, judge_reason="") for r in results]
    return results


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def _seconds(values: Sequence[float], percent: float) -> float | None:
    value = percentile_nearest_rank(values, percent)
    return None if value is None else round(value, 3)


def aggregate(results: Sequence[TakeResult], dataset: Dataset | None = None) -> dict:
    """Counts, rates and latency percentiles; never text."""
    n = len(results)
    valid = [r for r in results if r.valid]
    check_names = sorted({name for r in valid for name in r.checks})
    judged = [r for r in results if r.checks_passed and r.judged is not None]
    judge_complete = all(r.judged is not None for r in results if r.checks_passed)
    correct = sum(1 for r in results if r.correct is True)
    wer = corpus_wer((r.instruction, r.heard) for r in results)
    by_kind: dict[str, dict] = {}
    for kind in sorted({r.kind for r in results}):
        group = [r for r in results if r.kind == kind]
        by_kind[kind] = {
            "takes": len(group),
            "checks_passed": sum(r.checks_passed for r in group),
            "correct": sum(1 for r in group if r.correct is True) if judge_complete else None,
        }
    totals = [r.asr_s + r.rewrite_s for r in results]
    summary: dict = {
        "takes": n,
        "instruction_wer": None if wer is None else round(wer, 4),
        "instruction_exact": sum(normalize_words(r.instruction) == normalize_words(r.heard) for r in results),
        "valid": len(valid),
        "valid_rate": _rate(len(valid), n),
        "reasons": dict(sorted(Counter(r.reason for r in results).items())),
        "invalid_details": dict(sorted(Counter(r.detail for r in results if r.reason == command.INVALID_REWRITE).items())),
        "checks": {name: sum(1 for r in valid if r.checks.get(name) is True) for name in check_names},
        "checks_applicable": {name: sum(1 for r in valid if name in r.checks) for name in check_names},
        "checks_passed": sum(r.checks_passed for r in results),
        "checks_passed_rate": _rate(sum(r.checks_passed for r in results), n),
        "judged": len(judged) if judge_complete else 0,
        "judge_yes": sum(1 for r in judged if r.judged) if judge_complete else None,
        "correct": correct if judge_complete else None,
        "correct_rate": _rate(correct, n) if judge_complete else None,
        "by_kind": by_kind,
        "second_attempts": sum(r.attempts > 1 for r in results),
        "second_attempt_reasons": dict(sorted(Counter(code for r in results for code in r.retry).items())),
        "latency": {
            "instruction_p50_s": _seconds([r.asr_s for r in results], 50),
            "instruction_p95_s": _seconds([r.asr_s for r in results], 95),
            "rewrite_p50_s": _seconds([r.rewrite_s for r in results], 50),
            "rewrite_p95_s": _seconds([r.rewrite_s for r in results], 95),
            "total_p50_s": _seconds(totals, 50),
            "total_p95_s": _seconds(totals, 95),
        },
    }
    if dataset is not None:
        summary["dataset"] = {"script_rows": dataset.script_rows, "recorded": len(dataset.takes),
                              "pending": len(dataset.pending), "invalid": len(dataset.invalid)}
    return summary


# ---------------------------------------------------------------- private outputs


def _inside_results(path: Path, results_dir: Path) -> Path:
    target = Path(path).resolve()
    if Path(results_dir).resolve() not in target.parents:
        raise ValueError("rewrite outputs with spoken text may only be written under bench/results/")
    return target


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|") or NONE_MARK


def write_private(run_dir: Path, results: Sequence[TakeResult], results_dir: Path = RESULTS_DIR) -> tuple[Path, Path]:
    """Per-take JSON and the human-checkable table, only under bench/results/."""
    run_dir = _inside_results(Path(run_dir) / "takes.json", results_dir).parent
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [{
        "id": r.id, "kind": r.kind, "language": r.language, "instruction": r.instruction, "heard": r.heard,
        "selection": r.selection, "reply": r.reply, "rewrite": r.rewrite, "reason": r.reason, "detail": r.detail,
        "checks": r.checks, "missing": list(r.missing), "judge": r.judged, "judge_reason": r.judge_reason,
        "correct": r.correct, "asr_s": round(r.asr_s, 3), "rewrite_s": round(r.rewrite_s, 3),
        "attempts": r.attempts, "retry": list(r.retry),
    } for r in results]
    takes_path = run_dir / "takes.json"
    takes_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Modo comando: verificação manual das reescritas",
        "",
        "Para cada linha, leia a instrução, a seleção e a reescrita, e escreva `sim` na coluna Sponsor quando a",
        "reescrita faz o que a instrução pede sem mudar o sentido, ou `não` com o motivo. `válida` é a validação",
        "do produto; `verificações` são as regras determinísticas; `juiz` é o veredicto do modelo local;",
        "`tentativas` diz se o produto pediu uma segunda resposta ao modelo e porquê.",
        "Este ficheiro tem texto falado: fica só em bench/results/ (ignorado pelo Git).",
        "",
        "| id | caso | instrução | ouvido | seleção | reescrita | válida | verificações | juiz | tentativas | correta "
        "| Sponsor |",
        "|----|------|-----------|--------|---------|-----------|--------|--------------|------|------------|---------"
        "|---------|",
    ]
    for r in results:
        checks = ", ".join(f"{name} {'ok' if ok else 'falhou'}" for name, ok in r.checks.items())
        if r.missing:
            checks += "; em falta: " + "; ".join(r.missing)
        judge = NONE_MARK if r.judged is None else ("sim" if r.judged else "não") + (
            f" ({r.judge_reason})" if r.judge_reason else "")
        valid = "sim" if r.valid else f"não ({r.reason}{': ' + r.detail if r.detail else ''})"
        correct = {True: "sim", False: "não", None: NONE_MARK}[r.correct]
        shown = r.rewrite if r.rewrite is not None else r.reply
        attempts = str(r.attempts) + (f" ({', '.join(r.retry)})" if r.retry else "")
        lines.append("| " + " | ".join(_cell(value) for value in (
            r.id, r.kind, r.instruction, r.heard, r.selection, shown, valid, checks, judge, attempts, correct))
            + " |  |")
    table_path = run_dir / "table.md"
    table_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return takes_path, table_path


def spoken_texts(dataset: Dataset, rows: Sequence[RewriteRow], results: Sequence[TakeResult] = ()) -> list[str]:
    """Every text the summary must not contain: script instructions and selections, transcriptions, rewrites."""
    texts = list(dataset.reference_texts())
    for row in rows:
        texts.append(clean_text(parse_markup(row.instruction, row.id)))
        texts.append(row.selection)
    for r in results:
        texts += [r.heard, r.reply] + ([r.rewrite] if r.rewrite else [])
    return texts


# ---------------------------------------------------------------- defaults (GPU and Ollama)


Streamer = Callable[[Sequence[Take]], list[tuple[str, float]]]


def default_streamer(hints: Sequence[str]) -> tuple[Streamer, dict]:
    """The product streaming path, with its engine model and tuning; ``stream.close`` frees the model."""
    from dataclasses import asdict

    from bench.pipeline import ENGINE_COMPUTE, STREAM_MODEL
    from bench.streaming import stream_takes
    from quill.streaming import options_for
    from quill.whisper import Whisper

    options = options_for(STREAM_MODEL)
    model = Whisper(STREAM_MODEL, compute_type=ENGINE_COMPUTE)

    def stream(takes: Sequence[Take]) -> list[tuple[str, float]]:
        return stream_takes(model, takes, list(hints), options)

    stream.close = model.close
    return stream, {"model": STREAM_MODEL, **asdict(options)}


def default_rewriter(terms: Sequence[str] = ()) -> tuple[CommandRewriter, str]:
    """The product's command-mode rewriter (its Ollama URL and model from quill's config) with the kept terms."""
    from quill.config import load_config
    from quill.ollama import OllamaClient

    config = load_config()
    client = RecordingClient(OllamaClient(config.ollama_url, timeout_s=command.COMMAND_TIMEOUT_S))
    kept = tuple(terms)
    return CommandRewriter(client, config.ollama_model, terms=lambda: kept), config.ollama_model


def default_judge() -> tuple[Callable | None, str | None]:
    from bench.cleanup import CLEANUP_MODEL, OllamaClient
    from bench.run import ollama_ready

    client = OllamaClient()
    unavailable, _ = ollama_ready(client)
    return (None if unavailable else RewriteJudge(client, CLEANUP_MODEL).judge), unavailable


# ---------------------------------------------------------------- commands


def dry_run(settings: Settings, out: Callable[[str], None] = print) -> int:
    """Counts only; opens no audio, model or microphone."""
    rewrite = settings.for_set(SET_NAME)
    try:
        rows = load_rewrite_rows(rewrite)
        dataset = load_dataset(rewrite)
    except DatasetError as exc:
        out(f"error: {exc}")
        return 2
    kinds = Counter(row.kind for row in rows)
    languages = Counter(row.language for row in rows)
    out(f"rewrite script: {len(rows)} rows; kinds " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
        + "; languages " + ", ".join(f"{k} {v}" for k, v in sorted(languages.items())))
    out(f"recorded {len(dataset.takes)}, pending {len(dataset.pending)}, invalid {len(dataset.invalid)}, "
        f"discarded {dataset.discarded}; minimum {rewrite.minimum_takes}")
    for invalid in dataset.invalid:
        out(f"  invalid {invalid.id}: {invalid.reason}")
    return 0


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def report_lines(summary: dict) -> list[str]:
    latency = summary["latency"]
    correct = summary["correct_rate"]
    return [
        f"takes {summary['takes']}; instruction WER {_pct(summary['instruction_wer'])}",
        f"valid {summary['valid']}/{summary['takes']}; checks passed {summary['checks_passed']}/{summary['takes']}; "
        + (f"correct {summary['correct']}/{summary['takes']} ({_pct(correct)})" if correct is not None
           else "correct: not judged"),
        f"second attempts {summary.get('second_attempts', 0)}/{summary['takes']}",
        "latency p50/p95 s: instruction {} / {}, rewrite {} / {}, total {} / {}".format(
            latency["instruction_p50_s"], latency["instruction_p95_s"], latency["rewrite_p50_s"],
            latency["rewrite_p95_s"], latency["total_p50_s"], latency["total_p95_s"]),
    ]


def main(
    argv: list[str] | None = None,
    *,
    streamer_factory: Callable[[Sequence[str]], tuple[Streamer, dict]] = default_streamer,
    rewriter_factory: Callable[[Sequence[str]], tuple[CommandRewriter, str]] = default_rewriter,
    judge_factory: Callable[[], tuple[Callable | None, str | None]] = default_judge,
    results_dir: Path = RESULTS_DIR,
    clock: Callable[[], float] = time.perf_counter,
    out: Callable[[str], None] = print,
) -> int:
    from bench.metrics import load_terms
    from bench.pipeline import product_hints
    from quill.vocabulary import LOCAL_VOCABULARY, VocabularyError, hint_list, load_vocabulary

    parser = argparse.ArgumentParser(prog="bench.rewrite", description="Command-mode measurement on spoken instructions.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--dry-run", action="store_true", help="script and recording counts only; no GPU")
    parser.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    parser.add_argument("--vocabulary", type=Path, default=LOCAL_VOCABULARY,
                        help="personal vocabulary for the hints (default local/vocabulary.toml; missing is empty)")
    parser.add_argument("--summary", type=Path, default=None,
                        help="aggregate summary JSON (default bench/results/rewrite/<run>/summary.json)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        settings = load_settings(args.config)
        if args.dry_run:
            return dry_run(settings, out)
        rewrite = settings.for_set(SET_NAME)
        rows = load_rewrite_rows(rewrite)
        dataset = load_dataset(rewrite)
        vocabulary = load_vocabulary(args.vocabulary)
    except (SettingsError, DatasetError, VocabularyError) as exc:
        out(f"error: {exc}")
        return 2
    if len(dataset.takes) < rewrite.minimum_takes:
        out(f"error: {len(dataset.takes)} rewrite takes recorded, at least {rewrite.minimum_takes} needed "
            "(py -3.12 -m bench.record --set rewrite)")
        return 2
    by_id = {row.id: row for row in rows}
    hints = product_hints(vocabulary, [], load_terms()).vocabulary()
    kept_terms = hint_list(vocabulary, (), load_terms())  # the terms the app's rewriter keeps

    from bench.engines.base import EngineError, EngineUnavailable

    run_dir = Path(results_dir) / "rewrite" / time.strftime("%Y%m%d-%H%M%S")
    streamer = None
    try:
        streamer, stream_options = streamer_factory(hints)
        streamer(dataset.takes[:1])  # warm-up: loads the model and fills the caches
        transcripts = streamer(dataset.takes)
        rewriter, model = rewriter_factory(kept_terms)
        warm = rewriter.rewrite(WARMUP_SELECTION, WARMUP_INSTRUCTION)  # loads the Ollama model
        if warm.reason == command.OLLAMA_UNAVAILABLE:
            out("error: Ollama unavailable for the rewrite model")
            return 2
        judge, unavailable = (None, "disabled by --no-judge") if args.no_judge else judge_factory()
        if unavailable:
            out(f"judge: {unavailable}")
        results = measure(dataset.takes, by_id, transcripts, rewriter, judge, log=out)
    except (EngineUnavailable, EngineError) as exc:
        out(f"error: {' '.join(str(exc).split())[:200]}")
        return 2
    finally:
        close = getattr(streamer, "close", None)
        if close is not None:
            close()

    summary = {"schema": SUMMARY_SCHEMA, "kind": "rewrite", "engine": stream_options, "rewrite_model": model,
               "judge": None if judge is None else "local", **aggregate(results, dataset)}
    try:
        write_private(run_dir, results, results_dir)
        names = [entry.text for entry in vocabulary.names]
        target = args.summary or run_dir / "summary.json"
        write_summary(_inside_results(target, results_dir), summary, spoken_texts(dataset, rows, results), names)
    except ValueError as exc:
        out(f"error: {exc}")
        return 1
    for line in report_lines(summary):
        out(line)
    out("per-take outputs and the manual table under bench/results/rewrite/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
