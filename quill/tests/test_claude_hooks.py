"""The Claude Code attention alert: the hook notifier, the installer, the listener and the sound demo.

Everything runs on fakes and temporary files: no named event is created, no
sound plays and the user's Claude Code settings are never read or written
(``quill.tests`` forbids the real events, the real player and the default
settings path). The hook inputs are invented.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from quill import claude_hooks as H
from quill import notify as N
from quill import sound
from quill.tests import real_user_settings_path
from quill.tests.fakes import FakeAlertEvents, FakePlayer

REPO_ROOT = Path(__file__).resolve().parents[2]
PRIVATE = "texto privado inventado da resposta"
STOP = {"session_id": "s1", "transcript_path": "C:\\Invented\\t.jsonl", "cwd": "C:\\Invented",
        "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": PRIVATE}
PERMISSION = {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "permission_prompt",
              "message": PRIVATE}
ATTENDED = {N.ATTENDED_VARIABLE: "1"}
HEADLESS = {N.ATTENDED_VARIABLE: "0"}


def encoded(event: object) -> bytes:
    return json.dumps(event).encode("utf-8")


def reader(data: bytes, chunk: int = 7):
    """A stdin ``read(n)`` over ``data``, a few bytes at a time."""
    stream = io.BytesIO(data)

    def read(size: int) -> bytes:
        return stream.read(min(size, chunk))
    return read


def wait_for(condition, what: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


# ---------------------------------------------------------------- hook side


class ReadInputTest(unittest.TestCase):
    def test_reads_everything_until_end_of_file(self) -> None:
        self.assertEqual(N.read_input(reader(b"x" * 1000), limit=1000), b"x" * 1000)
        self.assertEqual(N.read_input(reader(b"")), b"")

    def test_oversized_input_is_dropped(self) -> None:
        self.assertIsNone(N.read_input(reader(b"x" * 1001, chunk=4096), limit=1000))

    def test_unreadable_input_is_dropped(self) -> None:
        def broken(size: int) -> bytes:
            raise OSError("fake: closed")
        self.assertIsNone(N.read_input(broken))

    def test_a_stdin_that_never_closes_cannot_hold_the_hook(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def endless(size: int) -> bytes:
            if release.wait(10):
                return b""
            return b""
        started = time.monotonic()
        self.assertIsNone(N.read_input(endless, timeout_s=0.1))
        self.assertLess(time.monotonic() - started, 2.0)


class ParseEventTest(unittest.TestCase):
    def test_final_reply_rings_done(self) -> None:
        self.assertEqual(N.parse_event(encoded(STOP), "stop"), sound.DONE)
        without_flag = {key: value for key, value in STOP.items() if key != "stop_hook_active"}
        self.assertEqual(N.parse_event(encoded(without_flag), "stop"), sound.DONE)

    def test_a_stop_continued_by_a_hook_does_not_ring(self) -> None:
        for value in (True, 1, "true"):
            with self.subTest(value=value):
                self.assertIsNone(N.parse_event(encoded({**STOP, "stop_hook_active": value}), "stop"))

    def test_only_permission_prompts_ring_permission(self) -> None:
        self.assertEqual(N.parse_event(encoded(PERMISSION), "permission"), sound.PERMISSION)
        for kind in ("idle_prompt", "auth_success", "elicitation_dialog", "", None):
            with self.subTest(kind=kind):
                self.assertIsNone(N.parse_event(encoded({**PERMISSION, "notification_type": kind}), "permission"))
        without_type = {key: value for key, value in PERMISSION.items() if key != "notification_type"}
        self.assertIsNone(N.parse_event(encoded(without_type), "permission"))

    def test_the_event_must_match_the_hook(self) -> None:
        self.assertIsNone(N.parse_event(encoded(STOP), "permission"))
        self.assertIsNone(N.parse_event(encoded(PERMISSION), "stop"))
        for name in ("SubagentStop", "PreToolUse", "PostToolUse", "UserPromptSubmit"):
            with self.subTest(name=name):
                self.assertIsNone(N.parse_event(encoded({**STOP, "hook_event_name": name}), "stop"))

    def test_malformed_input_is_ignored(self) -> None:
        for data in (None, b"", b"{", b"not json", b"\xff\xfe\x00", b"[]", b'"Stop"', b"null", b"3",
                     b"[" * 100_000, encoded({"hook_event_name": ["Stop"]})):
            with self.subTest(data=data[:20] if data else data):
                self.assertIsNone(N.parse_event(data, "stop"))

    def test_unknown_hooks_are_ignored(self) -> None:
        self.assertIsNone(N.parse_event(encoded(STOP), "Stop"))
        self.assertIsNone(N.parse_event(encoded(STOP), ""))


class FilterTest(unittest.TestCase):
    def test_attended_rings_only_sessions_marked_attended(self) -> None:
        self.assertTrue(N.session_allowed(ATTENDED, "attended"))
        for environ in (HEADLESS, {}, {N.ATTENDED_VARIABLE: ""}, {N.ATTENDED_VARIABLE: "true"}):
            with self.subTest(environ=environ):
                self.assertFalse(N.session_allowed(environ, "attended"))
        self.assertEqual(N.DEFAULT_FILTER, "attended")

    def test_unless_headless_rings_everything_not_marked_unattended(self) -> None:
        self.assertTrue(N.session_allowed(ATTENDED, "unless-headless"))
        self.assertTrue(N.session_allowed({}, "unless-headless"))
        self.assertFalse(N.session_allowed(HEADLESS, "unless-headless"))

    def test_all_rings_every_session(self) -> None:
        for environ in (ATTENDED, HEADLESS, {}):
            self.assertTrue(N.session_allowed(environ, "all"))

    def test_inherited_entrypoint_variables_do_not_count(self) -> None:
        # A claude -p child of the VS Code panel inherits these; only the attended flag decides.
        environ = {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode", "CLAUDE_AGENT_SDK_VERSION": "0", **HEADLESS}
        self.assertFalse(N.session_allowed(environ, "attended"))

    def test_settings_come_from_the_config_and_default_on_failure(self) -> None:
        loaded = mock.Mock()
        loaded.claude_alert.enabled, loaded.claude_alert.filter = False, "all"
        self.assertEqual(N.alert_settings(lambda: loaded), (False, "all"))

        def broken():
            raise OSError("fake: unreadable")
        self.assertEqual(N.alert_settings(broken), (True, "attended"))


class NotifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = FakeAlertEvents()
        for name in N.EVENT_NAMES.values():
            self.events.create_event(name)

    def run_hook(self, hook: str, event: object, environ=ATTENDED, settings=(True, "attended")) -> str:
        return N.notify(hook, reader(event if isinstance(event, bytes) else encoded(event)), environ,
                        events=self.events, settings=lambda: settings)

    def test_signals_the_named_event_of_its_kind(self) -> None:
        self.assertEqual(self.run_hook("stop", STOP), "signalled")
        self.assertEqual(self.events.pending, {N.EVENT_NAMES[sound.DONE]})
        self.assertEqual(self.run_hook("permission", PERMISSION), "signalled")
        self.assertEqual(self.events.pending, set(N.EVENT_NAMES.values()))

    def test_nothing_is_signalled_when_it_must_not_ring(self) -> None:
        cases = (
            ("stop", {**STOP, "stop_hook_active": True}, ATTENDED, (True, "attended"), "ignored"),
            ("permission", {**PERMISSION, "notification_type": "idle_prompt"}, ATTENDED, (True, "attended"),
             "ignored"),
            ("stop", b"{broken", ATTENDED, (True, "attended"), "ignored"),
            ("stop", b"x" * (N.MAX_INPUT + 1), ATTENDED, (True, "attended"), "ignored"),
            ("stop", STOP, HEADLESS, (True, "attended"), "filtered"),
            ("stop", STOP, {}, (True, "attended"), "filtered"),
            ("stop", STOP, ATTENDED, (False, "attended"), "disabled"),
            ("other", STOP, ATTENDED, (True, "attended"), "unknown_hook"),
        )
        for hook, event, environ, settings, reason in cases:
            with self.subTest(reason=reason, hook=hook):
                self.assertEqual(self.run_hook(hook, event, environ, settings), reason)
        self.assertEqual(self.events.pending, set())

    def test_quill_not_running_is_quiet(self) -> None:
        self.assertEqual(N.notify("stop", reader(encoded(STOP)), ATTENDED, events=FakeAlertEvents(),
                                  settings=lambda: (True, "attended")), "not_running")

    def test_the_message_and_reply_are_never_logged(self) -> None:
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            self.run_hook("stop", STOP)
            self.run_hook("permission", PERMISSION)
            self.run_hook("stop", b'{"message": "' + PRIVATE.encode() + b'"')
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertNotIn("privado", capture.getvalue())

    def test_main_prints_nothing_and_always_exits_zero(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.object(N, "notify", side_effect=RuntimeError("fake")) as called:
                self.assertEqual(N.main(["stop"]), 0)
            with mock.patch.object(N, "notify", side_effect=KeyboardInterrupt) as interrupted:
                self.assertEqual(N.main(["permission"]), 0)
            with mock.patch.object(N, "notify") as no_arguments:
                self.assertEqual(N.main([]), 0)
                self.assertEqual(N.main(["stop", "extra"]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(called.call_args.args[0], "stop")
        self.assertEqual(interrupted.call_args.args[0], "permission")
        self.assertEqual([call.args[0] for call in no_arguments.call_args_list], ["", ""])


class HookScriptTest(unittest.TestCase):
    """The script as Claude Code runs it, from another folder. Only inputs that are ignored
    before any event is touched are sent, so nothing can reach a running Quill."""

    def run_script(self, argument: str, data: bytes) -> subprocess.CompletedProcess:
        environ = {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}
        environ[N.ATTENDED_VARIABLE] = "0"
        with tempfile.TemporaryDirectory() as folder:
            started = time.monotonic()
            result = subprocess.run([sys.executable, str(REPO_ROOT / "quill" / "notify.py"), argument], input=data,
                                    capture_output=True, cwd=folder, env=environ, timeout=30)
            result.elapsed = time.monotonic() - started
        return result

    def test_script_runs_from_any_folder_silently_and_exits_zero(self) -> None:
        for argument, data in (("stop", b"{not json"), ("stop", encoded({**STOP, "stop_hook_active": True})),
                               ("permission", encoded({**PERMISSION, "notification_type": "idle_prompt"})),
                               ("unknown", encoded(STOP))):
            with self.subTest(argument=argument, data=data[:30]):
                result = self.run_script(argument, data)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
                self.assertLess(result.elapsed, 10.0)  # generous: interpreter start-up on a busy machine


# ---------------------------------------------------------------- app side


class ListenerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = FakeAlertEvents()
        self.alerts: list[str] = []
        self.listener = N.AlertListener(self.alerts.append, self.events, poll_s=0.02)
        self.addCleanup(self.listener.stop)

    def ring(self, kind: str) -> None:
        self.assertTrue(self.events.set_event(N.EVENT_NAMES[kind]))

    def test_each_event_becomes_one_alert_of_its_kind(self) -> None:
        self.listener.start()
        self.assertEqual(sorted(self.events.open), sorted(N.EVENT_NAMES.values()))
        self.ring(sound.DONE)
        wait_for(lambda: self.alerts == [sound.DONE], "the done alert")
        self.ring(sound.PERMISSION)
        wait_for(lambda: self.alerts == [sound.DONE, sound.PERMISSION], "the permission alert")

    def test_start_stop_start(self) -> None:
        self.listener.start()
        with self.assertRaises(RuntimeError):
            self.listener.start()
        self.listener.stop()
        self.assertFalse(self.listener.running)
        self.assertEqual(self.events.open, [])
        self.assertFalse(self.events.set_event(N.EVENT_NAMES[sound.DONE]))  # Quill stopped: nobody listens
        self.listener.stop()  # twice is harmless
        self.listener.start()
        self.ring(sound.DONE)
        wait_for(lambda: self.alerts == [sound.DONE], "the alert after a restart")

    def test_a_failed_start_leaves_nothing_open(self) -> None:
        created = []
        original = self.events.create_event

        def second_fails(name: str) -> int:
            if created:
                raise OSError("fake: CreateEventW failed")
            created.append(name)
            return original(name)
        self.events.create_event = second_fails
        with self.assertRaises(OSError):
            self.listener.start()
        self.assertFalse(self.listener.running)
        self.assertEqual(self.events.open, [])

    def test_a_failing_alert_does_not_stop_the_listener(self) -> None:
        calls = []

        def on_alert(kind: str) -> None:
            calls.append(kind)
            if len(calls) == 1:
                raise ValueError("fake")
        listener = N.AlertListener(on_alert, self.events, poll_s=0.02)
        self.addCleanup(listener.stop)
        with self.assertLogs("quill.notify", level="ERROR"):
            listener.start()
            self.ring(sound.DONE)
            wait_for(lambda: len(calls) == 1, "the failing alert")
        self.ring(sound.DONE)
        wait_for(lambda: len(calls) == 2, "the next alert")


# ---------------------------------------------------------------- sound


class SoundTest(unittest.TestCase):
    def test_the_real_player_is_forbidden_in_tests(self) -> None:
        with self.assertRaises(AssertionError):
            sound.WinsoundPlayer()

    def test_demo_refuses_without_the_flag(self) -> None:
        player, err = FakePlayer(), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(sound.main([], player=player), 2)
        self.assertEqual(player.plays, [])
        self.assertIn("--play-sound", err.getvalue())

    def test_demo_plays_each_kind_once_with_the_flag(self) -> None:
        player, sleeps = FakePlayer(), []
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sound.main(["--play-sound"], player=player, sleep=sleeps.append), 0)
        self.assertEqual(player.plays, [sound.DONE, sound.PERMISSION])
        self.assertEqual(player.stops, 1)
        self.assertEqual(len(sleeps), 2)


# ---------------------------------------------------------------- installer


class InstallerCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.repo = self.make_repo("repo")
        self.pythonw = str(self.repo / ".venv" / "Scripts" / "pythonw.exe")
        self.script = str(self.repo / "quill" / "notify.py")
        self.path = self.folder / "claude" / "settings.json"
        self.path.parent.mkdir()
        self.lines: list[str] = []
        self.answers: list[str] = []
        self.asked: list[str] = []

    def make_repo(self, name: str) -> Path:
        root = (self.folder / name).resolve()
        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "pythonw.exe").write_bytes(b"")
        (root / "quill").mkdir()
        (root / "quill" / "notify.py").write_bytes(b"")
        return root

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def run_action(self, action: str, *answers: str, repo: Path | None = None) -> int:
        self.answers = list(answers)
        return H.run(action, self.path, repo_root=repo or self.repo, out=self.lines.append, ask=self.ask,
                     clock=lambda: 1_790_000_000.0)

    def write(self, settings: object) -> bytes:
        raw = (json.dumps(settings, indent=4) + "\n").encode("utf-8")
        self.path.write_bytes(raw)
        return raw

    def read(self) -> dict:
        return json.loads(self.path.read_text("utf-8"))

    def backups(self) -> list[Path]:
        return sorted(self.path.parent.glob("settings.json.bak-quill-*"))

    def leftovers(self) -> list[Path]:
        return sorted(self.path.parent.glob(".settings-quill-*"))

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


OTHER_SETTINGS = {
    "model": "invented-model",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "C:\\Invented\\other.exe", "args": ["done"]}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo invented"}]}],
    },
}


class InstallTest(InstallerCase):
    def quill_hooks(self, settings: dict) -> dict[str, list]:
        found = {}
        for event, groups in settings.get("hooks", {}).items():
            for group in groups:
                for hook in group["hooks"]:
                    if H.is_quill_hook(hook):
                        found.setdefault(event, []).append((group.get("matcher"), hook))
        return found

    def test_show_prints_the_exact_change_and_writes_nothing(self) -> None:
        raw = self.write(OTHER_SETTINGS)
        self.assertEqual(self.run_action("show"), H.EXIT_OK)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual((self.asked, self.backups(), self.leftovers()), ([], [], []))
        self.assertIn("Nothing was written (--show)", self.output)
        added = [line for line in self.output.splitlines() if line.startswith("+") and not line.startswith("+++")]
        self.assertTrue(any("notify.py" in line for line in added))
        self.assertTrue(any('"permission_prompt"' in line for line in added))
        self.assertIn(json.dumps(self.script)[1:-1], self.output)
        self.assertIn('"permission_prompt"', self.output)
        # The diff, applied, is exactly what --install writes.
        self.assertEqual(self.run_action("install", "yes"), H.EXIT_OK)
        self.assertEqual(self.path.read_text("utf-8"), H.render(H.with_quill(OTHER_SETTINGS, self.pythonw,
                                                                              self.script)))

    def test_show_on_a_missing_file_writes_nothing(self) -> None:
        self.assertEqual(self.run_action("show"), H.EXIT_OK)
        self.assertFalse(self.path.exists())
        self.assertIn("does not exist", self.output)

    def test_install_writes_exec_form_hooks_and_keeps_everything_else(self) -> None:
        raw = self.write(OTHER_SETTINGS)
        self.assertEqual(self.run_action("install", "yes"), H.EXIT_OK)
        self.assertEqual(len(self.asked), 1)
        settings = self.read()
        self.assertEqual(settings["model"], "invented-model")
        self.assertEqual(settings["permissions"], OTHER_SETTINGS["permissions"])
        self.assertEqual(settings["hooks"]["PreToolUse"], OTHER_SETTINGS["hooks"]["PreToolUse"])
        self.assertEqual(settings["hooks"]["Stop"][0], OTHER_SETTINGS["hooks"]["Stop"][0])  # kept first
        found = self.quill_hooks(settings)
        self.assertEqual(sorted(found), ["Notification", "Stop"])
        for event, argument, matcher in (("Stop", "stop", None), ("Notification", "permission", "permission_prompt")):
            with self.subTest(event=event):
                self.assertEqual(found[event], [(matcher, {"type": "command", "command": self.pythonw,
                                                           "args": [self.script, argument],
                                                           "timeout": H.HOOK_TIMEOUT_S})])
                hook = found[event][0][1]
                self.assertTrue(Path(hook["command"]).is_absolute() and Path(hook["args"][0]).is_absolute())
                self.assertTrue(hook["command"].endswith("pythonw.exe"))
                self.assertLessEqual(hook["timeout"], 10)
        # A backup of the previous file, no temporary file left behind.
        self.assertEqual([path.read_bytes() for path in self.backups()], [raw])
        self.assertEqual(self.leftovers(), [])
        self.assertIn("Written. Previous file saved as", self.output)

    def test_install_is_idempotent(self) -> None:
        self.write(OTHER_SETTINGS)
        self.run_action("install", "yes")
        written = self.path.read_bytes()
        self.lines.clear()
        self.asked.clear()
        self.assertEqual(self.run_action("install", "yes"), H.EXIT_OK)
        self.assertEqual(self.path.read_bytes(), written)
        self.assertEqual(self.asked, [])
        self.assertEqual(len(self.backups()), 1)
        self.assertIn("already installed", self.output)

    def test_install_into_a_new_file(self) -> None:
        self.assertEqual(self.run_action("install", "yes"), H.EXIT_OK)
        self.assertEqual(self.read(), H.with_quill({}, self.pythonw, self.script))
        self.assertEqual(self.backups(), [])
        self.assertIn("new settings file", self.output)

    def test_install_needs_the_confirmation_word(self) -> None:
        raw = self.write(OTHER_SETTINGS)
        for answers in (("no",), ("",), ("y",), ()):
            with self.subTest(answers=answers):
                self.assertEqual(self.run_action("install", *answers), H.EXIT_FAILED)
                self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.backups(), [])
        self.assertIn("Not confirmed; nothing was written.", self.output)
        self.assertEqual(self.run_action("install", " YES "), H.EXIT_OK)

    def test_install_after_moving_the_repository_replaces_the_old_entries(self) -> None:
        self.write(OTHER_SETTINGS)
        self.run_action("install", "yes")
        moved = self.make_repo("moved")
        self.assertEqual(self.run_action("install", "yes", repo=moved), H.EXIT_OK)
        found = self.quill_hooks(self.read())
        self.assertEqual({event: [hook["args"][0] for _, hook in hooks] for event, hooks in found.items()},
                         {"Stop": [str(moved / "quill" / "notify.py")],
                          "Notification": [str(moved / "quill" / "notify.py")]})

    def test_a_quill_hook_edited_by_hand_is_repaired(self) -> None:
        self.run_action("install", "yes")
        settings = self.read()
        settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 600
        settings["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "C:\\Invented\\x.exe"})
        self.write(settings)
        self.assertEqual(self.run_action("install", "yes"), H.EXIT_OK)
        repaired = self.read()
        self.assertEqual(repaired["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": "C:\\Invented\\x.exe"}]},
                                                     H.quill_groups(self.pythonw, self.script)["Stop"]])

    def test_a_missing_venv_is_an_error_and_writes_nothing(self) -> None:
        raw = self.write(OTHER_SETTINGS)
        (self.repo / ".venv" / "Scripts" / "pythonw.exe").unlink()
        self.assertEqual(self.run_action("install", "yes"), H.EXIT_FAILED)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertIn("pythonw.exe not found", self.output)

    def test_a_file_changed_before_the_confirmation_is_not_overwritten(self) -> None:
        self.write(OTHER_SETTINGS)
        changed = {**OTHER_SETTINGS, "model": "changed-meanwhile"}

        def ask(prompt: str) -> str:
            self.write(changed)
            return "yes"
        code = H.run("install", self.path, repo_root=self.repo, out=self.lines.append, ask=ask)
        self.assertEqual(code, H.EXIT_FAILED)
        self.assertEqual(self.read(), changed)
        self.assertIn("changed meanwhile", self.output)
        self.assertEqual(self.backups(), [])


class RemoveTest(InstallerCase):
    def test_remove_deletes_only_quill_entries(self) -> None:
        self.write(OTHER_SETTINGS)
        self.run_action("install", "yes")
        self.assertEqual(self.run_action("remove", "yes"), H.EXIT_OK)
        self.assertEqual(self.read(), OTHER_SETTINGS)
        self.assertEqual(len(self.backups()), 2)

    def test_remove_drops_what_only_quill_used(self) -> None:
        self.write({"model": "invented-model"})
        self.run_action("install", "yes")
        self.run_action("remove", "yes")
        self.assertEqual(self.read(), {"model": "invented-model"})

    def test_remove_finds_entries_of_any_folder_and_path_style(self) -> None:
        settings = {"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "C:/Old/.venv/Scripts/pythonw.exe",
                                 "args": ["C:/Old/Quill/Notify.py", "stop"]},
                                {"type": "command", "command": "C:\\Invented\\keep.exe",
                                 "args": ["C:\\Invented\\quill\\notify.py", "other"]}]}],
            "Notification": [{"matcher": "permission_prompt", "hooks": [
                {"type": "command", "command": "D:\\Moved\\.venv\\Scripts\\pythonw.exe",
                 "args": ["D:\\Moved\\quill\\notify.py", "permission"]}]}],
        }}
        self.write(settings)
        self.assertEqual(self.run_action("remove", "yes"), H.EXIT_OK)
        self.assertEqual(self.read(), {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "C:\\Invented\\keep.exe", "args": ["C:\\Invented\\quill\\notify.py",
                                                                             "other"]}]}]}})

    def test_remove_without_quill_entries_changes_nothing(self) -> None:
        raw = self.write(OTHER_SETTINGS)
        self.assertEqual(self.run_action("remove", "yes"), H.EXIT_OK)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.asked, [])
        self.assertIn("nothing to remove", self.output)
        self.path.unlink()
        self.assertEqual(self.run_action("remove", "yes"), H.EXIT_OK)
        self.assertFalse(self.path.exists())

    def test_remove_needs_the_confirmation_word(self) -> None:
        self.run_action("install", "yes")
        installed = self.path.read_bytes()
        self.assertEqual(self.run_action("remove", "no"), H.EXIT_FAILED)
        self.assertEqual(self.path.read_bytes(), installed)


class BadSettingsTest(InstallerCase):
    def check_untouched(self, raw: bytes, fragment: str) -> None:
        for action in ("show", "install", "remove"):
            with self.subTest(action=action):
                self.lines.clear()
                self.assertEqual(self.run_action(action, "yes"), H.EXIT_FAILED)
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertIn("Claude Code settings unchanged:", self.output)
                self.assertIn(fragment, self.output)
                self.assertTrue(self.output.isascii())
        self.assertEqual((self.backups(), self.leftovers()), ([], []))

    def test_invalid_json_is_left_untouched(self) -> None:
        raw = b'{"model": "invented", "hooks": {'
        self.path.write_bytes(raw)
        self.check_untouched(raw, "not valid JSON")

    def test_non_utf8_is_left_untouched(self) -> None:
        raw = b'{"model": "\xff"}'
        self.path.write_bytes(raw)
        self.check_untouched(raw, "not valid JSON")

    def test_a_json_value_that_is_not_an_object_is_left_untouched(self) -> None:
        raw = b"[1, 2]"
        self.path.write_bytes(raw)
        self.check_untouched(raw, "does not hold a JSON object")

    def test_unexpected_hook_layouts_are_left_untouched(self) -> None:
        for hooks, fragment in (([], "'hooks' is not an object"), ({"Stop": {}}, "'hooks.Stop' is not a list"),
                                ({"Stop": [{"command": "x"}]}, "'hooks.Stop[0]' has no 'hooks' list"),
                                ({"Stop": ["x"]}, "'hooks.Stop[0]' has no 'hooks' list")):
            with self.subTest(hooks=hooks):
                raw = json.dumps({"hooks": hooks}).encode("utf-8")
                self.path.write_bytes(raw)
                self.check_untouched(raw, fragment)

    def test_an_unreadable_settings_file_is_left_untouched(self) -> None:
        self.path.write_bytes(b"{}")
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("fake: locked")):
            with mock.patch.object(H.time, "sleep"):
                self.assertEqual(self.run_action("install", "yes"), H.EXIT_FAILED)
        self.assertEqual(self.path.read_bytes(), b"{}")
        self.assertIn("cannot be read (PermissionError)", self.output)

    def test_a_bom_is_accepted(self) -> None:
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(OTHER_SETTINGS).encode("utf-8"))
        self.assertEqual(self.run_action("install", "yes"), H.EXIT_OK)
        self.assertEqual(self.read()["model"], "invented-model")


class PathTest(unittest.TestCase):
    def test_user_settings_path(self) -> None:
        self.assertEqual(real_user_settings_path({"CLAUDE_CONFIG_DIR": "C:\\Invented\\cfg"}),
                         Path("C:\\Invented\\cfg") / "settings.json")
        self.assertEqual(real_user_settings_path({}), Path.home() / ".claude" / "settings.json")

    def test_the_default_settings_path_is_forbidden_in_tests(self) -> None:
        with self.assertRaises(AssertionError):
            H.main(["--show"], out=lambda line: None, ask=lambda prompt: "")

    def test_main_uses_the_given_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            lines = []
            with mock.patch.object(H, "run", return_value=0) as run:
                self.assertEqual(H.main(["--remove", "--settings", str(path)], out=lines.append), 0)
            self.assertEqual(run.call_args.args, ("remove", path))

    def test_actions_are_exclusive_and_required(self) -> None:
        for argv in ([], ["--show", "--install"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    H.main(argv)

    def test_is_quill_hook(self) -> None:
        good = {"type": "command", "command": "C:\\R\\.venv\\Scripts\\pythonw.exe",
                "args": ["C:\\R\\quill\\notify.py", "stop"]}
        self.assertTrue(H.is_quill_hook(good))
        for hook in ({**good, "type": "prompt"}, {**good, "args": ["C:\\R\\quill\\notify.py"]},
                     {**good, "args": ["C:\\R\\other\\notify.py", "stop"]},
                     {**good, "args": ["C:\\R\\quill\\notify.py", "rm"]}, {**good, "args": "x"},
                     {"type": "command", "command": "C:\\R\\quill\\notify.py stop"}, "x", None):
            with self.subTest(hook=hook):
                self.assertFalse(H.is_quill_hook(hook))


if __name__ == "__main__":
    unittest.main()
