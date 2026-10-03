"""Replay the real recordings through the product's streaming transcription.

Usage:
    .venv\\Scripts\\python -m bench.streaming --set all --mode deterministic
    .venv\\Scripts\\python -m bench.streaming --set all --mode realtime
    .venv\\Scripts\\python -m bench.streaming --mode realtime --model large-v3 --step-s 1.0

The model defaults to the product's engine (``quill.whisper.DEFAULT_MODEL``)
with its tuning (``quill.streaming.options_for``); each option flag replaces
one field of that tuning.

Each take is read in place and fed to ``quill.streaming`` in 50 ms chunks,
the size of an MME capture buffer; the release comes right after the last
chunk, as when the trigger is released at the end of the take.

- ``deterministic``: the replay waits until the worker is idle after every
  chunk, so every partial runs at the same audio time on every run and the
  final text is reproducible. It measures the quality of the streamed text.
- ``realtime``: chunks arrive at the pace of the recording and the worker
  keeps up as it can (stale partials are dropped), with the model warm. It
  measures the time from release to final text; takes up to 15 s count
  towards p50/p95. A GPU snapshot (free memory, utilization, models loaded in
  the shared Ollama) is taken before and after.

Per-take text and every timing go only under ``bench/results/streaming/``;
the console shows aggregates only. The model is never downloaded and the
shared Ollama is only read (``/api/ps``), never changed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from bench.dataset import DatasetError, Take
from bench.engines.base import EngineError, EngineUnavailable, Hints, build_hints, wav_pcm
from bench.metrics import corpus_wer, load_terms, percentile_nearest_rank
from bench.settings import RESULTS_DIR, SettingsError, load_settings
from quill.streaming import (BYTES_PER_SECOND, FinalResult, HintSource, StreamingTranscriber, StreamOptions,
                             options_for)
from quill.whisper import DEFAULT_MODEL, MODELS, SessionHints

CHUNK_S = 0.05
MAX_LATENCY_AUDIO_S = 15.0
FINAL_TIMEOUT_S = 120.0
SETS = ("commands", "dictation")
BASELINE_SUMMARY = Path(__file__).resolve().parent.parent / "docs" / "research" / "phase2-summary.json"
GPU_QUERY = ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.free", "--format=csv,noheader,nounits"]


@dataclass(frozen=True)
class Replay:
    """One take through the streaming path. Holds spoken text: never printed."""

    take: Take = field(repr=False)
    set_name: str
    result: FinalResult = field(repr=False)
    release_to_final_s: float


def chunks(pcm: bytes, chunk_s: float = CHUNK_S) -> list[bytes]:
    size = max(2, int(chunk_s * BYTES_PER_SECOND) // 2 * 2)
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]


def _wait(handle, timeout: float) -> FinalResult:
    result = handle.wait(timeout)
    if result is None:
        raise TimeoutError("final text not ready in time")
    return result


def replay_deterministic(transcriber: StreamingTranscriber, pcm: bytes, chunk_s: float = CHUNK_S,
                         hints: SessionHints | HintSource | None = None) -> FinalResult:
    """Feed by audio time, draining the worker after every chunk: reproducible text.

    ``hints`` open the session as the app opens a voice session (None: the
    vocabulary hints). A hint source (``quill.heard.HeardHints``, as mouse 5
    into Claude Code gets) is asked by the session after each of the replay's
    own partials; as every partial runs at the same audio time, the hints it
    chooses and their switches (``FinalResult.hint_switches``) are
    reproducible too.
    """
    session = transcriber.open() if hints is None else transcriber.open(hints=hints)
    for chunk in chunks(pcm, chunk_s):
        session.feed(chunk)
        if not transcriber.drain(FINAL_TIMEOUT_S):
            raise TimeoutError("streaming worker did not become idle")
    return _wait(session.release(), FINAL_TIMEOUT_S)


def replay_realtime(
    transcriber: StreamingTranscriber,
    pcm: bytes,
    chunk_s: float = CHUNK_S,
    *,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[FinalResult, float]:
    """Feed at the recording's pace; returns the result and release-to-final seconds."""
    session = transcriber.open()
    started = clock()
    audio = 0.0
    for chunk in chunks(pcm, chunk_s):
        audio += len(chunk) / BYTES_PER_SECOND
        delay = started + audio - clock()
        if delay > 0:
            sleep(delay)
        session.feed(chunk)
    released = clock()
    result = _wait(session.release(), FINAL_TIMEOUT_S)
    return result, clock() - released


def ollama_loaded() -> list[dict]:
    """Models loaded in the shared Ollama (read-only ``/api/ps``)."""
    from bench.cleanup import OllamaClient

    return OllamaClient(timeout_s=10).loaded()


def gpu_state(run: Callable = subprocess.run, loaded: Callable[[], list[dict]] = ollama_loaded) -> dict:
    """GPU utilization and memory, plus the models loaded in the shared Ollama."""
    state: dict = {"time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        completed = run(GPU_QUERY, capture_output=True, text=True, timeout=15)
        util, used, free = (int(float(v)) for v in completed.stdout.strip().splitlines()[0].split(","))
        state.update(utilization_pct=util, used_mib=used, free_mib=free)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        state["error"] = "nvidia-smi unavailable"
    from bench.cleanup import OllamaError
    from bench.gpu import contention

    try:
        models = loaded()
        state["ollama_loaded"] = [{"name": m.get("name"), "vram_mib": m.get("vram_mib")} for m in models]
        state["contention"] = contention(models, own_model="")
    except (OllamaError, OSError) as exc:
        state["ollama_error"] = str(exc)[:120]
    return state


# ---------------------------------------------------------------- aggregates


def latency_block(replays: Sequence[Replay], max_audio_s: float = MAX_LATENCY_AUDIO_S) -> dict:
    """p50/p95 of release-to-final over takes up to ``max_audio_s``; no text."""
    counted = [r for r in replays if r.result.audio_s <= max_audio_s + 1e-9]
    values = [r.release_to_final_s for r in counted]
    return {
        "n": len(values),
        "excluded_longer": len(replays) - len(counted),
        "p50_s": None if not values else round(percentile_nearest_rank(values, 50), 3),
        "p95_s": None if not values else round(percentile_nearest_rank(values, 95), 3),
        "max_s": None if not values else round(max(values), 3),
        "speculative_hits": sum(1 for r in counted if r.result.speculative_hit),
        "errors": sum(1 for r in replays if not r.result.ok),
        "max_audio_s": round(max((r.result.audio_s for r in counted), default=0.0), 3),
    }


def quality_block(replays: Sequence[Replay]) -> dict:
    return {
        "n": len(replays),
        "wer_clean": None if not replays else round(corpus_wer((r.take.clean, r.result.text) for r in replays), 4),
        "errors": sum(1 for r in replays if not r.result.ok),
        "committed_words": sum(r.result.committed_words for r in replays),
        "partials": sum(r.result.partials for r in replays),
        "dropped": sum(r.result.dropped for r in replays),
    }


def write_private(run_dir: Path, name: str, payload: dict, results_dir: Path = RESULTS_DIR) -> Path:
    target = Path(run_dir).resolve() / f"{name}.json"
    if Path(results_dir).resolve() not in target.parents:
        raise ValueError("streaming outputs may only be written under bench/results/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def replay_rows(replays: Sequence[Replay]) -> list[dict]:
    rows = []
    for r in replays:
        result = asdict(r.result)
        rows.append({"set": r.set_name, "id": r.take.id, "clean": r.take.clean, "final": r.result.text,
                     "release_to_final_s": round(r.release_to_final_s, 4),
                     **{k: result[k] for k in ("error", "audio_s", "speech", "partials", "dropped", "committed_words",
                                               "tail_s", "speculative_hit", "waited_s", "compute_s")}})
    return rows


def baseline_wer(path: Path = BASELINE_SUMMARY) -> dict[str, float | None]:
    try:
        summary = json.loads(Path(path).read_text(encoding="utf-8"))
        return {name: summary["sets"][name]["stages"]["raw"]["wer_clean"] for name in SETS}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


# ---------------------------------------------------------------- runs


def stream_takes(
    model: object,
    takes: Sequence[Take],
    vocabulary: Sequence[str],
    options: StreamOptions = StreamOptions(),
    *,
    clock: Callable[[], float] = time.perf_counter,
    hints: SessionHints | None = None,
) -> list[tuple[str, float]]:
    """Deterministic streamed final text of every take, with its release-to-final seconds.

    Used by ``bench.pipeline`` for the ``streamed`` stage and, with a voice
    session's ``hints``, by ``bench.voice_commands``. The model stays
    loaded for the caller. A failed final raises ``EngineError``: partial
    results are never mixed with complete ones.
    """
    transcriber = StreamingTranscriber(model, options, vocabulary, clock=clock)
    transcriber.start()
    try:
        transcriber.ready.wait()
        if transcriber.load_error:
            raise EngineUnavailable(transcriber.load_error)
        out = []
        for take in takes:
            result = replay_deterministic(transcriber, wav_pcm(take.path.read_bytes())[0], hints=hints)
            if not result.ok:
                raise EngineError(f"streamed final failed: {result.error}")
            out.append((result.text, result.latency_s))
        return out
    finally:
        transcriber.stop(close_model=False)


def run_mode(
    mode: str,
    transcriber: StreamingTranscriber,
    sets: dict[str, Sequence[Take]],
    *,
    log: Callable[[str], None] = print,
) -> list[Replay]:
    replays = []
    for set_name, takes in sets.items():
        for take in takes:
            pcm = wav_pcm(take.path.read_bytes())[0]
            if mode == "deterministic":
                result = replay_deterministic(transcriber, pcm)
                replays.append(Replay(take, set_name, result, result.latency_s))
            else:
                result, seconds = replay_realtime(transcriber, pcm)
                replays.append(Replay(take, set_name, result, seconds))
        log(f"{set_name}: {len(takes)} takes replayed ({mode})")
    return replays


def options_from(args: argparse.Namespace) -> StreamOptions:
    """The tuning of ``args.model`` with the fields given on the command line replaced."""
    values = {f.name: getattr(args, f.name) for f in fields(StreamOptions) if getattr(args, f.name, None) is not None}
    return replace(options_for(getattr(args, "model", DEFAULT_MODEL)), **values)


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="bench.streaming", description="Streaming replay of the real recordings.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--set", choices=(*SETS, "all"), default="all")
    parser.add_argument("--mode", choices=("deterministic", "realtime"), default="deterministic")
    parser.add_argument("--repeat", type=int, default=1, help="realtime passes over the takes (default 1)")
    parser.add_argument("--model", choices=tuple(MODELS), default=DEFAULT_MODEL,
                        help=f"local model to replay with (default {DEFAULT_MODEL}, the product's engine)")
    for option in fields(StreamOptions):
        kind = {bool: None, int: int, float: float}[type(option.default)]
        flag = "--" + option.name.replace("_", "-")
        if kind is None:
            parser.add_argument(flag, type=lambda v: v.lower() in ("1", "true", "yes"), default=None)
        else:
            parser.add_argument(flag, type=kind, default=None)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from bench.pipeline import MAX_LATENCY_P95_S, load_sets
    from quill.whisper import Whisper, WhisperUnavailable

    try:
        options = options_from(args)
        settings = load_settings(args.config)
        names = SETS if args.set == "all" else (args.set,)
        loaded = load_sets(settings, names)
    except (SettingsError, DatasetError, ValueError) as exc:
        out(f"error: {exc}")
        return 2
    sets = {name: dataset.takes for name, (_, dataset) in loaded.items()}
    hints: Hints = build_hints(sorted({n for _, d in loaded.values() for n in d.names}), load_terms())
    transcriber = StreamingTranscriber(Whisper(args.model), options, hints.vocabulary())
    run_dir = RESULTS_DIR / "streaming" / time.strftime("%Y%m%d-%H%M%S")
    started = time.perf_counter()
    transcriber.start()
    transcriber.ready.wait()
    if transcriber.load_error:
        out(f"error: {transcriber.load_error}")
        transcriber.stop()
        return 2
    load_s = time.perf_counter() - started
    try:
        first = next((t for takes in sets.values() for t in takes), None)
        if first is not None:  # warm-up, not measured
            replay_realtime(transcriber, wav_pcm(first.path.read_bytes())[0])
        before = gpu_state()
        replays: list[Replay] = []
        for _ in range(max(1, args.repeat if args.mode == "realtime" else 1)):
            replays += run_mode(args.mode, transcriber, sets, log=out)
        after = gpu_state()
    finally:
        transcriber.stop()
    report: dict = {"mode": args.mode, "model": args.model, "options": asdict(options), "load_s": round(load_s, 3), "gpu": {"before": before, "after": after}, "sets": {}}
    baseline = baseline_wer()
    for name in sets:
        subset = [r for r in replays if r.set_name == name]
        report["sets"][name] = {"quality": quality_block(subset), "latency": latency_block(subset)}
    report["latency_all"] = latency_block(replays)
    write_private(run_dir, args.mode, {"report": report, "takes": replay_rows(replays)})
    for name, block in report["sets"].items():
        quality, latency = block["quality"], block["latency"]
        wer = quality["wer_clean"]
        base = baseline.get(name)
        out(f"{name}: WER clean {'n/a' if wer is None else f'{100 * wer:.1f} %'}"
            f" (raw baseline {'n/a' if base is None else f'{100 * base:.1f} %'}), errors {quality['errors']},"
            f" partials {quality['partials']}, dropped {quality['dropped']}")
        if args.mode == "realtime":
            out(f"  release-to-final (<= {MAX_LATENCY_AUDIO_S:.0f} s, n {latency['n']}): p50 {latency['p50_s']} s,"
                f" p95 {latency['p95_s']} s, max {latency['max_s']} s, speculative hits {latency['speculative_hits']}")
    if args.mode == "realtime":
        overall = report["latency_all"]
        met = overall["p95_s"] is not None and overall["p95_s"] <= MAX_LATENCY_P95_S
        out(f"{'ok  ' if met else 'FAIL'} latency: both sets p95 {overall['p95_s']} s (target <= {MAX_LATENCY_P95_S} s, n {overall['n']})")
        for label, state in (("before", before), ("after", after)):
            out(f"  GPU {label}: utilization {state.get('utilization_pct')} %, free {state.get('free_mib')} MiB,"
                f" Ollama loaded {len(state.get('ollama_loaded') or [])}, contention {len(state.get('contention') or [])}")
    out("per-take outputs and timings under bench/results/streaming/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
