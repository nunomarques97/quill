"""Voice-command measurement on the Sponsor's spoken commands ("abre VS Code no <projeto>").

Usage:
    py -3.12 -m bench.voice_commands --dry-run
    .venv\\Scripts\\python -m bench.voice_commands [--vocabulary FILE] [--summary PATH]

The ``voice`` set (``bench/dictation/guiao-comandos-pt.md``, recorded with
``py -3.12 -m bench.record --set voice``) pairs each spoken command with its
expected action: open the shortcut named by ``<projeto-N>`` (the names come
from ``[voice.projects]`` of the ignored ``local/bench.toml``), or open
nothing. Each recorded take goes through the product path: it is replayed
through ``quill.streaming`` (local large-v3-turbo) with exactly the hints the
app gives a voice session (``quill.voice.VoiceHints``: Portuguese, the
command prompt, the listed shortcut names and the personal-vocabulary names
with their variants; not the dictation hints), and the final text goes to the
app's own voice commands (``quill.voice``: the parser, the shortcut listing of
the quill config's ``[voice_commands] shortcut_dirs``, the matcher with the
personal vocabulary and the launch checks). The launcher only records what
it was asked to open: nothing is opened.

Measured, against the targets that are never lowered here (at least 95 %
correct actions and 0 wrong shortcuts):

- correct actions: the expected shortcut opened, or nothing opened for a
  negative;
- wrong shortcuts: a shortcut opened that is not the expected one (for a
  negative, any shortcut);
- ambiguous, unrecognized and no-match endings, and every other reason code;
- latency p50/p95: release to final text (streamed) and the command.

Spoken text goes only under ``bench/results/voice/<run>/``: the per-take JSON
and a human-checkable table with an empty Sponsor column. The summary holds
aggregates only and is written to the committed
``docs/research/voice-commands-summary.json`` (and to the run folder); it is
refused when a script text, a transcription or a name would leak into it.
The dry run reads the script, the manifest and the shortcut folder listing:
it prints counts only and never opens the microphone, the GPU or a shortcut.
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

from bench.dataset import PLACEHOLDER, Dataset, DatasetError, Take, load_dataset, parse_script
from bench.metrics import percentile_nearest_rank, write_summary
from bench.settings import REPO_ROOT, RESULTS_DIR, Settings, SettingsError, load_settings
from quill import shortcuts
from quill.voice import (OPEN_PROJECT, UNRECOGNIZED, OpenProject, Parser, VoiceCommands, VoiceHints, VoiceOutcome,
                         normalize)
from quill.vocabulary import Vocabulary
from quill.whisper import SessionHints

SET_NAME = "voice"
SUMMARY_SCHEMA = 1
DEFAULT_SUMMARY = REPO_ROOT / "docs" / "research" / "voice-commands-summary.json"
# Targets of the Phase 4 goal. Never lowered here.
TARGET_CORRECT_RATE = 0.95
TARGET_WRONG_SHORTCUTS = 0

OPEN = "abrir"
NOTHING = "nada"
NONE_MARK = "—"
CASES = ("exato", "irmão", "vocabulário", "variante", "negativo")
NEGATIVE = "negativo"
# The session's reason for an empty final text (quill.session.NO_SPEECH): the command is not run.
NO_SPEECH = "no_speech"


class VoiceScriptError(DatasetError):
    """The voice-command script is malformed (messages name the id, never the text)."""


@dataclass(frozen=True)
class VoiceRow:
    id: str
    case: str
    text: str = field(repr=False)  # the spoken command, with <projeto-N> placeholders
    action: str = OPEN  # "abrir" or "nada"
    project: str = field(default="", repr=False)  # the placeholder to open; "" for "nada"


def parse_voice_script(text: str, id_prefix: str = "vc") -> list[VoiceRow]:
    """The rows of the voice-command script, with their expected actions checked."""
    rows = []
    for row in parse_script(text, id_prefix, markup=True):
        if row.case not in CASES:
            raise VoiceScriptError(f"{row.id}: caso must be one of {', '.join(CASES)}")
        placeholders = PLACEHOLDER.findall(row.text)
        if row.intent == OPEN:
            if len(placeholders) != 1 or row.project != placeholders[0]:
                raise VoiceScriptError(f"{row.id}: 'abrir' needs one <projeto-N> in frase, repeated in projeto")
            if row.case == NEGATIVE:
                raise VoiceScriptError(f"{row.id}: a negative opens nothing")
            project = row.project
        elif row.intent == NOTHING:
            if placeholders or row.project != NONE_MARK:
                raise VoiceScriptError(f"{row.id}: 'nada' names no <projeto-N> and has projeto {NONE_MARK}")
            if row.case != NEGATIVE:
                raise VoiceScriptError(f"{row.id}: only a negative opens nothing")
            project = ""
        else:
            raise VoiceScriptError(f"{row.id}: intenção must be '{OPEN}' or '{NOTHING}'")
        rows.append(VoiceRow(row.id, row.case, row.text, row.intent, project))
    return rows


def load_voice_rows(settings: Settings) -> list[VoiceRow]:
    if not settings.recording_script.is_file():
        raise DatasetError(f"{settings.name}: recording script not found")
    return parse_voice_script(settings.recording_script.read_text(encoding="utf-8"), settings.id_prefix)


def placeholders(rows: Sequence[VoiceRow]) -> list[str]:
    return sorted({row.project for row in rows if row.project}, key=lambda p: int(p[len("<projeto-"):-1]))


def recording_names(settings: Settings, rows: Sequence[VoiceRow]) -> dict[str, str]:
    """Placeholder -> shortcut name shown while recording; every placeholder must be named."""
    mapping = dict(settings.projects or ())
    missing = [placeholder for placeholder in placeholders(rows) if placeholder not in mapping]
    if missing:
        raise DatasetError(f"voice: {len(missing)} placeholder(s) without a name: add them to [voice.projects] "
                           "in local/bench.toml")
    return mapping


def spoken_name(text: str) -> str | None:
    """The project name the open-project command would read from ``text``."""
    found = OPEN_PROJECT.match(normalize(text))
    return found.group("name") if found else None


@dataclass(frozen=True)
class FixtureCheck:
    """Whether the configured shortcut folders fit the script (counts only)."""

    shortcuts: int
    folders_failed: int
    named: int  # placeholders with a name in [voice.projects]
    placeholders: int
    found: int  # named placeholders whose name is a listed shortcut
    negatives: int
    negatives_clear: int  # negatives whose spoken name is not a listed shortcut
    problems: tuple[str, ...] = ()  # take ids, never names

    @property
    def ok(self) -> bool:
        return not self.problems and self.named == self.placeholders and self.shortcuts > 0


def check_fixture(rows: Sequence[VoiceRow], mapping: dict[str, str], listing: shortcuts.Listing) -> FixtureCheck:
    """Every expected name must be a listed shortcut; no negative may name one."""
    keys = {shortcuts.name_key(shortcut.name) for shortcut in listing.shortcuts}
    wanted = placeholders(rows)
    named = [p for p in wanted if p in mapping]
    found = [p for p in named if shortcuts.name_key(mapping[p]) in keys]
    problems = [row.id for row in rows if row.project and (row.project not in mapping
                                                           or shortcuts.name_key(mapping[row.project]) not in keys)]
    negatives = [row for row in rows if row.action == NOTHING]
    clear = 0
    for row in negatives:
        name = spoken_name(row.text)
        if name is None:
            problems.append(row.id)
        elif shortcuts.name_key(name) in keys:
            problems.append(row.id)
        else:
            clear += 1
    return FixtureCheck(len(listing.shortcuts), listing.folders_failed, len(named), len(wanted), len(found),
                        len(negatives), clear, tuple(sorted(set(problems))))


# ---------------------------------------------------------------- the app's voice commands, opening nothing


class RecordingLauncher:
    """The launcher interface of ``quill.shortcuts.launch``; it records the request and opens nothing."""

    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.started: list[list[str]] = []

    def open_shortcut(self, path: Path) -> None:
        self.opened.append(Path(path))

    def start(self, argv: Sequence[str]) -> None:
        self.started.append(list(argv))


class MeasuredCommands:
    """``quill.voice.default_parser``'s commands with a launcher that opens nothing.

    ``run`` returns the outcome and the name of the shortcut the app would
    have opened (None when it would open nothing).
    """

    def __init__(self, folders: Sequence[Path], vocabulary: Vocabulary, *,
                 lister: Callable[[Sequence[Path]], shortcuts.Listing] = shortcuts.list_shortcuts,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.launcher = RecordingLauncher()
        self._match: shortcuts.Match | None = None

        def matcher(*args: object, **kwargs: object) -> shortcuts.Match:
            self._match = shortcuts.match(*args, **kwargs)  # type: ignore[arg-type]
            return self._match

        self.parser = Parser([OpenProject(folders, lambda: vocabulary, self.launcher, lister=lister,
                                          matcher=matcher, clock=clock)])
        self.commands = VoiceCommands(self.parser, clock)

    def run(self, text: str) -> tuple[VoiceOutcome, str | None]:
        self._match = None
        outcome = self.commands.run(text)
        opened = None
        if outcome.ok and self._match is not None and self._match.shortcut is not None:
            opened = self._match.shortcut.name
        return outcome, opened


# ---------------------------------------------------------------- measurement


@dataclass(frozen=True)
class TakeResult:
    """One spoken command. Holds spoken text and names: never printed or summarized."""

    id: str
    case: str
    expected: str | None = field(repr=False)  # the shortcut to open; None: open nothing
    heard: str = field(repr=False)
    reason: str = ""
    opened: str | None = field(default=None, repr=False)
    shown: str = field(default="", repr=False)  # the indicator text
    asr_s: float = 0.0
    command_s: float = 0.0

    @property
    def correct(self) -> bool:
        if self.expected is None:
            return self.opened is None
        return self.opened is not None and shortcuts.name_key(self.opened) == shortcuts.name_key(self.expected)

    @property
    def wrong_shortcut(self) -> bool:
        return self.opened is not None and not self.correct


def expected_names(takes: Sequence[Take], rows: dict[str, VoiceRow]) -> dict[str, str | None]:
    """The shortcut each take must open: the name its placeholder had when it was recorded."""
    expected: dict[str, str | None] = {}
    for take in takes:
        row = rows[take.id]
        if row.action == NOTHING:
            expected[take.id] = None
        elif len(take.project_names) != 1:
            raise DatasetError(f"{take.id}: the take has no recorded project name")
        else:
            expected[take.id] = take.project_names[0]
    return expected


def measure(takes: Sequence[Take], rows: dict[str, VoiceRow], transcripts: Sequence[tuple[str, float]],
            commands: MeasuredCommands) -> list[TakeResult]:
    """Run each transcribed take through the app's voice commands, as the session does."""
    expected = expected_names(takes, rows)
    results = []
    for take, (heard, asr_s) in zip(takes, transcripts, strict=True):
        row = rows[take.id]
        if not heard.strip():
            results.append(TakeResult(take.id, row.case, expected[take.id], heard, NO_SPEECH, asr_s=asr_s))
            continue
        outcome, opened = commands.run(heard)
        results.append(TakeResult(take.id, row.case, expected[take.id], heard, outcome.reason, opened, outcome.text,
                                  asr_s, outcome.timings.get("total_s", 0.0)))
    return results


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def _seconds(values: Sequence[float], percent: float) -> float | None:
    value = percentile_nearest_rank(values, percent)
    return None if value is None else round(value, 3)


def targets() -> dict:
    return {"correct_rate_min": TARGET_CORRECT_RATE, "wrong_shortcuts_max": TARGET_WRONG_SHORTCUTS}


def dataset_counts(dataset: Dataset) -> dict:
    return {"script_rows": dataset.script_rows, "recorded": len(dataset.takes), "pending": len(dataset.pending),
            "invalid": len(dataset.invalid)}


def aggregate(results: Sequence[TakeResult], dataset: Dataset | None = None) -> dict:
    """Counts, rates, the targets and whether they are met; never text or names."""
    n = len(results)
    correct = sum(r.correct for r in results)
    wrong = sum(r.wrong_shortcut for r in results)
    rate = _rate(correct, n)
    by_case = {case: {"takes": sum(r.case == case for r in results),
                      "correct": sum(r.correct for r in results if r.case == case)}
               for case in CASES if any(r.case == case for r in results)}
    summary: dict = {
        "schema": SUMMARY_SCHEMA,
        "kind": "voice_commands",
        "status": "measured",
        "targets": targets(),
        "takes": n,
        "correct": correct,
        "correct_rate": rate,
        "wrong_shortcuts": wrong,
        "ambiguous": sum(r.reason == shortcuts.AMBIGUOUS for r in results),
        "unrecognized": sum(r.reason == UNRECOGNIZED for r in results),
        "no_match": sum(r.reason == shortcuts.NO_MATCH for r in results),
        "reasons": dict(sorted(Counter(r.reason for r in results).items())),
        "by_case": by_case,
        "meets_targets": {
            "correct_rate": rate is not None and rate >= TARGET_CORRECT_RATE,
            "wrong_shortcuts": n > 0 and wrong <= TARGET_WRONG_SHORTCUTS,
        },
        "latency": {
            "transcription_p50_s": _seconds([r.asr_s for r in results], 50),
            "transcription_p95_s": _seconds([r.asr_s for r in results], 95),
            "command_p50_s": _seconds([r.command_s for r in results], 50),
            "command_p95_s": _seconds([r.command_s for r in results], 95),
        },
    }
    summary["meets_targets"]["all"] = all(summary["meets_targets"].values())
    if dataset is not None:
        summary["dataset"] = dataset_counts(dataset)
    return summary


def pending_summary(dataset: Dataset | None = None, script_rows: int = 0) -> dict:
    """The committed summary before the recordings are complete: the targets, no results."""
    counts = dataset_counts(dataset) if dataset is not None else {
        "script_rows": script_rows, "recorded": 0, "pending": script_rows, "invalid": 0}
    return {
        "schema": SUMMARY_SCHEMA,
        "kind": "voice_commands",
        "status": "pending_recordings",
        "targets": targets(),
        "takes": 0,
        "correct": None,
        "correct_rate": None,
        "wrong_shortcuts": None,
        "ambiguous": None,
        "unrecognized": None,
        "no_match": None,
        "meets_targets": None,
        "dataset": counts,
    }


# ---------------------------------------------------------------- private outputs


def _inside(path: Path, root: Path) -> bool:
    return Path(root).resolve() in Path(path).resolve().parents


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|") or NONE_MARK


def write_private(run_dir: Path, results: Sequence[TakeResult], results_dir: Path = RESULTS_DIR) -> tuple[Path, Path]:
    """Per-take JSON and the human-checkable table, only under bench/results/."""
    if not _inside(Path(run_dir) / "takes.json", results_dir):
        raise ValueError("voice-command outputs with spoken text may only be written under bench/results/")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"id": r.id, "case": r.case, "expected": r.expected, "heard": r.heard, "reason": r.reason,
             "opened": r.opened, "shown": r.shown, "correct": r.correct, "wrong_shortcut": r.wrong_shortcut,
             "asr_s": round(r.asr_s, 3), "command_s": round(r.command_s, 3)} for r in results]
    takes_path = run_dir / "takes.json"
    takes_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Comandos de voz: verificação manual",
        "",
        "Para cada linha, confirme que a ação é a certa e escreva `sim` na coluna Sponsor, ou `não` com o motivo.",
        "`esperado` é o atalho que devia abrir (— quando não devia abrir nada); `abriria` é o atalho que o Quill",
        "abriria (nada foi aberto nesta medição); `indicador` é o texto que o Quill mostraria.",
        "Este ficheiro tem texto falado e nomes reais: fica só em bench/results/ (ignorado pelo Git).",
        "",
        "| id | caso | ouvido | esperado | abriria | motivo | indicador | certa | Sponsor |",
        "|----|------|--------|----------|---------|--------|-----------|-------|---------|",
    ]
    for r in results:
        lines.append("| " + " | ".join(_cell(value) for value in (
            r.id, r.case, r.heard, r.expected or NONE_MARK, r.opened or NONE_MARK, r.reason, r.shown,
            "sim" if r.correct else "não")) + " |  |")
    table_path = run_dir / "table.md"
    table_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return takes_path, table_path


def spoken_texts(dataset: Dataset, rows: Sequence[VoiceRow], results: Sequence[TakeResult] = ()) -> list[str]:
    """Every text the summary must not contain: the script commands and the transcriptions."""
    return [*dataset.reference_texts(), *(row.text for row in rows), *(r.heard for r in results)]


# ---------------------------------------------------------------- defaults (GPU and the app's config)


Streamer = Callable[[Sequence[Take]], list[tuple[str, float]]]


def default_streamer(hints: SessionHints) -> tuple[Streamer, dict]:
    """The product streaming path; every take is opened as a voice session with ``hints``."""
    from bench.rewrite import default_streamer as product_streamer

    return product_streamer((), hints)


def default_tokens() -> Callable[[str], int]:
    """The token counter of the measured engine model, as the app counts with its own."""
    from bench.pipeline import STREAM_MODEL
    from quill.whisper import TokenCounter

    return TokenCounter(STREAM_MODEL)


def hints_record(hints: SessionHints, shortcut_names: int, tokens: Callable[[str], int]) -> dict:
    """What the run was decoded with, for the summary: the kind and sizes, never the names."""
    exact = getattr(tokens, "exact", None)
    return {"kind": "voice", "language": hints.language, "prompt_chars": len(hints.prompt or ""),
            "hotwords_chars": len(hints.hotwords or ""), "shortcut_names": shortcut_names,
            "token_count": "unknown" if exact is None else ("tokenizer" if exact else "bytes")}


def shortcut_folders() -> tuple[Path, ...]:
    """``[voice_commands] shortcut_dirs`` of the app's config (local/quill.toml over the example)."""
    from quill.config import load_config

    return tuple(Path(folder) for folder in load_config().voice.shortcut_dirs)


# ---------------------------------------------------------------- commands


def _fixture_lines(check: FixtureCheck) -> list[str]:
    lines = [
        f"names: {check.named} of {check.placeholders} placeholders named in [voice.projects]",
        f"shortcuts: {check.shortcuts} listed ({check.folders_failed} folders failed); mapped names with a shortcut "
        f"{check.found} of {check.placeholders}; negatives without a shortcut {check.negatives_clear} of "
        f"{check.negatives}",
    ]
    if check.problems:
        lines.append("  fixture problems (take ids): " + ", ".join(check.problems))
    return lines


def dry_run(settings: Settings, out: Callable[[str], None] = print,
            folders: Callable[[], Sequence[Path]] = shortcut_folders,
            lister: Callable[[Sequence[Path]], shortcuts.Listing] = shortcuts.list_shortcuts) -> int:
    """Counts only; opens no audio, model, microphone or shortcut."""
    voice = settings.for_set(SET_NAME)
    try:
        rows = load_voice_rows(voice)
        dataset = load_dataset(voice)
    except DatasetError as exc:
        out(f"error: {exc}")
        return 2
    cases = Counter(row.case for row in rows)
    actions = Counter(row.action for row in rows)
    out(f"voice commands script: {len(rows)} rows; cases " + ", ".join(f"{c} {cases[c]}" for c in CASES if cases[c])
        + "; actions " + ", ".join(f"{k} {v}" for k, v in sorted(actions.items())))
    complete = not dataset.invalid and len(dataset.takes) >= voice.minimum_takes and not dataset.pending
    out(f"recorded {len(dataset.takes)} of {len(rows)}, pending {len(dataset.pending)}, invalid "
        f"{len(dataset.invalid)}, discarded {dataset.discarded}; complete: {'yes' if complete else 'no'}")
    for invalid in dataset.invalid:
        out(f"  invalid {invalid.id}: {invalid.reason}")
    try:
        configured = tuple(folders())
    except Exception as exc:  # noqa: BLE001 - the dry run reports the app config problem and goes on
        out(f"shortcuts: the quill config could not be read ({type(exc).__name__})")
        return 0
    if not configured:
        out("shortcuts: no [voice_commands] shortcut_dirs in local/quill.toml")
        return 0
    for line in _fixture_lines(check_fixture(rows, dict(voice.projects or ()), lister(configured))):
        out(line)
    return 0


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def report_lines(summary: dict) -> list[str]:
    latency = summary["latency"]
    met = summary["meets_targets"]
    return [
        f"takes {summary['takes']}; correct {summary['correct']}/{summary['takes']} ({_pct(summary['correct_rate'])}), "
        f"target >= {_pct(TARGET_CORRECT_RATE)}: {'met' if met['correct_rate'] else 'NOT met'}",
        f"wrong shortcuts {summary['wrong_shortcuts']}, target {TARGET_WRONG_SHORTCUTS}: "
        f"{'met' if met['wrong_shortcuts'] else 'NOT met'}",
        f"ambiguous {summary['ambiguous']}, unrecognized {summary['unrecognized']}, no match {summary['no_match']}; "
        "reasons " + ", ".join(f"{k} {v}" for k, v in summary["reasons"].items()),
        "by case: " + ", ".join(f"{k} {v['correct']}/{v['takes']}" for k, v in summary["by_case"].items()),
        "latency p50/p95 s: transcription {} / {}, command {} / {}".format(
            latency["transcription_p50_s"], latency["transcription_p95_s"], latency["command_p50_s"],
            latency["command_p95_s"]),
    ]


def main(
    argv: list[str] | None = None,
    *,
    streamer_factory: Callable[[SessionHints], tuple[Streamer, dict]] = default_streamer,
    tokens_factory: Callable[[], Callable[[str], int]] = default_tokens,
    folders: Callable[[], Sequence[Path]] = shortcut_folders,
    lister: Callable[[Sequence[Path]], shortcuts.Listing] = shortcuts.list_shortcuts,
    results_dir: Path = RESULTS_DIR,
    out: Callable[[str], None] = print,
) -> int:
    from quill.vocabulary import LOCAL_VOCABULARY, VocabularyError, load_vocabulary

    parser = argparse.ArgumentParser(prog="bench.voice_commands",
                                     description="Voice-command measurement on spoken commands.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--dry-run", action="store_true", help="script, recording and shortcut counts only")
    parser.add_argument("--vocabulary", type=Path, default=LOCAL_VOCABULARY,
                        help="personal vocabulary (default local/vocabulary.toml; missing is empty)")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY,
                        help="aggregate summary JSON (default docs/research/voice-commands-summary.json)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        settings = load_settings(args.config)
        if args.dry_run:
            return dry_run(settings, out, folders, lister)
        voice = settings.for_set(SET_NAME)
        rows = load_voice_rows(voice)
        dataset = load_dataset(voice)
        vocabulary = load_vocabulary(args.vocabulary)
    except (SettingsError, DatasetError, VocabularyError) as exc:
        out(f"error: {exc}")
        return 2
    if dataset.invalid or dataset.pending or len(dataset.takes) < voice.minimum_takes:
        out(f"error: {len(dataset.takes)} of {len(rows)} voice commands recorded ({len(dataset.invalid)} invalid); "
            "all are needed (py -3.12 -m bench.record --set voice)")
        return 2
    configured = tuple(folders())
    listing = lister(configured)
    check = check_fixture(rows, dict(voice.projects or ()), listing)
    if not check.ok:
        for line in _fixture_lines(check):
            out(line)
        out("error: the shortcut folders do not fit the script; nothing was measured")
        return 2
    by_id = {row.id: row for row in rows}
    # The app's voice hints (its builder and token count), from the listing the commands match against.
    tokens = tokens_factory()
    hints = VoiceHints(configured, lambda: vocabulary, tokens=tokens, lister=lambda _folders: listing)()

    from bench.engines.base import EngineError, EngineUnavailable

    run_dir = Path(results_dir) / "voice" / time.strftime("%Y%m%d-%H%M%S")
    streamer = None
    try:
        streamer, stream_options = streamer_factory(hints)
        streamer(dataset.takes[:1])  # warm-up: loads the model and fills the caches
        transcripts = streamer(dataset.takes)
    except (EngineUnavailable, EngineError) as exc:
        out(f"error: {' '.join(str(exc).split())[:200]}")
        return 2
    finally:
        close = getattr(streamer, "close", None)
        if close is not None:
            close()
    commands = MeasuredCommands(configured, vocabulary, lister=lambda _folders: listing)
    try:
        results = measure(dataset.takes, by_id, transcripts, commands)
    except DatasetError as exc:
        out(f"error: {exc}")
        return 2

    summary = {**aggregate(results, dataset),
               "engine": {**stream_options, "hints": hints_record(hints, len(listing.shortcuts), tokens)}}
    names = [entry.text for entry in vocabulary.names] + [value for _, value in voice.projects or ()] + [
        shortcut.name for shortcut in listing.shortcuts]
    texts = spoken_texts(dataset, rows, results)
    try:
        write_private(run_dir, results, results_dir)
        write_summary(run_dir / "summary.json", summary, texts, names)
        write_summary(Path(args.summary), summary, texts, names)
    except ValueError as exc:
        out(f"error: {exc}")
        return 1
    for line in report_lines(summary):
        out(line)
    out(f"summary written to {Path(args.summary).name}; per-take outputs and the manual table under "
        "bench/results/voice/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
