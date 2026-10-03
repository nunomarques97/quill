"""Mouse 5 measurement on the Sponsor's spoken Claude Code prompts: project context and prompt enrichment.

Usage:
    py -3.12 -m bench.prompts --dry-run [--set all|prompts|dictation] [--require]
    .venv\\Scripts\\python -m bench.prompts [--set all|prompts|dictation] [--summary PATH] [--require]
    py -3.12 -m bench.prompts --check SUMMARY

Two sets are measured. ``prompts`` (``bench/dictation/guiao-prompts-pt.md``,
recorded with ``py -3.12 -m bench.record --set prompts``) holds spoken Claude
Code prompts of the Sponsor's projects, each with ``<termo-N>`` domain terms;
``[prompts.projects]`` and ``[prompts.terms]`` of the ignored
``local/bench.toml`` give the real names and terms, and each take's manifest
keeps what it was read with. ``dictation`` is the existing dictation set,
limited to its ``claude-code`` takes: it has no domain terms, so it measures
content words lost, invented content words and latency without new
recordings.

Each take goes through the product path. It is replayed through
``quill.streaming`` with the app's engine model (``engine_model`` of the
quill config), on the deterministic audio-time schedule, twice:

- before: with today's hints (``quill.vocabulary.whisper_hints`` of the
  personal vocabulary and the generic terms);
- after: with the hints the app now gives mouse 5 in that window
  (``quill.app.project_hints``: in Claude Code with a detected project and
  a pack, the project name and its pack terms within the same budget; any
  other take keeps today's hints and its first transcription). The app
  gives them once click-to-focus has named the window, a fraction of a
  second into the hold; here the session decodes with them from its start.

Each transcription then follows the same steps, and the summary reports
the "after" run with the "before" aggregates beside it. The final text goes through the app's own text pipeline
(``quill.app.TextPipeline``: cleanup, vocabulary, learned corrections,
profile and project detection) for the Claude Code panel of VS Code, whose
title is simulated in the project hub format ("<project> | Claude Code -
Visual Studio Code [Claude Code]"; no project part for a take without one).
The project's folder comes from the app's ``ProjectFolders`` and its context
pack from the app's ``ContextPacks`` (the cache under ``local/``). Then mouse
5 runs twice with the app's rewriter (``quill.autorewrite``, the Ollama model
of the quill config), exactly as the session asks it:

- today: the correction only, with the window-title project hint
  (``quill.autorewrite.project_hint``, which takes the whole left part of a
  hub title), no pack, no context mode and no enrichment;
- new: the detected project, its pack, context mode and the enrichment.

Measured, against targets that are never lowered here:

- domain-term errors (prompts set): occurrences of the take's real terms in
  its reference that the text lacks: after the text pipeline, after today's
  mouse 5 and after the new correction (the dictated part of the new prompt;
  the enrichment may not add a term outside its context part). Target: the
  new count at most half of today's;
- content words lost: reference content words the mouse 5 input had right
  (one minimum-cost alignment) that the output no longer has, counted as a
  multiset outside the enriched prompt's context part, so reordering into
  labelled parts is no loss. Target 0;
- invented content words, counted here independently of the product guard:
  content words of the output beyond their count in the input that are
  neither a pack word nor a structure word (the labels of
  ``quill.enrich.LABELS`` and "projeto"/"project"); an added number always
  counts. Pack words the enrichment used outside the context part (beyond
  the input and the corrected text: a requirement taken from the pack) are
  counted too, as ``pack_outside_context``. Target 0 for both;
- latency p50/p95 before and after the project hints: transcription,
  today's correction, and the new pack
  lookup, correction, enrichment and their total. The model calls get a
  measurement timeout of ``BENCH_TIMEOUT_S`` so the counts do not depend on
  the machine's load; calls slower than the product timeouts are counted.

Spoken text goes only under ``bench/results/prompts/<run>/``: the per-take
JSON and ``exemplos.md``, the before/after enrichment examples for the
Sponsor to judge. The summary holds aggregates only and is refused when a
spoken phrase, a transcription, a name or a real domain term would leak into
it. ``--dry-run`` reads the script, the manifests and the local
configuration and prints counts only: it never opens the microphone, the GPU
or Ollama. ``--require`` exits 1 when a target is unmet or the prompts set has
missing takes (with ``--dry-run``: missing takes only); ``--check SUMMARY``
applies the same checks to a saved summary.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bench.dataset import (PLACEHOLDER, TERM_PLACEHOLDER, Dataset, DatasetError, Take, load_dataset, parse_script,
                           valid_term)
from bench.metrics import percentile_nearest_rank, term_recall, write_summary
from bench.normalize import normalize_words
from bench.settings import REPO_ROOT, RESULTS_DIR, Settings, SettingsError, load_settings
from quill import autorewrite, enrich
from quill.profiles import CLAUDE_CODE, WindowInfo
from quill.whisper import distinctive_term

SET_NAME = "prompts"
DICTATION = "dictation"
SETS = (SET_NAME, DICTATION)
SUMMARY_SCHEMA = 1
DEFAULT_SUMMARY = REPO_ROOT / "docs" / "research" / "prompts-summary.json"
# Targets of the Phase 6 goal. Never lowered here.
TARGET_TERM_ERROR_RATIO = 0.5  # new domain-term errors at most half of today's
TARGET_LOST = 0
TARGET_INVENTED = 0

CASES = ("termo", "restrição", "números")
NONE_MARK = "—"
# The session's reason for an empty final text (quill.session.NO_SPEECH): mouse 5 types nothing.
NO_SPEECH = "no_speech"
BENCH_TIMEOUT_S = 120.0
# The simulated target: the Claude Code panel of VS Code, titled in the project hub format.
VSCODE_PROCESS = "Code.exe"
VSCODE_CLASS = "Chrome_WidgetWin_1"
EDITOR = "Claude Code"
PANEL_MARKER = "Visual Studio Code [Claude Code]"

TOKEN = re.compile(r"[^\W_]+")


class PromptScriptError(DatasetError):
    """The prompts script is malformed (messages name the id, never the text)."""


@dataclass(frozen=True)
class PromptRow:
    id: str
    case: str
    intent: str
    text: str = field(repr=False)  # the spoken prompt, with <projeto-N> and <termo-N> placeholders
    project: str = field(default="", repr=False)  # its one <projeto-N>
    terms: tuple[str, ...] = field(default=(), repr=False)  # its <termo-N>, in phrase order


def _column(text: str, name: str) -> dict[str, str]:
    """id -> the cell of column ``name`` of the script table ('' when the column is missing)."""
    header: list[str] | None = None
    cells_by_id: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            if cells and cells[0].casefold() == "id" and "frase" in [c.casefold() for c in cells]:
                header = [c.casefold() for c in cells]
            continue
        if all(set(cell) <= set("-: ") for cell in cells) or len(cells) != len(header):
            continue
        if name in header:
            cells_by_id[cells[0]] = cells[header.index(name)]
    return cells_by_id


def parse_prompt_script(text: str, id_prefix: str = "pp") -> list[PromptRow]:
    """The rows of the prompts script, each with one project and at least one domain term checked."""
    listed = _column(text, "termos")
    rows = []
    for row in parse_script(text, id_prefix, markup=True):
        if row.case not in CASES:
            raise PromptScriptError(f"{row.id}: caso must be one of {', '.join(CASES)}")
        if row.style != CLAUDE_CODE:
            raise PromptScriptError(f"{row.id}: estilo must be {CLAUDE_CODE}")
        projects = PLACEHOLDER.findall(row.text)
        if len(set(projects)) != 1 or row.project != projects[0]:
            raise PromptScriptError(f"{row.id}: frase needs one <projeto-N>, repeated in projeto")
        terms = tuple(TERM_PLACEHOLDER.findall(row.text))
        if not terms:
            raise PromptScriptError(f"{row.id}: frase needs at least one <termo-N>")
        if [part.strip() for part in listed.get(row.id, "").split(",")] != list(terms):
            raise PromptScriptError(f"{row.id}: termos must list the <termo-N> of frase in order")
        rows.append(PromptRow(row.id, row.case, row.intent, row.text, row.project, terms))
    return rows


def load_prompt_rows(settings: Settings) -> list[PromptRow]:
    if not settings.recording_script.is_file():
        raise DatasetError(f"{settings.name}: recording script not found")
    return parse_prompt_script(settings.recording_script.read_text(encoding="utf-8"), settings.id_prefix)


def _numbered(placeholders: Iterable[str]) -> list[str]:
    return sorted(set(placeholders), key=lambda p: int(re.sub(r"\D", "", p)))


def recording_names(settings: Settings, rows: Sequence[PromptRow]) -> tuple[dict[str, str], dict[str, str]]:
    """(project names, domain terms) shown while recording; every placeholder of the script must be mapped."""
    projects = dict(settings.projects or ())
    terms = dict(settings.terms or ())
    missing_projects = [p for p in _numbered(row.project for row in rows) if p not in projects]
    missing_terms = [t for t in _numbered(t for row in rows for t in row.terms) if not valid_term(terms.get(t))]
    if missing_projects:
        raise DatasetError(f"prompts: {len(missing_projects)} placeholder(s) without a name: add them to "
                           "[prompts.projects] in local/bench.toml")
    if missing_terms:
        raise DatasetError(f"prompts: {len(missing_terms)} placeholder(s) without a domain term: add them to "
                           "[prompts.terms] in local/bench.toml")
    return projects, terms


def _fill(text: str, mapping: dict[str, str]) -> str:
    """``text`` with every mapped placeholder replaced; unmapped ones stay."""
    def replace(match: re.Match[str]) -> str:
        value = mapping.get(match.group(0))
        return value.strip() if isinstance(value, str) and value.strip() else match.group(0)

    return TERM_PLACEHOLDER.sub(replace, PLACEHOLDER.sub(replace, text))


def private_text(settings: Settings | None) -> tuple[list[str], frozenset[str], frozenset[str]]:
    """(phrases raw and resolved, real project names, real domain terms) of the prompts set, for the privacy guard.

    Resolved with the local ``[prompts.projects]``/``[prompts.terms]`` mapping
    and with every take's manifest mapping; a partial mapping resolves what it
    names. Opens no audio.
    """
    if settings is None:
        return [], frozenset(), frozenset()
    rows = load_prompt_rows(settings)
    mappings = [{**dict(settings.projects or ()), **dict(settings.terms or ())}]
    if settings.manifest.is_file():
        from bench.dataset import load_manifest

        for entry in load_manifest(settings.manifest).values():
            if not isinstance(entry, dict):
                continue
            mapping = {}
            for key in ("projetos", "termos"):
                part = entry.get(key)
                if isinstance(part, dict):
                    mapping.update({k: v for k, v in part.items() if isinstance(k, str) and isinstance(v, str)})
            if mapping and mapping not in mappings:
                mappings.append(mapping)
    names = {value.strip() for mapping in mappings for key, value in mapping.items()
             if PLACEHOLDER.fullmatch(key) and value.strip()}
    terms = {value.strip() for mapping in mappings for key, value in mapping.items()
             if TERM_PLACEHOLDER.fullmatch(key) and value.strip()}
    texts = [row.text for row in rows]
    texts += [_fill(row.text, mapping) for row in rows for mapping in mappings]
    return list(dict.fromkeys(texts)), frozenset(names), frozenset(terms)


def claude_code_takes(dataset: Dataset) -> tuple[Take, ...]:
    """The dictation takes recorded for Claude Code (script ``estilo`` claude-code)."""
    return tuple(take for take in dataset.takes if take.style == CLAUDE_CODE)


# ---------------------------------------------------------------- the simulated window


def window_title(project: str) -> str:
    """The VS Code title of the Claude Code panel in the project hub format; no project part without one."""
    return f"{project} | {EDITOR} - {PANEL_MARKER}" if project else PANEL_MARKER


def window(project: str) -> WindowInfo:
    return WindowInfo(VSCODE_PROCESS, VSCODE_CLASS, window_title(project))


def take_project(take: Take) -> str:
    """The project name the take was read with ('' when it names none or several)."""
    names = set(take.project_names)
    return next(iter(names)) if len(names) == 1 else ""


# ---------------------------------------------------------------- words


def fold(word: str) -> str:
    """Case and accents folded."""
    decomposed = unicodedata.normalize("NFD", word.casefold())
    return unicodedata.normalize("NFC", "".join(ch for ch in decomposed if not unicodedata.combining(ch)))


def is_number(word: str) -> bool:
    return any(ch.isdigit() for ch in word)


def is_content(word: str) -> bool:
    """Every word except function words and hesitations (negation, condition and contrast are content)."""
    return is_number(word) or unicodedata.normalize("NFC", word).casefold() not in enrich.FLEXIBLE


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


_LABEL_PARTS = {fold(label): part for labels in enrich.LABELS.values() for part, label in labels.items()}
STRUCTURE = frozenset(
    fold(word) for labels in enrich.LABELS.values() for label in labels.values() for word in tokens(label)
) | frozenset(fold(word) for words in enrich.EXTRA_STRUCTURE.values() for word in words)


def outside_context(text: str) -> str:
    """``text`` without the lines of the enriched prompt's context part (a text without labels is kept whole)."""
    kept, part = [], None
    for line in text.split("\n"):
        match = enrich.LABEL_LINE.match(line.strip())
        if match and fold(match.group(1).strip()) in _LABEL_PARTS:
            part = _LABEL_PARTS[fold(match.group(1).strip())]
        if part != enrich.CONTEXT:
            kept.append(line)
    return "\n".join(kept)


def correct_positions(reference: Sequence[str], hypothesis: Sequence[str]) -> frozenset[int]:
    from bench.pipeline import correct_positions as aligned

    return aligned(reference, hypothesis)


def lost_words(reference: str, source: str, output: str) -> int:
    """Reference content words that ``source`` had right and ``output`` (outside its context part) lacks."""
    words = normalize_words(reference)
    right = correct_positions(words, normalize_words(source))
    needed = Counter(words[i] for i in right if is_content(words[i]))
    have = Counter(normalize_words(outside_context(output)))
    return sum(max(0, count - have[word]) for word, count in needed.items())


def pack_vocabulary(pack: object | None, project: str = "") -> frozenset[str]:
    """Folded words of the pack's summary and terms and of the project name."""
    summary = getattr(pack, "summary", "") if pack is not None else ""
    terms = getattr(pack, "terms", ()) if pack is not None else ()
    parts = [summary if isinstance(summary, str) else "", project]
    if not isinstance(terms, str):
        parts += [term for term in terms if isinstance(term, str)]
    return frozenset(fold(word) for part in parts for word in tokens(part))


def _content_counts(text: str) -> Counter:
    return Counter(fold(word) for word in tokens(text) if is_content(word))


def invented_words(source: str, output: str, pack: object | None = None, project: str = "",
                   corrected: str | None = None) -> tuple[int, int]:
    """(invented content words, pack words outside the context part) of ``output`` against its input ``source``.

    A content word beyond its count in ``source`` is invented unless it is a
    structure word or a pack word; a number beyond its count always is. Pack
    words beyond their count outside the context part are counted apart,
    against ``corrected`` too when given: a misheard word the correction
    replaced with a pack term is the dictation's, not a requirement from the
    pack (it is measured as a domain-term fix).
    """
    known = pack_vocabulary(pack, project)
    before = _content_counts(source)
    extra = _content_counts(output) - before
    invented = sum(count for word, count in extra.items()
                   if is_number(word) or (word not in STRUCTURE and word not in known))
    dictated = before | _content_counts(corrected) if corrected is not None else before
    outside = _content_counts(outside_context(output)) - dictated
    from_pack = sum(count for word, count in outside.items()
                    if not is_number(word) and word not in STRUCTURE and word in known)
    return invented, from_pack


def term_errors(reference: str, text: str, terms: Sequence[str]) -> tuple[int, int]:
    """(occurrences of ``terms`` in ``reference``, how many of them ``text`` lacks)."""
    counts = term_recall([(reference, text)], sorted(set(terms)))
    return counts.expected, counts.expected - counts.found


# ---------------------------------------------------------------- mouse 5, today and new


class _CapturingEnricher:
    """The rewriter's enricher, keeping the corrected text it was asked to enrich and its outcome."""

    def __init__(self, enricher: object) -> None:
        self.enricher = enricher
        self.text: str | None = None
        self.result: object | None = None

    def wants(self, text: str) -> bool:
        return self.enricher.wants(text)

    def enrich(self, text: str, **kwargs: object) -> object:
        self.text = text
        self.result = self.enricher.enrich(text, **kwargs)
        return self.result


@dataclass
class Window:
    """What the text pipeline's ``describe`` returns: the simulated window of the current take."""

    info: WindowInfo | None = None

    def describe(self, target: object) -> WindowInfo | None:
        return self.info


@dataclass
class Product:
    """The app's parts that mouse 5 uses, built from its config (fakes in tests).

    ``pipeline(raw, target)`` is a ``quill.app.TextPipeline`` whose
    ``describe`` is ``window.describe``; ``names`` are the personal-vocabulary
    names that today's title hint prefers; ``packs(folder)`` returns
    (pack or None, reason code). ``hold_start()`` runs at each take before
    its model calls, as the app does when a mouse 5 hold starts (None:
    nothing); ``model_loaded()`` says whether the model is in memory now,
    read before the first model call (None: unknown). ``hints_for(info)``
    returns the (decoding hints or None, reason code) the app gives mouse 5
    in the window ``info`` (``quill.app.project_hints``; None: no take gets
    project hints).
    """

    pipeline: Callable[[str, object], object]
    window: Window
    rewriter: object
    names: tuple[str, ...] = field(default=(), repr=False)
    packs: Callable[[object], tuple[object | None, str]] | None = None
    info: dict = field(default_factory=dict)
    clock: Callable[[], float] = time.perf_counter
    hold_start: Callable[[], None] | None = None
    model_loaded: Callable[[], bool | None] | None = None
    hints_for: Callable[[WindowInfo], tuple[object | None, str]] | None = None


@dataclass(frozen=True)
class TakeResult:
    """One take through both mouse 5 paths. Holds spoken text: never printed or summarized."""

    id: str
    set: str
    case: str
    reference: str = field(repr=False)
    heard: str = field(repr=False)
    source: str = field(default="", repr=False)  # the text pipeline's output: mouse 5's input
    today: str = field(default="", repr=False)
    corrected: str = field(default="", repr=False)
    final: str = field(default="", repr=False)
    project_found: bool = False
    pack: str = ""  # the pack lookup's reason code; "" without a folder
    pack_found: bool = False
    today_reason: str = ""
    reason: str = ""
    detail: str = ""
    enrichment: str = ""
    enrich_detail: str = ""
    term_occurrences: int = 0
    term_errors_pipeline: int = 0
    term_errors_today: int = 0
    term_errors_new: int = 0
    lost_today: int = 0
    lost_new: int = 0
    invented_today: int = 0
    invented_new: int = 0
    pack_outside_context: int = 0
    asr_s: float = 0.0
    today_s: float = 0.0
    pack_s: float = 0.0
    correction_s: float = 0.0
    enrich_s: float = 0.0
    enrich_tidied: int = 0  # removals quill.enrich.tidy made before the guard
    hints: str = ""  # the reason code of the take's decoding hints ("" before the project hints)

    @property
    def spoken(self) -> bool:
        return self.reason != NO_SPEECH

    @property
    def enrich_called(self) -> bool:
        return self.enrichment not in ("", enrich.SHORT)

    @property
    def total_s(self) -> float:
        return self.pack_s + self.correction_s + self.enrich_s


def _today_fields(result: TakeResult) -> dict:
    """Today's mouse 5 of a take (today's hints and correction), reused by its run with the project hints."""
    return {"today": result.today, "today_reason": result.today_reason, "term_errors_today": result.term_errors_today,
            "lost_today": result.lost_today, "invented_today": result.invented_today, "today_s": result.today_s}


def run_take(take: Take, set_name: str, case: str, heard: str, asr_s: float, product: Product,
             today_from: TakeResult | None = None, hints: str = "") -> TakeResult:
    """The take's final text through the app's text pipeline, then today's and the new mouse 5.

    With ``today_from`` (the take's run with today's hints), today's mouse 5
    is that run's, not asked again: only the new mouse 5 runs on ``heard``.
    """
    from quill.inject import Target

    project = take_project(take)
    info = window(project)
    product.window.info = info
    base = {"id": take.id, "set": set_name, "case": case, "reference": take.clean, "heard": heard, "asr_s": asr_s,
            "hints": hints}
    processed = product.pipeline(heard, Target(0, 0)) if heard.strip() else None
    if processed is None or not processed.text.strip():
        today_fields = _today_fields(today_from) if today_from is not None else {"today_reason": NO_SPEECH}
        return TakeResult(**base, **today_fields, reason=NO_SPEECH)
    source = processed.text
    audio_s = take.duration_s
    profile = processed.rewrite_profile or processed.profile
    rewriter = product.rewriter
    if product.hold_start is not None:
        product.hold_start()  # with no hold time: the model call follows at once
    if today_from is None:
        today = rewriter.rewrite(source, audio_s=audio_s, profile=profile, keep=processed.keep,
                                 project=autorewrite.project_hint(info, product.names), force=True, context=False)
    folder = processed.project_folder
    pack, pack_reason, pack_s = None, "", 0.0
    if folder is not None and product.packs is not None:
        started = product.clock()
        pack, pack_reason = product.packs(folder)
        pack_s = product.clock() - started
    capture = _CapturingEnricher(rewriter.enricher)
    rewriter.enricher = capture
    try:
        new = rewriter.rewrite(source, audio_s=audio_s, profile=profile, keep=processed.keep,
                               project=processed.project, force=True, pack=pack, enrich_prompt=True, context=True)
    finally:
        rewriter.enricher = capture.enricher
    corrected = capture.text if capture.text is not None else (source if not new.corrected else new.text)
    occurrences, pipeline_errors = term_errors(take.clean, source, take.terms)
    if today_from is not None:
        today_fields = _today_fields(today_from)
    else:
        today_fields = {"today": today.text, "today_reason": today.reason,
                        "term_errors_today": term_errors(take.clean, today.text, take.terms)[1],
                        "lost_today": lost_words(take.clean, source, today.text),
                        "invented_today": invented_words(source, today.text)[0], "today_s": today.seconds}
    invented_new, from_pack = invented_words(source, new.text, pack, processed.project, corrected)
    return TakeResult(
        **base, **today_fields, source=source, corrected=corrected, final=new.text,
        project_found=folder is not None, pack=pack_reason, pack_found=pack is not None,
        reason=new.reason, detail=new.detail, enrichment=new.enrichment,
        enrich_detail=new.enrich_detail, term_occurrences=occurrences, term_errors_pipeline=pipeline_errors,
        term_errors_new=term_errors(take.clean, corrected, take.terms)[1],
        lost_new=lost_words(take.clean, source, new.text),
        invented_new=invented_new, pack_outside_context=from_pack,
        pack_s=pack_s, correction_s=new.seconds, enrich_s=new.enrich_seconds,
        enrich_tidied=getattr(capture.result, "tidied", 0) or 0,
    )


def measure(set_name: str, takes: Sequence[Take], cases: dict[str, str], transcripts: Sequence[tuple[str, float]],
            product: Product) -> list[TakeResult]:
    return [run_take(take, set_name, cases.get(take.id, take.case), heard, asr_s, product)
            for take, (heard, asr_s) in zip(takes, transcripts, strict=True)]


def take_hints(takes: Sequence[Take], product: Product) -> list[tuple[object | None, str]]:
    """The (decoding hints or None, reason code) the app gives mouse 5 in each take's window."""
    if product.hints_for is None:
        return [(None, "") for _ in takes]
    return [product.hints_for(window(take_project(take))) for take in takes]


def measure_after(set_name: str, takes: Sequence[Take], cases: dict[str, str],
                  transcripts: Sequence[tuple[str, float]], reasons: Sequence[str],
                  before: Sequence[TakeResult], product: Product) -> list[TakeResult]:
    """Each take's run with the project hints, against its run with today's hints (``before``).

    Today's mouse 5 is the earlier run's. A take heard exactly as before
    gets the same text, so its earlier mouse 5 is kept (with the new
    transcription time) rather than asked of the model again.
    """
    from dataclasses import replace

    results = []
    for take, (heard, asr_s), reason, earlier in zip(takes, transcripts, reasons, before, strict=True):
        if heard == earlier.heard:
            results.append(replace(earlier, asr_s=asr_s, hints=reason))
        else:
            results.append(run_take(take, set_name, cases.get(take.id, take.case), heard, asr_s, product,
                                    today_from=earlier, hints=reason))
    return results


def model_state(product: Product) -> bool | None:
    """Whether the model is in memory now; None when unknown or unreadable (read-only)."""
    if product.model_loaded is None:
        return None
    try:
        return product.model_loaded()
    except Exception:  # noqa: BLE001 - the state is only reported
        return None


def first_call(results: Sequence[TakeResult], loaded: bool | None) -> dict | None:
    """The run's first model call (today's correction of the first spoken take): how long and how it ended."""
    first = next((r for r in results if r.spoken), None)
    if first is None:
        return None
    return {"model_loaded_before": loaded, "seconds": round(first.today_s, 3), "reason": first.today_reason}


# ---------------------------------------------------------------- aggregates


def _seconds(values: Sequence[float], percent: float) -> float | None:
    value = percentile_nearest_rank(values, percent)
    return None if value is None else round(value, 3)


def dataset_counts(dataset: Dataset, takes: int | None = None) -> dict:
    return {"script_rows": dataset.script_rows, "recorded": len(dataset.takes) if takes is None else takes,
            "pending": len(dataset.pending), "invalid": len(dataset.invalid)}


def set_block(results: Sequence[TakeResult], dataset: dict, product_timeouts: dict | None = None,
              terms: bool = False) -> dict:
    """Counts, reasons and timings of one set; never text, names or terms."""
    spoken = [r for r in results if r.spoken]
    refusals = Counter(r.detail for r in spoken if r.reason == autorewrite.REFUSED)
    enrich_refusals = Counter(r.enrich_detail for r in spoken if r.enrichment == enrich.REFUSED)
    called = [r for r in spoken if r.enrich_called]
    timeouts = product_timeouts or {}
    block: dict = {
        "status": "measured",
        "dataset": dataset,
        "takes": len(results),
        "no_speech": len(results) - len(spoken),
        "projects_detected": sum(r.project_found for r in spoken),
        "packs_found": sum(r.pack_found for r in spoken),
        "term_occurrences": sum(r.term_occurrences for r in spoken) if terms else None,
        "term_errors": {
            "pipeline": sum(r.term_errors_pipeline for r in spoken),
            "today": sum(r.term_errors_today for r in spoken),
            "new": sum(r.term_errors_new for r in spoken),
        } if terms else None,
        "lost": {"today": sum(r.lost_today for r in spoken), "new": sum(r.lost_new for r in spoken)},
        "invented": {"today": sum(r.invented_today for r in spoken), "new": sum(r.invented_new for r in spoken)},
        "pack_outside_context": sum(r.pack_outside_context for r in spoken),
        "enriched": sum(r.enrichment == enrich.ENRICHED for r in spoken),
        "enrichment_requests": len(called),
        "tidied": {"replies": sum(r.enrich_tidied > 0 for r in called),
                   "enriched": sum(r.enrich_tidied > 0 and r.enrichment == enrich.ENRICHED for r in called)},
        "reasons": {
            "today": dict(sorted(Counter(r.today_reason for r in spoken).items())),
            "correction": dict(sorted(Counter(r.reason for r in spoken).items())),
            "correction_refusals": dict(sorted(refusals.items())),
            "enrichment": dict(sorted(Counter(r.enrichment or "not_asked" for r in spoken).items())),
            "enrichment_refusals": dict(sorted(enrich_refusals.items())),
            "packs": dict(sorted(Counter(r.pack or "no_folder" for r in spoken).items())),
        },
        "latency": {
            "transcription_p50_s": _seconds([r.asr_s for r in results], 50),
            "transcription_p95_s": _seconds([r.asr_s for r in results], 95),
            "today_correction_p50_s": _seconds([r.today_s for r in spoken], 50),
            "today_correction_p95_s": _seconds([r.today_s for r in spoken], 95),
            "pack_p50_s": _seconds([r.pack_s for r in spoken if r.pack], 50),
            "pack_p95_s": _seconds([r.pack_s for r in spoken if r.pack], 95),
            "correction_p50_s": _seconds([r.correction_s for r in spoken], 50),
            "correction_p95_s": _seconds([r.correction_s for r in spoken], 95),
            "enrichment_p50_s": _seconds([r.enrich_s for r in called], 50),
            "enrichment_p95_s": _seconds([r.enrich_s for r in called], 95),
            "total_p50_s": _seconds([r.total_s for r in spoken], 50),
            "total_p95_s": _seconds([r.total_s for r in spoken], 95),
        },
        "over_product_timeout": {
            "correction_calls": 2 * len(spoken),
            "correction": (sum(r.correction_s > timeouts["timeout_s"] for r in spoken)
                           + sum(r.today_s > timeouts["timeout_s"] for r in spoken))
            if "timeout_s" in timeouts else None,
            "enrichment": sum(r.enrich_s > timeouts["enrich_timeout_s"] for r in called)
            if "enrich_timeout_s" in timeouts else None,
        },
    }
    if any(r.hints for r in results):
        block["hints"] = dict(sorted(Counter(r.hints for r in results).items()))
    return block


def before_block(results: Sequence[TakeResult], terms: bool = False) -> dict:
    """The new mouse 5 of a set with today's decoding hints: the "before" of the project hints (counts only)."""
    spoken = [r for r in results if r.spoken]
    called = [r for r in spoken if r.enrich_called]
    return {
        "no_speech": len(results) - len(spoken),
        "term_errors": {"pipeline": sum(r.term_errors_pipeline for r in spoken),
                        "new": sum(r.term_errors_new for r in spoken)} if terms else None,
        "lost": sum(r.lost_new for r in spoken),
        "invented": sum(r.invented_new for r in spoken),
        "pack_outside_context": sum(r.pack_outside_context for r in spoken),
        "enriched": sum(r.enrichment == enrich.ENRICHED for r in spoken),
        "latency": {
            "transcription_p50_s": _seconds([r.asr_s for r in results], 50),
            "transcription_p95_s": _seconds([r.asr_s for r in results], 95),
            "correction_p50_s": _seconds([r.correction_s for r in spoken], 50),
            "correction_p95_s": _seconds([r.correction_s for r in spoken], 95),
            "enrichment_p50_s": _seconds([r.enrich_s for r in called], 50),
            "enrichment_p95_s": _seconds([r.enrich_s for r in called], 95),
            "total_p50_s": _seconds([r.total_s for r in spoken], 50),
            "total_p95_s": _seconds([r.total_s for r in spoken], 95),
        },
    }


def pending_block(dataset: dict) -> dict:
    return {"status": "pending_recordings", "dataset": dataset, "takes": 0}


def targets() -> dict:
    return {"term_errors_ratio_max": TARGET_TERM_ERROR_RATIO, "lost_max": TARGET_LOST,
            "invented_max": TARGET_INVENTED, "pack_outside_context_max": TARGET_INVENTED}


def complete(block: dict | None) -> bool:
    """Whether a set block has every script row recorded, valid and measured."""
    if not isinstance(block, dict) or block.get("status") != "measured":
        return False
    counts = block.get("dataset") or {}
    return (counts.get("pending") == 0 and counts.get("invalid") == 0
            and block.get("takes") == counts.get("script_rows") and block.get("takes", 0) > 0)


def check_targets(summary: dict) -> list[tuple[bool, str]]:
    """Each target with whether it is met and the measured aggregates; computed from the numbers."""
    sets = summary.get("sets") if isinstance(summary.get("sets"), dict) else {}
    measured = {name: block for name, block in sets.items()
                if isinstance(block, dict) and block.get("status") == "measured"}
    lines: list[tuple[bool, str]] = []
    prompts = sets.get(SET_NAME)
    lines.append((complete(prompts), "prompts set complete: " + (
        "{recorded} of {script_rows} recorded, {pending} pending, {invalid} invalid".format(**prompts["dataset"])
        if isinstance(prompts, dict) and isinstance(prompts.get("dataset"), dict) else "not measured")))
    errors = prompts.get("term_errors") if isinstance(prompts, dict) else None
    occurrences = prompts.get("term_occurrences") if isinstance(prompts, dict) else None
    if isinstance(errors, dict) and occurrences:
        met = errors["new"] <= TARGET_TERM_ERROR_RATIO * errors["today"]
        lines.append((met, f"domain-term errors: today {errors['today']}, new {errors['new']} of {occurrences} "
                           f"occurrences (after the text pipeline {errors['pipeline']}); target at most "
                           f"{TARGET_TERM_ERROR_RATIO:g} x today"))
    else:
        lines.append((False, "domain-term errors: not measured (no prompts takes with terms)"))
    lost = sum(block["lost"]["new"] for block in measured.values())
    invented = sum(block["invented"]["new"] for block in measured.values())
    from_pack = sum(block["pack_outside_context"] for block in measured.values())
    takes = sum(block["takes"] for block in measured.values())
    lines.append((takes > 0 and lost <= TARGET_LOST, f"content words lost (new mouse 5): {lost} in {takes} takes, "
                                                     f"target {TARGET_LOST}"))
    lines.append((takes > 0 and invented <= TARGET_INVENTED,
                  f"invented content words (new mouse 5): {invented}, target {TARGET_INVENTED}"))
    lines.append((takes > 0 and from_pack <= TARGET_INVENTED,
                  f"pack words outside the context part: {from_pack}, target {TARGET_INVENTED}"))
    return lines


def build_summary(blocks: dict[str, dict], engine: dict, rewrite: dict) -> dict:
    summary: dict = {"schema": SUMMARY_SCHEMA, "kind": "mouse5_prompts",
                     "status": "measured" if any(b.get("status") == "measured" for b in blocks.values())
                     else "pending_recordings",
                     "targets": targets(), "sets": blocks, "engine": engine, "rewrite": rewrite}
    checks = check_targets(summary)
    names = ("complete", "term_errors", "lost", "invented", "pack_outside_context")
    summary["meets_targets"] = {name: met for name, (met, _) in zip(names, checks)}
    summary["meets_targets"]["all"] = all(summary["meets_targets"].values())
    return summary


# ---------------------------------------------------------------- private outputs


def _inside(path: Path, root: Path) -> bool:
    return Path(root).resolve() in Path(path).resolve().parents


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|") or NONE_MARK


def _quoted(text: str) -> list[str]:
    return ["> " + line if line.strip() else ">" for line in (text or NONE_MARK).split("\n")]


def _rows(results: Sequence[TakeResult]) -> list[dict]:
    return [{"id": r.id, "set": r.set, "case": r.case, "reference": r.reference, "heard": r.heard,
             "source": r.source, "today": r.today, "corrected": r.corrected, "final": r.final, "hints": r.hints,
             "project_found": r.project_found, "pack": r.pack, "today_reason": r.today_reason, "reason": r.reason,
             "detail": r.detail, "enrichment": r.enrichment, "enrich_detail": r.enrich_detail,
             "enrich_tidied": r.enrich_tidied,
             "term_occurrences": r.term_occurrences, "term_errors_pipeline": r.term_errors_pipeline,
             "term_errors_today": r.term_errors_today, "term_errors_new": r.term_errors_new,
             "lost_today": r.lost_today, "lost_new": r.lost_new, "invented_today": r.invented_today,
             "invented_new": r.invented_new, "pack_outside_context": r.pack_outside_context,
             "asr_s": round(r.asr_s, 3), "today_s": round(r.today_s, 3), "pack_s": round(r.pack_s, 3),
             "correction_s": round(r.correction_s, 3), "enrich_s": round(r.enrich_s, 3)} for r in results]


def write_private(run_dir: Path, results: Sequence[TakeResult], results_dir: Path = RESULTS_DIR,
                  before: Sequence[TakeResult] = ()) -> tuple[Path, Path]:
    """Per-take JSON and the before/after enrichment examples, only under bench/results/.

    ``before`` (the runs with today's decoding hints) go to ``takes-before.json``.
    """
    run_dir = Path(run_dir)
    if not _inside(run_dir / "takes.json", results_dir):
        raise ValueError("prompt outputs with spoken text may only be written under bench/results/")
    run_dir.mkdir(parents=True, exist_ok=True)
    takes_path = run_dir / "takes.json"
    takes_path.write_text(json.dumps(_rows(results), ensure_ascii=False, indent=2), encoding="utf-8")
    if before:
        (run_dir / "takes-before.json").write_text(json.dumps(_rows(before), ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
    heard_before = {r.id: r.heard for r in before}
    lines = [
        "# Rato 5: exemplos antes e depois do enriquecimento",
        "",
        "Para cada take: o que foi dito (guião), o texto que chegou ao rato 5 depois do pipeline, o que o rato 5",
        "escreve hoje (só correção), o texto corrigido com o contexto do projeto e o prompt enriquecido que o",
        "Quill enviaria. Escreva por baixo de cada take `sim` se o prompt enriquecido é melhor e fiel ao que disse,",
        "ou `não` com o motivo.",
        "Este ficheiro tem texto falado, nomes e termos reais: fica só em bench/results/ (ignorado pelo Git).",
    ]
    for r in results:
        lines += ["", f"## {r.id} ({r.set}, {r.case or NONE_MARK})", ""]
        if not r.spoken:
            lines += ["Sem texto reconhecido: o rato 5 não escreveria nada.", "", "Sponsor:"]
            continue
        status = (f"correção {r.reason}{f' ({r.detail})' if r.detail else ''}; enriquecimento "
                  f"{r.enrichment or NONE_MARK}{f' ({r.enrich_detail})' if r.enrich_detail else ''}; pacote "
                  f"{'sim' if r.pack_found else 'não'}; termos errados hoje {r.term_errors_today}, novo "
                  f"{r.term_errors_new}; perdidas {r.lost_new}; inventadas {r.invented_new}")
        lines += ["Dito:", *_quoted(r.reference), ""]
        if r.id in heard_before and heard_before[r.id] != r.heard:
            lines += ["Ouvido com as dicas de hoje (antes dos termos do projeto):", *_quoted(heard_before[r.id]), ""]
        lines += ["Chegou ao rato 5:", *_quoted(r.source), "",
                  "Rato 5 hoje:", *_quoted(r.today), "", "Corrigido com contexto:", *_quoted(r.corrected), "",
                  "Enviado (novo):", *_quoted(r.final), "", f"`{status}`", "", "Sponsor:"]
    examples_path = run_dir / "exemplos.md"
    examples_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return takes_path, examples_path


def private_texts(results: Sequence[TakeResult]) -> list[str]:
    return [text for r in results for text in (r.reference, r.heard, r.source, r.today, r.corrected, r.final) if text]


# ---------------------------------------------------------------- defaults (GPU, Ollama and the app's config)


# stream(takes, hints=None): the final text and release-to-final seconds of each take; ``hints`` (one per
# take, None: the vocabulary hints) open each take's session as the app gives mouse 5 its decoding hints.
Streamer = Callable[..., list[tuple[str, float]]]


def default_streamer(hints: Sequence[str]) -> tuple[Streamer, dict]:
    """The product streaming path with the app's engine model and vocabulary ``hints``; ``stream.close`` frees it."""
    from dataclasses import asdict

    from bench.engines.base import EngineError, EngineUnavailable, wav_pcm
    from bench.pipeline import ENGINE_COMPUTE, STREAM_MODEL
    from bench.streaming import replay_deterministic
    from quill.config import load_config
    from quill.streaming import StreamingTranscriber, options_for
    from quill.whisper import Whisper

    name = load_config().engine_model or STREAM_MODEL
    options = options_for(name)
    model = Whisper(name, compute_type=ENGINE_COMPUTE)
    vocabulary = list(hints)

    def stream(takes: Sequence[Take], session_hints: Sequence[object | None] | None = None) -> list[tuple[str, float]]:
        per_take = list(session_hints) if session_hints is not None else [None] * len(takes)
        transcriber = StreamingTranscriber(model, options, vocabulary)
        transcriber.start()
        try:
            transcriber.ready.wait()
            if transcriber.load_error:
                raise EngineUnavailable(transcriber.load_error)
            out = []
            for take, take_hints in zip(takes, per_take, strict=True):
                result = replay_deterministic(transcriber, wav_pcm(take.path.read_bytes())[0], hints=take_hints)
                if not result.ok:
                    raise EngineError(f"streamed final failed: {result.error}")
                out.append((result.text, result.latency_s))
            return out
        finally:
            transcriber.stop(close_model=False)

    stream.close = model.close
    return stream, {"model": name, **asdict(options)}


def default_product(vocabulary: object, generic_terms: Sequence[str], product_timeouts: bool = False) -> Product:
    """The app's text pipeline, project folders, context packs and rewriter from its config, warmed up.

    The rewriter gets ``BENCH_TIMEOUT_S`` for both calls; the local model is
    only listed and asked one short warm-up turn: nothing is pulled, loaded
    on purpose or unloaded. With ``product_timeouts`` the rewriter is the
    app's own, with the config's timeouts, and the model is left as it is
    (no warm-up turn here), so a first call may meet a model that is not
    loaded. Raises SettingsError when Ollama cannot answer.
    """
    from dataclasses import replace

    from quill.app import TextPipeline
    from quill.config import load_config
    from quill.context_pack import ContextPacks
    from quill.corrections import CorrectionStore, Learner
    from quill.ollama import ModelWarmer, OllamaClient, OllamaError
    from quill.projects import ProjectDetector, ProjectFolders

    config = load_config()
    client = OllamaClient(config.ollama_url,
                          timeout_s=config.autorewrite.timeout_s if product_timeouts else BENCH_TIMEOUT_S,
                          keep_alive=config.autorewrite.keep_alive if product_timeouts else None)
    try:
        if config.ollama_model not in client.installed():
            raise SettingsError(f"prompts: {config.ollama_model} is not installed in Ollama")
        if not product_timeouts:
            client.chat(config.ollama_model, "Reply with ok.", "ok", max_tokens=8)
    except OllamaError as exc:
        raise SettingsError(f"prompts: Ollama unavailable: {' '.join(str(exc).split())[:160]}") from None
    warmer = None
    if product_timeouts:
        # As the app: the warm-up at each mouse 5 hold and the bounded wait before a correction.
        warmer = ModelWarmer(client, config.ollama_model)
        rewriter = autorewrite.AutoRewriter(client, config.ollama_model, config.autorewrite, warmer=warmer)
    else:
        settings = replace(config.autorewrite, timeout_s=BENCH_TIMEOUT_S, enrich_timeout_s=BENCH_TIMEOUT_S)
        rewriter = autorewrite.AutoRewriter(client, config.ollama_model, settings)
    holder = Window()
    folders = ProjectFolders(config.project_context.folders, config.voice.shortcut_dirs)
    pipeline = TextPipeline(config, vocabulary=vocabulary, generic_terms=generic_terms, describe=holder.describe,
                            learner=Learner(CorrectionStore(config.corrections_path)),
                            client=client if config.cleanup_mode == "llm" else None,
                            projects=ProjectDetector(folders))
    packs = ContextPacks.from_settings(config.project_context)
    info = {"model": config.ollama_model, "timeout_s": config.autorewrite.timeout_s,
            "enrich_timeout_s": config.autorewrite.enrich_timeout_s,
            "bench_timeout_s": None if product_timeouts else BENCH_TIMEOUT_S,
            "timeouts": "product" if product_timeouts else "bench", "cleanup": config.cleanup_mode}
    if product_timeouts:
        info.update(keep_alive=config.autorewrite.keep_alive, load_wait_s=config.autorewrite.load_wait_s)
    names = tuple(entry.text for entry in getattr(vocabulary, "names", ()))
    return Product(pipeline, holder, rewriter, names, packs.lookup, info,
                   hold_start=(lambda: warmer.warm("mouse 5 hold")) if warmer is not None else None,
                   model_loaded=lambda: config.ollama_model in client.loaded(),
                   hints_for=app_hints(pipeline, packs.get, vocabulary, generic_terms))


def app_hints(pipeline: object, pack_for: Callable[[object], object | None], vocabulary: object,
              generic_terms: Sequence[str]) -> Callable[[WindowInfo], tuple[object | None, str]]:
    """The app's mouse 5 decoding hints (``quill.app.project_hints``) for a simulated window: no process, title only."""
    from quill.app import project_hints

    def hints_for(info: WindowInfo) -> tuple[object | None, str]:
        return project_hints(info, 0, profiles=pipeline.profiles, projects=pipeline.projects, pack_for=pack_for,
                             vocabulary=vocabulary, generic_terms=generic_terms)

    return hints_for


@dataclass(frozen=True)
class Fixture:
    """Whether the local configuration fits the prompts script (counts only)."""

    projects: int
    folders: int  # projects with a local folder
    packs: int  # projects with a context pack
    terms: int
    in_pack: int  # terms found in their project's pack
    claude_code: bool  # the simulated window gets the claude-code profile
    problems: tuple[str, ...] = ()  # placeholders, never names or terms


def term_in_pack(term: str, pack: object | None) -> bool:
    if pack is None:
        return False
    key = fold(term)
    listed = {fold(t) for t in getattr(pack, "terms", ()) if isinstance(t, str)}
    words = [fold(word) for word in tokens(term)]
    summary = [fold(word) for word in tokens(getattr(pack, "summary", "") or "")]
    return key in listed or any(summary[i:i + len(words)] == words for i in range(len(summary)))


def check_fixture(rows: Sequence[PromptRow], projects: dict[str, str], terms: dict[str, str],
                  folder_for: Callable[[str], object | None], pack_for: Callable[[object], object | None],
                  profile_of: Callable[[WindowInfo], str]) -> Fixture:
    wanted = _numbered(row.project for row in rows)
    packs: dict[str, object | None] = {}
    folders = 0
    problems: list[str] = []
    for placeholder in wanted:
        folder = folder_for(projects[placeholder]) if placeholder in projects else None
        folders += folder is not None
        packs[placeholder] = pack_for(folder) if folder is not None else None
        if packs[placeholder] is None:
            problems.append(placeholder)
    pairs = sorted({(term, row.project) for row in rows for term in row.terms})
    in_pack = 0
    for term, project in pairs:
        if term in terms and term_in_pack(terms[term], packs.get(project)):
            in_pack += 1
        else:
            problems.append(term)
    claude = profile_of(window(projects.get(wanted[0], "") if wanted else "")) == CLAUDE_CODE
    return Fixture(len(wanted), folders, sum(p is not None for p in packs.values()), len(pairs), in_pack, claude,
                   tuple(dict.fromkeys(problems)))


def default_fixture(rows: Sequence[PromptRow], projects: dict[str, str], terms: dict[str, str]) -> Fixture:
    """The app's project folders, context packs (cache under local/) and profiles; no GPU or Ollama."""
    from quill.config import load_config
    from quill.context_pack import ContextPacks
    from quill.profiles import Profiles
    from quill.projects import ProjectFolders

    config = load_config()
    folders = ProjectFolders(config.project_context.folders, config.voice.shortcut_dirs)
    packs = ContextPacks.from_settings(config.project_context)
    return check_fixture(rows, projects, terms, folders.folder_for, packs.get, Profiles(config.profiles).select)


# ---------------------------------------------------------------- commands


@dataclass
class Loaded:
    """The datasets of the chosen sets (counts and takes; the takes hold text)."""

    rows: list[PromptRow] = field(default_factory=list, repr=False)
    prompts: Dataset | None = None
    dictation: Dataset | None = None
    dictation_takes: tuple[Take, ...] = field(default=(), repr=False)
    dictation_rows: int = 0


def load_sets(settings: Settings, chosen: Sequence[str]) -> Loaded:
    loaded = Loaded()
    if SET_NAME in chosen:
        prompts = settings.for_set(SET_NAME)
        loaded.rows = load_prompt_rows(prompts)
        loaded.prompts = load_dataset(prompts)
    if DICTATION in chosen:
        dictation = settings.for_set(DICTATION)
        loaded.dictation = load_dataset(dictation)
        loaded.dictation_takes = claude_code_takes(loaded.dictation)
        from bench.dataset import load_script

        loaded.dictation_rows = sum(row.style == CLAUDE_CODE for row in load_script(dictation))
    return loaded


def prompts_complete(settings: Settings, loaded: Loaded) -> bool:
    dataset = loaded.prompts
    return (dataset is not None and not dataset.invalid and not dataset.pending
            and len(dataset.takes) >= settings.for_set(SET_NAME).minimum_takes)


def _fixture_lines(check: Fixture) -> list[str]:
    lines = [f"projects: {check.folders} of {check.projects} with a local folder, {check.packs} with a context pack; "
             f"terms in their project's pack {check.in_pack} of {check.terms}; simulated window profile "
             f"{'claude-code' if check.claude_code else 'NOT claude-code'}"]
    if check.problems:
        lines.append("  fixture problems (placeholders): " + ", ".join(check.problems))
    return lines


def dry_run(settings: Settings, chosen: Sequence[str], out: Callable[[str], None] = print,
            fixture: Callable[..., Fixture] = default_fixture) -> tuple[int, bool]:
    """Counts only; opens no audio, model, microphone or Ollama. Returns (exit code, prompts complete)."""
    try:
        loaded = load_sets(settings, chosen)
    except DatasetError as exc:
        out(f"error: {exc}")
        return 2, False
    done = False
    if loaded.prompts is not None:
        rows, dataset = loaded.rows, loaded.prompts
        cases = Counter(row.case for row in rows)
        projects = len({row.project for row in rows})
        terms = len({term for row in rows for term in row.terms})
        out(f"prompts script: {len(rows)} rows; cases " + ", ".join(f"{c} {cases[c]}" for c in CASES if cases[c])
            + f"; {projects} projects, {terms} domain terms")
        prompts = settings.for_set(SET_NAME)
        mapped_projects = sum(p in dict(prompts.projects or ()) for p in _numbered(r.project for r in rows))
        mapped_terms = sum(valid_term(dict(prompts.terms or ()).get(t)) for t in
                           _numbered(t for r in rows for t in r.terms))
        out(f"local mapping: {mapped_projects} of {projects} projects and {mapped_terms} of {terms} terms named")
        done = prompts_complete(settings, loaded)
        out(f"recorded {len(dataset.takes)} of {len(rows)}, pending {len(dataset.pending)}, invalid "
            f"{len(dataset.invalid)}, discarded {dataset.discarded}; complete: {'yes' if done else 'no'}")
        for invalid in dataset.invalid:
            out(f"  invalid {invalid.id}: {invalid.reason}")
        if mapped_projects == projects and mapped_terms == terms:
            try:
                names, term_map = recording_names(prompts, rows)
                for line in _fixture_lines(fixture(rows, names, term_map)):
                    out(line)
            except Exception as exc:  # noqa: BLE001 - the dry run reports the local config problem and goes on
                out(f"fixture: not checked ({type(exc).__name__})")
    if loaded.dictation is not None:
        out(f"dictation claude-code takes: {len(loaded.dictation_takes)} recorded of {loaded.dictation_rows} "
            "script rows")
    return 0, done


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def report_lines(summary: dict) -> list[str]:
    lines = []
    for name, block in summary["sets"].items():
        if block.get("status") != "measured":
            lines.append(f"{name}: {block.get('status')}")
            continue
        latency = block["latency"]
        errors = block["term_errors"]
        lines.append(
            f"{name}: takes {block['takes']} (no speech {block['no_speech']}); projects {block['projects_detected']}, "
            f"packs {block['packs_found']}; enriched {block['enriched']} of {block.get('enrichment_requests', '?')}; "
            f"lost today {block['lost']['today']} / new {block['lost']['new']}; invented today {block['invented']['today']} / new {block['invented']['new']}; "
            f"pack words outside context {block['pack_outside_context']}"
            + (f"; domain-term errors pipeline {errors['pipeline']}, today {errors['today']}, new {errors['new']} of "
               f"{block['term_occurrences']}" if errors else ""))
        lines.append(
            f"  latency p50/p95 s: transcription {_pct(latency['transcription_p50_s'])} / "
            f"{_pct(latency['transcription_p95_s'])}, today {_pct(latency['today_correction_p50_s'])} / "
            f"{_pct(latency['today_correction_p95_s'])}, correction {_pct(latency['correction_p50_s'])} / "
            f"{_pct(latency['correction_p95_s'])}, enrichment {_pct(latency['enrichment_p50_s'])} / "
            f"{_pct(latency['enrichment_p95_s'])}, total {_pct(latency['total_p50_s'])} / "
            f"{_pct(latency['total_p95_s'])}")
        lines.append("  reasons: " + "; ".join(f"{k} " + ", ".join(f"{a} {b}" for a, b in v.items())
                                               for k, v in block["reasons"].items() if v))
        before = block.get("before_hints")
        if isinstance(before, dict):
            was = before["latency"]
            lines.append(
                f"  before the project hints: lost {before['lost']}, invented {before['invented']}, pack words outside "
                f"context {before['pack_outside_context']}, enriched {before['enriched']}"
                + (f"; domain-term errors pipeline {before['term_errors']['pipeline']}, new "
                   f"{before['term_errors']['new']}" if before.get("term_errors") else "")
                + f"; transcription {_pct(was['transcription_p50_s'])} / {_pct(was['transcription_p95_s'])}, total "
                  f"{_pct(was['total_p50_s'])} / {_pct(was['total_p95_s'])}")
        if block.get("hints"):
            lines.append("  decoding hints: " + ", ".join(f"{k or 'today'} {v}" for k, v in block["hints"].items()))
        over = block.get("over_product_timeout") or {}
        if over.get("correction") is not None:
            lines.append(f"  over the product timeout: correction {over['correction']} of "
                         f"{over.get('correction_calls', '?')} calls, enrichment {over.get('enrichment')}")
    first = (summary.get("rewrite") or {}).get("first_call")
    if first:
        lines.append(f"first model call: {first['seconds']:g} s, {first['reason']} (model loaded before: "
                     f"{first['model_loaded_before']})")
    return lines


def require_lines(summary: dict, out: Callable[[str], None]) -> int:
    code = 0
    for met, text in check_targets(summary):
        out(f"{'met' if met else 'NOT met'}: {text}")
        code = code or (0 if met else 1)
    return code


def main(
    argv: list[str] | None = None,
    *,
    product_factory: Callable[[object, Sequence[str]], Product] = default_product,
    streamer_factory: Callable[[Sequence[str]], tuple[Streamer, dict]] = default_streamer,
    fixture: Callable[..., Fixture] = default_fixture,
    results_dir: Path = RESULTS_DIR,
    out: Callable[[str], None] = print,
) -> int:
    from quill.vocabulary import LOCAL_VOCABULARY, VocabularyError, load_generic_terms, load_vocabulary, whisper_hints

    parser = argparse.ArgumentParser(prog="bench.prompts", description="Mouse 5 measurement on spoken prompts.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--set", choices=(*SETS, "all"), default="all", help="sets to measure (default all)")
    parser.add_argument("--dry-run", action="store_true", help="script, recording and fixture counts only")
    parser.add_argument("--vocabulary", type=Path, default=LOCAL_VOCABULARY,
                        help="personal vocabulary (default local/vocabulary.toml; missing is empty)")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY,
                        help="aggregate summary JSON (default docs/research/prompts-summary.json)")
    parser.add_argument("--require", action="store_true",
                        help="exit 1 when a target is unmet or the prompts set has missing takes")
    parser.add_argument("--check", type=Path, default=None, help="check the targets of a saved summary")
    parser.add_argument("--timeouts", choices=("bench", "product"), default="bench",
                        help="bench: a long measurement timeout after one warm-up turn (default); product: the "
                             "app's timeouts and warm-up, with the model as it is")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    chosen = SETS if args.set == "all" else (args.set,)

    if args.check is not None:
        try:
            summary = json.loads(args.check.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            out("error: the summary cannot be read")
            return 2
        if not isinstance(summary, dict) or summary.get("kind") != "mouse5_prompts":
            out("error: not a bench.prompts summary")
            return 2
        return require_lines(summary, out)

    try:
        settings = load_settings(args.config)
        if args.dry_run:
            code, done = dry_run(settings, chosen, out, fixture)
            if code == 0 and args.require and not done:
                out("NOT met: the prompts set has missing takes (py -3.12 -m bench.record --set prompts)")
                return 1
            return code
        loaded = load_sets(settings, chosen)
        vocabulary = load_vocabulary(args.vocabulary)
    except (SettingsError, DatasetError, VocabularyError) as exc:
        out(f"error: {exc}")
        return 2

    work: list[tuple[str, tuple[Take, ...], dict[str, str]]] = []
    blocks: dict[str, dict] = {}
    if loaded.prompts is not None:
        counts = dataset_counts(loaded.prompts)
        if loaded.prompts.takes:
            work.append((SET_NAME, loaded.prompts.takes, {row.id: row.case for row in loaded.rows}))
        else:
            blocks[SET_NAME] = pending_block(counts)
    if loaded.dictation is not None:
        if loaded.dictation_takes:
            work.append((DICTATION, loaded.dictation_takes, {}))
        else:
            blocks[DICTATION] = pending_block({"script_rows": loaded.dictation_rows, "recorded": 0,
                                               "pending": loaded.dictation_rows, "invalid": 0})
    if not work:
        out("error: no recorded takes to measure (py -3.12 -m bench.record --set prompts)")
        return 2

    generic = load_generic_terms()
    hints = whisper_hints(vocabulary, (), generic)
    from bench.engines.base import EngineError, EngineUnavailable

    try:
        product = (product_factory(vocabulary, generic) if args.timeouts == "bench"
                   else product_factory(vocabulary, generic, product_timeouts=True))
    except SettingsError as exc:
        out(f"error: {exc}")
        return 2
    streamer = None
    transcripts: dict[str, list[tuple[str, float]]] = {}
    hinted: dict[str, list[tuple[str, float]]] = {}
    reasons: dict[str, list[str]] = {}
    project_chars: list[int] = []
    project_words: set[str] = set()  # the project hints' words: the pack's terms are real project terms
    try:
        streamer, stream_options = streamer_factory(hints)
        streamer(work[0][1][:1])  # warm-up: loads the model and fills the caches
        for name, takes, _ in work:
            transcripts[name] = streamer(takes)
        for name, takes, _ in work:
            per_take = take_hints(takes, product)
            reasons[name] = [reason for _, reason in per_take]
            chosen_takes = [take for take, (h, _) in zip(takes, per_take) if h is not None]
            chosen_hints = [h for h, _ in per_take if h is not None]
            project_chars += [len(getattr(h, "hotwords", "") or "") for h in chosen_hints]
            project_words.update(word for h in chosen_hints for word in (getattr(h, "hotwords", "") or "").split())
            again = iter(streamer(chosen_takes, chosen_hints) if chosen_takes else [])
            hinted[name] = [next(again) if h is not None else earlier
                            for (h, _), earlier in zip(per_take, transcripts[name], strict=True)]
    except (EngineUnavailable, EngineError) as exc:
        out(f"error: {' '.join(str(exc).split())[:200]}")
        return 2
    finally:
        close = getattr(streamer, "close", None)
        if close is not None:
            close()

    results: list[TakeResult] = []
    earlier: list[TakeResult] = []
    loaded_before = model_state(product)
    for name, takes, cases in work:
        before = measure(name, takes, cases, transcripts[name], product)
        measured = measure_after(name, takes, cases, hinted[name], reasons[name], before, product)
        earlier.extend(before)
        results.extend(measured)
        dataset = loaded.prompts if name == SET_NAME else loaded.dictation
        counts = dataset_counts(dataset) if name == SET_NAME else {
            "script_rows": loaded.dictation_rows, "recorded": len(takes),
            "pending": max(0, loaded.dictation_rows - len(takes)), "invalid": 0}
        blocks[name] = set_block(measured, counts, product.info, terms=name == SET_NAME)
        blocks[name]["before_hints"] = before_block(before, terms=name == SET_NAME)
    ordered = {name: blocks[name] for name in SETS if name in blocks}
    engine = {**stream_options, "hints": {"count": len(hints), "chars": sum(len(h) for h in hints)},
              "project_hints": {"takes": len(project_chars),
                                "hotword_chars_max": max(project_chars) if project_chars else None}}
    rewrite = {key: product.info[key] for key in ("model", "timeout_s", "enrich_timeout_s", "bench_timeout_s",
                                                  "timeouts", "keep_alive", "load_wait_s", "cleanup")
               if key in product.info}
    rewrite["first_call"] = first_call(earlier, loaded_before)
    summary = build_summary(ordered, engine, rewrite)

    texts = private_texts([*results, *earlier])
    names = [entry.text for entry in vocabulary.names]
    for dataset in (loaded.prompts, loaded.dictation):
        if dataset is not None:
            texts += dataset.reference_texts()
            names += sorted(dataset.names)
            names += [term for take in dataset.takes for term in take.terms]
    try:
        phrases, projects, terms = private_text(settings.for_set(SET_NAME))
    except DatasetError as exc:
        out(f"error: {exc}")
        return 2
    texts += phrases
    names += sorted(projects) + sorted(terms)
    # A pack term spelled as only the project writes it is refused too; common words would refuse at random.
    names += sorted(word for word in project_words if distinctive_term(word))
    run_dir = Path(results_dir) / "prompts" / time.strftime("%Y%m%d-%H%M%S")
    try:
        write_private(run_dir, results, results_dir, before=earlier)
        write_summary(run_dir / "summary.json", summary, texts, names)
        write_summary(Path(args.summary), summary, texts, names)
    except ValueError as exc:
        out(f"error: {exc}")
        return 1
    for line in report_lines(summary):
        out(line)
    out(f"summary written to {Path(args.summary).name}; per-take outputs and the enrichment examples under "
        "bench/results/prompts/")
    if args.require:
        return require_lines(summary, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
