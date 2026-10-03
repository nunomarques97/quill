"""Mouse 5 measurement on the Sponsor's spoken Claude Code prompts: project context and prompt enrichment.

Usage:
    py -3.12 -m bench.prompts --dry-run [--set all|prompts|dictation] [--safety] [--require]
    .venv\\Scripts\\python -m bench.prompts [--set all|prompts|dictation] [--summary PATH] [--require]
        [--timeouts bench|product] [--correction-candidates | --no-correction-candidates]
        [--common-sense | --no-common-sense] [--variants] [--safety]
        [--final-pass | --no-final-pass] [--pass-model M] [--pass-beam N]
        [--pass-temperature-fallback | --no-pass-temperature-fallback] [--pass-hints | --no-pass-hints]
        [--pass-timeout S] [--pass-candidates all|NAME,...]
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
quill config), on the deterministic audio-time schedule
(``bench.streaming.replay_deterministic``), three times:

- today: with today's hints (``quill.vocabulary.whisper_hints`` of the
  personal vocabulary and the generic terms), the baseline of today's
  mouse 5 and of the target;
- before: with the Phase 7 project hints, chosen by relevance only (the
  ``initial`` hints of the app's hint source: the project name and its
  pack terms within the same budget, as with nothing heard);
- after: with the hint source the app now gives mouse 5 in that window
  (``quill.app.project_hints``: in Claude Code with a detected project and
  a pack, a ``quill.heard.HeardHints`` that puts the pack terms sounding
  like words already heard first). The session asks it again after each of
  the replay's own partials, so its choices are as reproducible as the
  text; the takes whose hints it switched are counted.

Any other take keeps today's hints and its first transcription in all three.
The app gives the project hints once click-to-focus has named the window, a
fraction of a second into the hold; here the session decodes with them from
its start.

Each transcription then follows the same steps, and the summary reports
the "after" run with the "before" and today's-hints aggregates beside it; a
take heard exactly as in an earlier pass keeps that pass's mouse 5 (no model
call). The final text goes through the app's own text pipeline
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
  Its correction prompt lists the terms that sound like the dictation
  first (``quill.autorewrite.likely_terms``; counted per set as
  ``likely_terms``) only when the app does (``quill.autorewrite.LIKELY_TERMS``,
  off); ``--correction-candidates`` and ``--no-correction-candidates`` turn
  the list on or off, an ablation reported as
  ``rewrite.correction_candidates``.

The new mouse 5 follows the app's correction settings: name fixes on
(``quill.autorewrite.NAME_FIXES``) and ``[autorewrite] common_sense_fixes``
of the quill config, which ``--common-sense``/``--no-common-sense``
override. ``--variants`` also runs the new mouse 5 of each set, on the
transcripts of the app's hint pass, as each correction variant
(``VARIANTS``): ``phase8`` (``AutoRewriter(name_fixes=False)``, common
sense off), ``names`` (name fixes only) and ``common_sense`` (name fixes
and common-sense fixes); the variant the app's settings are is that run
itself, not asked again. ``--safety`` runs each variant on every valid take
of the dictation set, transcribed with today's hints, as if dictated into
Claude Code without a project (context mode without a pack, no enrichment,
no today's mouse 5): a safety set reported apart (``safety``), outside the
targets. Per variant, counts only: the vocabulary and project names in the
reference and how many the text lacks before and after the correction
(``names_fixed``), domain-term errors, word errors and WER of the corrected
text against the clean reference, content words lost and invented against
the input (the rules below; a personal-vocabulary name is known, as a pack
word), of the invented ones those the correction itself brought
(``invented_fixes``: the ordinary words of a common-sense fix) and those
beyond them (``invented_beyond_fixes``), content words the correction
brought that the reference does not hold (``invented_reference``,
independently of the guard), enrichment requested/accepted and correction
p50/p95. ``common_sense_rule`` says from these numbers whether the
common-sense fixes lower the word errors against name fixes only (in all,
and in no set more) with lost, invented beyond the fixes and invented
against the reference 0 in every set; the Phase 9 measurement runs
``--variants --safety``.

Mouse 5 into Claude Code then decodes the released audio once more (the
final pass, ``quill.finalpass``) when the app's ``[final_pass]`` is on:
the main run (``sets``, the targets) types that pass's text, or the
streaming text it falls back to, exactly as the app. ``--final-pass`` and
``--no-final-pass`` override the section's ``enabled``, and ``--pass-model``,
``--pass-beam``, ``--pass-temperature-fallback``, ``--pass-hints`` and
``--pass-timeout`` its other keys. ``--pass-candidates`` also measures each
named candidate of ``PASS_CANDIDATES`` (the config's section with the
candidate's overrides) on the same streaming replay: each released take is
decoded by ``quill.finalpass.run_session`` with its session's hints and hint
source, on the engine model's worker or one second transcriber of the other
model, as the app runs it. Per set (and the safety set), ``final_pass``
reports the streaming text alone and each pass (``pass_block``): WER after
the transcription, after the text pipeline and after the full mouse 5,
domain-term errors, names, lost and invented words, enrichment, the pass
reason codes and p50/p95 of each stage (streaming final, final pass, text
pipeline, correction, enrichment, release to text) with the release-to-text
p95 of the takes up to ``LONG_TAKE_S``; ``gpu`` holds read-only snapshots
(nvidia-smi and Ollama's /api/ps) with the Whisper models loaded; ``choice``
names the pass that may ship (``pass_choice``).

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
- latency p50/p95 with each pass's hints: transcription,
  today's correction, and the new pack
  lookup, correction, enrichment and their total. The model calls get a
  measurement timeout of ``BENCH_TIMEOUT_S`` so the counts do not depend on
  the machine's load; calls slower than the product timeouts are counted;
- release to text p95 of the takes up to ``LONG_TAKE_S`` of audio in every
  set: at most ``TARGET_RELEASE_P95_S``;
- prompts WER after the full mouse 5 (the corrected text against the clean
  reference): at most ``TARGET_PROMPTS_WER``. Reported as met or NOT met
  beside the others, but not an exit code: the goal ships the best safe
  improvement when it is not met.

Spoken text goes only under ``bench/results/prompts/<run>/``: the per-take
JSON (``takes.json`` after, ``takes-before.json`` before,
``takes-today.json`` with today's hints) and ``exemplos.md``, the before/after enrichment examples for the
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
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from bench.dataset import (PLACEHOLDER, TERM_PLACEHOLDER, Dataset, DatasetError, Take, load_dataset, parse_script,
                           valid_term)
from bench.metrics import invented_against, percentile_nearest_rank, term_recall, text_edits, write_summary
from bench.normalize import normalize_words
from bench.settings import REPO_ROOT, RESULTS_DIR, Settings, SettingsError, load_settings
from quill import autorewrite, enrich
from quill.finalpass import FinalPassOutcome, FinalPassSettings
from quill.profiles import CLAUDE_CODE, WindowInfo
from quill.whisper import MODELS, distinctive_term

SET_NAME = "prompts"
DICTATION = "dictation"
SETS = (SET_NAME, DICTATION)
SAFETY = "safety"
SUMMARY_SCHEMA = 1
DEFAULT_SUMMARY = REPO_ROOT / "docs" / "research" / "prompts-summary.json"
# Targets of the Phase 6 goal. Never lowered here.
TARGET_TERM_ERROR_RATIO = 0.5  # new domain-term errors at most half of today's
TARGET_LOST = 0
TARGET_INVENTED = 0
# Targets of the Phase 11 goal. Never lowered here.
TARGET_PROMPTS_WER = 0.12  # prompts WER after the full mouse 5 pipeline (reported; the goal ships the best safe gain)
TARGET_RELEASE_P95_S = 6.0  # release-to-text p95 of the takes up to LONG_TAKE_S of audio
LONG_TAKE_S = 20.0

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

# The correction variants of mouse 5 into Claude Code (--variants, --safety): (name fixes, common-sense fixes).
PHASE8 = "phase8"  # AutoRewriter(name_fixes=False), common sense off: the Phase 8 correction
NAMES = "names"  # name fixes only
COMMON_SENSE = "common_sense"  # name fixes and common-sense fixes
VARIANTS = {PHASE8: (False, False), NAMES: (True, False), COMMON_SENSE: (True, True)}

# The mouse 5 final pass (quill.finalpass): the runs compared per set. STREAMING is the streaming text alone; APP the
# main run's [final_pass] (the config's, or as --final-pass and the --pass-* overrides set it).
STREAMING = "streaming"
APP = "app"
# The candidates (--pass-candidates): overrides of the config's [final_pass] section, each on the model it names as
# the app would decode it (large-v3 on the second model's worker, large-v3-turbo on the engine's).
PASS_CANDIDATES: dict[str, dict] = {
    "v3_beam10": {"model": "large-v3", "beam_size": 10},
    "v3_beam5": {"model": "large-v3", "beam_size": 5},
    "v3_beam10_fallback": {"model": "large-v3", "beam_size": 10, "temperature_fallback": True},
    "v3_beam10_vad": {"model": "large-v3", "beam_size": 10, "vad_filter": True},
    "turbo_beam10": {"model": "large-v3-turbo", "beam_size": 10},
}


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
                   corrected: str | None = None, names: Iterable[str] = ()) -> tuple[int, int]:
    """(invented content words, pack words outside the context part) of ``output`` against its input ``source``.

    A content word beyond its count in ``source`` is invented unless it is a
    structure word, a pack word or a word of a personal-vocabulary name in
    ``names`` (a misheard name the correction wrote as listed, as a pack
    term); a number beyond its count always is. Pack words beyond their
    count outside the context part are counted apart, against ``corrected``
    too when given: a misheard word the correction replaced with a pack term
    is the dictation's, not a requirement from the pack (it is measured as a
    domain-term fix).
    """
    known = pack_vocabulary(pack, project) | _name_words(names)
    before = _content_counts(source)
    extra = _content_counts(output) - before
    invented = sum(count for word, count in extra.items()
                   if is_number(word) or (word not in STRUCTURE and word not in known))
    dictated = before | _content_counts(corrected) if corrected is not None else before
    outside = _content_counts(outside_context(output)) - dictated
    from_pack = sum(count for word, count in outside.items()
                    if not is_number(word) and word not in STRUCTURE and word in known)
    return invented, from_pack


def _name_words(names: Iterable[str]) -> frozenset[str]:
    return frozenset(fold(word) for name in names if isinstance(name, str) for word in tokens(name))


def invented_beyond_fixes(source: str, corrected: str, output: str, pack: object | None = None, project: str = "",
                          names: Iterable[str] = ()) -> int:
    """Invented content words of ``output`` (the ``invented_words`` rule) beyond ``source`` and ``corrected``.

    What the correction itself brought (``invented_words(source,
    corrected)``: the ordinary words of a common-sense fix) is left to
    ``invented_reference``, which checks it against what was said; this
    counts everything else the output adds.
    """
    known = pack_vocabulary(pack, project) | _name_words(names)
    extra = _content_counts(output) - (_content_counts(source) | _content_counts(corrected))
    return sum(count for word, count in extra.items()
               if is_number(word) or (word not in STRUCTURE and word not in known))


def term_errors(reference: str, text: str, terms: Sequence[str]) -> tuple[int, int]:
    """(occurrences of ``terms`` in ``reference``, how many of them ``text`` lacks)."""
    counts = term_recall([(reference, text)], sorted(set(terms)))
    return counts.expected, counts.expected - counts.found


def invented_reference(reference: str, source: str, corrected: str) -> int:
    """Content words the correction brought (beyond their count in ``source``) that ``reference`` does not hold.

    Computed against the clean reference, independently of the product
    guard: a misheard word fixed to what was said brings nothing invented.
    """
    def content(text: str) -> list[str]:
        return [word for word in normalize_words(text) if is_content(word)]

    return invented_against(content(reference), content(source), content(corrected))


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
    returns the (decoding hints or hint source, or None; reason code) the
    app gives mouse 5 in the window ``info`` (``quill.app.project_hints``; None:
    no take gets project hints). ``final_pass`` is the app's ``[final_pass]``
    for mouse 5 into Claude Code (off when mouse 5 is not bound; None: off).
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
    final_pass: FinalPassSettings | None = None


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
    hints: str = ""  # the reason code of the take's decoding hints ("" with today's hints)
    hint_switches: int = 0  # hint changes the heard-term source chose from the replay's partials
    likely: int = 0  # terms the new correction's prompt listed as sounding like the dictation
    variant: str = ""  # the correction variant of the new mouse 5 ("": the app's settings)
    name_occurrences: int = 0  # vocabulary and project names in the reference
    name_errors_source: int = 0  # of them, missing from mouse 5's input
    name_errors_new: int = 0  # of them, missing from the new corrected text
    names_prestep: int = 0  # names the deterministic pre-step wrote as listed
    sensible: int = 0  # misheard groups the guard let common sense fix
    kept: int = 0  # fixes typed as dictated
    reference_words: int = 0  # words of the clean reference
    word_errors_source: int = 0  # word edits of mouse 5's input against the clean reference
    word_errors_new: int = 0  # word edits of the new corrected text against the clean reference
    invented_reference: int = 0  # content words the correction brought that the reference does not hold
    invented_fixes: int = 0  # invented content words (against the input) the correction itself brought
    invented_beyond: int = 0  # invented content words of the output beyond the input and the corrected text
    audio_s: float = 0.0  # seconds of the take's audio
    word_errors_heard: int = 0  # word edits of the transcription (before the text pipeline) against the reference
    pipeline_s: float = 0.0  # the text pipeline
    pass_s: float = 0.0  # the final pass, call to outcome (0 without one)
    pass_reason: str = ""  # the final pass's reason code ("": no pass asked)

    @property
    def spoken(self) -> bool:
        return self.reason != NO_SPEECH

    @property
    def enrich_called(self) -> bool:
        return self.enrichment not in ("", enrich.SHORT)

    @property
    def total_s(self) -> float:
        return self.pack_s + self.correction_s + self.enrich_s

    @property
    def release_s(self) -> float:
        """Release to the text mouse 5 types: streaming final, final pass, text pipeline and the new mouse 5."""
        return self.asr_s + self.pass_s + self.pipeline_s + self.total_s


def _today_fields(result: TakeResult) -> dict:
    """Today's mouse 5 of a take (today's hints and correction), reused by its run with the project hints."""
    return {"today": result.today, "today_reason": result.today_reason, "term_errors_today": result.term_errors_today,
            "lost_today": result.lost_today, "invented_today": result.invented_today, "today_s": result.today_s}


def run_take(take: Take, set_name: str, case: str, heard: str, asr_s: float, product: Product,
             today_from: TakeResult | None = None, hints: str = "", *, project: str | None = None,
             enrich_prompt: bool = True, today: bool = True, variant: str = "") -> TakeResult:
    """The take's final text through the app's text pipeline, then today's and the new mouse 5.

    With ``today_from`` (the take's run with today's hints), today's mouse 5
    is that run's, not asked again: only the new mouse 5 runs on ``heard``.
    ``project`` (None: the take's own) names the simulated window's project;
    ``enrich_prompt`` False asks no enrichment; ``today`` False (without
    ``today_from``) asks no today's mouse 5. ``variant`` only labels the
    result: the rewriter is used as it is set.
    """
    from quill.inject import Target

    project = take_project(take) if project is None else project
    info = window(project)
    product.window.info = info
    base = {"id": take.id, "set": set_name, "case": case, "reference": take.clean, "heard": heard, "asr_s": asr_s,
            "hints": hints, "variant": variant, "audio_s": take.duration_s,
            "word_errors_heard": text_edits(take.clean, heard).errors}
    started = product.clock()
    processed = product.pipeline(heard, Target(0, 0)) if heard.strip() else None
    base["pipeline_s"] = max(0.0, product.clock() - started)
    if processed is None or not processed.text.strip():
        today_fields = (_today_fields(today_from) if today_from is not None
                        else {"today_reason": NO_SPEECH if today else ""})
        return TakeResult(**base, **today_fields, reason=NO_SPEECH)
    source = processed.text
    audio_s = take.duration_s
    profile = processed.rewrite_profile or processed.profile
    rewriter = product.rewriter
    if product.hold_start is not None:
        product.hold_start()  # with no hold time: the model call follows at once
    if today_from is None and today:
        today_run = rewriter.rewrite(source, audio_s=audio_s, profile=profile, keep=processed.keep,
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
                               project=processed.project, force=True, pack=pack, enrich_prompt=enrich_prompt,
                               context=True)
    finally:
        rewriter.enricher = capture.enricher
    # The text before enrichment: the correction's, or on every other path (refused, timed out) the dictation
    # with the name pre-step's names, as mouse 5 types it.
    corrected = capture.text if capture.text is not None else new.text
    occurrences, pipeline_errors = term_errors(take.clean, source, take.terms)
    if today_from is not None:
        today_fields = _today_fields(today_from)
    elif today:
        today_fields = {"today": today_run.text, "today_reason": today_run.reason,
                        "term_errors_today": term_errors(take.clean, today_run.text, take.terms)[1],
                        "lost_today": lost_words(take.clean, source, today_run.text),
                        "invented_today": invented_words(source, today_run.text)[0], "today_s": today_run.seconds}
    else:
        today_fields = {}
    names = (*product.names, *take.project_names)
    invented_new, from_pack = invented_words(source, new.text, pack, processed.project, corrected, product.names)
    name_occurrences, name_errors_source = term_errors(take.clean, source, names)
    source_edits, new_edits = text_edits(take.clean, source), text_edits(take.clean, corrected)
    return TakeResult(
        **base, **today_fields, source=source, corrected=corrected, final=new.text,
        project_found=folder is not None, pack=pack_reason, pack_found=pack is not None,
        reason=new.reason, detail=new.detail, enrichment=new.enrichment,
        enrich_detail=new.enrich_detail, term_occurrences=occurrences, term_errors_pipeline=pipeline_errors,
        term_errors_new=term_errors(take.clean, corrected, take.terms)[1],
        lost_new=lost_words(take.clean, source, new.text),
        invented_new=invented_new, pack_outside_context=from_pack,
        pack_s=pack_s, correction_s=new.seconds, enrich_s=new.enrich_seconds,
        enrich_tidied=getattr(capture.result, "tidied", 0) or 0, likely=new.likely,
        name_occurrences=name_occurrences, name_errors_source=name_errors_source,
        name_errors_new=term_errors(take.clean, corrected, names)[1], names_prestep=getattr(new, "names", 0),
        sensible=getattr(new, "sensible", 0), kept=getattr(new, "kept", 0),
        reference_words=source_edits.reference_words, word_errors_source=source_edits.errors,
        word_errors_new=new_edits.errors, invented_reference=invented_reference(take.clean, source, corrected),
        invented_fixes=invented_words(source, corrected, pack, processed.project, names=product.names)[0],
        invented_beyond=invented_beyond_fixes(source, corrected, new.text, pack, processed.project, product.names),
    )


def measure(set_name: str, takes: Sequence[Take], cases: dict[str, str], transcripts: Sequence[tuple[str, float]],
            product: Product) -> list[TakeResult]:
    return [run_take(take, set_name, cases.get(take.id, take.case), heard, asr_s, product)
            for take, (heard, asr_s) in zip(takes, transcripts, strict=True)]


def take_hints(takes: Sequence[Take], product: Product) -> list[tuple[object | None, str]]:
    """The (decoding hints or hint source, or None; reason code) the app gives mouse 5 in each take's window."""
    if product.hints_for is None:
        return [(None, "") for _ in takes]
    return [product.hints_for(window(take_project(take))) for take in takes]


def relevance_hints(hints: object | None) -> object | None:
    """The Phase 7 hints of a take: a hint source's hints with nothing heard; plain hints (or None) as they are."""
    from quill.streaming import is_hint_source

    if not is_hint_source(hints):
        return hints
    return hints.initial if hasattr(hints, "initial") else hints("")


def heard_source(hints: object | None) -> object | None:
    """The take's heard-term hint source; None when the app gives it plain hints or none."""
    from quill.streaming import is_hint_source

    return hints if is_hint_source(hints) else None


def hint_terms(hints: object | None) -> set[str]:
    """Every word the take's hints can hold: the hotwords, and every pack term a hint source may put first."""
    words = set((getattr(relevance_hints(hints), "hotwords", "") or "").split())
    source = heard_source(hints)
    if source is not None:
        words.update(word for term in getattr(source, "terms", ()) if isinstance(term, str) for word in term.split())
    return words


def measure_after(set_name: str, takes: Sequence[Take], cases: dict[str, str],
                  transcripts: Sequence[tuple[str, float]], reasons: Sequence[str],
                  before: Sequence[TakeResult], product: Product, *, switches: Sequence[int] | None = None,
                  reuse: Sequence[Sequence[TakeResult]] = (),
                  passes: Sequence[tuple[float, str]] | None = None) -> list[TakeResult]:
    """Each take's run with project hints, against its run with today's hints (``before``).

    Today's mouse 5 is the earlier run's. A take heard exactly as in an
    earlier pass (each of ``reuse`` in order, then ``before``) gets the same
    text, so that pass's mouse 5 is kept (with this pass's transcription
    time, hint reason and ``switches``) rather than asked of the model again.
    ``passes`` gives each take's final pass (seconds, reason code) when its
    text is a final pass outcome's (``pass_rows``).
    """
    from dataclasses import replace

    counts = list(switches) if switches is not None else [0] * len(takes)
    finals = list(passes) if passes is not None else [(0.0, "")] * len(takes)
    earlier_passes = [list(results) for results in reuse]
    results = []
    for index, (take, (heard, asr_s), reason, earlier, switched, (pass_s, pass_reason)) in enumerate(
            zip(takes, transcripts, reasons, before, counts, finals, strict=True)):
        same = next((runs[index] for runs in (*earlier_passes, before) if runs[index].heard == heard), None)
        if same is not None:
            results.append(replace(same, asr_s=asr_s, hints=reason, hint_switches=switched, pass_s=pass_s,
                                   pass_reason=pass_reason))
        else:
            result = run_take(take, set_name, cases.get(take.id, take.case), heard, asr_s, product,
                              today_from=earlier, hints=reason)
            results.append(replace(result, hint_switches=switched, pass_s=pass_s, pass_reason=pass_reason))
    return results


def pass_rows(transcripts: Sequence[tuple[str, float]], outcomes: Sequence[dict],
              key: str | None) -> tuple[list[tuple[str, float]], list[tuple[float, str]]]:
    """((text, streaming seconds), (pass seconds, reason code)) of each take with the final pass ``key``.

    ``outcomes`` holds each take's ``FinalPassOutcome`` by pass key; the
    outcome's text is the pass text, or the streaming text it fell back to.
    ``key`` None, or a take without that outcome, keeps the streaming text
    without a pass.
    """
    texts, finals = [], []
    for (text, asr_s), chosen in zip(transcripts, outcomes, strict=True):
        outcome = chosen.get(key) if key is not None else None
        if isinstance(outcome, FinalPassOutcome):
            texts.append((outcome.text, asr_s))
            finals.append((outcome.elapsed_s, outcome.reason))
        else:
            texts.append((text, asr_s))
            finals.append((0.0, ""))
    return texts, finals


@contextmanager
def rewriter_variant(rewriter: object, variant: str) -> Iterator[None]:
    """The rewriter set as the correction ``variant`` (``VARIANTS``); its own settings come back afterwards."""
    name_fixes, common_sense = VARIANTS[variant]
    saved = (getattr(rewriter, "name_fixes", autorewrite.NAME_FIXES), getattr(rewriter, "common_sense_fixes", False))
    rewriter.name_fixes, rewriter.common_sense_fixes = name_fixes, common_sense
    try:
        yield
    finally:
        rewriter.name_fixes, rewriter.common_sense_fixes = saved


def app_variant(rewriter: object) -> str | None:
    """The correction variant the rewriter is set as (the app's settings or the override); None: none of them."""
    state = (bool(getattr(rewriter, "name_fixes", autorewrite.NAME_FIXES)),
             bool(getattr(rewriter, "common_sense_fixes", False)))
    return next((name for name, value in VARIANTS.items() if value == state), None)


def measure_variants(set_name: str, takes: Sequence[Take], cases: dict[str, str],
                     transcripts: Sequence[tuple[str, float]], reasons: Sequence[str], after: Sequence[TakeResult],
                     product: Product) -> dict[str, list[TakeResult]]:
    """The new mouse 5 of each correction variant on the transcripts of the app's hint pass (``after``'s).

    The variant the rewriter is set as is the ``after`` run itself; each
    other one asks the new mouse 5 again (today's mouse 5 stays ``after``'s).
    """
    from dataclasses import replace

    main = app_variant(product.rewriter)
    runs: dict[str, list[TakeResult]] = {}
    for variant in VARIANTS:
        if variant == main:
            runs[variant] = [replace(result, variant=variant) for result in after]
            continue
        with rewriter_variant(product.rewriter, variant):
            runs[variant] = [run_take(take, set_name, cases.get(take.id, take.case), heard, asr_s, product,
                                      today_from=earlier, hints=reason, variant=variant)
                             for take, (heard, asr_s), reason, earlier in zip(takes, transcripts, reasons, after,
                                                                              strict=True)]
    return runs


def measure_safety(takes: Sequence[Take], transcripts: Sequence[tuple[str, float]], product: Product,
                   passes: Sequence[tuple[float, str]] | None = None) -> dict[str, list[TakeResult]]:
    """Each correction variant on every dictation take as if dictated into Claude Code without a project.

    Context mode without a project or pack, no enrichment and no today's
    mouse 5: a safety set for what the corrections change in ordinary
    dictation. ``passes``: each take's final pass (seconds, reason code).
    """
    from dataclasses import replace

    finals = list(passes) if passes is not None else [(0.0, "")] * len(takes)
    runs: dict[str, list[TakeResult]] = {}
    for variant in VARIANTS:
        with rewriter_variant(product.rewriter, variant):
            runs[variant] = [replace(run_take(take, SAFETY, take.case, heard, asr_s, product, project="",
                                              enrich_prompt=False, today=False, variant=variant),
                                     pass_s=pass_s, pass_reason=reason)
                             for take, (heard, asr_s), (pass_s, reason) in zip(takes, transcripts, finals,
                                                                               strict=True)]
    return runs


def measure_safety_pass(takes: Sequence[Take], transcripts: Sequence[tuple[str, float]],
                        passes: Sequence[tuple[float, str]], product: Product,
                        reuse: Sequence[Sequence[TakeResult]] = ()) -> list[TakeResult]:
    """The safety set with the app's correction variant on one final pass's texts (``pass_rows``).

    A take heard exactly as in an earlier run (each of ``reuse`` in order)
    keeps that run's mouse 5, with this run's pass time and reason.
    """
    from dataclasses import replace

    variant = app_variant(product.rewriter) or ""
    earlier_runs = [list(results) for results in reuse]
    results = []
    for index, (take, (heard, asr_s), (pass_s, reason)) in enumerate(zip(takes, transcripts, passes, strict=True)):
        same = next((runs[index] for runs in earlier_runs if runs[index].heard == heard), None)
        if same is None:
            same = run_take(take, SAFETY, take.case, heard, asr_s, product, project="", enrich_prompt=False,
                            today=False, variant=variant)
        results.append(replace(same, asr_s=asr_s, pass_s=pass_s, pass_reason=reason))
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


def _ratio(errors: int, words: int) -> float | None:
    return round(errors / words, 4) if words else None


def reference_block(spoken: Sequence[TakeResult]) -> dict:
    """Names, word errors and invented words of the new corrected text against the clean reference; counts only.

    ``transcription`` is the text heard, ``source`` mouse 5's input (after
    the text pipeline), ``corrected`` the text after the correction (before
    enrichment); ``names_fixed`` is source minus corrected.
    """
    words = sum(r.reference_words for r in spoken)
    heard = sum(r.word_errors_heard for r in spoken)
    source = sum(r.word_errors_source for r in spoken)
    corrected = sum(r.word_errors_new for r in spoken)
    names_source = sum(r.name_errors_source for r in spoken)
    names_new = sum(r.name_errors_new for r in spoken)
    return {"name_occurrences": sum(r.name_occurrences for r in spoken),
            "name_errors": {"source": names_source, "corrected": names_new},
            "names_fixed": names_source - names_new,
            "names_prestep": sum(r.names_prestep for r in spoken),
            "common_sense_fixes": sum(r.sensible for r in spoken),
            "kept_as_dictated": sum(r.kept for r in spoken),
            "reference_words": words,
            "word_errors": {"transcription": heard, "source": source, "corrected": corrected},
            "wer": {"transcription": _ratio(heard, words), "source": _ratio(source, words),
                    "corrected": _ratio(corrected, words)},
            "invented_reference": sum(r.invented_reference for r in spoken)}


def variant_block(results: Sequence[TakeResult], terms: bool = False) -> dict:
    """One correction variant of a set: counts, reasons and correction timings; never text, names or terms."""
    spoken = [r for r in results if r.spoken]
    called = [r for r in spoken if r.enrich_called]
    refusals = Counter(r.detail for r in spoken if r.reason == autorewrite.REFUSED)
    return {
        "takes": len(results),
        "no_speech": len(results) - len(spoken),
        "term_errors": {"pipeline": sum(r.term_errors_pipeline for r in spoken),
                        "new": sum(r.term_errors_new for r in spoken)} if terms else None,
        **reference_block(spoken),
        "lost": sum(r.lost_new for r in spoken),
        "invented": sum(r.invented_new for r in spoken),
        "invented_fixes": sum(r.invented_fixes for r in spoken),
        "invented_beyond_fixes": sum(r.invented_beyond for r in spoken),
        "pack_outside_context": sum(r.pack_outside_context for r in spoken),
        "enrichment_requests": len(called),
        "enriched": sum(r.enrichment == enrich.ENRICHED for r in spoken),
        "enrichment_refused": sum(r.enrichment == enrich.REFUSED for r in spoken),
        "reasons": {"correction": dict(sorted(Counter(r.reason for r in spoken).items())),
                    "correction_refusals": dict(sorted(refusals.items()))},
        "latency": {"correction_p50_s": _seconds([r.correction_s for r in spoken], 50),
                    "correction_p95_s": _seconds([r.correction_s for r in spoken], 95),
                    "total_p50_s": _seconds([r.total_s for r in spoken], 50),
                    "total_p95_s": _seconds([r.total_s for r in spoken], 95)},
    }


def variants_block(runs: dict[str, Sequence[TakeResult]], terms: bool = False) -> dict:
    return {name: variant_block(results, terms) for name, results in runs.items()}


def safety_block(runs: dict[str, Sequence[TakeResult]], dataset: dict, app: str | None = None) -> dict:
    """The safety set: every dictation take into Claude Code without a project, per variant; counts only.

    ``latency`` holds the stage timings of the variant ``app`` (the app's settings), when given.
    """
    block = {"status": "measured", "dataset": dataset, "takes": len(next(iter(runs.values()), ())),
             "enrichment": False, "variants": variants_block(runs)}
    if app in runs:
        block["latency"] = stage_latency(runs[app])
    return block


RELEASE_KEY = f"release_up_to_{LONG_TAKE_S:g}s_p95_s"


def stage_latency(results: Sequence[TakeResult]) -> dict:
    """p50/p95 seconds of each stage after the release and of release to text; counts only.

    Stages: the streaming final, the final pass (takes that asked one), the
    text pipeline, the correction, the enrichment (takes that asked one) and
    release to the text mouse 5 types (``TakeResult.release_s``), also as the
    p95 of the takes of at most ``LONG_TAKE_S`` of audio (``RELEASE_KEY``).
    """
    spoken = [r for r in results if r.spoken]
    called = [r for r in spoken if r.enrich_called]
    short = [r for r in spoken if r.audio_s <= LONG_TAKE_S]
    stages = {"streaming_final": [r.asr_s for r in results], "final_pass": [r.pass_s for r in results if r.pass_reason],
              "text_pipeline": [r.pipeline_s for r in spoken], "correction": [r.correction_s for r in spoken],
              "enrichment": [r.enrich_s for r in called], "release": [r.release_s for r in spoken]}
    latency = {f"{name}_p{percent}_s": _seconds(values, percent)
               for name, values in stages.items() for percent in (50, 95)}
    latency[RELEASE_KEY] = _seconds([r.release_s for r in short], 95)
    latency["takes_up_to_limit"] = len(short)
    return latency


def pass_block(results: Sequence[TakeResult], terms: bool = False) -> dict:
    """One final-pass run of a set (or the streaming text alone); counts and timings, never text.

    Word errors against the clean reference after the transcription, after
    the text pipeline (mouse 5's input) and after the full mouse 5 (the
    corrected text, before enrichment, as the Phase 9 WER).
    """
    spoken = [r for r in results if r.spoken]
    called = [r for r in spoken if r.enrich_called]
    words = sum(r.reference_words for r in spoken)
    errors = {"transcription": sum(r.word_errors_heard for r in spoken),
              "pipeline": sum(r.word_errors_source for r in spoken), "full": sum(r.word_errors_new for r in spoken)}
    return {
        "takes": len(results),
        "no_speech": len(results) - len(spoken),
        "passes": dict(sorted(Counter(r.pass_reason for r in results if r.pass_reason).items())),
        "reference_words": words,
        "word_errors": errors,
        "wer": {stage: _ratio(count, words) for stage, count in errors.items()},
        "term_errors": {"pipeline": sum(r.term_errors_pipeline for r in spoken),
                        "full": sum(r.term_errors_new for r in spoken)} if terms else None,
        "name_occurrences": sum(r.name_occurrences for r in spoken),
        "name_errors": {"pipeline": sum(r.name_errors_source for r in spoken),
                        "full": sum(r.name_errors_new for r in spoken)},
        "lost": sum(r.lost_new for r in spoken),
        "invented": sum(r.invented_new for r in spoken),
        "pack_outside_context": sum(r.pack_outside_context for r in spoken),
        "enrichment_requests": len(called),
        "enriched": sum(r.enrichment == enrich.ENRICHED for r in spoken),
        "latency": stage_latency(results),
    }


def pass_settings(settings: FinalPassSettings) -> dict:
    """A final pass's settings as the summary reports them."""
    from dataclasses import asdict

    return asdict(settings)


def pass_choice(section: dict, term_errors_today: int | None) -> dict:
    """Which final pass may ship, from the numbers of each run; ``chosen`` is STREAMING when none qualifies.

    A run qualifies with prompts domain-term errors after the full mouse 5 at
    most ``TARGET_TERM_ERROR_RATIO`` x today's, content words lost, invented
    and pack words outside the context part 0 in every set (and the safety
    set) and release-to-text p95 of the takes up to ``LONG_TAKE_S`` at most
    ``TARGET_RELEASE_P95_S`` in every set. Of the qualifying passes with a
    prompts WER below the streaming text's, the lowest WER wins (then the
    lower p95). The voice commands are measured apart (``bench.voice_commands``).
    """
    sets = section.get("sets") or {}
    safety = section.get(SAFETY) or {}
    prompts = sets.get(SET_NAME) or {}
    keys = list(dict.fromkeys(key for runs in (*sets.values(), safety) for key in runs))
    rows = {}
    for key in keys:
        blocks = [runs[key] for runs in (*sets.values(), safety) if key in runs]
        terms = (prompts.get(key) or {}).get("term_errors")
        p95 = [block["latency"].get(RELEASE_KEY) for block in blocks]
        term_ok = terms is not None and term_errors_today is not None and (
            terms["full"] <= TARGET_TERM_ERROR_RATIO * term_errors_today)
        lost = sum(block["lost"] for block in blocks)
        invented = sum(block["invented"] + block["pack_outside_context"] for block in blocks)
        latency_ok = bool(p95) and all(value is not None and value <= TARGET_RELEASE_P95_S for value in p95)
        rows[key] = {"prompts_wer": ((prompts.get(key) or {}).get("wer") or {}).get("full"),
                     "term_errors": terms["full"] if terms else None, "lost": lost, "invented": invented,
                     "release_p95_s": max((v for v in p95 if v is not None), default=None),
                     "qualifies": term_ok and lost <= TARGET_LOST and invented <= TARGET_INVENTED and latency_ok}
    baseline = (rows.get(STREAMING) or {}).get("prompts_wer")
    better = [key for key, row in rows.items() if key != STREAMING and row["qualifies"]
              and row["prompts_wer"] is not None and baseline is not None and row["prompts_wer"] < baseline]
    chosen = min(better, key=lambda key: (rows[key]["prompts_wer"], rows[key]["release_p95_s"] or 0.0, key == APP,
                                          key)) if better else STREAMING
    wer = rows.get(chosen, {}).get("prompts_wer")
    return {"runs": rows, "chosen": chosen, "prompts_wer": wer,
            "prompts_wer_target_met": wer is not None and wer <= TARGET_PROMPTS_WER}


def common_sense_rule(blocks: dict[str, dict]) -> dict | None:
    """Whether the common-sense fixes may ship on, from each set's variants (None: no variants measured).

    Against name fixes only: fewer reference word errors in all and none
    more in any set, with content words lost 0, invented against the input
    beyond the correction's own fixes 0 (``invented_beyond_fixes``) and
    invented against the reference 0 in every set. A common-sense fix always
    brings words the input lacks (``invented_fixes``, reported); whether
    they are what was said is ``invented_reference``. Computed from the
    numbers only; the decision itself stays with the run that ships it.
    """
    measured = {name: block["variants"] for name, block in blocks.items()
                if isinstance(block, dict) and isinstance(block.get("variants"), dict)
                and NAMES in block["variants"] and COMMON_SENSE in block["variants"]}
    if not measured:
        return None
    sets = {}
    for name, variants in measured.items():
        off, on = variants[NAMES], variants[COMMON_SENSE]
        sets[name] = {"word_errors_off": off["word_errors"]["corrected"],
                      "word_errors_on": on["word_errors"]["corrected"], "lost": on["lost"],
                      "invented_beyond_fixes": on["invented_beyond_fixes"], "invented_fixes": on["invented_fixes"],
                      "invented_reference": on["invented_reference"]}
    off_total = sum(entry["word_errors_off"] for entry in sets.values())
    on_total = sum(entry["word_errors_on"] for entry in sets.values())
    met = on_total < off_total and all(
        entry["word_errors_on"] <= entry["word_errors_off"] and entry["lost"] == 0
        and entry["invented_beyond_fixes"] == 0 and entry["invented_reference"] == 0 for entry in sets.values())
    return {"compared": [NAMES, COMMON_SENSE], "word_errors": {"off": off_total, "on": on_total}, "sets": sets,
            "met": met}


def likely_block(spoken: Sequence[TakeResult]) -> dict:
    """Takes whose new correction prompt listed terms that sound like the dictation, and those terms in all."""
    return {"takes": sum(r.likely > 0 for r in spoken), "terms": sum(r.likely for r in spoken)}


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
        "enrichment_refused": sum(r.enrichment == enrich.REFUSED for r in spoken),
        "tidied": {"replies": sum(r.enrich_tidied > 0 for r in called),
                   "enriched": sum(r.enrich_tidied > 0 and r.enrichment == enrich.ENRICHED for r in called)},
        "likely_terms": likely_block(spoken),
        "against_reference": reference_block(spoken),
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
            **stage_latency(results),
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
    """The new mouse 5 of a set with one pass's decoding hints (today's or the Phase 7 project hints; counts only)."""
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
        "enrichment_requests": len(called),
        "enrichment_refused": sum(r.enrichment == enrich.REFUSED for r in spoken),
        "likely_terms": likely_block(spoken),
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


def heard_block(results: Sequence[TakeResult], before: Sequence[TakeResult], sources: int) -> dict:
    """How the heard-term hints changed the set against the Phase 7 hints (``before``); counts only.

    ``sources``: takes the app gave a heard-term hint source; ``takes_switched``:
    those whose source changed the hints at least once (``switches`` in all);
    ``heard_changed``: takes heard otherwise than with the Phase 7 hints.
    """
    return {"sources": sources, "takes_switched": sum(r.hint_switches > 0 for r in results),
            "switches": sum(r.hint_switches for r in results),
            "heard_changed": sum(r.heard != b.heard for r, b in zip(results, before, strict=True))}


def pending_block(dataset: dict) -> dict:
    return {"status": "pending_recordings", "dataset": dataset, "takes": 0}


def targets() -> dict:
    return {"term_errors_ratio_max": TARGET_TERM_ERROR_RATIO, "lost_max": TARGET_LOST,
            "invented_max": TARGET_INVENTED, "pack_outside_context_max": TARGET_INVENTED,
            "release_p95_s_max": TARGET_RELEASE_P95_S, "release_takes_audio_s_max": LONG_TAKE_S,
            "prompts_wer_max": TARGET_PROMPTS_WER}


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
    safety = summary.get(SAFETY)
    releases = [(name, (block.get("latency") or {}).get(RELEASE_KEY)) for name, block in
                (*measured.items(), *([(SAFETY, safety)] if isinstance(safety, dict) else []))]
    known = [(name, value) for name, value in releases if value is not None]
    lines.append((bool(known) and len(known) == len(releases) and all(v <= TARGET_RELEASE_P95_S for _, v in known),
                  f"release-to-text p95 (takes up to {LONG_TAKE_S:g} s): "
                  + (", ".join(f"{name} {value:g} s" for name, value in known) if known else "not measured")
                  + f"; target at most {TARGET_RELEASE_P95_S:g} s"))
    return lines


def prompts_wer_line(summary: dict) -> tuple[bool, str]:
    """The prompts WER after the full mouse 5 against its target: reported, not an exit-code target.

    The goal ships the best safe improvement when the target is not met; the
    target itself is never lowered.
    """
    prompts = (summary.get("sets") or {}).get(SET_NAME)
    reference = prompts.get("against_reference") if isinstance(prompts, dict) else None
    if not isinstance(reference, dict) or reference.get("wer", {}).get("corrected") is None:
        return False, "prompts WER after the full mouse 5 pipeline: not measured"
    wer, errors = reference["wer"], reference["word_errors"]
    heard = wer.get("transcription")
    return (wer["corrected"] <= TARGET_PROMPTS_WER,
            f"prompts WER after the full mouse 5 pipeline: {wer['corrected']:.1%} ({errors['corrected']} of "
            f"{reference['reference_words']} words; after the text pipeline {wer['source']:.1%}"
            + (f", after transcription {heard:.1%}" if heard is not None else "")
            + f"); target at most {TARGET_PROMPTS_WER:.0%} (reported, not an exit code)")


def build_summary(blocks: dict[str, dict], engine: dict, rewrite: dict, safety: dict | None = None,
                  final_pass: dict | None = None) -> dict:
    summary: dict = {"schema": SUMMARY_SCHEMA, "kind": "mouse5_prompts",
                     "status": "measured" if any(b.get("status") == "measured" for b in blocks.values())
                     else "pending_recordings",
                     "targets": targets(), "sets": blocks, "engine": engine, "rewrite": rewrite}
    if safety is not None:
        summary[SAFETY] = safety
    if final_pass is not None:
        summary["final_pass"] = final_pass
    rule = common_sense_rule({**blocks, **({SAFETY: safety} if safety is not None else {})})
    if rule is not None:
        summary["common_sense_rule"] = rule
    checks = check_targets(summary)
    names = ("complete", "term_errors", "lost", "invented", "pack_outside_context", "release_p95")
    summary["meets_targets"] = {name: met for name, (met, _) in zip(names, checks, strict=True)}
    # "all": the exit-code targets of --require; the prompts WER target is reported beside them.
    summary["meets_targets"]["all"] = all(summary["meets_targets"].values())
    summary["meets_targets"]["prompts_wer"] = prompts_wer_line(summary)[0]
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
             "hint_switches": r.hint_switches,
             "project_found": r.project_found, "pack": r.pack, "today_reason": r.today_reason, "reason": r.reason,
             "detail": r.detail, "enrichment": r.enrichment, "enrich_detail": r.enrich_detail,
             "enrich_tidied": r.enrich_tidied,
             "term_occurrences": r.term_occurrences, "term_errors_pipeline": r.term_errors_pipeline,
             "term_errors_today": r.term_errors_today, "term_errors_new": r.term_errors_new,
             "lost_today": r.lost_today, "lost_new": r.lost_new, "invented_today": r.invented_today,
             "invented_new": r.invented_new, "pack_outside_context": r.pack_outside_context,
             "asr_s": round(r.asr_s, 3), "today_s": round(r.today_s, 3), "pack_s": round(r.pack_s, 3),
             "correction_s": round(r.correction_s, 3), "enrich_s": round(r.enrich_s, 3), "variant": r.variant,
             "name_occurrences": r.name_occurrences, "name_errors_source": r.name_errors_source,
             "name_errors_new": r.name_errors_new, "names_prestep": r.names_prestep, "sensible": r.sensible,
             "kept": r.kept, "reference_words": r.reference_words, "word_errors_source": r.word_errors_source,
             "word_errors_new": r.word_errors_new, "invented_reference": r.invented_reference,
             "invented_fixes": r.invented_fixes, "invented_beyond": r.invented_beyond, "audio_s": round(r.audio_s, 3),
             "word_errors_heard": r.word_errors_heard, "pipeline_s": round(r.pipeline_s, 3),
             "pass_s": round(r.pass_s, 3), "pass_reason": r.pass_reason} for r in results]


def write_private(run_dir: Path, results: Sequence[TakeResult], results_dir: Path = RESULTS_DIR,
                  before: Sequence[TakeResult] = (), today: Sequence[TakeResult] = (),
                  extra: dict[str, Sequence[TakeResult]] | None = None) -> tuple[Path, Path]:
    """Per-take JSON and the before/after enrichment examples, only under bench/results/.

    ``before`` (the runs with the Phase 7 project hints) go to
    ``takes-before.json``, ``today`` (the runs with today's decoding hints)
    to ``takes-today.json`` and each of ``extra`` (the correction variants
    and the safety set) to ``takes-<key>.json``.
    """
    run_dir = Path(run_dir)
    if not _inside(run_dir / "takes.json", results_dir):
        raise ValueError("prompt outputs with spoken text may only be written under bench/results/")
    run_dir.mkdir(parents=True, exist_ok=True)
    takes_path = run_dir / "takes.json"
    takes_path.write_text(json.dumps(_rows(results), ensure_ascii=False, indent=2), encoding="utf-8")
    named = [("takes-before.json", before), ("takes-today.json", today)]
    named += [(f"takes-{key}.json", rows) for key, rows in (extra or {}).items()]
    for name, rows in named:
        if rows:
            (run_dir / name).write_text(json.dumps(_rows(rows), ensure_ascii=False, indent=2), encoding="utf-8")
    heard_today = {r.id: r.heard for r in today}
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
        if r.id in heard_today and heard_today[r.id] != r.heard:
            lines += ["Ouvido com as dicas de hoje (antes dos termos do projeto):", *_quoted(heard_today[r.id]), ""]
        if r.id in heard_before and heard_before[r.id] != r.heard:
            lines += ["Ouvido com os termos do projeto da fase 7 (antes dos termos ouvidos):",
                      *_quoted(heard_before[r.id]), ""]
        lines += ["Chegou ao rato 5:", *_quoted(r.source), "",
                  "Rato 5 hoje:", *_quoted(r.today), "", "Corrigido com contexto:", *_quoted(r.corrected), "",
                  "Enviado (novo):", *_quoted(r.final), "", f"`{status}`", "", "Sponsor:"]
    examples_path = run_dir / "exemplos.md"
    examples_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return takes_path, examples_path


def private_texts(results: Sequence[TakeResult]) -> list[str]:
    return [text for r in results for text in (r.reference, r.heard, r.source, r.today, r.corrected, r.final) if text]


def refused_project_words(words: Iterable[str]) -> list[str]:
    """The project hint words the summary refuses: the distinctive ones, except the Whisper model names.

    The summary names its models (``[final_pass] model``, the engine); a
    project whose pack lists a public model name would otherwise refuse
    every summary.
    """
    public = {" ".join(normalize_words(name)) for name in MODELS}
    return sorted(word for word in words if distinctive_term(word) and " ".join(normalize_words(word)) not in public)


# ---------------------------------------------------------------- defaults (GPU, Ollama and the app's config)


# stream(takes, hints=None): the final text, release-to-final seconds and hint switches of each take (a pair
# counts no switch); ``hints`` (one per take, None: the vocabulary hints; plain hints or a hint source) open
# each take's session as the app gives mouse 5 its decoding hints. A streamer built with final passes adds to each
# take a fourth item: its ``FinalPassOutcome`` by pass key. ``stream.snapshot(label)`` (optional) returns a read-only
# GPU and Ollama snapshot.
Streamer = Callable[..., list[tuple]]


def split_streamed(items: Sequence[tuple]) -> tuple[list[tuple[str, float]], list[int]]:
    """((text, seconds) of each take, its hint switches) from a streamer's output."""
    pairs = [(item[0], item[1]) for item in items]
    switches = [int(item[2]) if len(item) > 2 else 0 for item in items]
    return pairs, switches


def split_passes(items: Sequence[tuple]) -> list[dict]:
    """Each take's final pass outcomes by pass key from a streamer's output ({} without passes)."""
    return [dict(item[3]) if len(item) > 3 and isinstance(item[3], dict) else {} for item in items]


def replay_session(transcriber: object, pcm: bytes, hints: object | None = None) -> tuple[object, object]:
    """``bench.streaming.replay_deterministic``, keeping the released session for its final pass."""
    from bench.streaming import CHUNK_S, FINAL_TIMEOUT_S, chunks

    session = transcriber.open() if hints is None else transcriber.open(hints=hints)
    for chunk in chunks(pcm, CHUNK_S):
        session.feed(chunk)
        if not transcriber.drain(FINAL_TIMEOUT_S):
            raise TimeoutError("streaming worker did not become idle")
    result = session.release().wait(FINAL_TIMEOUT_S)
    if result is None:
        raise TimeoutError("final text not ready in time")
    return session, result


def gpu_snapshot(label: str) -> dict:
    """GPU memory and utilization and the models loaded in the shared Ollama; read-only (nvidia-smi, /api/ps)."""
    from bench.streaming import gpu_state

    state = gpu_state()
    state.pop("time", None)
    return {"label": label, **state}


def default_streamer(hints: Sequence[str], passes: dict[str, FinalPassSettings] | None = None) -> tuple[Streamer, dict]:
    """The product streaming path with the app's engine model and vocabulary ``hints``; ``stream.close`` frees it.

    With ``passes`` each released take also gets each final pass
    (``quill.finalpass.run`` with the session's hints and hint source), as
    the app runs it: a pass on the engine model decodes on the streaming
    transcriber's worker, a pass on the other model on one second
    transcriber of that model (one instance, loaded at the first take).
    """
    from dataclasses import asdict

    from bench.engines.base import EngineError, EngineUnavailable, wav_pcm
    from bench.pipeline import ENGINE_COMPUTE, STREAM_MODEL
    from quill import finalpass
    from quill.config import load_config
    from quill.streaming import StreamingTranscriber, options_for
    from quill.whisper import Whisper

    name = load_config().engine_model or STREAM_MODEL
    options = options_for(name)
    model = Whisper(name, compute_type=ENGINE_COMPUTE)
    vocabulary = list(hints)
    passes = dict(passes or {})
    others = sorted({settings.model for settings in passes.values()} - {name})
    if len(others) > 1:
        raise ValueError("the final passes may use one model besides the engine model")
    second: dict[str, StreamingTranscriber] = {}

    def pass_transcriber(model_name: str) -> StreamingTranscriber:
        if model_name not in second:
            transcriber = StreamingTranscriber(Whisper(model_name, compute_type=ENGINE_COMPUTE),
                                               options_for(model_name), vocabulary)
            transcriber.start()
            second[model_name] = transcriber
        transcriber = second[model_name]
        transcriber.ready.wait()
        if transcriber.load_error:
            raise EngineUnavailable(transcriber.load_error)
        return transcriber

    def stream(takes: Sequence[Take], session_hints: Sequence[object | None] | None = None) -> list[tuple]:
        per_take = list(session_hints) if session_hints is not None else [None] * len(takes)
        transcriber = StreamingTranscriber(model, options, vocabulary)
        transcriber.start()
        try:
            transcriber.ready.wait()
            if transcriber.load_error:
                raise EngineUnavailable(transcriber.load_error)
            workers = {key: transcriber if settings.model == name else pass_transcriber(settings.model)
                       for key, settings in passes.items()}
            out = []
            for take, take_hints in zip(takes, per_take, strict=True):
                session, result = replay_session(transcriber, wav_pcm(take.path.read_bytes())[0], take_hints)
                if not result.ok:
                    raise EngineError(f"streamed final failed: {result.error}")
                item = (result.text, result.latency_s, result.hint_switches)
                if passes:
                    item += ({key: finalpass.run_session(workers[key], session, result, settings)
                              for key, settings in passes.items()},)
                out.append(item)
            return out
        finally:
            transcriber.stop(close_model=False)

    def close() -> None:
        for transcriber in second.values():
            transcriber.stop()
        model.close()

    stream.close = close
    stream.snapshot = gpu_snapshot
    return stream, {"model": name, **asdict(options)}


def default_product(vocabulary: object, generic_terms: Sequence[str], product_timeouts: bool = False,
                    correction_candidates: bool = autorewrite.LIKELY_TERMS,
                    common_sense: bool | None = None) -> Product:
    """The app's text pipeline, project folders, context packs and rewriter from its config, warmed up.

    The rewriter gets ``BENCH_TIMEOUT_S`` for both calls; the local model is
    only listed and asked one short warm-up turn: nothing is pulled, loaded
    on purpose or unloaded. With ``product_timeouts`` the rewriter is the
    app's own, with the config's timeouts, and the model is left as it is
    (no warm-up turn here), so a first call may meet a model that is not
    loaded. ``correction_candidates`` lists the terms that sound like the
    dictation first in the correction prompt (``AutoRewriter(likely_terms=
    ...)``; the app's default when not given). ``common_sense`` overrides
    ``[autorewrite] common_sense_fixes`` (None: the config's). As in the app,
    the rewriter's name fixes read the personal vocabulary names. Raises
    SettingsError when Ollama cannot answer.
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
    names = tuple(entry.text for entry in getattr(vocabulary, "names", ()))
    switches = {"likely_terms": correction_candidates, "names": lambda: names, "common_sense_fixes": common_sense}
    if product_timeouts:
        # As the app: the warm-up at each mouse 5 hold and the bounded wait before a correction.
        warmer = ModelWarmer(client, config.ollama_model)
        rewriter = autorewrite.AutoRewriter(client, config.ollama_model, config.autorewrite, warmer=warmer,
                                            **switches)
    else:
        settings = replace(config.autorewrite, timeout_s=BENCH_TIMEOUT_S, enrich_timeout_s=BENCH_TIMEOUT_S)
        rewriter = autorewrite.AutoRewriter(client, config.ollama_model, settings, **switches)
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
    from quill.app import final_pass_on

    final_pass = config.final_pass if final_pass_on(config) else replace(config.final_pass, enabled=False)
    return Product(pipeline, holder, rewriter, names, packs.lookup, info,
                   hold_start=(lambda: warmer.warm("mouse 5 hold")) if warmer is not None else None,
                   model_loaded=lambda: config.ollama_model in client.loaded(),
                   hints_for=app_hints(pipeline, packs.get, vocabulary, generic_terms), final_pass=final_pass)


def app_hints(pipeline: object, pack_for: Callable[[object], object | None], vocabulary: object,
              generic_terms: Sequence[str]) -> Callable[[WindowInfo], tuple[object | None, str]]:
    """The app's mouse 5 decoding hints (``quill.app.project_hints``) for a simulated window: no process, title only."""
    from quill.app import project_hints

    def hints_for(info: WindowInfo) -> tuple[object | None, str]:
        return project_hints(info, 0, profiles=pipeline.profiles, projects=pipeline.projects, pack_for=pack_for,
                             vocabulary=vocabulary, generic_terms=generic_terms)

    return hints_for


def pass_candidates(text: str | None) -> list[str]:
    """The candidate names of ``--pass-candidates`` ('all' or a comma list); ValueError names an unknown one."""
    if text is None:
        return []
    if text.strip() == "all":
        return list(PASS_CANDIDATES)
    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [name for name in names if name not in PASS_CANDIDATES]
    if unknown or not names:
        raise ValueError(f"--pass-candidates takes 'all' or names among {', '.join(PASS_CANDIDATES)}")
    return list(dict.fromkeys(names))


def config_pass(settings: FinalPassSettings | None) -> FinalPassSettings:
    """The app's ``[final_pass]`` (``Product.final_pass``); None is off with the section's defaults."""
    return settings if isinstance(settings, FinalPassSettings) else FinalPassSettings(enabled=False)


def main_pass_settings(settings: FinalPassSettings | None, args: argparse.Namespace) -> FinalPassSettings:
    """The main run's final pass: the app's ``[final_pass]`` with ``--final-pass`` and the ``--pass-*`` overrides.

    ValueError when an override is out of the section's range.
    """
    from dataclasses import replace

    overrides = {field_name: value for field_name, value in (
        ("enabled", args.final_pass), ("model", args.pass_model), ("beam_size", args.pass_beam),
        ("temperature_fallback", args.pass_temperature_fallback), ("hints", args.pass_hints),
        ("timeout_s", args.pass_timeout)) if value is not None}
    return replace(config_pass(settings), **overrides)


def candidate_pass(settings: FinalPassSettings | None, name: str) -> FinalPassSettings:
    """The candidate ``name`` of ``PASS_CANDIDATES``: the app's ``[final_pass]`` on, with the candidate's overrides."""
    from dataclasses import replace

    return replace(config_pass(settings), enabled=True, **PASS_CANDIDATES[name])


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
    safety_takes: tuple[Take, ...] = field(default=(), repr=False)  # every valid dictation take (--safety)


def load_sets(settings: Settings, chosen: Sequence[str], safety: bool = False) -> Loaded:
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
    if safety:
        if loaded.dictation is None:
            loaded.dictation = load_dataset(settings.for_set(DICTATION))
        loaded.safety_takes = tuple(loaded.dictation.takes)
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
            fixture: Callable[..., Fixture] = default_fixture, safety: bool = False) -> tuple[int, bool]:
    """Counts only; opens no audio, model, microphone or Ollama. Returns (exit code, prompts complete)."""
    try:
        loaded = load_sets(settings, chosen, safety)
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
    if loaded.dictation is not None and DICTATION in chosen:
        out(f"dictation claude-code takes: {len(loaded.dictation_takes)} recorded of {loaded.dictation_rows} "
            "script rows")
    if safety:
        out(f"safety set: {len(loaded.safety_takes)} valid dictation takes, each correction variant into Claude Code "
            "without a project")
    return 0, done


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def after_view(block: dict) -> dict:
    """The new mouse 5 of a measured set block in the shape of ``before_block``."""
    errors = block.get("term_errors")
    return {"term_errors": {"pipeline": errors["pipeline"], "new": errors["new"]} if errors else None,
            "lost": block["lost"]["new"], "invented": block["invented"]["new"],
            "pack_outside_context": block["pack_outside_context"], "enriched": block["enriched"],
            "enrichment_requests": block.get("enrichment_requests", "?"),
            "enrichment_refused": block.get("enrichment_refused", "?"), "latency": block["latency"]}


def pass_line(view: dict) -> str:
    """One pass's counts and p50/p95 seconds (``before_block`` shape) on one line."""
    errors = view.get("term_errors")
    latency = view.get("latency") or {}

    def pair(name: str) -> str:
        return f"{_pct(latency.get(name + '_p50_s'))} / {_pct(latency.get(name + '_p95_s'))}"

    return ((f"domain-term errors pipeline {errors['pipeline']}, final {errors['new']}; " if errors else "")
            + f"lost {view['lost']}, invented {view['invented']}, pack words outside context "
              f"{view['pack_outside_context']}; enrichment requested {view.get('enrichment_requests', '?')}, accepted "
              f"{view['enriched']}, refused {view.get('enrichment_refused', '?')}; p50/p95 s: transcription "
              f"{pair('transcription')}, correction {pair('correction')}, enrichment {pair('enrichment')}, mouse 5 "
              f"{pair('total')}")


def variant_line(name: str, view: dict) -> str:
    """One correction variant's counts and correction p50/p95 seconds (``variant_block`` shape) on one line."""
    errors = view.get("term_errors")
    names, words, wer = view["name_errors"], view["word_errors"], view["wer"]
    latency = view.get("latency") or {}
    return (f"{name}: names fixed {view['names_fixed']} of {view['name_occurrences']} (missing {names['source']} -> "
            f"{names['corrected']}; pre-step {view['names_prestep']}); "
            + (f"domain-term errors {errors['new']}; " if errors else "")
            + f"word errors {words['source']} -> {words['corrected']} of {view['reference_words']} (WER "
              f"{_pct(wer['source'])} -> {_pct(wer['corrected'])}); lost {view['lost']}, invented {view['invented']} "
              f"(by the fixes {view['invented_fixes']}, beyond them {view['invented_beyond_fixes']}), invented vs "
              f"reference {view['invented_reference']}; common-sense fixes {view['common_sense_fixes']}; "
              f"enrichment requested {view['enrichment_requests']}, accepted {view['enriched']}; correction p50/p95 s "
              f"{_pct(latency.get('correction_p50_s'))} / {_pct(latency.get('correction_p95_s'))}")


def _variant_lines(block: dict, indent: str = "  ") -> list[str]:
    variants = block.get("variants")
    if not isinstance(variants, dict):
        return []
    app = block.get("variant_app")
    return [f"{indent}variant {variant_line(name, view)}" + (" (the app's settings)" if name == app else "")
            for name, view in variants.items()]


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
        heard = block.get("heard_hints")
        passes = [("before (relevance hints)", block.get("relevance_hints"))]
        if isinstance(heard, dict):
            passes.append(("after (heard hints)", after_view(block)))
        passes.append(("with today's hints", block.get("today_hints")))
        lines += [f"  {label}: {pass_line(view)}" for label, view in passes if isinstance(view, dict)]
        if isinstance(heard, dict):
            lines.append(f"  heard-term hints: switched in {heard['takes_switched']} of {heard['sources']} takes "
                         f"({heard['switches']} switches); heard otherwise than before {heard['heard_changed']}")
        likely = block.get("likely_terms")
        if isinstance(likely, dict):
            lines.append(f"  correction prompts listing terms that sound like the dictation: {likely['takes']} "
                         f"({likely['terms']} terms)")
        if block.get("hints"):
            lines.append("  decoding hints: " + ", ".join(f"{k or 'today'} {v}" for k, v in block["hints"].items()))
        over = block.get("over_product_timeout") or {}
        if over.get("correction") is not None:
            lines.append(f"  over the product timeout: correction {over['correction']} of "
                         f"{over.get('correction_calls', '?')} calls, enrichment {over.get('enrichment')}")
        lines += _variant_lines(block)
    safety = summary.get(SAFETY)
    if isinstance(safety, dict):
        lines.append(f"safety: {safety['takes']} dictation takes into Claude Code without a project, no enrichment")
        lines += _variant_lines(safety)
    rule = summary.get("common_sense_rule")
    if isinstance(rule, dict):
        lines.append(f"common-sense rule: {'met' if rule['met'] else 'NOT met'} (word errors with name fixes only "
                     f"{rule['word_errors']['off']}, with common sense {rule['word_errors']['on']}; lost, invented "
                     "beyond the fixes and invented vs reference must be 0 in every set)")
    lines += final_pass_lines(summary.get("final_pass"))
    rewrite = summary.get("rewrite") or {}
    if isinstance(rewrite.get("common_sense_fixes"), bool):
        lines.append(f"correction settings: name fixes {'on' if rewrite.get('name_fixes') else 'off'}, common-sense "
                     f"fixes {'on' if rewrite['common_sense_fixes'] else 'off'}")
    candidates = (summary.get("rewrite") or {}).get("correction_candidates")
    if isinstance(candidates, bool) and candidates is not autorewrite.LIKELY_TERMS:
        lines.append(f"correction candidates: {'on' if candidates else 'off'} (ablation)")
    first = (summary.get("rewrite") or {}).get("first_call")
    if first:
        lines.append(f"first model call: {first['seconds']:g} s, {first['reason']} (model loaded before: "
                     f"{first['model_loaded_before']})")
    return lines


def settings_text(settings: dict) -> str:
    """A final pass's settings on one line (``pass_settings`` shape)."""
    if not settings.get("enabled"):
        return "off"
    switches = [name for name in ("temperature_fallback", "condition_on_previous_text", "vad_filter")
                if settings.get(name)]
    return (f"{settings.get('model')}, beam {settings.get('beam_size')}, hints "
            f"{'on' if settings.get('hints') else 'off'}, timeout {settings.get('timeout_s')} s"
            + "".join(f", {name}" for name in switches))


def pass_view_line(name: str, view: dict) -> str:
    """One final-pass run of a set (``pass_block`` shape) on one line."""
    wer, errors, latency = view["wer"], view["word_errors"], view.get("latency") or {}
    terms = view.get("term_errors")

    def pair(stage: str) -> str:
        return f"{_pct(latency.get(stage + '_p50_s'))} / {_pct(latency.get(stage + '_p95_s'))}"

    return (f"{name}: WER transcription {_pct(wer['transcription'])}, text pipeline {_pct(wer['pipeline'])}, full "
            f"mouse 5 {_pct(wer['full'])} ({errors['full']} of {view['reference_words']}); "
            + (f"domain-term errors {terms['full']}; " if terms else "")
            + f"names missing {view['name_errors']['full']} of {view['name_occurrences']}; lost {view['lost']}, "
              f"invented {view['invented']}; enrichment requested {view['enrichment_requests']}, accepted "
              f"{view['enriched']}; passes " + (", ".join(f"{k} {v}" for k, v in view["passes"].items()) or "none")
            + f"; p50/p95 s: streaming final {pair('streaming_final')}, final pass {pair('final_pass')}, text "
              f"pipeline {pair('text_pipeline')}, correction {pair('correction')}, enrichment {pair('enrichment')}, "
              f"release to text {pair('release')} (p95 up to {LONG_TAKE_S:g} s {_pct(latency.get(RELEASE_KEY))})")


def final_pass_lines(section: object) -> list[str]:
    """The final pass part of the report: settings, each run per set, GPU snapshots and the choice."""
    if not isinstance(section, dict):
        return []
    lines = [f"final pass (main run): {settings_text(section.get('main') or {})}"]
    for name, settings in (section.get("candidates") or {}).items():
        lines.append(f"  candidate {name}: {settings_text(settings)}")
    for set_name, runs in [*(section.get("sets") or {}).items(), (SAFETY, section.get(SAFETY) or {})]:
        if runs:
            lines.append(f"  {set_name}:")
            lines += [f"    {pass_view_line(key, view)}" for key, view in runs.items()]
    for state in section.get("gpu") or []:
        loaded = ", ".join(f"{m.get('name')} {m.get('vram_mib')} MiB" for m in state.get("ollama_loaded") or [])
        lines.append(f"  GPU {state.get('label')}: " + (
            f"used {state.get('used_mib')} MiB, free {state.get('free_mib')} MiB; Ollama loaded: {loaded or 'none'}"
            if "used_mib" in state else str(state.get("error"))))
    choice = section.get("choice")
    if isinstance(choice, dict):
        lines.append(f"  choice: {choice['chosen']} (prompts WER {_pct(choice['prompts_wer'])}; qualifying: "
                     + (", ".join(k for k, row in choice["runs"].items() if row["qualifies"]) or "none") + ")")
    return lines


def require_lines(summary: dict, out: Callable[[str], None]) -> int:
    """Each target met or NOT met; exit 1 when an exit-code target is unmet (the prompts WER is only reported)."""
    code = 0
    for met, text in check_targets(summary):
        out(f"{'met' if met else 'NOT met'}: {text}")
        code = code or (0 if met else 1)
    met, text = prompts_wer_line(summary)
    out(f"{'met' if met else 'NOT met'}: {text}")
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
    candidates = parser.add_mutually_exclusive_group()
    candidates.add_argument("--correction-candidates", dest="correction_candidates", action="store_const",
                            const=True, default=None,
                            help="ablation: list the terms that sound like the dictation first in the correction "
                                 "prompt (the app does not)")
    candidates.add_argument("--no-correction-candidates", dest="correction_candidates", action="store_const",
                            const=False, help="leave them out (the app's default)")
    common = parser.add_mutually_exclusive_group()
    common.add_argument("--common-sense", dest="common_sense", action="store_const", const=True, default=None,
                        help="the main run fixes short misheard groups with ordinary words (overrides "
                             "[autorewrite] common_sense_fixes)")
    common.add_argument("--no-common-sense", dest="common_sense", action="store_const", const=False,
                        help="the main run keeps the terms-only rule (overrides [autorewrite] common_sense_fixes)")
    parser.add_argument("--variants", action="store_true",
                        help="also run the Phase 8, names-only and common-sense corrections on the transcripts of "
                             "the app's hint pass")
    parser.add_argument("--safety", action="store_true",
                        help="also run each correction variant on every valid dictation take, into Claude Code "
                             "without a project (no enrichment)")
    final = parser.add_mutually_exclusive_group()
    final.add_argument("--final-pass", dest="final_pass", action="store_const", const=True, default=None,
                       help="the main run decodes mouse 5 once more on release (overrides [final_pass] enabled)")
    final.add_argument("--no-final-pass", dest="final_pass", action="store_const", const=False,
                       help="the main run types the streaming text (overrides [final_pass] enabled)")
    parser.add_argument("--pass-model", choices=tuple(MODELS), default=None,
                        help="the main run's final pass model (overrides [final_pass] model)")
    parser.add_argument("--pass-beam", type=int, default=None, help="overrides [final_pass] beam_size (1-10)")
    fallback = parser.add_mutually_exclusive_group()
    fallback.add_argument("--pass-temperature-fallback", dest="pass_temperature_fallback", action="store_const",
                          const=True, default=None, help="overrides [final_pass] temperature_fallback")
    fallback.add_argument("--no-pass-temperature-fallback", dest="pass_temperature_fallback", action="store_const",
                          const=False, help="overrides [final_pass] temperature_fallback")
    pass_hints = parser.add_mutually_exclusive_group()
    pass_hints.add_argument("--pass-hints", dest="pass_hints", action="store_const", const=True, default=None,
                            help="overrides [final_pass] hints")
    pass_hints.add_argument("--no-pass-hints", dest="pass_hints", action="store_const", const=False,
                            help="overrides [final_pass] hints")
    parser.add_argument("--pass-timeout", type=float, default=None, help="overrides [final_pass] timeout_s")
    parser.add_argument("--pass-candidates", default=None,
                        help="also measure these final passes against the streaming text: 'all' or a comma list of "
                             + ", ".join(PASS_CANDIDATES))
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
        candidates = pass_candidates(args.pass_candidates)
    except ValueError as exc:
        out(f"error: {exc}")
        return 2
    try:
        settings = load_settings(args.config)
        if args.dry_run:
            code, done = dry_run(settings, chosen, out, fixture, safety=args.safety)
            if code == 0 and args.require and not done:
                out("NOT met: the prompts set has missing takes (py -3.12 -m bench.record --set prompts)")
                return 1
            return code
        loaded = load_sets(settings, chosen, safety=args.safety)
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
    if loaded.dictation is not None and DICTATION in chosen:
        if loaded.dictation_takes:
            work.append((DICTATION, loaded.dictation_takes, {}))
        else:
            blocks[DICTATION] = pending_block({"script_rows": loaded.dictation_rows, "recorded": 0,
                                               "pending": loaded.dictation_rows, "invalid": 0})
    if not work and not loaded.safety_takes:
        out("error: no recorded takes to measure (py -3.12 -m bench.record --set prompts)")
        return 2

    generic = load_generic_terms()
    hints = whisper_hints(vocabulary, (), generic)
    from bench.engines.base import EngineError, EngineUnavailable

    options: dict[str, bool] = {}
    if args.timeouts == "product":
        options["product_timeouts"] = True
    if args.correction_candidates is not None:
        options["correction_candidates"] = args.correction_candidates
    if args.common_sense is not None:
        options["common_sense"] = args.common_sense
    try:
        product = product_factory(vocabulary, generic, **options)
    except SettingsError as exc:
        out(f"error: {exc}")
        return 2
    try:
        main_pass = main_pass_settings(product.final_pass, args)
        candidate_settings = {name: candidate_pass(product.final_pass, name) for name in candidates}
    except ValueError as exc:
        out(f"error: {exc}")
        return 2
    # Every final pass to decode: the main run's and the candidates' (identical settings decode once).
    decode: dict[str, FinalPassSettings] = {}
    alias: dict[str, str] = {}
    for key, wanted in {**({APP: main_pass} if main_pass.enabled else {}), **candidate_settings}.items():
        alias[key] = next((known for known, value in decode.items() if value == wanted), key)
        decode.setdefault(alias[key], wanted)
    main_key = alias.get(APP)

    streamer = None
    transcripts: dict[str, list[tuple[str, float]]] = {}  # today's hints
    relevance: dict[str, list[tuple[str, float]]] = {}  # the Phase 7 project hints: before
    heard: dict[str, list[tuple[str, float]]] = {}  # the heard-term hint source: after
    outcomes: dict[str, list[dict]] = {}  # each take's final passes, in the replay of the app's hints
    switches: dict[str, list[int]] = {}
    sources: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    safety: list[tuple[str, float]] = []  # every dictation take with today's hints (--safety)
    safety_outcomes: list[dict] = []
    project_chars: list[int] = []
    project_words: set[str] = set()  # the project hints' words: the pack's terms are real project terms
    gpu: list[dict] = []

    def replay(takes: Sequence[Take], per_take: Sequence[object | None], otherwise: Sequence[tuple[str, float]],
               otherwise_passes: Sequence[dict]) -> tuple[list[tuple[str, float]], list[int], list[dict]]:
        """Each take with its own hints; a take without any keeps ``otherwise`` and its passes (no replay)."""
        chosen = [(take, h) for take, h in zip(takes, per_take, strict=True) if h is not None]
        items = streamer([t for t, _ in chosen], [h for _, h in chosen]) if chosen else []
        pairs, counts = split_streamed(items)
        again, switched, passed = iter(pairs), iter(counts), iter(split_passes(items))
        texts, moves, finals = [], [], []
        for h, before_pair, before_passes in zip(per_take, otherwise, otherwise_passes, strict=True):
            texts.append(next(again) if h is not None else before_pair)
            moves.append(next(switched) if h is not None else 0)
            finals.append(next(passed) if h is not None else before_passes)
        return texts, moves, finals

    def snapshot(label: str) -> None:
        take = getattr(streamer, "snapshot", None)
        if decode and take is not None:
            try:
                gpu.append(take(label))
            except Exception as exc:  # noqa: BLE001 - a snapshot is only reported
                gpu.append({"label": label, "error": type(exc).__name__})

    try:
        streamer, stream_options = streamer_factory(hints, decode) if decode else streamer_factory(hints)
        streamer((work[0][1] if work else loaded.safety_takes)[:1])  # warm-up: loads the models and fills the caches
        snapshot("Whisper models loaded, before the replay")
        today_passes: dict[str, list[dict]] = {}
        for name, takes, _ in work:
            items = streamer(takes)
            transcripts[name] = split_streamed(items)[0]
            today_passes[name] = split_passes(items)
        if loaded.safety_takes:
            # Into Claude Code without a project the app gives today's hints: a take already heard so is reused.
            known = {take.id: (pair, passed) for name, takes, _ in work if name == DICTATION
                     for take, pair, passed in zip(takes, transcripts[name], today_passes[name], strict=True)}
            missing = [take for take in loaded.safety_takes if take.id not in known]
            items = streamer(missing) if missing else []
            known.update(zip((take.id for take in missing),
                             zip(split_streamed(items)[0], split_passes(items), strict=True), strict=True))
            safety = [known[take.id][0] for take in loaded.safety_takes]
            safety_outcomes = [known[take.id][1] for take in loaded.safety_takes]
        for name, takes, _ in work:
            per_take = take_hints(takes, product)
            reasons[name] = [reason for _, reason in per_take]
            before_hints = [relevance_hints(h) for h, _ in per_take]
            after_hints = [heard_source(h) for h, _ in per_take]
            sources[name] = sum(h is not None for h in after_hints)
            project_chars += [len(getattr(h, "hotwords", "") or "") for h in before_hints if h is not None]
            project_words.update(word for h, _ in per_take for word in hint_terms(h))
            relevance[name], _, relevance_passes = replay(takes, before_hints, transcripts[name], today_passes[name])
            heard[name], switches[name], outcomes[name] = replay(takes, after_hints, relevance[name],
                                                                 relevance_passes)
        snapshot("Whisper models loaded, after the replay")
    except (EngineUnavailable, EngineError) as exc:
        out(f"error: {' '.join(str(exc).split())[:200]}")
        return 2
    finally:
        close = getattr(streamer, "close", None)
        if close is not None:
            close()

    results: list[TakeResult] = []  # after: the heard-term hints (with the main run's final pass when on)
    previous: list[TakeResult] = []  # before: the Phase 7 project hints
    earlier: list[TakeResult] = []  # today's hints
    extra: dict[str, list[TakeResult]] = {}  # the correction variants, the final passes and the safety set
    pass_sets: dict[str, dict] = {}
    loaded_before = model_state(product)
    for name, takes, cases in work:
        today = measure(name, takes, cases, transcripts[name], product)
        before = measure_after(name, takes, cases, relevance[name], reasons[name], today, product)
        streaming = measure_after(name, takes, cases, heard[name], reasons[name], today, product,
                                  switches=switches[name], reuse=(before,))
        after, main_texts = streaming, heard[name]
        if main_key is not None:
            main_texts, main_finals = pass_rows(heard[name], outcomes[name], main_key)
            after = measure_after(name, takes, cases, main_texts, reasons[name], today, product,
                                  switches=switches[name], reuse=(streaming, before), passes=main_finals)
        earlier.extend(today)
        previous.extend(before)
        results.extend(after)
        dataset = loaded.prompts if name == SET_NAME else loaded.dictation
        counts = dataset_counts(dataset) if name == SET_NAME else {
            "script_rows": loaded.dictation_rows, "recorded": len(takes),
            "pending": max(0, loaded.dictation_rows - len(takes)), "invalid": 0}
        blocks[name] = set_block(after, counts, product.info, terms=name == SET_NAME)
        blocks[name]["relevance_hints"] = before_block(before, terms=name == SET_NAME)
        blocks[name]["today_hints"] = before_block(today, terms=name == SET_NAME)
        blocks[name]["heard_hints"] = heard_block(streaming, before, sources[name])
        if decode:
            runs = {STREAMING: streaming, **({APP: after} if main_key is not None else {})}
            for candidate in candidate_settings:
                texts, finals = pass_rows(heard[name], outcomes[name], alias[candidate])
                runs[candidate] = measure_after(name, takes, cases, texts, reasons[name], today, product,
                                                switches=switches[name], reuse=(after, streaming, before),
                                                passes=finals)
            pass_sets[name] = {key: pass_block(rows, terms=name == SET_NAME) for key, rows in runs.items()}
            for key, rows in runs.items():
                if key != APP:
                    extra.setdefault(f"pass-{key}", []).extend(rows)
        if args.variants:
            runs = measure_variants(name, takes, cases, main_texts, reasons[name], after, product)
            blocks[name]["variants"] = variants_block(runs, terms=name == SET_NAME)
            blocks[name]["variant_app"] = app_variant(product.rewriter)
            for variant, rows in runs.items():
                extra.setdefault(variant, []).extend(rows)
    safety_summary = None
    safety_passes: dict[str, dict] = {}
    if loaded.safety_takes:
        app = app_variant(product.rewriter)
        main_texts, main_finals = (pass_rows(safety, safety_outcomes, main_key) if main_key is not None
                                   else (safety, None))
        runs = measure_safety(loaded.safety_takes, main_texts, product, passes=main_finals)
        safety_summary = safety_block(runs, dataset_counts(loaded.dictation), app)
        safety_summary["variant_app"] = app
        extra.update({f"{SAFETY}-{variant}": rows for variant, rows in runs.items()})
        if decode:
            main_run = runs.get(app) or []
            streaming = (main_run if main_key is None else
                         measure_safety_pass(loaded.safety_takes, *pass_rows(safety, safety_outcomes, None),
                                             product, reuse=(main_run,)))
            pass_runs = {STREAMING: streaming, **({APP: main_run} if main_key is not None else {})}
            for candidate in candidate_settings:
                pass_runs[candidate] = measure_safety_pass(
                    loaded.safety_takes, *pass_rows(safety, safety_outcomes, alias[candidate]), product,
                    reuse=(main_run, streaming))
            safety_passes = {key: pass_block(rows) for key, rows in pass_runs.items()}
            extra.update({f"{SAFETY}-pass-{key}": rows for key, rows in pass_runs.items() if key != APP})
    ordered = {name: blocks[name] for name in SETS if name in blocks}
    engine = {**stream_options, "hints": {"count": len(hints), "chars": sum(len(h) for h in hints)},
              "project_hints": {"takes": len(project_chars),
                                "hotword_chars_max": max(project_chars) if project_chars else None}}
    rewrite = {key: product.info[key] for key in ("model", "timeout_s", "enrich_timeout_s", "bench_timeout_s",
                                                  "timeouts", "keep_alive", "load_wait_s", "cleanup")
               if key in product.info}
    rewrite["correction_candidates"] = (autorewrite.LIKELY_TERMS if args.correction_candidates is None
                                        else args.correction_candidates)
    rewrite["name_fixes"] = bool(getattr(product.rewriter, "name_fixes", autorewrite.NAME_FIXES))
    rewrite["common_sense_fixes"] = bool(getattr(product.rewriter, "common_sense_fixes", False))
    rewrite["first_call"] = first_call(earlier, loaded_before)
    final_pass: dict = {"main": pass_settings(main_pass)}
    if decode:
        final_pass.update(candidates={name: pass_settings(value) for name, value in candidate_settings.items()},
                          sets={name: pass_sets[name] for name in SETS if name in pass_sets}, gpu=gpu)
        if safety_passes:
            final_pass[SAFETY] = safety_passes
        prompts_errors = (ordered.get(SET_NAME) or {}).get("term_errors")
        final_pass["choice"] = pass_choice(final_pass, prompts_errors["today"] if prompts_errors else None)
    summary = build_summary(ordered, engine, rewrite, safety_summary, final_pass)

    texts = private_texts([*results, *previous, *earlier, *(r for rows in extra.values() for r in rows)])
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
    names += refused_project_words(project_words)
    run_dir = Path(results_dir) / "prompts" / time.strftime("%Y%m%d-%H%M%S")
    try:
        write_private(run_dir, results, results_dir, before=previous, today=earlier, extra=extra)
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
