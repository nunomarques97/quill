"""Where mouse 5 transcription errors come from: an offline replay of the Sponsor's real recordings.

Usage:
    py -3.12 -m bench.asr_errors --dry-run [--set all|prompts|claude_code|dictation|rewrite]
    .venv\\Scripts\\python -m bench.asr_errors [--set ...] [--summary PATH] [--config FILE] [--vocabulary FILE]
    py -3.12 -m bench.asr_errors --check SUMMARY

Four sets are replayed from the recordings already on disk, never from
synthetic speech: ``prompts`` (the spoken Claude Code prompts, with domain
terms), ``claude_code`` (the dictation takes recorded for Claude Code),
``dictation`` (every valid dictation take) and ``rewrite`` (the spoken
rewrite instructions). A take shared by two sets is decoded once.

Each take is transcribed under every configuration of ``CONFIGS``; each
configuration changes one thing against the configuration it is compared
with, so each possible cause of the errors is isolated:

- ``baseline``: ``stream_app``, the product's streaming final (the app's
  engine model, its ``StreamOptions`` and the mouse 5 hints ``bench.prompts``
  gives each take: in Claude Code with a project and a pack, the heard-term
  hint source, asked by the session after each partial), replayed on the
  deterministic audio-time schedule;
- ``segmentation``: ``whole_app``, one decode of the whole utterance with the
  same model and the same hints (a fresh hint source of the take asked with
  the streaming final text) and the session's own energy trimming. Every
  other configuration is compared with it;
- ``vad_trimming``: no trimming at all, or faster-whisper's Silero VAD
  filter instead of the energy trimming;
- ``engine``: large-v3 (whole audio, and its own streaming final);
- ``hints``: no hints, or the vocabulary hints only;
- ``hint_budget``: the app's hint list cut at a smaller and a larger budget;
- ``language``: the language detected by the model instead of Portuguese;
- ``beam``: beam 1 and a larger beam instead of 5;
- ``temperature_fallback``: faster-whisper's temperature fallback;
- ``condition_on_previous_text``: each 30 s window prompted with the last;
- ``initial_prompt``: the hints only as hotwords (no initial prompt), only
  as the initial prompt (no hotwords), or after one generic instruction
  sentence in European Portuguese (``INSTRUCTION_PROMPT``: a prompt in the
  imperative style of a Claude Code request instead of a bare word list).

Per configuration and set the summary holds counts only: reference words,
word errors (substitutions, deletions, insertions) and WER with the same
normalizer and alignment as ``bench.prompts`` (``bench.metrics``), the
errors classified on that alignment (``classify``): one reference word heard
as two or more words (``splits``, the "falha" heard as "file é" kind), two
or more heard as one (``merges``), errors on the first and last two
reference words (``edges``: trimming), errors on domain-term words against
the other words, domain-term errors (``bench.prompts.term_errors``), the
same after the app's text pipeline (``pipeline``), the languages decoded in,
and decode latency p50/p95. ``causes`` ranks each cause by how many prompts
word errors its best configuration removes against the configuration it is
compared with. Read-only GPU snapshots (``nvidia-smi`` used/free MiB and the
models in the shared Ollama, ``/api/ps``) are taken at the start, after each
model loads and at the end; the models load one at a time. Nothing is
unloaded, pulled or downloaded, and Ollama is never asked anything else.

Per-take text goes only under ``bench/results/asr/<run>/``. The summary is
written through ``bench.metrics.write_summary``, so a spoken phrase, a name
or a real domain term refuses it. ``--dry-run`` reads the scripts and the
manifests and prints set and configuration counts only: it opens no audio,
GPU, microphone or Ollama. ``--check SUMMARY`` exits 0 only when every set
has every configuration block with the required count fields.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from bench.dataset import Dataset, DatasetError, Take, load_dataset
from bench.metrics import percentile_nearest_rank, text_edits, write_summary
from bench.normalize import normalize, normalize_words
from bench.settings import REPO_ROOT, RESULTS_DIR, Settings, SettingsError, load_settings
from quill.streaming import SAMPLE_RATE, SAMPLE_WIDTH, SpeechDetector, StreamOptions
from quill.whisper import (DEFAULT_MODEL, PRECISE_MODEL, Decode, SessionHints, distinctive_term, project_terms,
                           session_hints)

SUMMARY_SCHEMA = 1
KIND = "asr_errors"
DEFAULT_SUMMARY = REPO_ROOT / "docs" / "research" / "asr-errors-summary.json"
PROMPTS, CLAUDE_CODE_SET, DICTATION, REWRITE = "prompts", "claude_code", "dictation", "rewrite"
SETS = (PROMPTS, CLAUDE_CODE_SET, DICTATION, REWRITE)
APP = "app"  # the app's engine model (``[engine] model`` of the quill config)
HINT_BUDGET = 330  # quill.vocabulary.HINT_MAX_CHARS, the app's budget
SMALL_BUDGET, LARGE_BUDGET = 150, 600
EDGE_WORDS = 2  # reference words at each end counted as edges
FINAL_TIMEOUT_S = 120.0
TOKENS = ("substitutions", "deletions", "insertions")
# A generic request in the imperative, as one dictates to Claude Code; invented, not from any script.
INSTRUCTION_PROMPT = "Revê o código, corrige o que falhar e escreve testes para cada ficheiro alterado."


@dataclass(frozen=True)
class Config:
    """One way of transcribing a take; every field but the name and cause is a decoding choice."""

    name: str
    cause: str
    compared_with: str | None = "whole_app"
    model: str = APP
    stream: bool = False  # the product's streaming final instead of one whole-audio decode
    hints: str = "app"  # none | vocabulary | app (the take's mouse 5 hints)
    budget: int = HINT_BUDGET
    use: str = "both"  # both | hotwords (no initial prompt) | prompt (no hotwords)
    trim: bool = True  # the session's energy trimming (lead and tail)
    vad_filter: bool = False
    beam: int = 5
    detect_language: bool = False
    temperature_fallback: bool = False
    condition_on_previous_text: bool = False
    instruction: bool = False  # INSTRUCTION_PROMPT before the hints' initial prompt

    def describe(self, app_model: str) -> dict:
        return {"cause": self.cause, "compared_with": self.compared_with, "model": self.model_name(app_model),
                "decode": "streaming final" if self.stream else "whole audio", "hints": self.hints,
                "hint_budget_chars": self.budget, "hints_as": self.use, "energy_trim": self.trim,
                "vad_filter": self.vad_filter, "beam": self.beam,
                "language": "auto" if self.detect_language else "pt",
                "temperature_fallback": self.temperature_fallback,
                "condition_on_previous_text": self.condition_on_previous_text,
                "instruction_prompt": self.instruction}

    def model_name(self, app_model: str) -> str:
        return app_model if self.model == APP else self.model


CONFIGS = (
    Config("stream_app", "baseline", None, stream=True),
    Config("whole_app", "segmentation", "stream_app"),
    Config("whole_untrimmed", "vad_trimming", trim=False),
    Config("whole_vad_filter", "vad_trimming", trim=False, vad_filter=True),
    Config("hints_none", "hints", hints="none"),
    Config("hints_vocabulary", "hints", hints="vocabulary"),
    Config(f"hints_budget_{SMALL_BUDGET}", "hint_budget", budget=SMALL_BUDGET),
    Config(f"hints_budget_{LARGE_BUDGET}", "hint_budget", budget=LARGE_BUDGET),
    Config("language_auto", "language", detect_language=True),
    Config("beam_1", "beam", beam=1),
    Config("beam_10", "beam", beam=10),
    Config("temperature_fallback", "temperature_fallback", temperature_fallback=True),
    Config("condition_previous", "condition_on_previous_text", condition_on_previous_text=True),
    Config("hotwords_only", "initial_prompt", use="hotwords"),
    Config("prompt_only", "initial_prompt", use="prompt"),
    Config("prompt_instruction", "initial_prompt", instruction=True),
    Config("whole_large_v3", "engine", model=PRECISE_MODEL),
    Config("large_v3_hints_none", "engine", "hints_none", model=PRECISE_MODEL, hints="none"),
    Config("large_v3_beam_10", "engine", "beam_10", model=PRECISE_MODEL, beam=10),
    Config("stream_large_v3", "engine", "stream_app", model=PRECISE_MODEL, stream=True),
)
CONFIG_NAMES = tuple(config.name for config in CONFIGS)
BLOCK_FIELDS = ("takes", "failed", "reference_words", "word_errors", "wer", "splits", "merges", "edges",
                "term_words", "other_words", "domain_terms", "pipeline", "languages", "latency_s")


# ---------------------------------------------------------------- error classification


@dataclass(frozen=True)
class Errors:
    """Word errors of one hypothesis against its reference, classified on one alignment. Counts only."""

    reference_words: int = 0
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    splits: int = 0  # one reference word heard as two or more words
    split_errors: int = 0
    merges: int = 0  # two or more reference words heard as one
    merge_errors: int = 0
    edge_words: int = 0
    edge_errors: int = 0
    term_words: int = 0
    term_errors: int = 0
    other_errors: int = 0

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    def __add__(self, other: Errors) -> Errors:
        return Errors(*(a + b for a, b in zip(self.astuple(), other.astuple(), strict=True)))

    def astuple(self) -> tuple[int, ...]:
        return tuple(getattr(self, name) for name in self.__dataclass_fields__)


def align(reference: Sequence[str], hypothesis: Sequence[str]) -> list[tuple[str, int | None, int | None]]:
    """One minimum-cost word alignment: ("M"|"S"|"D"|"I", reference index, hypothesis index) in order.

    The costs and ties are those of ``bench.metrics.word_edits`` (equal words
    always match; among substitution, deletion and insertion of equal totals,
    fewer substitutions, then fewer deletions, win), so the totals of S, D and
    I are the same.
    """
    rows, cols = len(reference), len(hypothesis)
    table = [[(j, 0, 0, j) for j in range(cols + 1)]]
    moves = [["I"] * (cols + 1)]
    for i in range(1, rows + 1):
        current = [(i, 0, i, 0)]
        move = ["D"]
        previous = table[i - 1]
        for j in range(1, cols + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                current.append(previous[j - 1])
                move.append("M")
                continue
            sub, dele, ins = previous[j - 1], previous[j], current[j - 1]
            options = (((sub[0] + 1, sub[1] + 1, sub[2], sub[3]), "S"),
                       ((dele[0] + 1, dele[1], dele[2] + 1, dele[3]), "D"),
                       ((ins[0] + 1, ins[1], ins[2], ins[3] + 1), "I"))
            best = min(options, key=lambda option: option[0])
            current.append(best[0])
            move.append(best[1])
        table.append(current)
        moves.append(move)
    steps: list[tuple[str, int | None, int | None]] = []
    i, j = rows, cols
    while i > 0 or j > 0:
        step = moves[i][j] if i > 0 else "I"
        if step in ("M", "S"):
            steps.append((step, i - 1, j - 1))
            i, j = i - 1, j - 1
        elif step == "D":
            steps.append(("D", i - 1, None))
            i -= 1
        else:
            steps.append(("I", None, j - 1))
            j -= 1
    steps.reverse()
    return steps


def term_positions(reference: Sequence[str], terms: Sequence[str]) -> frozenset[int]:
    """Reference word positions inside an occurrence of a domain term (normalized word sequences)."""
    positions: set[int] = set()
    for term in {tuple(normalize_words(t)) for t in terms if normalize_words(t)}:
        size = len(term)
        index = 0
        while index <= len(reference) - size:
            if tuple(reference[index : index + size]) == term:
                positions.update(range(index, index + size))
                index += size
            else:
                index += 1
    return frozenset(positions)


def classify(reference: str, hypothesis: str, terms: Sequence[str] = ()) -> Errors:
    """Word errors of ``hypothesis`` classified on one alignment (``align``); counts only.

    The alignment's error steps are grouped into regions (maximal runs between
    matched words). A region of one reference word and two or more heard
    words is a split; two or more reference words and one heard word, a
    merge. An error is an edge error when it is on one of the first or last
    ``EDGE_WORDS`` reference words; an insertion is one when its region has
    such a word, or, in a region of insertions only, when it comes before the
    second reference word or after the second-to-last. A region touching a domain-term word counts all its
    errors as term errors; any other, as other errors.
    """
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    n = len(ref)
    terms_at = term_positions(ref, terms)
    steps = align(ref, hyp)
    counts = Counter(step for step, _, _ in steps)
    splits = split_errors = merges = merge_errors = edge_errors = term_errors = other_errors = 0
    consumed = 0  # reference words before the current step
    region: list[tuple[str, int | None, int]] = []  # error steps: (kind, reference index, words before)

    def edge_word(index: int) -> bool:
        return index < EDGE_WORDS or index >= n - EDGE_WORDS

    def close() -> None:
        nonlocal splits, split_errors, merges, merge_errors, edge_errors, term_errors, other_errors
        if not region:
            return
        indexes = [index for _, index, _ in region if index is not None]
        r = len(indexes)
        h = sum(1 for kind, _, _ in region if kind in ("S", "I"))
        if r == 1 and h >= 2:
            splits, split_errors = splits + 1, split_errors + len(region)
        elif r >= 2 and h == 1:
            merges, merge_errors = merges + 1, merge_errors + len(region)
        # Insertions go with their region's reference words, whatever order the alignment put them in.
        on_edge = any(edge_word(index) for index in indexes) if indexes else (
            region[0][2] < EDGE_WORDS or region[0][2] > n - EDGE_WORDS)
        edge_errors += sum(1 for kind, index, _ in region
                           if (index is not None and edge_word(index)) or (index is None and on_edge))
        if any(index in terms_at for index in indexes):
            term_errors += len(region)
        else:
            other_errors += len(region)
        region.clear()

    for kind, index, _ in steps:
        if kind == "M":
            close()
        else:
            region.append((kind, index, consumed))
        if kind != "I":
            consumed += 1
    close()
    return Errors(reference_words=n, substitutions=counts["S"], deletions=counts["D"], insertions=counts["I"],
                  splits=splits, split_errors=split_errors, merges=merges, merge_errors=merge_errors,
                  edge_words=min(n, 2 * EDGE_WORDS), edge_errors=edge_errors, term_words=len(terms_at),
                  term_errors=term_errors, other_errors=other_errors)


# ---------------------------------------------------------------- decoding choices


def _bytes(seconds: float) -> int:
    return max(0, int(round(seconds * SAMPLE_RATE))) * SAMPLE_WIDTH


def trim_range(pcm: bytes, options: StreamOptions) -> tuple[int, int] | None:
    """(start, end) bytes a released session decodes; None without enough speech.

    From ``lead_s`` before the first speech frame to ``tail_pad_s`` after the
    last, judged as a session with nothing committed judges them
    (``quill.streaming.Session``: the frames' levels do not depend on how the
    audio was chunked).
    """
    detector = SpeechDetector(options.min_speech_rms, options.floor_ratio)
    detector.feed(pcm)
    if not detector.has_speech:
        return None
    end = min(len(pcm), detector.speech_end + _bytes(options.tail_pad_s))
    first, seconds = detector.speech_in(0, end)
    if first is None or seconds + 1e-9 < options.min_speech_s:
        return None
    return max(0, first - _bytes(options.lead_s)), end


def app_words(source: object | None, vocabulary: object, generic_terms: Sequence[str], heard: str,
              budget: int = HINT_BUDGET) -> list[str]:
    """The take's mouse 5 hint words after ``heard``, within ``budget`` characters.

    ``source`` is a fresh hint source of the take (``quill.heard.HeardHints``)
    or None (the vocabulary hints). At the app's budget this is exactly what
    a fresh source gives for ``heard`` (``HeardHints.words``).
    """
    from quill.vocabulary import whisper_hints

    if source is None:
        return whisper_hints(vocabulary, (), generic_terms, max_chars=budget)
    matcher = source.matcher
    first = matcher.ranked(matcher.closest(heard)) if heard else []
    part = project_terms(source.project, source.terms, source.today, first=first)
    return whisper_hints(source.vocabulary, part, source.generic_terms, max_chars=budget)


def config_hints(config: Config, source: object | None, vocabulary: object, generic_terms: Sequence[str],
                 heard: str) -> SessionHints | None:
    """The hints a whole-audio decode of ``config`` uses for one take; None: no hints at all."""
    from quill.vocabulary import whisper_hints

    if config.hints == "none":
        return None
    if config.hints == "vocabulary":
        return session_hints(whisper_hints(vocabulary, (), generic_terms, max_chars=config.budget))
    return session_hints(app_words(source, vocabulary, generic_terms, heard, config.budget))


def whole_decode(config: Config, hints: SessionHints | None, seconds: float, options: StreamOptions) -> Decode:
    """The ``Decode`` of one whole-audio decode, with the session's generated-token bound."""
    prompt = hints.prompt if hints is not None and config.use != "hotwords" else None
    if config.instruction:
        prompt = f"{INSTRUCTION_PROMPT} {prompt}" if prompt else INSTRUCTION_PROMPT
    hotwords = hints.hotwords if hints is not None and config.use != "prompt" else None
    return Decode(beam_size=config.beam, initial_prompt=prompt or None, hotwords=hotwords or None,
                  without_timestamps=True,
                  max_new_tokens=int(math.ceil(min(seconds, 30.0) * options.tokens_per_s)) + options.min_new_tokens,
                  language=hints.language if hints is not None else None, detect_language=config.detect_language,
                  temperature_fallback=config.temperature_fallback,
                  condition_on_previous_text=config.condition_on_previous_text, vad_filter=config.vad_filter)


# ---------------------------------------------------------------- the replay


@dataclass(frozen=True)
class Row:
    """One take under one configuration. Holds spoken text: written only under bench/results/."""

    config: str
    id: str
    text: str = field(repr=False)
    pipeline: str = field(default="", repr=False)
    seconds: float = 0.0
    language: str | None = None
    error: str | None = None


@dataclass
class Context:
    """The app's mouse 5 surroundings for a simulated window: hints, text pipeline and decoding options."""

    vocabulary: object
    generic_terms: Sequence[str]
    vocabulary_hints: Sequence[str]
    app_model: str
    options_for: Callable[[str], StreamOptions]
    hints_for: Callable[[str], object | None]  # project name -> a fresh hint source or None
    pipeline: Callable[[str, str], str]  # (text, project) -> the text the app's pipeline makes of it
    info: dict = field(default_factory=dict)


def pcm_of(take: Take) -> bytes:
    from bench.engines.base import wav_pcm

    return wav_pcm(take.path.read_bytes())[0]


def take_project(take: Take) -> str:
    from bench.prompts import take_project as project

    return project(take)


def stream_rows(config: Config, model: object, context: Context, takes: Sequence[Take]) -> list[Row]:
    """The product's streaming final of each take, on the deterministic schedule (``bench.streaming``)."""
    from bench.streaming import replay_deterministic
    from quill.streaming import StreamingTranscriber

    options = context.options_for(config.model_name(context.app_model))
    transcriber = StreamingTranscriber(model, options, context.vocabulary_hints)
    transcriber.start()
    rows: list[Row] = []
    try:
        transcriber.ready.wait(FINAL_TIMEOUT_S)
        if transcriber.load_error:
            reason = "model unavailable"
            return [Row(config.name, take.id, "", error=reason) for take in takes]
        for take in takes:
            source = context.hints_for(take_project(take))
            try:
                result = replay_deterministic(transcriber, pcm_of(take), hints=source)
            except TimeoutError:
                rows.append(Row(config.name, take.id, "", error="timeout"))
                continue
            if not result.ok:
                rows.append(Row(config.name, take.id, "", error="final failed"))
                continue
            rows.append(Row(config.name, take.id, result.text, seconds=result.latency_s, language="pt"))
    finally:
        transcriber.stop(close_model=False)
    return rows


def whole_rows(config: Config, model: object, context: Context, takes: Sequence[Take], heard: dict[str, str],
               clock: Callable[[], float] = time.perf_counter) -> list[Row]:
    """One whole-audio decode of each take; ``heard`` (take id -> the streaming final) picks the app's hints."""
    from quill.whisper import WhisperError

    options = context.options_for(config.model_name(context.app_model))
    rows: list[Row] = []
    for take in takes:
        pcm = pcm_of(take)
        if config.trim:
            span = trim_range(pcm, options)
            if span is None:
                rows.append(Row(config.name, take.id, "", language=None))
                continue
            pcm = pcm[span[0] : span[1]]
        source = context.hints_for(take_project(take)) if config.hints == "app" else None
        hints = config_hints(config, source, context.vocabulary, context.generic_terms, heard.get(take.id, ""))
        decode = whole_decode(config, hints, len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH), options)
        started = clock()
        try:
            transcript = model.transcribe(pcm, decode)
        except WhisperError:
            rows.append(Row(config.name, take.id, "", error="transcription failed"))
            continue
        seconds = clock() - started
        language = getattr(transcript, "language", None) or ("pt" if not config.detect_language else None)
        rows.append(Row(config.name, take.id, transcript.text, seconds=seconds, language=language))
    return rows


def with_pipeline(rows: Sequence[Row], takes: dict[str, Take], context: Context) -> list[Row]:
    return [replace(row, pipeline=context.pipeline(row.text, take_project(takes[row.id])) if row.text.strip() else "")
            for row in rows]


def model_order(configs: Sequence[Config], app_model: str) -> list[str]:
    """The models to load, one at a time: the app's engine first (its streaming final feeds the others' hints)."""
    names = [app_model] + [config.model_name(app_model) for config in configs]
    return list(dict.fromkeys(names))


def run_configs(configs: Sequence[Config], context: Context, takes: Sequence[Take],
                model_factory: Callable[[str], object], gpu: Callable[[], dict],
                out: Callable[[str], None] = print) -> tuple[dict[str, list[Row]], list[dict]]:
    """Every configuration on every take, one model loaded at a time; (rows by config, GPU snapshots)."""
    from quill.whisper import WhisperUnavailable

    snapshots = [{"label": "start", **gpu()}]
    rows: dict[str, list[Row]] = {}
    heard: dict[str, str] = {}
    by_id = {take.id: take for take in takes}
    for name in model_order(configs, context.app_model):
        chosen = [config for config in configs if config.model_name(context.app_model) == name]
        if not chosen:
            continue
        model = model_factory(name)
        try:
            try:
                load = getattr(model, "load", None)
                if load is not None:
                    load()
            except WhisperUnavailable:
                snapshots.append({"label": f"{name} failed to load", **gpu()})
                for config in chosen:
                    rows[config.name] = [Row(config.name, take.id, "", error="model unavailable") for take in takes]
                continue
            snapshots.append({"label": f"{name} loaded", **gpu()})
            if takes:  # warm-up: the first decode fills the caches; not measured
                whole_rows(Config("warm_up", "warm_up", model=name), model, context, takes[:1], heard)
            for config in sorted(chosen, key=lambda c: not c.stream):
                out(f"  {config.name} ({name}) ...")
                if config.stream:
                    found = stream_rows(config, model, context, takes)
                    if config.name == "stream_app":
                        heard = {row.id: row.text for row in found}
                else:
                    found = whole_rows(config, model, context, takes, heard)
                rows[config.name] = with_pipeline(found, by_id, context)
            snapshots.append({"label": f"{name} after its configurations", **gpu()})
        finally:
            close = getattr(model, "close", None)
            if close is not None:
                close()
    snapshots.append({"label": "end", **gpu()})
    return rows, snapshots


# ---------------------------------------------------------------- aggregates


def _seconds(values: Sequence[float], percent: float) -> float | None:
    value = percentile_nearest_rank(values, percent)
    return None if value is None else round(value, 3)


def _ratio(errors: int, words: int) -> float | None:
    return round(errors / words, 4) if words else None


def config_block(rows: Sequence[Row], takes: dict[str, Take]) -> dict:
    """Counts of one configuration on one set; never text, names or terms."""
    from bench.prompts import term_errors

    done = [row for row in rows if row.error is None]
    total = Errors()
    occurrences = term_misses = pipeline_errors = pipeline_terms = 0
    for row in done:
        take = takes[row.id]
        total += classify(take.clean, row.text, take.terms)
        found, missed = term_errors(take.clean, row.text, take.terms)
        occurrences += found
        term_misses += missed
        pipeline_errors += text_edits(take.clean, row.pipeline).errors
        pipeline_terms += term_errors(take.clean, row.pipeline, take.terms)[1]
    seconds = [row.seconds for row in done]
    return {
        "takes": len(done),
        "failed": len(rows) - len(done),
        "reference_words": total.reference_words,
        "word_errors": {"substitutions": total.substitutions, "deletions": total.deletions,
                        "insertions": total.insertions, "total": total.errors},
        "wer": _ratio(total.errors, total.reference_words),
        "splits": {"events": total.splits, "errors": total.split_errors},
        "merges": {"events": total.merges, "errors": total.merge_errors},
        "edges": {"reference_words": total.edge_words, "errors": total.edge_errors},
        "term_words": {"reference_words": total.term_words, "errors": total.term_errors},
        "other_words": {"reference_words": total.reference_words - total.term_words, "errors": total.other_errors},
        "domain_terms": {"occurrences": occurrences, "errors": term_misses},
        "pipeline": {"word_errors": pipeline_errors, "wer": _ratio(pipeline_errors, total.reference_words),
                     "domain_term_errors": pipeline_terms},
        "languages": dict(sorted(Counter(row.language or "none" for row in done).items())),
        "latency_s": {"n": len(seconds), "p50": _seconds(seconds, 50), "p95": _seconds(seconds, 95)},
    }


def set_blocks(rows: dict[str, list[Row]], members: dict[str, Sequence[Take]], configs: Sequence[Config]) -> dict:
    out: dict[str, dict] = {}
    for set_name, takes in members.items():
        ids = {take.id for take in takes}
        by_id = {take.id: take for take in takes}
        audio = [take.duration_s for take in takes]
        out[set_name] = {
            "takes": len(takes),
            "audio_s": {"total": round(sum(audio), 1), "p50": _seconds(audio, 50), "max": _seconds(audio, 100)},
            "configs": {config.name: config_block([row for row in rows.get(config.name, []) if row.id in ids], by_id)
                        for config in configs},
        }
    return out


def rank_causes(block: dict, configs: Sequence[Config] = CONFIGS) -> list[dict]:
    """Each cause's best configuration against the one it is compared with, by word errors removed (one set)."""
    ranked: dict[str, dict] = {}
    for config in configs:
        if config.compared_with is None:
            continue
        mine, theirs = block.get(config.name), block.get(config.compared_with)
        if not mine or not theirs or not mine.get("takes") or mine.get("takes") != theirs.get("takes"):
            continue
        removed = theirs["word_errors"]["total"] - mine["word_errors"]["total"]
        entry = {"cause": config.cause, "config": config.name, "compared_with": config.compared_with,
                 "word_errors_removed": removed,
                 "wer": {"compared": theirs["wer"], "config": mine["wer"]},
                 "pipeline_word_errors_removed": theirs["pipeline"]["word_errors"] - mine["pipeline"]["word_errors"],
                 "domain_term_errors": {"compared": theirs["domain_terms"]["errors"],
                                        "config": mine["domain_terms"]["errors"]}}
        if config.cause not in ranked or removed > ranked[config.cause]["word_errors_removed"]:
            ranked[config.cause] = entry
    return sorted(ranked.values(), key=lambda entry: (-entry["word_errors_removed"], entry["cause"]))


def build_summary(sets: dict, configs: Sequence[Config], context_info: dict, snapshots: list[dict]) -> dict:
    app_model = context_info.get("app_model", DEFAULT_MODEL)
    return {
        "schema": SUMMARY_SCHEMA,
        "kind": KIND,
        "engine": context_info,
        "configurations": {config.name: config.describe(app_model) for config in configs},
        "sets": sets,
        "causes": {name: rank_causes(block["configs"], configs) for name, block in sets.items()},
        "gpu": snapshots,
    }


# ---------------------------------------------------------------- check


def check_summary(summary: object) -> list[tuple[bool, str]]:
    """(ok, line) per set: every configuration block with every required count field."""
    if not isinstance(summary, dict) or summary.get("kind") != KIND:
        return [(False, "not a bench.asr_errors summary")]
    lines: list[tuple[bool, str]] = []
    sets = summary.get("sets")
    if not isinstance(sets, dict):
        return [(False, "no sets")]
    for set_name in SETS:
        block = sets.get(set_name)
        configs = block.get("configs") if isinstance(block, dict) else None
        if not isinstance(configs, dict):
            lines.append((False, f"{set_name}: missing"))
            continue
        missing = [name for name in CONFIG_NAMES if not _complete(configs.get(name), set_name == PROMPTS)]
        if missing:
            lines.append((False, f"{set_name}: {len(missing)} of {len(CONFIG_NAMES)} configuration blocks "
                                 f"incomplete ({', '.join(missing)})"))
        else:
            lines.append((True, f"{set_name}: {len(CONFIG_NAMES)} configuration blocks complete"))
    gpu = summary.get("gpu")
    lines.append((isinstance(gpu, list) and len(gpu) >= 2, "GPU snapshots: "
                  + (str(len(gpu)) if isinstance(gpu, list) else "missing")))
    return lines


def _complete(block: object, prompts: bool) -> bool:
    if not isinstance(block, dict) or any(key not in block for key in BLOCK_FIELDS):
        return False
    if not isinstance(block["takes"], int) or block["takes"] < 1 or not isinstance(block["reference_words"], int):
        return False
    errors = block["word_errors"]
    if not isinstance(errors, dict) or any(not isinstance(errors.get(key), int) for key in (*TOKENS, "total")):
        return False
    pairs = (("splits", "events"), ("merges", "events"), ("edges", "errors"), ("term_words", "errors"),
             ("other_words", "errors"), ("domain_terms", "errors"), ("pipeline", "word_errors"))
    if any(not isinstance(block[key], dict) or not isinstance(block[key].get(inner), int) for key, inner in pairs):
        return False
    if prompts and not block["domain_terms"].get("occurrences"):
        return False
    latency = block["latency_s"]
    return isinstance(latency, dict) and latency.get("p95") is not None and block["wer"] is not None


# ---------------------------------------------------------------- sets


@dataclass
class Loaded:
    members: dict[str, tuple[Take, ...]] = field(default_factory=dict, repr=False)
    datasets: list[Dataset] = field(default_factory=list, repr=False)

    def unique(self) -> list[Take]:
        seen: dict[str, Take] = {}
        for takes in self.members.values():
            for take in takes:
                seen.setdefault(take.id, take)
        return list(seen.values())


def load_sets(settings: Settings, chosen: Sequence[str]) -> Loaded:
    from bench.prompts import claude_code_takes

    loaded = Loaded()
    if PROMPTS in chosen:
        dataset = load_dataset(settings.for_set(PROMPTS))
        loaded.datasets.append(dataset)
        loaded.members[PROMPTS] = tuple(dataset.takes)
    if CLAUDE_CODE_SET in chosen or DICTATION in chosen:
        dataset = load_dataset(settings.for_set(DICTATION))
        loaded.datasets.append(dataset)
        if CLAUDE_CODE_SET in chosen:
            loaded.members[CLAUDE_CODE_SET] = claude_code_takes(dataset)
        if DICTATION in chosen:
            loaded.members[DICTATION] = tuple(dataset.takes)
    if REWRITE in chosen:
        dataset = load_dataset(settings.for_set(REWRITE))
        loaded.datasets.append(dataset)
        loaded.members[REWRITE] = tuple(dataset.takes)
    return loaded


def dry_run(loaded: Loaded, out: Callable[[str], None] = print, configs: Sequence[Config] = CONFIGS) -> int:
    """Set and configuration counts only; opens no audio, GPU, microphone or Ollama."""
    for name, takes in loaded.members.items():
        out(f"{name}: {len(takes)} takes")
    unique = loaded.unique()
    streams = sum(config.stream for config in configs)
    models = sorted({config.model for config in configs})
    out(f"unique takes: {len(unique)}; configurations: {len(configs)} ({streams} streaming, "
        f"{len(configs) - streams} whole audio) on {len(models)} models; decodes: {len(unique) * len(configs)}")
    causes = Counter(config.cause for config in configs)
    out("causes: " + ", ".join(f"{cause} {count}" for cause, count in causes.items()))
    return 0


def default_sets(config: Path | None, chosen: Sequence[str]) -> tuple[Settings, Loaded]:
    """The bench settings (``local/bench.toml``) and the chosen sets; reads manifests and scripts, no audio."""
    settings = load_settings(config)
    return settings, load_sets(settings, chosen)


# ---------------------------------------------------------------- defaults (GPU, Ollama read-only, app config)


def default_context(vocabulary: object) -> Context:
    """The app's text pipeline, project detection and context packs from its config; no Ollama call."""
    from bench.prompts import Window, window
    from quill.app import TextPipeline, project_hints
    from quill.config import load_config
    from quill.context_pack import ContextPacks
    from quill.corrections import CorrectionStore, Learner
    from quill.inject import Target
    from quill.projects import ProjectDetector, ProjectFolders
    from quill.streaming import options_for
    from quill.vocabulary import load_generic_terms, whisper_hints

    config = load_config()
    cleanup = config.cleanup_mode
    if cleanup != "rules":
        config = replace(config, cleanup_mode="rules")  # the bench never asks Ollama
    generic = load_generic_terms()
    holder = Window()
    folders = ProjectFolders(config.project_context.folders, config.voice.shortcut_dirs)
    pipeline = TextPipeline(config, vocabulary=vocabulary, generic_terms=generic, describe=holder.describe,
                            learner=Learner(CorrectionStore(config.corrections_path)),
                            projects=ProjectDetector(folders))
    packs = ContextPacks.from_settings(config.project_context)

    def hints_for(project: str) -> object | None:
        source, _ = project_hints(window(project), 0, profiles=pipeline.profiles, projects=pipeline.projects,
                                  pack_for=packs.get, vocabulary=vocabulary, generic_terms=generic)
        return source

    def run_pipeline(text: str, project: str) -> str:
        holder.info = window(project)
        processed = pipeline(text, Target(0, 0))
        return processed.text if processed is not None else ""

    from bench.pipeline import ENGINE_COMPUTE

    app_model = config.engine_model or DEFAULT_MODEL
    hints = whisper_hints(vocabulary, (), generic)
    info = {"app_model": app_model, "compute_type": ENGINE_COMPUTE, "cleanup": cleanup,
            "cleanup_measured": "rules", "vocabulary_hints": {"count": len(hints), "chars": sum(map(len, hints))},
            "hint_budgets_chars": [SMALL_BUDGET, HINT_BUDGET, LARGE_BUDGET]}
    return Context(vocabulary, generic, hints, app_model, options_for, hints_for, run_pipeline, info)


def default_model(name: str) -> object:
    from bench.pipeline import ENGINE_COMPUTE
    from quill.whisper import Whisper

    return Whisper(name, compute_type=ENGINE_COMPUTE)


def default_gpu() -> dict:
    from bench.streaming import gpu_state

    state = gpu_state()
    state.pop("time", None)
    return state


# ---------------------------------------------------------------- privacy and output


def private_names(settings: Settings | None, loaded: Loaded, vocabulary: object,
                  context: Context) -> tuple[list[str], list[str]]:
    """(texts, names) the summary must not hold: references, phrases, names, terms and project terms."""
    from bench.prompts import private_text

    texts = [text for dataset in loaded.datasets for text in dataset.reference_texts()]
    texts += [text for take in loaded.unique() for text in (take.reference, take.clean) if text]
    names = [entry.text for entry in getattr(vocabulary, "names", ())]
    for dataset in loaded.datasets:
        names += sorted(dataset.names)
    names += [name for take in loaded.unique() for name in (*take.terms, *take.project_names)]
    try:
        phrases, projects, terms = private_text(settings.for_set(PROMPTS) if settings is not None else None)
    except (DatasetError, SettingsError):
        phrases, projects, terms = [], frozenset(), frozenset()
    texts += phrases
    names += sorted(projects) + sorted(terms)
    # A pack may list a public Whisper model name; the summary must name the models it measured.
    models = {normalize(name) for name in (DEFAULT_MODEL, PRECISE_MODEL, context.app_model)}
    for take in loaded.unique():
        source = context.hints_for(take_project(take))
        for term in getattr(source, "terms", ()) if source is not None else ():
            if isinstance(term, str):
                names += [word for word in term.split() if distinctive_term(word) and normalize(word) not in models]
        if source is not None and isinstance(getattr(source, "project", None), str):
            names.append(source.project)
    return texts, names


def write_private(run_dir: Path, rows: dict[str, list[Row]], takes: dict[str, Take], results_dir: Path) -> None:
    """Per-take text of every configuration, only inside the ignored results folder."""
    root = Path(results_dir).resolve()
    target = Path(run_dir).resolve()
    if root not in target.parents:
        raise ValueError("per-take outputs must stay under bench/results/")
    target.mkdir(parents=True, exist_ok=True)
    data = [{"config": row.config, "id": row.id, "reference": takes[row.id].clean, "text": row.text,
             "pipeline": row.pipeline, "seconds": round(row.seconds, 3), "language": row.language,
             "error": row.error} for config_rows in rows.values() for row in config_rows]
    (target / "takes.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def report_lines(summary: dict) -> list[str]:
    lines = []
    for set_name, block in summary["sets"].items():
        base = block["configs"].get("stream_app", {})
        lines.append(f"{set_name}: {block['takes']} takes; stream_app WER {base.get('wer')} "
                     f"({base.get('word_errors', {}).get('total')} of {base.get('reference_words')})")
        for entry in summary["causes"].get(set_name, [])[:6]:
            lines.append(f"  {entry['cause']}: {entry['config']} removes {entry['word_errors_removed']} against "
                         f"{entry['compared_with']} (WER {entry['wer']['compared']} -> {entry['wer']['config']})")
    return lines


def main(
    argv: list[str] | None = None,
    *,
    context_factory: Callable[[object], Context] = default_context,
    model_factory: Callable[[str], object] = default_model,
    gpu: Callable[[], dict] = default_gpu,
    sets_loader: Callable[[Path | None, Sequence[str]], tuple[Settings | None, Loaded]] = default_sets,
    results_dir: Path = RESULTS_DIR,
    out: Callable[[str], None] = print,
) -> int:
    from quill.vocabulary import LOCAL_VOCABULARY, VocabularyError, load_vocabulary

    parser = argparse.ArgumentParser(prog="bench.asr_errors",
                                     description="Where mouse 5 transcription errors come from (offline replay).")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--set", choices=(*SETS, "all"), default="all", help="sets to replay (default all)")
    parser.add_argument("--dry-run", action="store_true", help="set and configuration counts only")
    parser.add_argument("--vocabulary", type=Path, default=LOCAL_VOCABULARY,
                        help="personal vocabulary (default local/vocabulary.toml; missing is empty)")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY,
                        help="aggregate summary JSON (default docs/research/asr-errors-summary.json)")
    parser.add_argument("--check", type=Path, default=None, help="check a saved summary's blocks")
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
        lines = check_summary(summary)
        for ok, line in lines:
            out(("ok: " if ok else "NOT met: ") + line)
        return 0 if all(ok for ok, _ in lines) else 1

    try:
        settings, loaded = sets_loader(args.config, chosen)
        if args.dry_run:
            return dry_run(loaded, out)
        vocabulary = load_vocabulary(args.vocabulary)
    except (SettingsError, DatasetError, VocabularyError) as exc:
        out(f"error: {exc}")
        return 2
    takes = loaded.unique()
    if not takes:
        out("error: no recorded takes to replay")
        return 2
    context = context_factory(vocabulary)
    from bench.engines.base import EngineError, EngineUnavailable

    try:
        rows, snapshots = run_configs(CONFIGS, context, takes, model_factory, gpu, out)
    except (EngineUnavailable, EngineError) as exc:
        out(f"error: {' '.join(str(exc).split())[:200]}")
        return 2
    sets = set_blocks(rows, loaded.members, CONFIGS)
    summary = build_summary(sets, CONFIGS, context.info, snapshots)
    texts, names = private_names(settings, loaded, vocabulary, context)
    texts += [text for config_rows in rows.values() for row in config_rows for text in (row.text, row.pipeline)
              if text]
    run_dir = Path(results_dir) / "asr" / time.strftime("%Y%m%d-%H%M%S")
    by_id = {take.id: take for take in takes}
    try:
        write_private(run_dir, rows, by_id, results_dir)
        write_summary(run_dir / "summary.json", summary, texts, names)
        write_summary(Path(args.summary), summary, texts, names)
    except ValueError as exc:
        out(f"error: {exc}")
        return 1
    for line in report_lines(summary):
        out(line)
    out(f"summary written to {Path(args.summary).name}; per-take outputs under bench/results/asr/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
