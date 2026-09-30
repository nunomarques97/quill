"""Record a committed script take by take through MME at 16 kHz mono PCM16.

Usage:
    py -3.12 -m bench.record                   # record every take not recorded yet
    py -3.12 -m bench.record --redo dt-05,dt-07
    py -3.12 -m bench.record --set rewrite     # the command-mode rewrite instructions
    py -3.12 -m bench.record --set voice       # the voice commands ("abre VS Code no <projeto>")
    py -3.12 -m bench.record --set prompts     # Claude Code prompts with real domain terms
    py -3.12 -m bench.record --list-devices    # MME inputs; the microphone is not opened

Four sets are recorded: ``dictation`` (the default, ``dt-NN``),
``rewrite`` (``rw-NN``, the spoken instructions of command mode; the
invented selected text of each take is shown first, for context only, and is
not read aloud) and ``voice`` (``vc-NN``, the spoken voice commands; the
names are the project-hub shortcut names of ``[voice.projects]``, and every
placeholder must have one before the microphone opens) and ``prompts``
(``pp-NN``, spoken Claude Code prompts; ``[prompts.projects]`` names the
``<projeto-N>`` placeholders and ``[prompts.terms]`` the real domain terms of
the ``<termo-N>`` ones, all required before the microphone opens; each take's
manifest entry keeps the terms under ``termos``). Each phrase is shown
with the placeholders replaced by the names and terms from the local
configuration. Enter starts and stops a take; ``s``
skips it (it stays pending), ``q`` quits, ``r`` repeats the take just saved.
A take is rejected, and asked again, when it has 0.5 s or more of exact
zeros, falls behind (or runs ahead of) the clock, is near-silent or shorter
than a second. A repeated take keeps the previous file as
``dt-NN.invalida-K.wav``. Running the command again resumes at the first take
not recorded yet.

WAV files and the manifest are written only under the ignored ``local/``
folder. The prompts are in European Portuguese for the Sponsor; nothing
spoken is logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bench.audio_mme import (
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    AudioError,
    Capture,
    WinMM,
    input_devices,
    name_matches,
    select_device,
    take_problem,
)
from bench.dataset import (
    DISCARDED_MARKER,
    PLACEHOLDER,
    DatasetError,
    ScriptRow,
    display_text,
    dictation_projects,
    known_names,
    load_reference_names,
    load_script,
    parse_markup,
    resolve_placeholders,
    resolve_terms,
)
from bench.settings import LOCAL_DIR, Settings, SettingsError, load_settings

MANIFEST_VERSION = 1
RECORDED_SETS = ("dictation", "rewrite", "voice", "prompts")


class Console:
    """Terminal prompts; tests replace it with a scripted fake."""

    def __init__(self) -> None:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str) -> str:
        try:
            return input(prompt).strip().casefold()
        except EOFError:
            return "q"


def configured_device(settings: Settings) -> str:
    """[recorder].device from local/bench.toml, else the reference config's microphone name."""
    if settings.recorder_device:
        return settings.recorder_device
    path = settings.reference_config
    if path.is_file():
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError:
            data = {}
        table = data.get("microfone", data.get("microphone"))
        if isinstance(table, dict):
            name = table.get("nome", table.get("name"))
            if isinstance(name, str) and name.strip():
                return name.strip()
    raise SettingsError("no microphone configured: set [recorder].device in local/bench.toml")


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root.resolve() or root.resolve() in resolved.parents


def next_discarded_name(folder: Path, take_id: str) -> str:
    index = 1
    while (folder / f"{take_id}{DISCARDED_MARKER}{index}.wav").exists():
        index += 1
    return f"{take_id}{DISCARDED_MARKER}{index}.wav"


def write_wav(path: Path, pcm: bytes) -> None:
    temporary = path.with_name(path.name + ".part")
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    os.replace(temporary, path)


@dataclass
class Report:
    saved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rejected: int = 0
    quit: bool = False


@dataclass
class Session:
    rows: Sequence[ScriptRow]
    mapping: dict[str, str] = field(repr=False)
    known: frozenset[str] = field(repr=False)
    recordings_dir: Path
    manifest_path: Path
    capture_factory: Callable[[], Capture]
    console: Console
    now: Callable[[], datetime] = datetime.now
    # Text shown before the phrase for context, never read aloud (rewrite set: the selected text).
    context: Callable[[ScriptRow], str | None] = field(default=lambda row: None, repr=False)
    # <termo-N> -> real domain term shown and stored with each take (prompts set).
    terms: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not _inside(self.recordings_dir, LOCAL_DIR) or not _inside(self.manifest_path, LOCAL_DIR):
            raise SettingsError("recordings must stay under the ignored local/ folder")

    # ------------------------------------------------------------ manifest

    def load_manifest(self) -> dict:
        if not self.manifest_path.is_file():
            return {"versao": MANIFEST_VERSION, "lingua": "pt", "projetos": dict(self.mapping), "gravacoes": {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DatasetError("dictation manifest is not valid JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("gravacoes"), dict):
            raise DatasetError("dictation manifest has no 'gravacoes' table")
        return data

    def save_manifest(self, data: dict) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_name(self.manifest_path.name + ".part")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def recorded(self, manifest: dict, take_id: str) -> bool:
        entry = manifest["gravacoes"].get(take_id)
        return isinstance(entry, dict) and (self.recordings_dir / f"{take_id}.wav").is_file()

    def queue(self, redo: Sequence[str] = ()) -> list[ScriptRow]:
        by_id = {row.id: row for row in self.rows}
        if redo:
            unknown = [take_id for take_id in redo if take_id not in by_id]
            if unknown:
                raise DatasetError(f"not in the recording script: {', '.join(unknown)}")
            return [by_id[take_id] for take_id in redo]
        manifest = self.load_manifest()
        return [row for row in self.rows if not self.recorded(manifest, row.id)]

    # ------------------------------------------------------------ takes

    def phrase(self, row: ScriptRow) -> str:
        resolved, _ = resolve_placeholders(row.text, self.mapping, self.known, row.id)
        resolved, _ = resolve_terms(resolved, self.terms, row.id)
        return display_text(parse_markup(resolved, row.id))

    def save(self, row: ScriptRow, pcm: bytes, elapsed_s: float) -> float:
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.load_manifest()
        target = self.recordings_dir / f"{row.id}.wav"
        replaced = None
        if target.exists():
            replaced = next_discarded_name(self.recordings_dir, row.id)
            os.replace(target, self.recordings_dir / replaced)
        write_wav(target, pcm)
        duration = round(len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE, 3)
        manifest["gravacoes"][row.id] = {
            "ficheiro": target.name,
            "duracao_s": duration,
            "tempo_real_s": round(elapsed_s, 3),
            "origem": "microfone",
            "projetos": dict(self.mapping),
            "gravado_em": self.now().isoformat(timespec="seconds"),
            "substituiu": replaced,
        }
        if self.terms:
            manifest["gravacoes"][row.id]["termos"] = dict(self.terms)
        self.save_manifest(manifest)
        return duration

    def record_one(self, row: ScriptRow, position: str, report: Report) -> str:
        """Record ``row`` until saved or skipped; returns "next", "skip" or "quit"."""
        say, ask = self.console.say, self.console.ask
        while True:
            say()
            say(f"[{position}] {row.id} · {row.style or '—'} · {row.case or '—'}")
            context = self.context(row)
            if context:
                say(f"  Texto selecionado (não ler): {context}")
                say(f"  Instrução a dizer: {self.phrase(row)}")
            else:
                say(f"  {self.phrase(row)}")
            answer = ask("Enter = gravar · s = saltar · q = sair: ")
            if answer == "q":
                return "quit"
            if answer == "s":
                report.skipped.append(row.id)
                return "skip"
            capture = self.capture_factory()
            capture.start()
            try:
                answer = ask("  A gravar… Enter para parar. ")
            finally:
                result = capture.stop()
            problem = take_problem(result)
            if problem is not None:
                report.rejected += 1
                say(f"  Take rejeitado ({problem}). Vamos repetir esta frase.")
                continue
            duration = self.save(row, result.pcm, result.elapsed_s)
            say(f"  Guardado: {row.id} ({duration:.1f} s).")
            answer = ask("Enter = próxima · r = repetir esta · q = sair: ")
            if answer == "r":
                continue
            if row.id not in report.saved:
                report.saved.append(row.id)
            return "quit" if answer == "q" else "next"

    def run(self, redo: Sequence[str] = ()) -> Report:
        report = Report()
        queue = self.queue(redo)
        if not queue:
            self.console.say("Todas as frases do guião já estão gravadas.")
            return report
        for number, row in enumerate(queue, start=1):
            outcome = self.record_one(row, f"{number}/{len(queue)}", report)
            if outcome == "quit":
                report.quit = True
                break
        manifest = self.load_manifest()
        done = sum(1 for row in self.rows if self.recorded(manifest, row.id))
        self.console.say()
        self.console.say(f"Gravadas {done} de {len(self.rows)} frases; faltam {len(self.rows) - done}.")
        return report


def rewrite_context(settings: Settings) -> Callable[[ScriptRow], str | None]:
    """The invented selected text of each rewrite take, shown while recording its instruction."""
    from bench.rewrite import load_rewrite_rows

    selections = {row.id: row.selection for row in load_rewrite_rows(settings)}
    return lambda row: selections.get(row.id)


def list_devices(settings_path: Path | None, api: object | None = None, console: Console | None = None) -> int:
    console = console or Console()
    try:
        names = input_devices(api)
    except (AudioError, OSError) as exc:
        console.say(f"error: {exc}")
        return 2
    configured = None
    try:
        configured = configured_device(load_settings(settings_path))
    except (SettingsError, DatasetError):
        pass
    console.say(f"MME inputs: {len(names)} (the microphone is not opened)")
    for index, name in enumerate(names):
        mark = "  <- configured" if configured and name_matches(name, configured) else ""
        console.say(f"  {index}: {name}{mark}")
    if configured is None:
        console.say("no microphone configured: set [recorder].device in local/bench.toml")
    return 0


def main(argv: list[str] | None = None, *, api: object | None = None, console: Console | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.record", description="Record a committed script through MME.")
    parser.add_argument("--config", type=Path, default=None, help="settings file (default local/bench.toml)")
    parser.add_argument("--set", choices=RECORDED_SETS, default="dictation", help="script to record (default dictation)")
    parser.add_argument("--list-devices", action="store_true", help="list MME inputs without opening them")
    parser.add_argument("--redo", default="", help="comma-separated take ids to record again")
    args = parser.parse_args(argv)
    if args.list_devices:
        return list_devices(args.config, api, console)
    console = console or Console()
    try:
        settings = load_settings(args.config)
        chosen = settings.for_set(args.set)
        rows = load_script(chosen)
        context = rewrite_context(chosen) if args.set == "rewrite" else (lambda row: None)
        terms: dict[str, str] = {}
        if args.set == "prompts":
            from bench.prompts import load_prompt_rows, recording_names as prompt_names

            mapping, terms = prompt_names(chosen, load_prompt_rows(chosen))
            known = known_names(chosen)
        elif args.set == "voice":
            from bench.voice_commands import load_voice_rows, recording_names

            mapping = recording_names(chosen, load_voice_rows(chosen))
            known = known_names(chosen)
        elif any(PLACEHOLDER.search(row.text) for row in rows) or args.set == "dictation":
            mapping = dictation_projects(chosen, settings)
            known = load_reference_names(chosen.reference_config)
        else:
            mapping, known = {}, frozenset()
        device = configured_device(settings)
        api = api if api is not None else WinMM()
        index = select_device(input_devices(api), device)
        session = Session(
            rows=rows,
            mapping=mapping,
            known=known,
            recordings_dir=chosen.recordings_dir,
            manifest_path=chosen.manifest,
            capture_factory=lambda: Capture(api, index),
            console=console,
            context=context,
            terms=terms,
        )
        redo = [part.strip() for part in args.redo.split(",") if part.strip()]
        session.run(redo)
    except (SettingsError, DatasetError, AudioError) as exc:
        console.say(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        console.say("interrupted; takes saved so far are kept")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
