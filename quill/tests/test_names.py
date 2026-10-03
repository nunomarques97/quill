"""quill.clips and quill.names with invented names, generated tones, temp folders and fakes only.

No test opens the microphone, plays sound or reads the real home folder or
Claude Code config: captures are fakes that return generated PCM.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import tempfile
import unittest
import wave
from array import array
from datetime import datetime, timedelta
from pathlib import Path

from quill import clips, names, tests
from quill.audio import SAMPLE_RATE, CaptureResult, take_problem
from quill.clips import ClipsError, ClipStore, clip_file_name, clip_key, process_take, trim_and_normalise
from quill.config import load_config
from quill.projects import DRIVE_FIXED
from quill.speech import MAX_NAME_CHARS, spoken

MIC = "Invented Mic (USB)"


def noise(seconds: float, level: int = 12, seed: int = 7) -> list[int]:
    """Room noise far below speech, never exact zeros."""
    values, state = [], seed
    for _ in range(int(seconds * SAMPLE_RATE)):
        state = (state * 1103515245 + 12345) % 2**31
        values.append((state % (2 * level + 1)) - level or 1)
    return values


def sine(seconds: float, amplitude: float) -> list[int]:
    return [round(amplitude * 32767 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE))
            for i in range(int(seconds * SAMPLE_RATE))]


def pcm(*parts: list[int]) -> bytes:
    return array("h", [value for part in parts for value in part]).tobytes()


def take(speech_s: float = 0.6, amplitude: float = 0.2, lead_s: float = 0.5, tail_s: float = 0.5) -> bytes:
    return pcm(noise(lead_s), sine(speech_s, amplitude), noise(tail_s, seed=11))


def rms(data: bytes) -> float:
    samples = array("h")
    samples.frombytes(data)
    return (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768.0


def peak(data: bytes) -> float:
    samples = array("h")
    samples.frombytes(data)
    return max(abs(s) for s in samples) / 32768.0


# ---------------------------------------------------------------- keys and takes


class KeyTest(unittest.TestCase):
    def test_separators_case_and_accents_share_a_key(self):
        keys = {clip_key(text) for text in ("x-public", "X Public", "x_public", "X.PUBLIC", "  x   public ")}
        self.assertEqual(keys, {"x public"})
        self.assertEqual(clip_key("Órbita-Nó"), clip_key("orbita no"))

    def test_empty_and_long_names(self):
        self.assertEqual(clip_key("-_."), "")
        self.assertEqual(clip_key(42), "")
        self.assertEqual(len(clip_key("a" * 100)), MAX_NAME_CHARS)

    def test_file_name_is_a_hash_never_the_name(self):
        name = clip_file_name(clip_key("Nimbus-Deck"))
        self.assertRegex(name, clips.CLIP_FILE)
        self.assertNotIn("nimbus", name.casefold())
        self.assertEqual(name, clip_file_name(clip_key("nimbus deck")))
        self.assertNotEqual(name, clip_file_name(clip_key("nimbus decks")))


class TrimTest(unittest.TestCase):
    def test_silence_is_trimmed_with_a_short_pad(self):
        out = trim_and_normalise(take(speech_s=0.6, lead_s=1.0, tail_s=1.2))
        seconds = len(out) / 2 / SAMPLE_RATE
        self.assertAlmostEqual(seconds, 0.6 + 2 * clips.PAD_S, delta=2 * clips.FRAME_S)

    def test_quiet_and_loud_takes_end_at_the_same_loudness(self):
        quiet = trim_and_normalise(take(amplitude=0.03))
        loud = trim_and_normalise(take(amplitude=0.6))
        speech = slice(int(clips.PAD_S * SAMPLE_RATE) * 2, -int(clips.PAD_S * SAMPLE_RATE) * 2)
        self.assertAlmostEqual(rms(quiet[speech]), clips.TARGET_RMS, delta=0.01)
        self.assertAlmostEqual(rms(loud[speech]), clips.TARGET_RMS, delta=0.01)
        self.assertAlmostEqual(rms(quiet), rms(loud), delta=0.002)

    def test_peak_is_capped_below_full_scale(self):
        clicks = [0] * SAMPLE_RATE
        for index in range(0, len(clicks), 800):
            clicks[index] = 20000
        speech = [value + click for value, click in zip(sine(1.0, 0.01), clicks)]
        out = trim_and_normalise(pcm(noise(0.3), speech, noise(0.3)))
        self.assertIsNotNone(out)
        self.assertLessEqual(peak(out), clips.MAX_PEAK + 0.001)
        self.assertGreater(peak(out), clips.MAX_PEAK - 0.01)
        self.assertLess(rms(out), clips.TARGET_RMS)

    def test_rejections_have_a_portuguese_reason(self):
        self.assertEqual(process_take(pcm(noise(1.5))), (None, clips.TOO_QUIET))
        self.assertEqual(process_take(b""), (None, clips.TOO_QUIET))
        self.assertEqual(process_take(take(speech_s=0.1)), (None, clips.TOO_SHORT))
        self.assertEqual(process_take(take(speech_s=4.5)), (None, clips.TOO_LONG))
        self.assertIsNone(trim_and_normalise(take(speech_s=0.1)))
        self.assertIn("nome", clips.TOO_SHORT)

    def test_accepted_take_has_no_reason(self):
        out, reason = process_take(take())
        self.assertIsNone(reason)
        self.assertEqual(len(out) % 2, 0)


# ---------------------------------------------------------------- store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "local"
        self.root.mkdir()
        self.store = ClipStore(self.root / "names", local_root=self.root)
        self.when = datetime(2026, 1, 2, 10, 0, 0)


class StoreTest(StoreCase):
    def test_refuses_a_folder_outside_local(self):
        for folder in (Path(self.temp.name) / "names", self.root, self.root / ".." / "names"):
            with self.assertRaises(ClipsError):
                ClipStore(folder, local_root=self.root)
        with self.assertRaises(ClipsError):
            ClipStore(Path(self.temp.name) / "names")  # the default root is the repository's local/

    def test_save_writes_a_mono_pcm16_wav_and_the_manifest(self):
        data = trim_and_normalise(take())
        duration = self.store.save_clip("Nimbus-Deck", data, self.when)
        manifest = self.store.load()
        entry = manifest.clips["nimbus deck"]
        self.assertEqual(entry["file"], clip_file_name("nimbus deck"))
        self.assertEqual(entry["display"], "Nimbus-Deck")
        self.assertEqual(entry["duration_s"], duration)
        self.assertEqual(entry["recorded_at"], "2026-01-02T10:00:00")
        with wave.open(str(self.store.clip_path(manifest, "nimbus deck")), "rb") as handle:
            self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate()), (1, 2, SAMPLE_RATE))
            self.assertEqual(handle.readframes(handle.getnframes()), data)
        self.assertEqual(sorted(path.name for path in self.store.folder.iterdir()),
                         sorted([entry["file"], clips.MANIFEST_NAME]))

    def test_unsafe_entries_are_ignored(self):
        self.store.save_clip("Nimbus-Deck", trim_and_normalise(take()), self.when)
        outside = Path(self.temp.name) / "outside.wav"
        outside.write_bytes(b"RIFF")
        (self.store.folder / ("clip-" + "0" * 24 + ".wav")).mkdir()
        data = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        for key, file in (("a", "../outside.wav"), ("b", str(outside)), ("c", "sub/" + clip_file_name("c")),
                          ("d", "clip-" + "0" * 24 + ".wav"), ("e", "notes.txt"), ("Bad Key", clip_file_name("x")),
                          ("f", clip_file_name("f"))):
            data["clips"][key] = {"file": file, "display": key}
        data["clips"]["g"] = "not a table"
        self.store.manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = self.store.load()
        self.assertEqual(sorted(manifest.clips), ["d", "f", "nimbus deck"])
        for key in ("a", "b", "c", "d", "e", "f", "g", "Bad Key"):
            self.assertIsNone(self.store.clip_path(manifest, key), key)  # d is a folder, f has no file
        self.assertIsNotNone(self.store.clip_path(manifest, "nimbus deck"))

    def test_corrupt_manifest_is_an_error(self):
        self.store.folder.mkdir()
        for text in ("{not json", "[]", '{"clips": []}', '{"clips": {}, "skipped": "x"}'):
            self.store.manifest_path.write_text(text, encoding="utf-8")
            with self.assertRaises(ClipsError):
                self.store.load()

    def test_skip_and_add_persist(self):
        self.store.skip("orbita")
        self.store.skip("orbita")
        self.store.add("Nova Base")
        self.store.add("nova-base")
        manifest = self.store.load()
        self.assertEqual((manifest.skipped, manifest.added), (["orbita"], ["Nova Base"]))
        self.store.save_clip("Órbita", trim_and_normalise(take()), self.when)
        self.assertEqual(self.store.load().skipped, [])


# ---------------------------------------------------------------- discovery


class FakeFolders:
    def __init__(self, entries=()):
        self.entries = tuple(entries)

    def shortcut_folders(self):
        return self.entries


class DiscoveryCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.work = Path(self.temp.name) / "work"
        self.work.mkdir()

    def folder(self, *parts: str, git: bool = False) -> str:
        path = self.work.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        if git:
            (path / ".git").mkdir()
        return str(path)

    def sources(self, claude=(), entries=(), configured=(), vocabulary=(), drives=lambda drive: DRIVE_FIXED,
                temp_dirs=()):
        return names.Sources(claude_paths=lambda: list(claude), folders=FakeFolders(entries), configured=configured,
                             vocabulary_names=lambda: list(vocabulary), drives=drives, temp_dirs=temp_dirs)


class TakeReasonTest(unittest.TestCase):
    def test_every_take_problem_reads_in_portuguese(self):
        silent = CaptureResult(bytes(2 * SAMPLE_RATE * 2), 2.0)
        behind = CaptureResult(take(), 5.0)
        ahead = CaptureResult(take(), 0.5)
        quiet = CaptureResult(pcm(noise(1.5)), 1.5)
        short = CaptureResult(take(lead_s=0.1, tail_s=0.1), 0.8)
        for result, expected in ((short, 0), (silent, 1), (behind, 2), (ahead, 3), (quiet, 4)):
            self.assertEqual(names.take_reason(take_problem(result)), names.TAKE_REASONS[expected][1])
        self.assertEqual(names.take_reason("other"), "other")


class DiscoveryTest(DiscoveryCase):
    def test_claude_projects_are_named_as_an_alert_names_them(self):
        repo = self.folder("Nimbus-Deck", git=True)
        sub = self.folder("Nimbus-Deck", "src", "deep")
        plain = self.folder("Orbita_Base")
        found = names.discover(self.sources(claude=[sub, plain, repo.replace("\\", "/")]))
        self.assertEqual([(name.display, name.spoken, name.key) for name in found.names],
                         [("Nimbus-Deck", "Nimbus Deck", "nimbus deck"), ("Orbita_Base", "Orbita Base", "orbita base")])
        self.assertEqual(found.counts["claude_code"], 3)

    def test_missing_network_temp_and_removable_paths_are_dropped(self):
        kept = self.folder("Kept")
        temp = self.folder("scratch", "Temporaria")
        self.folder("Removivel")
        claude = [kept, str(self.work / "Gone"), "\\\\server\\share\\Rede", "relative\\Pasta", temp,
                  str(self.work / "Removivel"), 42]
        drive = os.path.splitdrive(kept)[0].upper()
        found = names.discover(self.sources(claude=claude, temp_dirs=[str(self.work / "SCRATCH")]))
        self.assertEqual([name.display for name in found.names], ["Kept", "Removivel"])
        found = names.discover(self.sources(claude=claude, drives=lambda d: 2 if d == drive else DRIVE_FIXED))
        self.assertEqual(found.names, [])

    def test_folders_vocabulary_and_added_collapse_by_key(self):
        repo = self.folder("x-public", git=True)
        shortcut = self.folder("Painel Antigo")
        found = names.discover(
            self.sources(claude=[repo], configured=[("X Public", Path(repo))], entries=[("Atalho Painel", shortcut)],
                         vocabulary=["x_public", "Vela", "vela"]),
            added=["VELA", "Nova  Base", "   "])
        self.assertEqual([name.display for name in found.names],
                         ["x-public", "Atalho Painel", "Painel Antigo", "Vela", "Nova Base"])
        self.assertEqual(found.counts, {"claude_code": 1, "folders": 4, "vocabulary": 3, "added": 3})

    def test_long_names_are_cut_like_spoken(self):
        long = "Projeto-" + "muito-" * 12
        found = names.discover(self.sources(vocabulary=[long]))
        self.assertEqual(found.names[0].spoken, spoken(long))
        self.assertLessEqual(len(found.names[0].spoken), MAX_NAME_CHARS)
        self.assertLess(len(found.names[0].spoken), len(long))
        self.assertEqual(found.names[0].key, clip_key(long))

    def test_a_failing_source_keeps_the_others(self):
        def broken():
            raise names.NamesError("claude_config_invalid")

        def vocabulary():
            raise ValueError("vocabulary: names[0] is empty")

        sources = self.sources(entries=[("Vela", self.folder("Vela"))])
        sources.claude_paths, sources.vocabulary_names = broken, vocabulary
        with self.assertLogs("quill.names", "INFO") as logs:
            found = names.discover(sources)
        self.assertEqual([name.display for name in found.names], ["Vela"])
        self.assertEqual(found.problems, ["claude_config_invalid", "vocabulary_unreadable:ValueError"])
        self.assertNotIn("Vela", "\n".join(logs.output))


class ClaudeConfigTest(DiscoveryCase):
    def test_only_the_project_keys_are_read(self):
        path = Path(self.temp.name) / ".claude.json"
        path.write_text(json.dumps({"oauthAccount": {"emailAddress": "someone@example.invalid"},
                                    "projects": {"D:/one/Alpha": {"history": ["private"]}, "E:\\two\\Beta": {}}}),
                        encoding="utf-8")
        self.assertEqual(names.claude_project_paths(path), ["D:/one/Alpha", "E:\\two\\Beta"])
        self.assertEqual(names.claude_project_paths(Path(self.temp.name) / "missing.json"), [])
        for text in ("{oops", "[1, 2]", '{"projects": []}'):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(names.NamesError):
                names.claude_project_paths(path)

    def test_the_real_config_is_never_read_in_tests(self):
        with self.assertRaises(AssertionError):
            names.claude_project_paths()
        with self.assertRaises(AssertionError):
            names.default_sources(load_config(None)).claude_paths()

    def test_config_path_rule(self):
        self.assertEqual(tests.real_claude_config_path({"CLAUDE_CONFIG_DIR": "D:\\cfg"}), Path("D:\\cfg") / ".claude.json")
        self.assertEqual(tests.real_claude_config_path({}), Path.home() / ".claude.json")


# ---------------------------------------------------------------- command


class ScriptedConsole:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.lines: list[str] = []

    def say(self, text=""):
        self.lines.append(text)

    def ask(self, prompt):
        self.lines.append(prompt)
        if not self.answers:
            raise AssertionError("the command asked more than the script answers")
        answer = self.answers.pop(0)
        return answer() if callable(answer) else answer

    @property
    def text(self):
        return "\n".join(self.lines)


class FakeCapture:
    def __init__(self, owner):
        self.owner = owner
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        data = self.owner.takes.pop(0)
        return CaptureResult(data, len(data) / 2 / SAMPLE_RATE)


class FakeCaptures:
    def __init__(self, *takes):
        self.takes = list(takes)
        self.made: list[FakeCapture] = []

    def __call__(self):
        capture = FakeCapture(self)
        self.made.append(capture)
        return capture


class FakeDevices:
    def __init__(self, devices=("Other input", MIC)):
        self.devices = list(devices)
        self.opened = 0

    def device_count(self):
        return len(self.devices)

    def device_name(self, index):
        return self.devices[index]

    def open(self, *args):
        self.opened += 1
        raise AssertionError("tests never open a device")


VOCABULARY = ["Nimbus-Deck", "Orbita", "Vela"]


class CommandCase(DiscoveryCase):
    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(load_config(None), microphone=MIC)
        self.clock = iter(datetime(2026, 1, 2, 10, 0, 0) + timedelta(minutes=minute) for minute in range(100))
        self.vocabulary = list(VOCABULARY)

    def run_main(self, argv, answers=(), takes=(), console=None, **kwargs):
        console = console or ScriptedConsole(answers)
        self.captures = FakeCaptures(*takes)
        kwargs.setdefault("capture_factory", self.captures)
        code = names.main(argv, config=self.config, sources=self.sources(vocabulary=self.vocabulary),
                          store=self.store, console=console, now=lambda: next(self.clock), **kwargs)
        return code, console

    def recorded(self):
        manifest = self.store.load()
        return sorted(key for key in manifest.clips if self.store.has_clip(manifest, key))


class DryRunTest(CommandCase):
    def test_counts_only_and_no_capture(self):
        self.store.save_clip("Orbita", trim_and_normalise(take()), datetime(2026, 1, 1))
        self.store.skip(clip_key("Vela"))
        code, console = self.run_main(["--dry-run"], capture_factory=None, api=FakeDevices())
        self.assertEqual(code, 0)
        self.assertIn("names found: 3, recorded: 1, missing: 1, skipped: 1", console.text)
        self.assertEqual(self.captures.made, [])
        for name in VOCABULARY:
            self.assertNotIn(name.casefold(), console.text.casefold())

    def test_corrupt_manifest_exits_2_with_an_english_error(self):
        self.store.folder.mkdir()
        self.store.manifest_path.write_text("{broken", encoding="utf-8")
        for argv in (["--dry-run"], []):
            code, console = self.run_main(argv)
            self.assertEqual(code, 2)
            self.assertEqual(console.lines, ["error: name clips manifest is not valid JSON"])
            self.assertEqual(self.captures.made, [])


class ListDevicesTest(CommandCase):
    def test_marks_the_configured_microphone_without_opening_it(self):
        api = FakeDevices()
        code, console = self.run_main(["--list-devices"], api=api)
        self.assertEqual(code, 0)
        self.assertIn(f"  1: {MIC}  <- configured", console.lines)
        self.assertIn("  0: Other input", console.lines)
        self.assertEqual((api.opened, self.captures.made), (0, []))


class RecordTest(CommandCase):
    def test_records_quits_and_resumes_at_the_first_name_without_a_clip(self):
        code, console = self.run_main([], answers=["", "", "q"], takes=[take()])
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded(), ["nimbus deck"])
        self.assertIn("[1/3] Nome: Nimbus-Deck", console.lines)
        self.assertIn("  Diz: Nimbus Deck", console.lines)
        self.assertTrue(self.captures.made[0].stopped)
        self.assertIn("names found: 3, recorded: 1, missing: 2, skipped: 0", console.text)
        code, console = self.run_main([], answers=["", "", "q"], takes=[take()])
        self.assertIn("[1/2] Nome: Orbita", console.lines)
        self.assertEqual(self.recorded(), ["nimbus deck", "orbita"])

    def test_skip_is_remembered_and_include_skipped_asks_again(self):
        code, _ = self.run_main([], answers=["s", "s", "s"])
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load().skipped, ["nimbus deck", "orbita", "vela"])
        self.assertEqual(self.captures.made, [])
        code, console = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("Todos os nomes já têm gravação; nada a gravar.", console.lines)
        self.assertIn("names found: 3, recorded: 0, missing: 0, skipped: 3", console.text)
        code, console = self.run_main(["--include-skipped"], answers=["q"])
        self.assertIn("[1/3] Nome: Nimbus-Deck", console.lines)

    def test_r_after_saved_replaces_the_clip_and_the_entry(self):
        first, second = take(amplitude=0.2, speech_s=0.5), take(amplitude=0.3, speech_s=0.9)
        code, console = self.run_main([], answers=["", "", "r", "", "", "q"], takes=[first, second])
        self.assertEqual(code, 0)
        manifest = self.store.load()
        entry = manifest.clips["nimbus deck"]
        self.assertAlmostEqual(entry["duration_s"], 0.9 + 2 * clips.PAD_S, delta=0.05)
        self.assertEqual(entry["recorded_at"], "2026-01-02T10:01:00")
        with wave.open(str(self.store.clip_path(manifest, "nimbus deck")), "rb") as handle:
            self.assertEqual(handle.readframes(handle.getnframes()), trim_and_normalise(second))
        self.assertEqual(len([path for path in self.store.folder.iterdir() if path.suffix == ".wav"]), 1)
        self.assertEqual(console.lines.count("[1/3] Nome: Nimbus-Deck"), 2)

    def test_rejected_take_is_asked_again(self):
        code, console = self.run_main([], answers=["", "", "", "", "", "", "", "", "q"],
                                      takes=[pcm(noise(1.5)), pcm(noise(0.2)), take(speech_s=0.1), take()])
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded(), ["nimbus deck"])
        rejected = [line for line in console.lines if line.startswith("  Take rejeitado")]
        self.assertEqual(rejected, [f"  Take rejeitado ({names.take_reason('level too low (0.0002)')}). "
                                    "Vamos repetir este nome.",
                                    f"  Take rejeitado ({names.TAKE_REASONS[0][1]}). Vamos repetir este nome.",
                                    f"  Take rejeitado ({clips.TOO_SHORT}). Vamos repetir este nome."])
        self.assertNotIn("too", " ".join(rejected))
        self.assertEqual(console.lines.count("[1/3] Nome: Nimbus-Deck"), 4)

    def test_redo_records_one_name_again(self):
        self.run_main([], answers=[""] * 9, takes=[take(), take(), take()])
        before = self.store.load().clips["orbita"]
        code, console = self.run_main(["--redo", "ORBITA"], answers=["", "", ""], takes=[take(speech_s=1.2)])
        self.assertEqual(code, 0)
        self.assertIn("[1/1] Nome: Orbita", console.lines)
        after = self.store.load().clips["orbita"]
        self.assertNotEqual(before["recorded_at"], after["recorded_at"])
        self.assertEqual(before["file"], after["file"])
        code, console = self.run_main(["--redo", "Desconhecido"])
        self.assertEqual(code, 2)
        self.assertEqual(console.lines, ["error: --redo: that name is not in the list (add it with --add)"])

    def test_add_keeps_the_name_and_records_it(self):
        code, console = self.run_main(["--add", "Nova_Base"], answers=["", "", ""], takes=[take()])
        self.assertEqual(code, 0)
        self.assertIn("[1/1] Nome: Nova_Base", console.lines)
        manifest = self.store.load()
        self.assertEqual(manifest.added, ["Nova_Base"])
        self.assertIn("nova base", self.recorded())
        code, console = self.run_main(["--dry-run"])
        self.assertIn("names found: 4, recorded: 1, missing: 3, skipped: 0", console.text)
        code, console = self.run_main(["--add", " - "])
        self.assertEqual(code, 2)

    def test_the_configured_microphone_is_required(self):
        self.config = dataclasses.replace(self.config, microphone="Absent Mic Name")
        api = FakeDevices()
        code, console = self.run_main([], capture_factory=None, api=api)
        self.assertEqual(code, 2)
        self.assertTrue(console.lines[0].startswith("error: configured microphone not found"))
        self.assertEqual(api.opened, 0)


if __name__ == "__main__":
    unittest.main()
