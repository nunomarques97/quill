"""End-to-end tests of the Quill app with fakes.

The real hook service, trigger machine, click-to-focus, streaming
transcriber, text pipeline, session manager and injector run; the hooks,
Win32 layer, microphone, model, indicator, kernel objects and registry are
fakes. Nothing sends real input, opens a window or the microphone, or reads
the Sponsor's personal files: the config's personal paths point to a
temporary folder. The spoken words are invented bursts (``w1``, ``w2``...).
"""

import dataclasses
import io
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quill import app as A
from quill import notify as N
from quill import autorewrite as R
from quill import inject
from quill import session as S
from quill import sound
from quill import speech as SP
from quill import startup
from quill import vocabulary as V
from quill.config import EXAMPLE_CONFIG, ClaudeAlert, ClaudeCode, VoiceSettings, load_config
from quill.corrections import CorrectionStore
from quill.edits import UNDO_EDITED, UNDO_ENTERED, UNDO_EXPIRED, UNDO_MESSAGES, UNDO_NOTHING, UNDO_OTHER_WINDOW, \
    UNDO_UNSURE
from quill.indicator.render import (CLAUDE_DONE, CLAUDE_PERMISSION, ERROR, LISTENING, LOADING, REVIEWING, SENT, VOICE,
                                    TRANSCRIBING, VOICE_NONE, VOICE_OPEN)
from quill.ollama import ChatReply, OllamaError
from quill.tests.fakes import (
    OTHER_HWND,
    TARGET,
    FakeAlertEvents,
    FakeCaptures,
    FakeClock,
    FakeHooks,
    FakeIndicator,
    FakeKernel,
    FakePlayer,
    FakeRegistry,
    FakeSpeechEngine,
    FakeWin32,
)
from quill.tests.test_edits import FakeLayout
from quill.tests.test_shortcuts import CODE as SHORTCUT_CODE
from quill.tests.test_shortcuts import FakeLauncher, write_link
from quill.tests.test_streaming import FakeModel, speech
from quill.tests.test_uia import CLAUDE_CHAIN, CLAUDE_INPUT, FakeReader, Node, webview, workbench
from quill.uia import EDIT, Focus, FocusProbe
from quill.tests.test_voice import word_tokens
from quill.shortcuts import list_shortcuts
from quill.voice import VOICE_PROMPT, voice_hints
from quill.whisper import Transcript, Word
from quill.win32 import (
    INTEGRITY_MEDIUM,
    LLKHF_INJECTED,
    VK_CONTROL,
    VK_MBUTTON,
    VK_RCONTROL,
    VK_RETURN,
    VK_SHIFT,
    VK_XBUTTON1,
    VK_XBUTTON2,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_MBUTTONDOWN,
    WM_MBUTTONUP,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    XBUTTON1,
    XBUTTON2,
)

MIDDLE = "middle"  # the middle button, for ``AppCase.button``
CLAUDE_HWND = 500
CLAUDE_PID = 50
F13 = 0x7C
F16 = 0x7F
KEY_C = 0x43
VK_F9 = 0x78
WAIT_S = 10.0


def sent_segments(api):
    """The text typed before each plain Enter, then the text after the last one (Shift+Enter is a newline)."""
    segments, units, shift = [], [], False
    for event in api.events:
        if event.is_unicode:
            if not event.is_keyup:
                units.append(event.scan)
        elif event.vk == VK_SHIFT:
            shift = not event.is_keyup
        elif event.vk == VK_RETURN and not event.is_keyup:
            if shift:
                units.append(0x0A)
            else:
                segments.append(b"".join(unit.to_bytes(2, "little") for unit in units).decode("utf-16-le"))
                units = []
    segments.append(b"".join(unit.to_bytes(2, "little") for unit in units).decode("utf-16-le"))
    return segments


def wait_for(condition, what):
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


class GatedModel(FakeModel):
    """A fake model whose load waits for ``loaded`` (set by default)."""

    def __init__(self):
        super().__init__()
        self.loaded = threading.Event()
        self.loaded.set()

    def load(self):
        self.loaded.wait(WAIT_S)
        super().load()


class FakeOllama:
    def __init__(self, down=True):
        self.down = down
        self.calls = 0

    def installed(self, timeout_s=None):
        if self.down:
            raise OllamaError("Ollama unreachable: URLError")
        return ["qwen3:8b"]

    def chat(self, model, system, user, max_tokens=None):
        self.calls += 1
        raise OllamaError("Ollama unreachable: URLError")


class AppCase(unittest.TestCase):
    min_hold_ms = 50

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.config = self.make_config()
        self.api = FakeWin32()
        self.api.windows[CLAUDE_HWND] = CLAUDE_PID
        self.api.integrity[CLAUDE_PID] = INTEGRITY_MEDIUM
        self.api.images = {TARGET.pid: "C:\\Invented\\notepad.exe", CLAUDE_PID: "C:\\Invented\\WindowsTerminal.exe"}
        self.api.titles = {CLAUDE_HWND: "Claude Code"}
        self.model = GatedModel()
        self.captures = FakeCaptures()
        self.indicator = FakeIndicator()
        self.kernel = FakeKernel()
        self.hook_fakes = []
        self.app = self.make_app()

    def make_config(self, **changes):
        config = load_config(None, EXAMPLE_CONFIG)
        values = dict(min_hold_ms=self.min_hold_ms, vocabulary_path=self.folder / "vocabulary.toml",
                      corrections_path=self.folder / "corrections.json", style_dir=self.folder / "style")
        values.update(changes)
        return dataclasses.replace(config, **values)

    def new_hooks(self):
        hooks = FakeHooks()
        self.hook_fakes.append(hooks)
        return hooks

    @property
    def hooks(self):
        return self.hook_fakes[-1]

    def make_app(self, config=None, client=None, kernel=None, **parts):
        focus_clock, inject_clock = FakeClock(), FakeClock()
        parts = A.Parts(api=self.api, model=self.model, capture_factory=self.captures, indicator=self.indicator,
                        instance=A.InstanceLock(kernel or self.kernel), hooks_factory=self.new_hooks,
                        client=client, focus_options={"sleep": focus_clock.sleep, "clock": focus_clock},
                        inject_options={"sleep": inject_clock.sleep, "clock": inject_clock},
                        **{"run_hints": lambda job: job(), **parts})  # mouse 5 hints at once: deterministic
        quill = A.QuillApp(config or self.config, parts)
        self.addCleanup(quill.stop)
        return quill

    def start(self, quill=None):
        quill = quill or self.app
        quill.start()
        wait_for(lambda: quill.sessions.ready, "the model to load")
        return quill

    # Trigger helpers: the fake hook callbacks return 1 when the event is swallowed.
    def button(self, down, which=XBUTTON1):
        vk = VK_MBUTTON if which == MIDDLE else VK_XBUTTON1 if which == XBUTTON1 else VK_XBUTTON2
        if down:
            self.api.keys_down.add(vk)
        else:
            self.api.keys_down.discard(vk)
        if which == MIDDLE:
            return self.hooks.mouse(WM_MBUTTONDOWN if down else WM_MBUTTONUP)
        return self.hooks.mouse(WM_XBUTTONDOWN if down else WM_XBUTTONUP, which << 16)

    def key(self, down, vk):
        (self.api.keys_down.add if down else self.api.keys_down.discard)(vk)
        return self.hooks.key(WM_KEYDOWN if down else WM_KEYUP, vk)

    def ignored_press(self, which=XBUTTON1, quill=None):
        """Press and release a mouse trigger while a session is pending; returns its outcome."""
        quill = quill or self.app
        done = len(quill.sessions.outcomes)
        self.assertEqual(self.button(True, which), 1)  # still swallowed: no Back/Forward action
        self.outcomes(done + 1, quill)
        time.sleep(self.min_hold_ms * 4 / 1000)  # past the click-to-focus moment of a normal hold
        self.assertEqual(self.button(False, which), 1)
        return quill.sessions.outcomes[done]

    def outcomes(self, count, quill=None):
        quill = quill or self.app
        wait_for(lambda: len(quill.sessions.outcomes) >= count, f"{count} session outcomes")
        return list(quill.sessions.outcomes)

    def hold(self, words=(1, 2), which=XBUTTON1, quill=None, wait=True):
        """Hold a mouse trigger, speak ``words``, release; returns the capture."""
        quill = quill or self.app
        clicks, made, done = len(self.api.mouse_calls), len(self.captures.made), len(quill.sessions.outcomes)
        self.assertEqual(self.button(True, which), 1)  # swallowed: no Back/Forward action
        wait_for(lambda: len(self.captures.made) > made, "the capture to start")
        capture = self.captures.made[-1]
        wait_for(lambda: len(self.api.mouse_calls) > clicks, "the click to focus")
        capture.push(speech(words))
        self.assertEqual(self.button(False, which), 1)
        if wait:
            self.outcomes(done + 1, quill)
        return capture


class FlowTest(AppCase):
    def test_dictation_from_press_to_typed_text(self):
        with self.assertLogs("quill", level="INFO") as logs:
            self.start()
            self.assertEqual(self.indicator.states[:2], [LOADING, "hide"])
            capture = self.hold((1, 2, 3))
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual(outcome.reason, S.TYPED)
        self.assertEqual(self.api.received_text(), "w1 w2 w3.")
        self.assertEqual(len(self.api.mouse_calls), 1)  # one click at the pointer
        self.assertEqual(self.api.enter_presses(), [])
        self.assertTrue(capture.stopped)
        self.assertIn(LISTENING, self.indicator.states)
        self.assertEqual(self.indicator.last, ("hide",))
        text = "\n".join(logs.output)
        self.assertIn("release to typed", text)
        self.assertNotRegex(text.casefold(), r"\bw[0-9]")  # never the spoken or typed words
        self.assertEqual(self.model.loads, 1)

    def test_keyboard_trigger_f13(self):
        self.start()
        self.assertEqual(self.key(True, F13), 1)
        wait_for(lambda: self.captures.made and self.api.mouse_calls, "the capture and the click")
        self.captures.made[-1].push(speech((4,)))
        self.assertEqual(self.key(False, F13), 1)
        self.assertEqual(self.outcomes(1)[0].reason, S.TYPED)
        self.assertEqual(self.api.received_text(), "w4.")

    def test_short_tap_is_ignored(self):
        quill = self.make_app(self.make_config(min_hold_ms=2000))
        self.start(quill)
        self.assertEqual(self.button(True), 1)
        wait_for(lambda: self.captures.made, "the capture to start")
        self.assertEqual(self.button(False), 1)
        self.assertEqual(self.outcomes(1, quill)[0].reason, S.CANCELLED)
        self.assertEqual(self.api.mouse_calls, [])  # no click
        self.assertEqual(self.api.calls, [])  # nothing typed
        self.assertEqual(self.captures.open, [])
        self.assertEqual(self.indicator.last, ("hide",))

    def test_right_ctrl_combo_cancels_and_passes_through(self):
        self.start()
        self.assertEqual(self.key(True, VK_RCONTROL), 0)  # passed to the system
        wait_for(lambda: self.captures.made, "the capture to start")
        self.assertEqual(self.key(True, KEY_C), 0)  # Ctrl+C: a combo
        self.key(False, KEY_C)
        self.key(False, VK_RCONTROL)
        self.assertEqual(self.outcomes(1)[0].reason, S.CANCELLED)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.captures.open, [])

    def test_a_press_while_the_previous_session_is_pending_starts_nothing(self):
        self.start()
        gate = threading.Event()
        self.model.gate, self.model.gate_when = gate, lambda call: True
        self.hold((1,), wait=False)
        wait_for(lambda: self.model.entered.is_set(), "the engine to be busy")
        # The first session is being finalized: the next press clicks, records and types nothing.
        clicks, made = len(self.api.mouse_calls), len(self.captures.made)
        outcome = self.ignored_press()
        self.assertEqual(outcome.reason, S.PREVIOUS_PENDING)
        self.assertEqual((len(self.api.mouse_calls), len(self.captures.made)), (clicks, made))
        self.assertIn(("show", TRANSCRIBING, S.MESSAGES[S.PREVIOUS_PENDING]), self.indicator.calls)
        gate.set()
        self.assertEqual([o.reason for o in self.outcomes(2)], [S.PREVIOUS_PENDING, S.TYPED])
        self.assertEqual(self.api.received_text(), "w1.")
        self.hold((2,))  # the first press after it works normally
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.TYPED)
        self.assertEqual(self.api.received_text(), "w1.w2.")

    def test_failures_then_success(self):
        self.start()
        # Microphone missing.
        self.captures.fail_start = True
        self.assertEqual(self.button(True), 1)
        wait_for(lambda: self.app.sessions.outcomes, "the microphone error")
        self.button(False)
        self.captures.fail_start = False
        # Engine error.
        self.model.fail = lambda call: RuntimeError("fake engine failure")
        self.hold()
        self.model.fail = lambda call: None
        # The foreground window changed before typing.
        self.model.gate, self.model.gate_when = threading.Event(), lambda call: True
        self.hold(wait=False)
        wait_for(lambda: self.model.entered.is_set(), "the engine to be busy")
        self.api.foreground = OTHER_HWND
        self.model.gate.set()
        self.outcomes(3)
        self.model.gate = None
        self.api.foreground = TARGET.hwnd
        # The target window closed.
        self.model.gate, self.model.entered = threading.Event(), threading.Event()
        self.hold(wait=False)
        wait_for(lambda: self.model.entered.is_set(), "the engine to be busy")
        del self.api.windows[TARGET.hwnd]
        self.model.gate.set()
        self.outcomes(4)
        self.model.gate = None
        self.api.windows[TARGET.hwnd] = TARGET.pid
        # Then a dictation works.
        self.hold((5,))
        reasons = [o.reason for o in self.outcomes(5)]
        self.assertEqual(reasons, [S.MIC_ERROR, S.ENGINE_ERROR, inject.FOREGROUND_CHANGED, inject.TARGET_GONE, S.TYPED])
        self.assertEqual(self.api.received_text(), "w5.")
        self.assertEqual(self.captures.open, [])
        self.assertEqual(self.indicator.last, ("hide",))
        self.assertEqual(self.app.sessions._live, [])

    def test_ollama_down_falls_back_to_the_rules(self):
        client = FakeOllama()
        quill = self.make_app(self.make_config(cleanup_mode="llm"), client=client)
        self.start(quill)
        self.hold((1, 2), quill=quill)
        self.assertEqual(quill.sessions.outcomes[-1].reason, S.CLEANUP_FALLBACK)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.api.received_text(), "w1 w2.")
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.CLEANUP_FALLBACK]))


class LoadingTest(AppCase):
    def test_release_during_loading_records_nothing(self):
        self.model.loaded.clear()
        self.app.start()
        self.assertEqual(self.indicator.last, ("show", LOADING, ""))
        self.assertEqual(self.button(True), 1)
        time.sleep(0.1)  # past the minimum hold
        self.assertEqual(self.button(False), 1)
        self.assertEqual(self.outcomes(1)[0].reason, S.WHILE_LOADING)
        self.assertEqual(self.captures.made, [])
        self.assertEqual(self.api.mouse_calls, [])  # a press while loading does not click either
        self.model.loaded.set()
        wait_for(lambda: self.app.sessions.ready, "the model to load")
        self.assertEqual(self.indicator.last, ("hide",))
        self.hold((3,))
        self.assertEqual(self.api.received_text(), "w3.")

    def test_model_load_failure_shows_the_error(self):
        self.model.fail_load = OSError("fake: model files missing")
        self.app.start()
        wait_for(lambda: self.indicator.last and self.indicator.last[1] == ERROR, "the error state")
        self.assertEqual(self.indicator.last[2], S.MESSAGES[S.MODEL_UNAVAILABLE])
        self.button(True)
        time.sleep(0.1)
        self.button(False)
        self.outcomes(1)
        self.assertEqual(self.captures.made, [])


class SendClaudeTest(AppCase):
    def test_enter_only_in_claude_code(self):
        self.start()
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND
        self.hold((1,), which=XBUTTON2)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER)
        self.assertEqual(self.api.enter_presses(), [False])  # one plain Enter
        self.assertEqual(self.indicator.last, ("show", SENT, ""))
        # The normal trigger in Claude Code types without Enter.
        self.hold((2,))
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.TYPED)
        self.assertEqual(self.api.enter_presses(), [False])

    def test_other_window_types_without_enter_and_says_so(self):
        self.start()
        self.hold((1,), which=XBUTTON2)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.NOT_CLAUDE)
        self.assertEqual(self.api.received_text(), "w1.")
        self.assertEqual(self.api.enter_presses(), [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.NOT_CLAUDE]))

    def test_vscode_needs_the_focused_claude_code_view(self):
        self.api.images[CLAUDE_PID] = "C:\\Invented\\Code.exe"
        self.api.titles[CLAUDE_HWND] = "invented - Visual Studio Code [Claude Code]"
        self.start()
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND
        self.hold((1,), which=XBUTTON2)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER)
        self.assertEqual(self.api.enter_presses(), [False])
        # The integrated terminal or a Claude Code editor tab: typed, never Enter.
        for number, title in ((2, "invented - Visual Studio Code [Terminal]"),
                              (3, "Invented topic - invented - Visual Studio Code []")):
            self.api.titles[CLAUDE_HWND] = title
            self.hold((number,), which=XBUTTON2)
            self.assertEqual(self.app.sessions.outcomes[-1].reason, S.NOT_CLAUDE)
            self.assertEqual(self.app.sessions.outcomes[-1].typed, len("w1."))
        self.assertEqual(self.api.enter_presses(), [False])

    def test_claude_code_in_a_vscode_file_name_is_not_the_marker(self):
        self.api.images[CLAUDE_PID] = "C:\\Invented\\Code.exe"
        self.start()
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND
        titles = ("[Claude Code].md - invented - Visual Studio Code [Text Editor]",
                  "Claude Code notes.md - invented - Visual Studio Code",
                  "invented [Claude Code] - Visual Studio Code")
        for number, title in enumerate(titles, start=1):
            self.api.titles[CLAUDE_HWND] = title
            for which in (XBUTTON2, MIDDLE):
                self.hold((number,), which=which)
                self.assertEqual(self.app.sessions.outcomes[-1].reason, S.NOT_CLAUDE, title)
                self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.NOT_CLAUDE]))
        self.assertEqual(self.api.enter_presses(), [])

    def test_middle_click_sends_with_one_plain_enter_in_claude_code_only(self):
        self.start()
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND
        self.hold((1,), which=MIDDLE)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.action, outcome.reason), ("send_raw", S.SENT_ENTER))
        self.assertEqual(self.api.enter_presses(), [False])  # one plain Enter
        self.api.under_pointer = self.api.foreground = TARGET.hwnd
        self.hold((2,), which=MIDDLE)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.NOT_CLAUDE)
        self.assertEqual(sent_segments(self.api), ["w1.", "w2."])  # Enter after the whole first text only
        self.assertEqual(self.api.enter_presses(), [False])

    def test_the_previous_send_claude_layout_keeps_working(self):
        local = self.folder / "quill.toml"
        local.write_text('[triggers.send_claude]\nbuttons = ["xbutton2", "middle"]\nkeys = ["f15"]\n', "utf-8")
        quill = self.make_app(self.make_config(triggers=load_config(local, EXAMPLE_CONFIG).triggers))
        self.start(quill)
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND
        for which in (XBUTTON2, MIDDLE):
            self.hold((1,), which=which, quill=quill)
            outcome = quill.sessions.outcomes[-1]
            self.assertEqual((outcome.action, outcome.reason), ("send_claude", S.SENT_ENTER))
        self.assertEqual(self.api.enter_presses(), [False, False])


class ScreenReaderTitleTest(AppCase):
    """VS Code with its screen reader optimization: " - <editor state>" follows the focused-view marker."""

    def setUp(self):
        super().setUp()
        self.api.images[CLAUDE_PID] = "C:\\Invented\\Code.exe"
        self.start()
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND

    def test_the_claude_code_view_with_the_state_suffix_gets_enter(self):
        titles = ("● main.py - invented - Visual Studio Code [Claude Code] - Modified",
                  "invented | notes.md - Visual Studio Code [Claude Code] - Untracked, 1 problem",
                  "invented | - Visual Studio Code [Claude Code]")
        for number, title in enumerate(titles, start=1):
            self.api.titles[CLAUDE_HWND] = title
            self.hold((number,), which=XBUTTON2)
            self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER, title)
        self.assertEqual(self.api.enter_presses(), [False, False, False])

    def test_an_ordinary_editor_tab_with_the_suffix_never_gets_enter(self):
        titles = ("main.py - invented - Visual Studio Code [Text Editor] - Modified",
                  "main.py - invented - Visual Studio Code [Editor de Texto] - Modificado",
                  "invented | notes.md - Visual Studio Code [Terminal] - Modified",
                  "invented | notes.md - Visual Studio Code [Explorer]",
                  "invented | Invented topic - Visual Studio Code []",
                  "invented | Invented topic - Visual Studio Code [] - Modified")
        for number, title in enumerate(titles, start=1):
            self.api.titles[CLAUDE_HWND] = title
            for which in (XBUTTON2, MIDDLE):
                self.hold((number,), which=which)
                self.assertEqual(self.app.sessions.outcomes[-1].reason, S.NOT_CLAUDE, title)
        self.assertEqual(self.api.enter_presses(), [])

    def withheld_after_typing(self, change):
        """Mouse 5 into the Claude Code view; ``change()`` runs once the text is typed, before Enter."""
        self.api.titles[CLAUDE_HWND] = "main.py - invented - Visual Studio Code [Claude Code]"
        self.api.after_send = lambda index: change()
        with self.assertLogs("quill.session", level="WARNING") as logs:
            self.hold((1,), which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.typed), (S.ENTER_WITHHELD, len("w1.")))
        self.assertEqual(self.api.enter_presses(), [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.ENTER_WITHHELD]))
        return "\n".join(logs.output)

    def test_no_enter_when_the_title_gains_a_dirty_marker(self):
        logs = self.withheld_after_typing(lambda: self.api.titles.__setitem__(
            CLAUDE_HWND, "● main.py - invented - Visual Studio Code [Claude Code]"))
        self.assertIn("Enter withheld (editor_dirty)", logs)
        self.assertNotIn("invented", logs)

    def test_no_enter_when_the_focus_left_the_claude_code_view(self):
        logs = self.withheld_after_typing(lambda: self.api.titles.__setitem__(
            CLAUDE_HWND, "main.py - invented - Visual Studio Code [Text Editor]"))
        self.assertIn("Enter withheld (not_claude_code)", logs)

    def test_no_enter_when_another_window_came_forward(self):
        logs = self.withheld_after_typing(lambda: setattr(self.api, "foreground", TARGET.hwnd))
        self.assertIn("Enter withheld (foreground_changed)", logs)

    def test_no_enter_when_the_window_was_reused(self):
        logs = self.withheld_after_typing(lambda: self.api.windows.__setitem__(CLAUDE_HWND, OTHER_PID_FOR_REUSE))
        self.assertIn("Enter withheld (target_gone)", logs)

    def test_an_already_dirty_title_still_gets_enter(self):
        self.api.titles[CLAUDE_HWND] = "● main.py - invented - Visual Studio Code [Claude Code] - Modified"
        self.hold((1,), which=XBUTTON2)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER)
        self.assertEqual(self.api.enter_presses(), [False])


OTHER_PID_FOR_REUSE = 77


class LifecycleTest(AppCase):
    def test_start_stop_start(self):
        self.start()
        self.hold((1,))
        self.app.stop()
        first = self.hook_fakes[0]
        self.assertEqual(sorted(first.unhooked), sorted(first.handles.values()))
        self.assertEqual(self.indicator.stops, 1)
        self.assertEqual(self.model.closes, 1)
        self.assertEqual(self.kernel.objects, {})  # mutex and stop event closed
        self.assertFalse(self.app.running)
        self.start()
        self.assertEqual(len(self.hook_fakes), 2)
        self.hold((2,))
        self.assertEqual(self.api.received_text(), "w1.w2.")
        self.assertEqual(self.model.loads, 2)
        self.app.stop()
        self.app.stop()  # idempotent
        self.assertEqual(self.kernel.objects, {})

    def test_stop_during_a_hold_releases_the_microphone(self):
        self.start()
        self.button(True)
        wait_for(lambda: self.captures.made, "the capture to start")
        self.app.stop()
        self.assertEqual(self.captures.open, [])
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.CANCELLED)
        self.assertEqual(self.api.calls, [])

    def test_single_instance(self):
        self.start()
        other = self.make_app()
        with self.assertRaises(A.AlreadyRunning):
            other.start()
        self.assertEqual(other.run(poll_s=0.01), A.EXIT_ALREADY_RUNNING)
        self.assertEqual(len(self.hook_fakes), 1)  # the second one installed nothing
        self.assertTrue(self.app.parts.instance.held)
        self.hold((1,))
        self.assertEqual(self.api.received_text(), "w1.")

    def test_run_ends_on_the_stop_request(self):
        codes = []
        runner = threading.Thread(target=lambda: codes.append(self.app.run(poll_s=0.01)))
        runner.start()
        wait_for(lambda: self.app.running and self.app.sessions.ready, "the app to run")
        self.assertTrue(A.InstanceLock(self.kernel).signal_stop())
        runner.join(WAIT_S)
        self.assertEqual(codes, [A.EXIT_OK])
        self.assertFalse(self.app.running)
        self.assertFalse(A.InstanceLock(self.kernel).signal_stop())  # nothing runs any more

    def test_hooks_failing_to_install_release_everything(self):
        self.new_hooks = lambda: FakeHooks(fail_kind=13)  # WH_KEYBOARD_LL
        quill = self.make_app()
        with self.assertRaises(OSError):
            quill.start()
        self.assertFalse(quill.running)
        self.assertEqual(self.kernel.objects, {})
        self.assertEqual(self.indicator.stops, 1)


class ClaudeAlertTest(AppCase):
    """The Claude Code alert listener inside the app, on fake named events and a fake player."""

    def setUp(self):
        self.events = FakeAlertEvents()
        super().setUp()

    def make_app(self, config=None, client=None, kernel=None, **parts):
        self.player = FakePlayer(recording=lambda: bool(self.captures.open))
        parts.setdefault("alert_events", self.events)
        parts.setdefault("player", self.player)
        return super().make_app(config, client, kernel, **parts)

    def alert_config(self, **changes):
        return self.make_config(claude_alert=ClaudeAlert(**changes))

    def ring(self, kind=sound.DONE):
        self.assertTrue(self.events.set_event(N.EVENT_NAMES[kind]))

    def test_start_stop_start_with_the_listener(self):
        self.start()
        self.assertEqual(sorted(self.events.open), sorted(N.EVENT_NAMES.values()))
        self.ring()
        wait_for(lambda: self.player.plays == [sound.DONE], "the alert sound")
        wait_for(lambda: self.indicator.last == ("show", CLAUDE_DONE, ""), "the alert state")
        self.app.stop()
        self.assertEqual(self.events.open, [])
        self.assertFalse(self.events.set_event(N.EVENT_NAMES[sound.DONE]))
        self.start()
        self.ring(sound.PERMISSION)
        wait_for(lambda: self.player.plays == [sound.DONE, sound.PERMISSION], "the alert after a restart")
        self.hold((1,))
        self.assertEqual(self.api.received_text(), "w1.")
        self.app.stop()
        self.assertEqual(self.events.open, [])

    def test_an_alert_during_a_hold_rings_after_it(self):
        self.start()
        self.assertEqual(self.button(True), 1)
        wait_for(lambda: self.captures.made, "the capture to start")
        self.ring()
        self.ring()
        time.sleep(0.2)
        self.assertEqual(self.player.plays, [])
        capture = self.captures.made[-1]
        wait_for(lambda: self.api.mouse_calls, "the click to focus")
        capture.push(speech((1,)))
        self.assertEqual(self.button(False), 1)
        self.outcomes(1)
        wait_for(lambda: self.player.plays == [sound.DONE], "the deferred alert")
        self.assertEqual(self.player.plays_while_recording, 0)
        self.assertEqual(self.api.received_text(), "w1.")

    def test_disabled_alert_creates_no_event(self):
        quill = self.make_app(self.alert_config(enabled=False))
        self.start(quill)
        self.assertIsNone(quill.alerts)
        self.assertEqual(self.events.created, [])

    def test_sound_off_shows_the_alert_silently(self):
        quill = self.make_app(self.alert_config(sound=False))
        self.start(quill)
        self.ring(sound.PERMISSION)
        wait_for(lambda: self.indicator.last == ("show", CLAUDE_PERMISSION, ""), "the alert state")
        self.assertEqual(self.player.plays, [])

    def test_a_listener_that_cannot_start_leaves_quill_working(self):
        self.events.fail_create = True
        with self.assertLogs("quill.app", level="ERROR"):
            quill = self.start(self.make_app())
        self.assertFalse(quill.alerts.running)
        self.hold((1,), quill=quill)
        self.assertEqual(self.api.received_text(), "w1.")

    # Project names: invented folders in a temporary folder, the hook run in-process on the fake events.

    def named_setup(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.alerts_dir = self.base / "local" / "alerts"
        self.now = [1000.0]
        self.app = self.make_app(alerts_dir=self.alerts_dir, clock=lambda: self.now[0])
        return self.app

    def hook(self, project, hook="stop"):
        cwd = self.base / "projects" / project
        (cwd / ".git").mkdir(parents=True, exist_ok=True)
        (cwd / "src").mkdir(exist_ok=True)
        event = ({"hook_event_name": "Stop", "stop_hook_active": False} if hook == "stop" else
                 {"hook_event_name": "Notification", "notification_type": "permission_prompt"})
        data = io.BytesIO(json.dumps({**event, "cwd": str(cwd / "src")}).encode("utf-8"))
        return N.notify(hook, data.read, {N.ATTENDED_VARIABLE: "1"}, events=self.events,
                        settings=lambda: (True, "attended"), alerts_dir=self.alerts_dir)

    def test_a_named_alert_shows_its_project_and_no_log_holds_the_name(self):
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        quill_log = logging.getLogger("quill")
        old_level = quill_log.level
        quill_log.addHandler(handler)
        quill_log.setLevel(logging.DEBUG)
        try:
            quill = self.start(self.named_setup())
            self.assertTrue(self.alerts_dir.is_dir())
            self.assertEqual(self.hook("zorblat-kit"), "signalled")
            wait_for(lambda: self.indicator.last == ("show", CLAUDE_DONE, "zorblat-kit: Claude acabou"),
                     "the named alert")
            self.now[0] += S.ALERT_SHOW_S  # the first alert has left the screen
            self.assertEqual(self.hook("quenta-tree", "permission"), "signalled")
            wait_for(lambda: self.indicator.last == ("show", CLAUDE_PERMISSION, "quenta-tree: Claude pede permissão"),
                     "the named permission")
            quill.stop()
        finally:
            quill_log.removeHandler(handler)
            quill_log.setLevel(old_level)
        self.assertEqual(self.player.plays, [sound.DONE, sound.PERMISSION])
        output = capture.getvalue()
        self.assertIn("claude alert", output)  # the alerts were logged, by reason and count only
        for private in ("zorblat", "quenta", str(self.base), self.base.name):
            self.assertNotIn(private, output)
        self.assertEqual(list(self.alerts_dir.iterdir()), [])

    def test_named_alerts_during_a_hold_are_shown_together_after_it(self):
        self.start(self.named_setup())
        self.assertEqual(self.button(True), 1)
        wait_for(lambda: self.captures.made, "the capture to start")
        self.assertEqual(self.hook("zorblat-kit"), "signalled")
        self.assertEqual(self.hook("quenta-tree", "permission"), "signalled")
        self.assertEqual(self.hook("vellum-app"), "signalled")
        time.sleep(0.2)
        self.assertEqual(self.player.plays, [])
        capture = self.captures.made[-1]
        wait_for(lambda: self.api.mouse_calls, "the click to focus")
        capture.push(speech((1,)))
        self.assertEqual(self.button(False), 1)
        self.outcomes(1)
        expected = ("show", CLAUDE_PERMISSION, "zorblat-kit, vellum-app: Claude acabou; quenta-tree: Claude pede permissão")
        wait_for(lambda: self.indicator.last == expected, "the combined alert")
        self.assertEqual(self.player.plays, [sound.PERMISSION])
        self.assertEqual(self.player.plays_while_recording, 0)

    def test_named_alerts_arriving_while_idle_are_all_shown(self):
        self.start(self.named_setup())
        names = ["zorblat-kit", "quenta-tree", "vellum-app", "brask-lab", "orrin-ops"]
        results = []
        threads = [threading.Thread(target=lambda name=name: results.append(self.hook(name))) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["signalled"] * 5)

        def shown():
            if len(self.indicator.last or ()) < 3:
                return False
            state, text = self.indicator.last[1:3]
            head, _, phrase = text.partition(": ")
            return state == CLAUDE_DONE and phrase == "Claude acabou" and sorted(head.split(", ")) == sorted(names)
        wait_for(shown, "every project in one alert")
        self.assertEqual(self.player.plays, [sound.DONE])
        # A permission request after them is added last, and a later finished reply never hides it.
        self.now[0] += 1
        self.assertEqual(self.hook("rinn-desk", "permission"), "signalled")
        wait_for(lambda: self.indicator.last[1] == CLAUDE_PERMISSION
                 and self.indicator.last[2].endswith("; rinn-desk: Claude pede permissão"), "the permission request")
        self.now[0] += 1
        self.assertEqual(self.hook("tavi-notes"), "signalled")
        wait_for(lambda: "tavi-notes" in self.indicator.last[2], "the later finished reply")
        self.assertEqual(self.indicator.last[1], CLAUDE_PERMISSION)
        self.assertTrue(self.indicator.last[2].endswith(", tavi-notes: Claude acabou; rinn-desk: Claude pede permissão"))
        for name in names:
            self.assertIn(name, self.indicator.last[2])
        self.assertEqual(self.player.plays, [sound.DONE])
        alerts = [call for call in self.indicator.calls if call[0] == "show" and call[1] in S.ALERT_STATES.values()]
        self.assertFalse(any(call[2] == "" for call in alerts))  # no nameless duplicate

    # Spoken names: a Speaker without its thread on a fake engine and the app's fake clock.

    def speaker_setup(self, **changes):
        self.engine = FakeSpeechEngine()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.alerts_dir = self.base / "local" / "alerts"
        self.now = [1000.0]
        self.speaker = SP.Speaker(self.engine, clock=lambda: self.now[0], threaded=False)
        config = self.alert_config(**changes) if changes else None
        return self.make_app(config, alerts_dir=self.alerts_dir, clock=lambda: self.now[0], speaker=self.speaker)

    def test_the_project_name_is_spoken_after_the_sound(self):
        quill = self.start(self.speaker_setup())
        self.assertIs(quill.sessions.speaker, self.speaker)
        self.assertEqual(self.hook("zorblat-kit"), "signalled")
        wait_for(lambda: self.speaker.pending, "the pending speech")
        self.assertEqual(self.player.plays, [sound.DONE])
        self.assertFalse(self.speaker.tick())
        self.now[0] += SP.CHIME_GAP_S
        self.assertTrue(self.speaker.tick())
        self.assertEqual(self.engine.spoken, [["zorblat kit"]])
        quill.stop()
        self.assertEqual(self.engine.processes[0].terminated, 1)

    def test_a_hold_stops_the_spoken_name(self):
        quill = self.start(self.speaker_setup())
        self.assertEqual(self.hook("zorblat-kit", "permission"), "signalled")
        wait_for(lambda: self.speaker.pending, "the pending speech")
        self.now[0] += SP.CHIME_GAP_S
        self.assertTrue(self.speaker.tick())
        process = self.engine.processes[0]
        at_start = []
        self.captures.on_start = lambda capture: at_start.append(process.terminated)
        self.hold((1,), quill=quill)
        self.assertEqual(at_start, [1])
        self.assertEqual(self.api.received_text(), "w1.")

    def test_speak_project_off_or_sound_off_speaks_nothing(self):
        for changes in ({"speak_project": False}, {"sound": False}):
            with self.subTest(changes=changes):
                quill = self.start(self.speaker_setup(**changes))
                self.assertIsNone(quill.sessions.speaker)
                self.assertEqual(self.hook("zorblat-kit"), "signalled")
                wait_for(lambda: self.indicator.last == ("show", CLAUDE_DONE, "zorblat-kit: Claude acabou"),
                         "the named alert")
                self.now[0] += SP.CHIME_GAP_S
                self.assertFalse(self.speaker.tick())
                self.assertEqual(self.engine.processes, [])
                self.assertEqual(self.player.plays, [] if changes.get("sound") is False else [sound.DONE])
                quill.stop()

    def test_without_an_alerts_folder_the_alerts_stay_nameless(self):
        self.start()
        self.ring()
        wait_for(lambda: self.indicator.last == ("show", CLAUDE_DONE, ""), "the nameless alert")


class SpokenModel(GatedModel):
    """A fake model whose burst words read as the invented words of ``SPOKEN_WORDS``."""

    def transcribe(self, pcm, options):
        result = super().transcribe(pcm, options)
        say = lambda text: " ".join(SPOKEN_WORDS.get(word, word) for word in text.split())  # noqa: E731
        return Transcript(say(result.text), tuple(Word(w.start, w.end, say(w.text)) for w in result.words))


SPOKEN_WORDS = {"w1": "abre", "w2": "vs", "w3": "code", "w4": "no", "w5": "orla", "w6": "public", "w7": "quasar"}


class VoiceCommandTest(AppCase):
    """F9 held: the spoken command opens an invented hub shortcut through a fake launcher."""

    def voice_app(self, launcher, vocabulary_names=None, voice_model=None, **hint_options):
        hub = self.folder / "hub"
        hub.mkdir(exist_ok=True)
        for name in ("orla", "orla-public", "nimbus-deck"):
            write_link(hub, name, SHORTCUT_CODE, arguments=f'--new-window "C:\\Work\\{name}.code-workspace"')
        self.model = SpokenModel()
        config = self.make_config(voice=VoiceSettings((hub,)), min_hold_ms=1)  # no click to wait for
        parts = {}
        if vocabulary_names is not None:
            path = self.config.vocabulary_path
            path.write_text(vocabulary_names, "utf-8")
            source = V.VocabularyFile(path)
            parts = dict(vocabulary=source.load(), vocabulary_file=source)
        # An invented token count: the tokenizer of a real model is never read here.
        parts["voice_hint_options"] = {"tokens": word_tokens, **hint_options}
        if voice_model is not None:
            parts["voice_model"] = voice_model
        return self.start(self.make_app(config, launcher=launcher, **parts)), hub

    def speak(self, quill, words):
        done, clicks = len(quill.sessions.outcomes), len(self.api.mouse_calls)
        self.assertEqual(self.key(True, VK_F9), 1)  # swallowed: F9 never reaches the focused program
        wait_for(lambda: len(self.captures.made) > done, "the capture to start")
        self.captures.made[-1].push(speech(words))
        time.sleep(0.01)  # longer than the 1 ms minimum hold
        self.assertEqual(self.key(False, VK_F9), 1)
        outcome = self.outcomes(done + 1, quill)[-1]
        self.assertEqual(len(self.api.mouse_calls), clicks)  # never a click
        return outcome

    def test_f9_opens_the_named_shortcut_and_never_clicks_types_or_enters(self):
        launcher = FakeLauncher()
        with self.assertLogs("quill", level="INFO") as logs:
            quill, hub = self.voice_app(launcher)
            self.assertIsNotNone(quill.voice)
            outcome = self.speak(quill, (1, 2, 3, 4, 5))
        self.assertEqual((outcome.action, outcome.reason), ("voice", "opened"))
        self.assertEqual(launcher.opened, [hub / "orla.lnk"])
        self.assertEqual(launcher.started, [])
        self.assertEqual((self.api.received_text(), self.api.enter_presses()), ("", []))
        self.assertEqual(self.indicator.last, ("show", VOICE_OPEN, "A abrir orla"))
        self.assertIn(VOICE, self.indicator.states)
        # The sibling name opens the sibling.
        self.speak(quill, (1, 2, 3, 4, 5, 6))
        self.assertEqual(launcher.opened[-1], hub / "orla-public.lnk")
        # No project with that name: nothing opens.
        outcome = self.speak(quill, (1, 2, 3, 4, 7))
        self.assertEqual(outcome.reason, "no_match")
        self.assertEqual(len(launcher.opened), 2)
        self.assertEqual(self.indicator.last[1], VOICE_NONE)
        text = "\n".join(logs.output)
        self.assertIn("voice commands on (1 folders)", text)
        for private in ("orla", "quasar", "abre", str(hub)):
            self.assertNotIn(private, text)

    def test_f9_is_decoded_with_the_voice_hints_and_dictation_keeps_the_vocabulary_hints(self):
        launcher = FakeLauncher()
        quill, hub = self.voice_app(launcher, 'names = ["nimbus-deck"]' + chr(10) + '[variants]' + chr(10)
                                    + '"nimbus-deck" = ["quasar"]' + chr(10))
        self.assertEqual(self.speak(quill, (1, 2, 3, 4, 5)).reason, "opened")
        voice = list(self.model.calls)
        self.assertTrue(voice)
        listed = [shortcut.name for shortcut in list_shortcuts((hub,)).shortcuts]
        expected = voice_hints(listed, quill.vocabulary, tokens=word_tokens)
        self.assertIn("orla-public", expected.prompt)
        self.assertNotIn("orla", expected.hotwords)
        self.assertIn("quasar", expected.hotwords)
        for call in voice:  # partials and the final alike
            self.assertEqual((call.options.initial_prompt, call.options.hotwords, call.options.language),
                             (expected.prompt, expected.hotwords, "pt"))
        # A dictation afterwards gets the vocabulary hints and the model's own language again.
        self.hold((5, 6), quill=quill)
        dictation = self.model.calls[len(voice):]
        self.assertTrue(dictation)
        for call in dictation:
            self.assertEqual((call.options.initial_prompt, call.options.hotwords, call.options.language),
                             ("Vocabulário: nimbus-deck.", "nimbus-deck", None))

    def test_hints_that_cannot_be_listed_never_stop_the_command(self):
        def lister(_folders):
            raise PermissionError("invented")

        launcher = FakeLauncher()
        with self.assertLogs("quill", level="INFO") as logs:
            quill, hub = self.voice_app(launcher, lister=lister)
            self.assertEqual(self.speak(quill, (1, 2, 3, 4, 5)).reason, "opened")
        self.assertEqual(launcher.opened, [hub / "orla.lnk"])
        self.assertTrue(all(call.options.initial_prompt == VOICE_PROMPT for call in self.model.calls))
        text = chr(10).join(logs.output)
        self.assertIn("hints_shortcuts_unlisted (PermissionError)", text)
        for private in ("orla", str(hub)):
            self.assertNotIn(private, text)

    def test_unrecognized_speech_does_nothing(self):
        launcher = FakeLauncher()
        quill, hub = self.voice_app(launcher)
        self.assertEqual(self.speak(quill, (1, 2, 3, 4)).reason, "unrecognized")  # filler only
        self.assertEqual(self.indicator.last, ("show", VOICE_NONE, "Comando não reconhecido"))
        self.assertEqual((launcher.opened, self.api.received_text()), ([], ""))
        # The name alone, without the verb, is a command.
        self.assertEqual(self.speak(quill, (5, 6)).reason, "opened")
        self.assertEqual((launcher.opened, self.api.received_text()), ([hub / "orla-public.lnk"], ""))

    def test_a_reloaded_vocabulary_is_used_by_the_next_command(self):
        launcher = FakeLauncher()
        quill, hub = self.voice_app(launcher, 'names = ["nimbus-deck"]\n')
        self.assertEqual(self.speak(quill, (1, 2, 3, 4, 7)).reason, "no_match")
        self.config.vocabulary_path.write_text('names = ["nimbus-deck"]\n[variants]\n"nimbus-deck" = ["quasar"]\n',
                                               "utf-8")
        self.assertEqual(self.speak(quill, (1, 2, 3, 4, 7)).reason, "opened")
        self.assertEqual(launcher.opened, [hub / "nimbus-deck.lnk"])

    def test_the_voice_model_decodes_f9_once_loaded_after_the_engine_model(self):
        launcher = FakeLauncher()
        voice_model = SpokenModel()
        voice_model.loaded.clear()  # the voice model is still loading
        with self.assertLogs("quill", level="INFO") as logs:
            quill, hub = self.voice_app(launcher, voice_model=voice_model)
            self.assertIsNotNone(quill.voice_transcriber)
            # Loading: the engine model decodes F9.
            wait_for(lambda: voice_model.loads == 0 and quill.voice_transcriber.running, "the voice model to load")
            self.assertEqual(self.speak(quill, (1, 2, 3, 4, 5)).reason, "opened")
            engine_calls = len(self.model.calls)
            self.assertTrue(engine_calls)
            self.assertEqual(voice_model.calls, [])
            voice_model.loaded.set()
            wait_for(lambda: quill.voice_transcriber.ready.is_set(), "the voice model to be ready")
            wait_for(lambda: "voice model ready" in "\n".join(logs.output), "the voice model log line")
            # Ready: the voice model decodes F9 with the voice hints; the engine model is not used.
            self.assertEqual(self.speak(quill, (1, 2, 3, 4, 5, 6)).reason, "opened")
            self.assertEqual(launcher.opened, [hub / "orla.lnk", hub / "orla-public.lnk"])
            self.assertTrue(voice_model.calls)
            self.assertTrue(all(call.options.language == "pt" for call in voice_model.calls))
            self.assertEqual(len(self.model.calls), engine_calls)
            # Dictation keeps the engine model.
            voice_calls = len(voice_model.calls)
            self.hold((5, 6), quill=quill)
            self.assertGreater(len(self.model.calls), engine_calls)
            self.assertEqual(len(voice_model.calls), voice_calls)
            quill.stop()
        self.assertEqual((voice_model.loads, voice_model.closes), (1, 1))
        text = "\n".join(logs.output)
        self.assertIn("voice model large-v3", text)
        for private in ("orla", str(hub)):
            self.assertNotIn(private, text)

    def test_a_voice_model_that_fails_leaves_f9_on_the_engine_model(self):
        launcher = FakeLauncher()
        voice_model = SpokenModel()
        voice_model.fail_load = RuntimeError("invented")
        with self.assertLogs("quill", level="INFO") as logs:
            quill, hub = self.voice_app(launcher, voice_model=voice_model)
            wait_for(lambda: quill.voice_transcriber.ready.is_set(), "the voice model load to end")
            self.assertEqual(self.speak(quill, (1, 2, 3, 4, 5)).reason, "opened")
        self.assertEqual(launcher.opened, [hub / "orla.lnk"])
        self.assertEqual(voice_model.calls, [])
        self.assertTrue(self.model.calls)
        self.assertIn("voice model not loaded, voice commands use the engine model", "\n".join(logs.output))

    def test_the_voice_model_is_never_loaded_when_the_engine_model_fails(self):
        self.model = GatedModel()
        self.model.fail_load = RuntimeError("invented")
        voice_model = GatedModel()
        quill = self.make_app(self.make_config(voice=VoiceSettings((self.folder,))), launcher=FakeLauncher(),
                              voice_model=voice_model)
        quill.start()
        wait_for(lambda: quill.transcriber.ready.is_set(), "the engine model load to end")
        wait_for(lambda: quill._loader is not None and not quill._loader.is_alive(), "the loader to end")
        quill.stop()
        self.assertEqual((voice_model.loads, voice_model.closes), (0, 0))

    def test_without_a_launcher_or_a_voice_key_there_are_no_voice_commands(self):
        self.assertIsNone(self.make_app(self.make_config(voice=VoiceSettings((self.folder,)))).voice)
        config = load_config(None, EXAMPLE_CONFIG)
        off = dataclasses.replace(config, triggers=tuple(
            dataclasses.replace(t, inputs=()) if t.action == "voice" else t for t in config.triggers))
        self.assertIsNone(self.make_app(off, launcher=FakeLauncher()).voice)


class VocabularyReloadTest(AppCase):
    """The personal vocabulary file (in the temporary folder) edited while Quill runs."""

    def reloading_app(self):
        path = self.config.vocabulary_path
        path.write_bytes(b'names = ["Zulo"]\n')
        source = V.VocabularyFile(path)
        quill = self.make_app(vocabulary=source.load(), vocabulary_file=source)
        return self.start(quill), path

    def prompts(self, since=0):
        return [call.options.initial_prompt or "" for call in self.model.calls[since:]]

    def test_a_changed_file_is_used_from_the_next_dictation(self):
        quill, path = self.reloading_app()
        self.hold((1, 2), quill=quill)
        self.assertEqual(self.api.received_text(), "w1 w2.")
        self.assertTrue(self.prompts() and all("Vocabulário: Zulo." in p for p in self.prompts()))
        calls = len(self.model.calls)
        with self.assertLogs("quill", level="INFO") as logs:
            V.add_entries(path, terms=["Grafana"], variants=[("Zulo", "w2")])
            self.hold((1, 2), quill=quill)
        self.assertEqual(self.api.received_text(), "w1 w2.w1 Zulo.")  # the new matcher
        self.assertTrue(all("Vocabulário: Zulo, Grafana." in p for p in self.prompts(calls)))  # the new hints
        text = "\n".join(logs.output)
        self.assertIn("vocabulary reloaded (1 names, 1 terms, 1 variants)", text)
        self.assertNotIn("Grafana", text)
        self.assertEqual(quill.pipeline.keep, ("Zulo", "Grafana"))

    def test_an_invalid_file_keeps_the_previous_vocabulary(self):
        quill, path = self.reloading_app()
        path.write_bytes(b'names = ["Zulo"]\n[variants]\n"Zulo" = ["w2"]\n')
        self.hold((2,), quill=quill)
        self.assertEqual(self.api.received_text(), "Zulo.")
        calls = len(self.model.calls)
        path.write_bytes(b'names = ["Secretname", "secretname"]\n')
        with self.assertLogs("quill", level="INFO") as logs:
            self.hold((2,), quill=quill)
        self.assertEqual(self.api.received_text(), "Zulo.Zulo.")
        self.assertTrue(all("Vocabulário: Zulo." in p for p in self.prompts(calls)))
        text = "\n".join(logs.output)
        self.assertIn("names[1] repeats names[0]; the previous vocabulary stays", text)
        self.assertNotIn("Secretname", text)
        self.assertNotIn("vocabulary reloaded", text)
        # Fixed again: used from the next dictation.
        path.write_bytes(b'names = ["Anta"]\n[variants]\n"Anta" = ["w2"]\n')
        self.hold((2,), quill=quill)
        self.assertEqual(self.api.received_text(), "Zulo.Zulo.Anta.")

    def test_an_unchanged_file_is_not_read_again(self):
        quill, path = self.reloading_app()
        lexicon = (quill.pipeline.keep, quill.pipeline.matcher, quill.transcriber.vocabulary)
        with mock.patch.object(V, "parse_text", wraps=V.parse_text) as parse:
            self.hold((1,), quill=quill)
            self.hold((2,), quill=quill)
        parse.assert_not_called()
        self.assertEqual(self.api.received_text(), "w1.w2.")
        after = (quill.pipeline.keep, quill.pipeline.matcher, quill.transcriber.vocabulary)
        self.assertTrue(all(a is b for a, b in zip(lexicon, after)))

    def test_reloaded_hints_stay_within_the_cap(self):
        quill, path = self.reloading_app()
        terms = [f"Term{i:03}" for i in range(80)]
        added = V.add_entries(path, terms=terms).vocabulary
        self.hold((1,), quill=quill)
        hints = quill.transcriber.vocabulary
        self.assertEqual(hints[:2], ("Zulo", "Term000"))
        self.assertLessEqual(len(", ".join(hints)), V.HINT_MAX_CHARS)
        self.assertLess(len(hints), len(added.entries))
        self.assertEqual(len(quill.pipeline.keep), len(added.entries))  # cleanup still keeps every entry

    def test_reload_survives_stop_and_start(self):
        quill, path = self.reloading_app()
        quill.stop()
        V.add_entries(path, variants=[("Zulo", "w3")])
        self.start(quill)
        self.hold((3,), quill=quill)
        self.assertEqual(self.api.received_text(), "Zulo.")


class CorrectionKeyTest(AppCase):
    def test_correction_key_learns_from_the_selection(self):
        # Wait for the learner's save to return, never for the file to appear: the
        # file exists as soon as the save's rename starts, and reading it before the
        # rename finishes is a Windows sharing violation (PermissionError).
        self.start()
        self.hold((1, 2))
        saved = threading.Event()
        learn = self.app.learner.learn

        def learn_then_signal(*args, **kwargs):
            try:
                return learn(*args, **kwargs)
            finally:
                saved.set()

        with mock.patch.object(self.app.correction_key, "read_selection", return_value="w1 w9."), \
                mock.patch.object(self.app.learner, "learn", side_effect=learn_then_signal):
            self.assertEqual(self.key(True, F16), 0)  # not swallowed
            self.key(False, F16)
            self.assertTrue(saved.wait(WAIT_S), "timed out waiting for the learned correction")
        stored = CorrectionStore(self.config.corrections_path).load()
        self.assertEqual([(e.source, e.target) for e in stored.entries], [("w2", "w9")])


WORDS = tuple(range(1, 13))  # 12 invented words: long once min_words is 10
SPOKEN = "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10 w11 w12."
REWRITTEN = "W1 w2 w3 w4 w5 w6, w7 w8 w9 w10 w11 w12."  # what the guard accepts: capital and comma
F17 = 0x80  # the example's undo key
VK_A = 0x41
VK_RETURN = 0x0D


class RewriteOllama:
    """A local Ollama stand-in for the automatic rewrite: a reply, an error or a wait."""

    def __init__(self, reply=REWRITTEN):
        self.reply = reply
        self.error = None
        self.gate = None  # threading.Event the reply waits for
        self.entered = threading.Event()
        self.calls = []  # (system prompt, user message, timeout_s)
        self.memory = ["qwen3:8b"]  # the models /api/ps lists
        self.warms = []  # (model, thread name) of each warm-up
        self.warm_gate = None  # threading.Event a warm-up waits for
        self.order = []  # "warm" and "chat", in the order they were asked
        self.lists = 0  # /api/ps reads

    def installed(self, timeout_s=None):
        return ["qwen3:8b"]

    def loaded(self, timeout_s=None):
        self.lists += 1
        return list(self.memory)

    def warm(self, model, timeout_s=None):
        self.warms.append((model, threading.current_thread().name))
        self.order.append("warm")
        if self.warm_gate is not None:
            self.warm_gate.wait(WAIT_S)
        self.memory = [model]

    def chat(self, model, system, user, max_tokens=None, timeout_s=None):
        self.order.append("chat")
        self.calls.append((system, user, timeout_s))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(WAIT_S)
        if self.error is not None:
            raise self.error
        return ChatReply(self.reply)


class RewriteCase(AppCase):
    def setUp(self):
        super().setUp()
        self.ollama = RewriteOllama()
        self.app = self.rewriting_app()

    def rewriting_app(self, **changes):
        rewrite = dataclasses.replace(self.config.autorewrite, **{"min_words": 10, **changes})
        return self.make_app(self.make_config(autorewrite=rewrite), rewrite_client=self.ollama, layout=FakeLayout())

    def press_undo(self, quill=None):
        quill = quill or self.app
        shown = len(self.indicator.calls)
        self.assertEqual(self.key(True, F17), 0)  # never swallowed
        self.key(False, F17)
        wait_for(lambda: len(self.indicator.calls) > shown, "the undo outcome on the indicator")
        return self.indicator.last

    def refused(self, reason):
        return ("show", ERROR, UNDO_MESSAGES[reason])


class RewriteAppTest(RewriteCase):
    def test_short_dictation_never_calls_ollama(self):
        self.start()
        self.hold((1, 2, 3))
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.TYPED, None))
        self.assertEqual(self.ollama.calls, [])
        self.assertEqual(self.api.received_text(), "w1 w2 w3.")
        self.assertNotIn(REVIEWING, self.indicator.states)

    def test_long_dictation_is_rewritten_with_the_indicator_state(self):
        with self.assertLogs("quill", level="INFO") as logs:
            self.start()
            self.hold(WORDS)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.TYPED, R.REWRITTEN))
        self.assertEqual(self.api.received_text(), REWRITTEN)
        self.assertIn(REVIEWING, self.indicator.states)
        self.assertEqual(self.indicator.last, ("hide",))
        system, user, timeout = self.ollama.calls[0]
        self.assertIn(SPOKEN, user)
        self.assertEqual(timeout, self.config.autorewrite.timeout_s)
        text = "\n".join(logs.output)
        self.assertIn("automatic rewrite on", text)
        self.assertNotRegex(text.casefold(), r"\bw[0-9]")  # never the spoken, rewritten or original words

    def test_ollama_down_timeout_or_refusal_types_the_original_at_once(self):
        self.start()
        cases = ((OllamaError("Ollama unreachable: URLError"), REWRITTEN, R.FAILED),
                 (TimeoutError("timed out"), REWRITTEN, R.TIMEOUT),
                 (None, "W1 w2 w3.", R.REFUSED))  # words lost: the guard refuses
        typed = ""
        for number, (error, reply, reason) in enumerate(cases, start=1):
            with self.subTest(reason):
                self.ollama.error, self.ollama.reply = error, reply
                self.hold(WORDS)
                outcome = self.app.sessions.outcomes[-1]
                self.assertEqual((outcome.reason, outcome.rewrite), (reason, reason))
                typed += SPOKEN
                self.assertEqual(self.api.received_text(), typed)
                self.assertEqual(self.indicator.last, ("show", ERROR, R.MESSAGES[reason]))
                self.assertEqual(len(self.ollama.calls), number)
        # Nothing to undo after the original was typed.
        self.assertEqual(self.press_undo(), self.refused(UNDO_NOTHING))

    def test_a_dictation_press_during_a_rewrite_is_ignored(self):
        self.ollama.gate = threading.Event()
        self.start()
        self.hold(WORDS, wait=False)
        self.assertTrue(self.ollama.entered.wait(WAIT_S))
        wait_for(lambda: REVIEWING in self.indicator.states, "the reviewing state")
        clicks, made = len(self.api.mouse_calls), len(self.captures.made)
        self.assertEqual(self.ignored_press().reason, S.PREVIOUS_PENDING)
        self.assertEqual((len(self.api.mouse_calls), len(self.captures.made)), (clicks, made))
        self.assertEqual(self.api.received_text(), "")
        self.ollama.gate.set()
        outcomes = self.outcomes(2)
        self.assertEqual([o.reason for o in outcomes], [S.PREVIOUS_PENDING, S.TYPED])
        self.assertEqual(self.api.received_text(), REWRITTEN)
        self.assertEqual(self.indicator.last, ("hide",))
        self.assertEqual(len(self.ollama.calls), 1)

    def test_start_stop_start_with_the_rewrite_and_undo(self):
        self.start()
        self.hold(WORDS)
        self.assertEqual(self.api.received_text(), REWRITTEN)
        self.app.stop()
        self.assertFalse(self.app.undo.pending)  # stop forgets the rewrite
        self.start()
        self.assertEqual(self.press_undo(), self.refused(UNDO_NOTHING))
        self.hold(WORDS)
        self.assertEqual(self.api.received_text(), REWRITTEN * 2)
        self.assertEqual(self.press_undo(), ("hide",))
        self.assertEqual(self.api.received_text(), REWRITTEN + SPOKEN)
        self.app.stop()
        self.app.stop()
        self.assertEqual(self.kernel.objects, {})


class ClaudeRewriteTest(RewriteCase):
    LINES = "- W1 w2 w3 w4 w5 w6.\n- W7 w8 w9 w10 w11 w12."

    def setUp(self):
        super().setUp()
        self.ollama.reply = self.LINES
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND

    def test_send_in_the_claude_code_panel_types_lines_with_shift_enter_then_one_enter(self):
        self.api.images[CLAUDE_PID] = "C:\\Invented\\Code.exe"
        self.api.titles[CLAUDE_HWND] = "invented - Visual Studio Code [Claude Code]"
        self.start()
        self.hold(WORDS, which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.SENT_ENTER, R.REWRITTEN))
        self.assertEqual(self.api.enter_presses(), [True, False])  # Shift+Enter inside, one Enter at the end
        self.assertEqual(self.api.events[-2].vk, VK_RETURN)  # the Enter comes after the whole text
        self.assertIn("own line", self.ollama.calls[0][0])  # the Claude Code layout
        self.assertEqual(self.press_undo(), self.refused(UNDO_NOTHING))  # sent with Enter: never undone

    def test_the_terminal_gets_one_paragraph_and_no_line_breaks(self):
        self.ollama.reply = REWRITTEN
        self.start()
        self.hold(WORDS, which=XBUTTON2)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER)
        self.assertEqual(self.api.enter_presses(), [False])
        self.assertNotIn("own line", self.ollama.calls[0][0])
        self.assertIn("one paragraph", self.ollama.calls[0][0])

    def test_undo_in_the_claude_code_panel_counts_line_breaks(self):
        self.api.images[CLAUDE_PID] = "C:\\Invented\\Code.exe"
        self.api.titles[CLAUDE_HWND] = "invented - Visual Studio Code [Claude Code]"
        self.start()
        self.hold(WORDS)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.TYPED)
        self.assertEqual(self.api.received_text(), self.LINES)
        self.assertEqual(self.press_undo(), ("hide",))
        self.assertEqual(self.api.backspaces(), len(self.LINES))
        self.assertEqual(self.api.received_text(), SPOKEN)


class UndoKeyTest(RewriteCase):
    def setUp(self):
        super().setUp()
        self.start()
        self.hold(WORDS)
        self.assertEqual(self.api.received_text(), REWRITTEN)

    def test_undo_restores_the_original_and_learning_uses_it(self):
        calls = len(self.api.calls)
        self.assertEqual(self.press_undo(), ("hide",))
        self.assertEqual(self.api.received_text(), SPOKEN)
        self.assertEqual(self.api.backspaces(), len(REWRITTEN))
        self.assertTrue(all(event.vk != VK_RETURN for call in self.api.calls[calls:] for event in call))
        self.assertEqual(self.app.correction_key.last.text, SPOKEN)  # the correction key works on the original
        self.assertEqual(self.app.edits.tracker._dictation.text, SPOKEN)
        # A second press has nothing left to undo.
        self.assertEqual(self.press_undo(), self.refused(UNDO_NOTHING))
        self.assertEqual(self.api.received_text(), SPOKEN)

    def test_refused_after_a_manual_edit(self):
        self.key(True, VK_A)
        self.key(False, VK_A)
        self.assertEqual(self.press_undo(), self.refused(UNDO_EDITED))
        self.assertEqual(self.api.backspaces(), 0)

    def test_refused_in_another_window(self):
        self.api.foreground = OTHER_HWND
        self.assertEqual(self.press_undo(), self.refused(UNDO_OTHER_WINDOW))
        self.assertEqual(self.api.backspaces(), 0)

    def test_refused_after_enter(self):
        self.key(True, VK_RETURN)
        self.key(False, VK_RETURN)
        self.assertEqual(self.press_undo(), self.refused(UNDO_ENTERED))
        self.assertEqual(self.api.backspaces(), 0)

    def test_refused_after_the_time_window(self):
        later = time.monotonic() + self.config.autorewrite.undo_window_s
        with mock.patch.object(self.app.undo, "clock", lambda: later):
            self.assertEqual(self.press_undo(), self.refused(UNDO_EXPIRED))
        self.assertEqual(self.api.backspaces(), 0)

    def test_a_new_dictation_forgets_the_rewrite(self):
        self.hold((1,))
        self.assertEqual(self.press_undo(), self.refused(UNDO_NOTHING))
        self.assertEqual(self.api.backspaces(), 0)

    def test_an_injected_undo_key_is_ignored(self):
        shown = len(self.indicator.calls)
        self.hooks.key(WM_KEYDOWN, F17, flags=LLKHF_INJECTED)
        time.sleep(0.05)
        self.assertEqual(len(self.indicator.calls), shown)
        self.assertTrue(self.app.undo.pending)


class UndoWithoutTrackingTest(RewriteCase):
    def rewriting_app(self, **changes):
        rewrite = dataclasses.replace(self.config.autorewrite, min_words=10, **changes)
        return self.make_app(self.make_config(autorewrite=rewrite), rewrite_client=self.ollama)  # no layout

    def test_without_the_manual_edit_tracker_nothing_is_undone(self):
        self.start()
        self.hold(WORDS)
        self.assertEqual(self.press_undo(), self.refused(UNDO_UNSURE))
        self.assertEqual(self.api.received_text(), REWRITTEN)


class RewriteOffTest(RewriteCase):
    def test_disabled_rewrite_never_calls_ollama(self):
        # Without send_polished bound, nothing is rewritten at all.
        triggers = tuple(dataclasses.replace(t, inputs=()) if t.action == "send_polished" else t
                         for t in self.config.triggers)
        rewrite = dataclasses.replace(self.config.autorewrite, enabled=False, min_words=10)
        quill = self.make_app(self.make_config(autorewrite=rewrite, triggers=triggers), rewrite_client=self.ollama,
                              layout=FakeLayout())
        self.assertIsNone(quill.rewriter)
        self.assertFalse(A.wants_rewriter(quill.config))
        self.start(quill)
        self.hold(WORDS, quill=quill)
        self.assertEqual(self.ollama.calls, [])
        self.assertEqual(self.api.received_text(), SPOKEN)

    def test_disabled_rewrite_still_polishes_mouse_5_only(self):
        quill = self.rewriting_app(enabled=False)
        self.assertIsNotNone(quill.rewriter)  # send_polished is bound in the example
        self.start(quill)
        self.hold(WORDS, quill=quill)  # mouse 4, long: [autorewrite] is off
        self.assertEqual(self.ollama.calls, [])
        self.assertEqual(self.api.received_text(), SPOKEN)


class SendPolishedAppTest(RewriteCase):
    """Mouse 5 always goes through the local model; the middle click never does."""

    def into_claude(self):
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND

    def test_mouse_5_rewrites_a_short_dictation_then_presses_one_enter(self):
        for quill in (self.app, self.rewriting_app(enabled=False, min_words=1000, min_audio_s=120)):
            with self.subTest(enabled=quill.config.autorewrite.enabled):
                self.ollama.calls.clear()
                self.start(quill)
                self.addCleanup(quill.stop)
                self.into_claude()
                self.ollama.reply = "W1 w2 w3."
                self.hold((1, 2, 3), which=XBUTTON2, quill=quill)
                quill.stop()  # one Quill at a time
                outcome = quill.sessions.outcomes[-1]
                self.assertEqual((outcome.action, outcome.reason, outcome.rewrite),
                                 ("send_polished", S.SENT_ENTER, R.REWRITTEN))
                self.assertEqual(len(self.ollama.calls), 1)
                self.assertIn("<dictation>\nw1 w2 w3.\n</dictation>", self.ollama.calls[0][1])
                self.assertIn(REVIEWING, self.indicator.states)
        self.assertEqual(sent_segments(self.api), ["W1 w2 w3.", "W1 w2 w3.", ""])  # Enter after each whole text
        self.assertEqual(self.api.enter_presses(), [False, False])  # one plain Enter each time

    def test_mouse_5_types_the_original_with_the_notice_when_ollama_fails(self):
        self.start()
        self.into_claude()
        self.ollama.error = OllamaError("Ollama unreachable: URLError")
        self.hold((1, 2, 3), which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite, outcome.notice), (S.SENT_ENTER, R.FAILED, R.FAILED))
        self.assertEqual(sent_segments(self.api), ["w1 w2 w3.", ""])  # the original, then Enter
        self.assertEqual(self.api.enter_presses(), [False])
        self.assertEqual(self.indicator.last, ("show", SENT, R.MESSAGES[R.FAILED]))

    def test_mouse_5_rewrite_followed_by_enter_is_never_offered_to_the_undo_key(self):
        self.start()
        self.into_claude()
        self.hold(WORDS, which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.SENT_ENTER, R.REWRITTEN))
        self.assertEqual(self.api.enter_presses(), [False])
        self.assertEqual(self.press_undo(), self.refused(UNDO_NOTHING))

    def test_middle_click_never_calls_ollama_even_when_long(self):
        self.start()
        self.into_claude()
        self.hold(WORDS, which=MIDDLE)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.action, outcome.reason, outcome.rewrite), ("send_raw", S.SENT_ENTER, None))
        self.assertEqual(self.ollama.calls, [])
        self.assertNotIn(REVIEWING, self.indicator.states)
        self.assertEqual(sent_segments(self.api), [SPOKEN, ""])
        self.assertEqual(self.api.enter_presses(), [False])


class SequenceOllama(RewriteOllama):
    """One reply per call, in order (the correction, then the enrichment); the last one repeats."""

    def __init__(self, *replies):
        super().__init__(replies[0])
        self.replies = list(replies)

    def chat(self, model, system, user, max_tokens=None, timeout_s=None):
        self.reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return super().chat(model, system, user, max_tokens, timeout_s)


class FakePacks:
    """``quill.context_pack.ContextPacks`` stand-in: ``get`` records the folders; a pack, None or an error."""

    def __init__(self):
        self.folders = []
        self.pack = SimpleNamespace(summary="Projeto de carteira digital.", terms=("carteira", "saldo"))
        self.error = None

    def get(self, folder):
        self.folders.append(folder)
        if self.error is not None:
            raise self.error
        return self.pack


class PolishAppTest(RewriteCase):
    """Mouse 5 into Claude Code: the detected project's pack, the correction, then the enrichment."""

    ENRICHED = ("Pedido: w1 w2 w3 w4 w5 w6.\nContexto: projeto invented, carteira digital.\n"
                "Restrições:\n- w7 w8 w9\n- w10 w11 w12.")
    PARAGRAPH = ("Pedido: w1 w2 w3 w4 w5 w6. Contexto: projeto invented, carteira digital. "
                 "Restrições: w7 w8 w9; w10 w11 w12.")
    HUB_TITLE = "invented | notes.md - Visual Studio Code [Claude Code]"

    def setUp(self):
        super().setUp()
        self.project = self.folder / "invented"
        self.project.mkdir()
        self.packs = FakePacks()
        self.ollama = SequenceOllama(REWRITTEN, self.ENRICHED)
        context = dataclasses.replace(self.config.project_context, folders=(("invented", self.project),))
        rewrite = dataclasses.replace(self.config.autorewrite, min_words=10)
        self.app = self.make_app(self.make_config(autorewrite=rewrite, project_context=context),
                                 rewrite_client=self.ollama, layout=FakeLayout(), context_packs=self.packs)
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND

    def in_the_panel(self, title=HUB_TITLE):
        self.api.images[CLAUDE_PID] = "C:\\Invented\\Code.exe"
        self.api.titles[CLAUDE_HWND] = title

    def test_the_hub_panel_gets_the_pack_and_the_enriched_lines_then_one_enter(self):
        self.in_the_panel()
        self.start()
        self.hold(WORDS, which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite, outcome.enrichment),
                         (S.SENT_ENTER, R.REWRITTEN, "enrich_enriched"))
        self.assertEqual(self.packs.folders, [self.project, self.project])  # the hints, then the correction
        correct, enrich = self.ollama.calls
        self.assertIn("<project_terms>", correct[1])
        self.assertIn("<project_summary>", enrich[1])
        self.assertIn(f"<dictation>\n{REWRITTEN}\n</dictation>", enrich[1])  # the corrected text is enriched
        self.assertEqual(sent_segments(self.api), [self.ENRICHED, ""])
        self.assertEqual(self.api.enter_presses(), [True, True, True, True, False])  # Shift+Enter, then one Enter
        self.assertEqual(self.indicator.last, ("show", SENT, "Prompt enriquecido e enviado"))
        self.assertIn(("show", REVIEWING, S.ENRICHING), self.indicator.calls)
        self.assertEqual(self.press_undo(), self.refused(UNDO_NOTHING))  # sent with Enter: never undone

    def test_a_second_mouse_5_press_while_the_first_is_corrected_is_ignored(self):
        # The logged case: two mouse 5 presses 1 s apart; the second one used to fail with no_speech.
        self.in_the_panel()
        self.ollama.gate = threading.Event()
        with self.assertLogs("quill", level="INFO") as logs:
            self.start()
            self.hold(WORDS, which=XBUTTON2, wait=False)
            self.assertTrue(self.ollama.entered.wait(WAIT_S))
            clicks, made = len(self.api.mouse_calls), len(self.captures.made)
            outcome = self.ignored_press(XBUTTON2)
            self.assertEqual((outcome.action, outcome.reason), ("send_polished", S.PREVIOUS_PENDING))
            self.assertEqual((len(self.api.mouse_calls), len(self.captures.made)), (clicks, made))
            self.assertEqual(self.api.enter_presses(), [])
            self.ollama.gate.set()
            outcomes = self.outcomes(2)
        self.assertEqual([o.reason for o in outcomes], [S.PREVIOUS_PENDING, S.SENT_ENTER])
        self.assertEqual(outcomes[-1].enrichment, "enrich_enriched")
        self.assertEqual(sent_segments(self.api), [self.ENRICHED, ""])
        self.assertEqual(self.api.enter_presses(), [True, True, True, True, False])  # one Enter, after the text
        self.assertEqual(len(self.ollama.calls), 2)  # one correction and one enrichment
        self.assertNotIn(S.NO_SPEECH, [o.reason for o in self.app.sessions.outcomes])
        text = "\n".join(logs.output)
        self.assertIn("ignored (previous_pending)", text)
        self.assertNotRegex(text.casefold(), r"\bw[0-9]")

    def test_the_terminal_gets_one_paragraph_with_the_labels(self):
        self.api.titles[CLAUDE_HWND] = "invented - Claude Code"
        self.start()
        self.hold(WORDS, which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, "enrich_enriched"))
        self.assertEqual(self.packs.folders, [self.project, self.project])  # the hints, then the correction
        self.assertIn("one paragraph", self.ollama.calls[0][0])  # the terminal layout of the correction
        self.assertEqual(sent_segments(self.api), [self.PARAGRAPH, ""])
        self.assertEqual(self.api.enter_presses(), [False])

    def test_an_unknown_project_still_enriches_without_a_pack(self):
        self.ollama.replies = [REWRITTEN, "Pedido: w1 w2 w3 w4 w5 w6.\nRestrições: w7 w8 w9 w10 w11 w12."]
        self.in_the_panel("unknown | notes.md - Visual Studio Code [Claude Code]")
        self.start()
        self.hold(WORDS, which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, "enrich_enriched"))
        self.assertEqual(self.packs.folders, [])
        self.assertNotIn("<project_summary>", self.ollama.calls[1][1])
        self.assertEqual(sent_segments(self.api), ["Pedido: w1 w2 w3 w4 w5 w6.\nRestrições: w7 w8 w9 w10 w11 w12.", ""])

    def test_a_failing_pack_or_a_refused_enrichment_never_loses_the_dictation(self):
        self.in_the_panel()
        self.packs.error = OSError("fake")
        self.start()
        self.hold(WORDS, which=XBUTTON2)  # the pack fails: the enrichment reply names a pack word, refused
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite, outcome.enrichment, outcome.notice),
                         (S.SENT_ENTER, R.REWRITTEN, "enrich_refused", "enrich_refused"))
        self.assertEqual(sent_segments(self.api), [REWRITTEN, ""])  # the corrected text
        self.assertEqual(self.indicator.last, ("show", SENT, "Enriquecimento recusado; foi o texto corrigido"))

    def test_mouse_4_and_the_middle_click_keep_todays_behaviour(self):
        self.in_the_panel()
        self.start()
        self.hold(WORDS)  # mouse 4, long: today's rewrite, no pack, no enrichment
        self.assertEqual(self.app.sessions.outcomes[-1].enrichment, None)
        self.assertEqual(len(self.ollama.calls), 1)
        self.assertNotIn("<project_terms>", self.ollama.calls[0][1])
        self.assertEqual(self.api.received_text(), REWRITTEN)
        self.assertEqual(self.press_undo(), ("hide",))  # no Enter: the undo key restores the dictation
        self.assertEqual(self.api.received_text(), SPOKEN)
        self.hold(WORDS, which=MIDDLE)
        self.assertEqual(self.app.sessions.outcomes[-1].enrichment, None)
        self.assertEqual(len(self.ollama.calls), 1)
        self.assertEqual(self.packs.folders, [])

    def test_mouse_5_outside_claude_code_keeps_todays_rewrite(self):
        self.api.under_pointer = self.api.foreground = TARGET.hwnd
        self.start()
        self.hold(WORDS, which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite, outcome.enrichment), (S.NOT_CLAUDE, R.REWRITTEN, None))
        self.assertEqual((len(self.ollama.calls), self.packs.folders), (1, []))
        self.assertEqual(self.api.received_text(), REWRITTEN)
        self.assertEqual(self.press_undo(), ("hide",))  # no Enter: the dictation comes back
        self.assertEqual(self.api.received_text(), SPOKEN)

    # Decoding hints: the project's pack terms for mouse 5 into Claude Code only.

    PROJECT_PROMPT = "Vocabulário: invented, carteira, saldo."

    def prompts(self):
        """The initial prompts the model decoded with, in order."""
        return [call.options.initial_prompt for call in self.model.calls]

    def hold_after_hints(self, words=WORDS):
        """Hold mouse 5 and speak only once the session decodes with its project hints."""
        clicks, made, done = len(self.api.mouse_calls), len(self.captures.made), len(self.app.sessions.outcomes)
        self.assertEqual(self.button(True, XBUTTON2), 1)
        wait_for(lambda: len(self.captures.made) > made, "the capture to start")
        wait_for(lambda: len(self.api.mouse_calls) > clicks, "the click to focus")
        hold = self.app.sessions._active
        wait_for(lambda: getattr(hold.asr, "hints", None) is not None, "the project hints")
        # Reading the window, the project and its pack typed nothing and clicked nothing more.
        self.assertEqual((self.api.calls, len(self.api.mouse_calls), self.api.foreground), ([], clicks + 1, CLAUDE_HWND))
        self.captures.made[-1].push(speech(words))
        self.assertEqual(self.button(False, XBUTTON2), 1)
        self.outcomes(done + 1)

    def test_mouse_5_into_claude_code_decodes_with_the_project_and_its_pack_terms(self):
        self.in_the_panel()
        self.start()
        with self.assertLogs("quill", level="INFO") as logs:
            self.hold_after_hints()
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, "enrich_enriched"))
        self.assertTrue(self.model.calls)
        self.assertEqual(set(self.prompts()), {self.PROJECT_PROMPT})  # every decode after the hints, the final too
        self.assertEqual(self.model.calls[-1].options.hotwords, "invented carteira saldo")
        self.assertEqual(sent_segments(self.api), [self.ENRICHED, ""])
        text = "\n".join(logs.output)
        self.assertIn("decoding hints: project_hints", text)
        self.assertIn("project hints applied", text)
        for private in ("invented", "carteira", "saldo", str(self.project), self.HUB_TITLE, "Code.exe"):
            self.assertNotIn(private.casefold(), text.casefold())

    def test_the_next_dictation_after_project_hints_gets_todays_hints_again(self):
        self.in_the_panel()
        self.start()
        self.hold_after_hints()
        calls = len(self.model.calls)
        self.hold(WORDS)  # mouse 4 into the same panel
        self.assertEqual(set(self.prompts()[calls:]), {None})  # no vocabulary in this test: no hints at all
        self.assertEqual(self.packs.folders, [self.project, self.project])  # mouse 4 asks for no pack

    def test_other_triggers_and_windows_keep_todays_hints(self):
        self.in_the_panel()
        self.start()
        self.hold(WORDS)  # mouse 4 into the panel
        self.hold(WORDS, which=MIDDLE)  # middle click into the panel
        self.api.under_pointer = self.api.foreground = TARGET.hwnd
        self.hold(WORDS, which=XBUTTON2)  # mouse 5 outside Claude Code
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.NOT_CLAUDE)
        self.assertEqual(set(self.prompts()), {None})
        self.assertEqual(self.packs.folders, [])

    def test_a_failing_or_missing_pack_or_project_gives_todays_hints(self):
        cases = {"pack error": ("error", None),
                 "no pack": ("none", None),
                 "unknown project": ("ok", "unknown | notes.md - Visual Studio Code [Claude Code]")}
        self.start()
        for label, (pack, title) in cases.items():
            with self.subTest(label):
                self.in_the_panel(title or self.HUB_TITLE)
                self.packs.error = OSError("fake") if pack == "error" else None
                self.packs.pack = None if pack == "none" else FakePacks().pack
                calls = len(self.model.calls)
                with self.assertLogs("quill", level="INFO") as logs:
                    self.hold(WORDS, which=XBUTTON2)
                self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER)
                self.assertEqual(set(self.prompts()[calls:]), {None})
                self.assertNotIn("project hints applied", "\n".join(logs.output))
        self.assertEqual(sent_segments(self.api)[-1], "")  # each one still sent with Enter

    def test_without_context_packs_mouse_5_enriches_without_a_pack(self):
        self.ollama.replies = [REWRITTEN, "Pedido: w1 w2 w3 w4 w5 w6.\nRestrições: w7 w8 w9 w10 w11 w12."]
        quill = self.make_app(self.app.config, rewrite_client=self.ollama, layout=FakeLayout())  # no packs
        self.assertIsNone(quill.sessions.context_pack)
        self.in_the_panel()
        self.start(quill)
        self.hold(WORDS, which=XBUTTON2, quill=quill)
        outcome = quill.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.enrichment), (S.SENT_ENTER, "enrich_enriched"))
        self.assertNotIn("<project_summary>", self.ollama.calls[1][1])


def tree(element, ancestors, root=CLAUDE_HWND):
    """A fake UI Automation read of the focused element in window ``root``."""
    return Focus(element, ancestors, root)


CLAUDE_FOCUSED = tree(CLAUDE_INPUT, CLAUDE_CHAIN)
# Focused elements that are not the Claude Code input, as read in VS Code (invented names).
NOT_CLAUDE_FOCUSED = {
    "text editor": tree(Node(50026, "native-edit-context"),
                        workbench(Node(50026, "overflow-guard"), Node(50026, "monaco-editor vs-dark"))),
    "terminal": tree(Node(EDIT, "xterm-helper-textarea"), workbench(Node(50026, "terminal xterm"))),
    "markdown preview": tree(Node(50030, "vscode-body"), webview()),
    "settings": tree(Node(EDIT, "monaco-inputbox"), workbench(Node(50026, "settings-editor"))),
    "another window": tree(CLAUDE_INPUT, CLAUDE_CHAIN, root=OTHER_HWND),
    "nothing focused": None,
}


class EditorTabTest(RewriteCase):
    """The Claude Code editor tab (and any VS Code title without the marker): recognised by its focused element."""

    TAB_TITLE = "invented | Invented topic - Visual Studio Code []"
    ENRICHED = PolishAppTest.ENRICHED

    def setUp(self):
        super().setUp()
        self.project = self.folder / "invented"
        self.project.mkdir()
        self.packs = FakePacks()
        self.ollama = SequenceOllama(REWRITTEN, self.ENRICHED)
        self.reader = FakeReader(CLAUDE_FOCUSED)
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.api.images[CLAUDE_PID] = "C:\\Invented\\Code.exe"
        self.api.titles[CLAUDE_HWND] = self.TAB_TITLE
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND
        self.app = self.tab_app()

    def tab_app(self, budget_s=2.0, **changes):
        context = dataclasses.replace(self.config.project_context, folders=(("invented", self.project),))
        rewrite = dataclasses.replace(self.config.autorewrite, min_words=10)
        self.probe = FocusProbe(lambda: self.reader, budget_s=budget_s)
        return self.make_app(self.make_config(autorewrite=rewrite, project_context=context, **changes),
                             rewrite_client=self.ollama, layout=FakeLayout(), context_packs=self.packs,
                             focus_probe=self.probe)

    def test_the_editor_tab_gets_the_pack_the_enrichment_and_one_enter(self):
        self.start()
        with self.assertLogs("quill.app", level="INFO") as logs:
            self.hold(WORDS, which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.rewrite, outcome.enrichment),
                         (S.SENT_ENTER, R.REWRITTEN, "enrich_enriched"))
        self.assertEqual(self.packs.folders, [self.project, self.project])  # the hints, then the correction
        self.assertIn("<project_terms>", self.ollama.calls[0][1])  # context mode
        self.assertEqual(sent_segments(self.api), [self.ENRICHED, ""])
        self.assertEqual(self.api.enter_presses(), [True, True, True, True, False])  # Shift+Enter lines, one Enter
        self.assertEqual(self.reader.reads, 3)  # the hints, before typing, and again before the Enter
        self.assertIn("focus check: claude_code_input (claude-code profile)", "\n".join(logs.output))
        self.assertNotIn("invented", "\n".join(logs.output).replace("quill.app", ""))

    def test_a_title_without_any_marker_gets_enter_too(self):
        self.start()
        for number, title in enumerate(("invented | Invented topic - Visual Studio Code",
                                        "Invented topic - invented - Visual Studio Code [] - Modified"), start=1):
            self.api.titles[CLAUDE_HWND] = title
            self.hold((number,), which=MIDDLE)
            self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER, title)
        self.assertEqual(self.api.enter_presses(), [False, False])

    def test_any_other_focus_types_without_enter(self):
        self.start()
        for label, read in NOT_CLAUDE_FOCUSED.items():
            self.reader.focus = read
            for which in (XBUTTON2, MIDDLE):
                self.hold((1,), which=which)
                outcome = self.app.sessions.outcomes[-1]
                self.assertEqual((outcome.reason, outcome.enrichment), (S.NOT_CLAUDE, None), label)
        self.assertEqual(self.api.enter_presses(), [])
        self.assertEqual(self.packs.folders, [])  # mouse 5 kept today's plain correction

    def test_a_raising_reader_never_gets_enter(self):
        self.reader.error = OSError("fake: COM failure")
        self.start()
        self.hold((1,), which=XBUTTON2)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.NOT_CLAUDE)
        self.assertEqual(self.api.enter_presses(), [])

    def test_a_slow_reader_times_out_without_enter(self):
        self.reader.gate = self.gate
        quill = self.tab_app(budget_s=0.05)
        self.start(quill)
        self.hold((1,), which=XBUTTON2, quill=quill)
        self.assertEqual(quill.sessions.outcomes[-1].reason, S.NOT_CLAUDE)
        self.assertEqual(self.api.enter_presses(), [])

    def withheld(self, change):
        """Mouse 5 into the editor tab; ``change()`` runs once the text is typed, before Enter."""
        self.start()
        self.api.after_send = lambda index: change()
        with self.assertLogs("quill.session", level="WARNING") as logs:
            self.hold((1,), which=MIDDLE)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.reason, outcome.typed), (S.ENTER_WITHHELD, len("w1.")))
        self.assertEqual(self.api.enter_presses(), [])
        self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[S.ENTER_WITHHELD]))
        return "\n".join(logs.output)

    def test_no_enter_when_the_focus_left_the_input_before_enter(self):
        for label, read in NOT_CLAUDE_FOCUSED.items():
            with self.subTest(label):
                self.api.calls.clear()
                self.reader.focus = CLAUDE_FOCUSED

                def leave(read=read):
                    self.reader.focus = read

                self.assertIn("Enter withheld (left_claude_input)", self.withheld(leave))
                self.app.stop()
                self.app = self.tab_app()

    def test_no_enter_when_the_title_now_says_claude_code_but_the_focus_left(self):
        def change():
            self.api.titles[CLAUDE_HWND] = "invented | notes.md - Visual Studio Code [Claude Code]"
            self.reader.focus = NOT_CLAUDE_FOCUSED["text editor"]

        self.assertIn("Enter withheld (left_claude_input)", self.withheld(change))

    def test_no_enter_when_the_check_before_enter_times_out(self):
        self.app = self.tab_app(budget_s=0.2)
        self.assertIn("Enter withheld (left_claude_input)",
                      self.withheld(lambda: setattr(self.reader, "gate", self.gate)))

    def test_no_enter_when_the_tab_became_dirty(self):
        logs = self.withheld(lambda: self.api.titles.__setitem__(CLAUDE_HWND, "● " + self.TAB_TITLE))
        self.assertIn("Enter withheld (editor_dirty)", logs)

    def test_the_sidebar_marker_needs_no_focus_check(self):
        self.api.titles[CLAUDE_HWND] = "invented | notes.md - Visual Studio Code [Claude Code]"
        self.reader.focus = None  # would refuse: never asked
        self.start()
        self.hold((1,), which=MIDDLE)
        self.assertEqual(self.app.sessions.outcomes[-1].reason, S.SENT_ENTER)
        self.assertEqual(self.reader.reads, 0)

    def test_focus_check_off_keeps_the_editor_tab_without_enter(self):
        quill = self.tab_app(claude_code=ClaudeCode(focus_check=False))
        self.start(quill)
        self.hold((1,), which=XBUTTON2, quill=quill)
        self.assertEqual(quill.sessions.outcomes[-1].reason, S.NOT_CLAUDE)
        self.assertEqual((self.reader.reads, self.api.enter_presses()), (0, []))

    def test_ctrl_enter_send_key_in_the_panel_only(self):
        quill = self.tab_app(claude_code=ClaudeCode(send_key="ctrl+enter"))
        self.start(quill)
        self.hold((1,), which=MIDDLE, quill=quill)
        self.assertEqual(quill.sessions.outcomes[-1].reason, S.SENT_ENTER)
        last = [(event.vk, event.is_keyup) for event in self.api.events[-4:]]
        self.assertEqual(last, [(VK_CONTROL, False), (VK_RETURN, False), (VK_RETURN, True), (VK_CONTROL, True)])
        self.assertEqual(sent_segments(self.api), ["w1.", ""])
        # A Claude Code terminal always gets a plain Enter.
        self.api.images[CLAUDE_PID] = "C:\\Invented\\WindowsTerminal.exe"
        self.api.titles[CLAUDE_HWND] = "Claude Code"
        self.api.calls.clear()
        self.hold((2,), which=MIDDLE, quill=quill)
        self.assertEqual(quill.sessions.outcomes[-1].reason, S.SENT_ENTER)
        self.assertNotIn(VK_CONTROL, [event.vk for event in self.api.events])
        self.assertEqual(self.api.enter_presses(), [False])


class WarmModelTest(unittest.TestCase):
    def test_loads_then_decodes_quiet_noise_once(self):
        model = FakeModel()
        warm = A.WarmModel(model)
        warm.load()
        self.assertEqual(model.loads, 1)
        self.assertEqual(len(model.calls), 1)
        self.assertAlmostEqual(model.calls[0].seconds, A.WARMUP_S)

    def test_a_failed_warm_up_is_only_logged(self):
        model = FakeModel()
        model.fail = lambda call: RuntimeError("fake")
        with self.assertLogs("quill.app", level="WARNING"):
            A.WarmModel(model).load()


class CheckTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        config = load_config(None, EXAMPLE_CONFIG)
        self.config = dataclasses.replace(config, vocabulary_path=self.folder / "vocabulary.toml",
                                          corrections_path=self.folder / "corrections.json",
                                          microphone="Invented Microphone")
        self.models = self.folder / "models"
        self.venv = self.folder / ".venv"

    def ready_files(self):
        model = self.models / "faster-whisper-large-v3-turbo"
        model.mkdir(parents=True)
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (model / name).write_bytes(b"")
        for package in A.VENV_PACKAGES:
            (self.venv / "Lib" / "site-packages" / package).mkdir(parents=True)

    def check(self, devices=("Invented Microphone (USB)",), client=None):
        return A.check_readiness(self.config, models_dir=self.models, venv=self.venv, devices=lambda: list(devices),
                                 client=client or FakeOllama(down=False), registry=FakeRegistry(), now=0.0)

    def test_ready(self):
        self.ready_files()
        lines = self.check()
        out = []
        self.assertEqual(A.print_check(lines, out.append), A.EXIT_OK)
        self.assertEqual(out[-1], "Quill is ready.")
        self.assertEqual({line.name for line in lines if line.required},
                         {"model", "venv", "microphone", "vocabulary"})
        self.assertNotIn("Invented", "\n".join(out))  # the microphone name is not printed

    def test_missing_parts_fail(self):
        lines = {line.name: line for line in self.check(devices=("Another Input",))}
        self.assertFalse(lines["model"].ok)
        self.assertFalse(lines["venv"].ok)
        self.assertFalse(lines["microphone"].ok)
        self.assertEqual(A.print_check(list(lines.values()), lambda line: None), A.EXIT_FAILED)

    def test_the_voice_model_is_checked_but_not_required(self):
        self.ready_files()
        line = {line.name: line for line in self.check()}["voice model"]
        self.assertEqual((line.ok, line.required), (False, False))
        self.assertIn("large-v3: files missing in models/faster-whisper-large-v3; voice commands use "
                      "large-v3-turbo", line.detail)
        (self.models / "faster-whisper-large-v3").mkdir()
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (self.models / "faster-whisper-large-v3" / name).write_bytes(b"")
        line = {line.name: line for line in self.check()}["voice model"]
        self.assertEqual((line.ok, line.detail), (True, "large-v3: files present"))
        self.config = dataclasses.replace(self.config, voice=VoiceSettings(model="large-v3-turbo"))
        line = {line.name: line for line in self.check()}["voice model"]
        self.assertEqual((line.ok, line.detail), (True, "large-v3-turbo (the engine model)"))

    def test_ollama_is_required_only_for_the_llm_cleanup(self):
        self.ready_files()
        lines = {line.name: line for line in self.check(client=FakeOllama(down=True))}
        self.assertFalse(lines["ollama"].ok)
        self.assertFalse(lines["ollama"].required)
        self.config = dataclasses.replace(self.config, cleanup_mode="llm")
        lines = self.check(client=FakeOllama(down=True))
        self.assertEqual(A.print_check(lines, lambda line: None), A.EXIT_FAILED)


class MainTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(startup.WinRegistry, "__init__", _forbidden)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = []

    def main(self, *argv, **options):
        options.setdefault("registry", FakeRegistry())
        options.setdefault("kernel", FakeKernel())
        options.setdefault("logging_setup", lambda: None)
        return A.main(list(argv) + ["--config", str(EXAMPLE_CONFIG)], out=self.out.append, **options)

    def test_check_uses_the_probes_only(self):
        seen = []

        def readiness(config, registry=None):
            seen.append(config.engine_model)
            return [A.CheckLine("model", True, "files present")]

        self.assertEqual(self.main("--check", readiness=readiness), A.EXIT_OK)
        self.assertEqual(seen, ["large-v3-turbo"])
        self.assertEqual(self.out[-1], "Quill is ready.")

    def test_startup_commands_use_the_given_registry(self):
        registry = FakeRegistry()
        with mock.patch.object(startup, "launch_command", return_value='"pythonw.exe" "__main__.py"'):
            self.assertEqual(self.main("--install-startup", registry=registry), 0)
            self.assertEqual(registry.values, {"Quill": '"pythonw.exe" "__main__.py"'})
            self.assertEqual(self.main("--remove-startup", registry=registry), 0)
        self.assertEqual(registry.values, {})

    def test_stop_without_a_running_quill(self):
        self.assertEqual(self.main("--stop"), A.EXIT_FAILED)
        self.assertEqual(self.out, ["Quill is not running."])

    def test_invalid_config_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "quill.toml"
            bad.write_text("[input]\nmin_hold_ms = 1\n", encoding="utf-8")
            self.assertEqual(A.main(["--check", "--config", str(bad)], out=self.out.append), A.EXIT_USAGE)

    def test_run_with_fake_parts_and_already_running(self):
        kernel = FakeKernel()
        holder = A.InstanceLock(kernel)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)

        def parts(config):
            return A.Parts(api=FakeWin32(), model=FakeModel(), capture_factory=FakeCaptures(),
                           indicator=FakeIndicator(), instance=A.InstanceLock(kernel), hooks_factory=FakeHooks)

        self.assertEqual(self.main(parts_factory=parts), A.EXIT_ALREADY_RUNNING)
        self.assertEqual(self.out, ["Quill is already running."])


def _forbidden(*args, **kwargs):
    raise AssertionError("tests must use a fake registry, never the real Run key")


class WarmUpAppTest(RewriteCase):
    """The rewrite model loads in the background: at start, and when a mouse 5 hold starts."""

    def into_claude(self):
        self.api.under_pointer = CLAUDE_HWND
        self.api.foreground = CLAUDE_HWND

    def started(self, quill=None):
        quill = self.start(quill)
        wait_for(lambda: self.ollama.lists, "the start warm-up check")  # it first reads what is in memory
        wait_for(lambda: not quill.warmer.busy, "the start warm-up to end")
        return quill

    def test_start_loads_a_model_not_in_memory_on_its_own_thread(self):
        self.ollama.memory = []
        with self.assertLogs("quill", level="INFO") as logs:
            self.started()
        self.assertEqual(self.ollama.warms, [("qwen3:8b", "quill-model-warm-up")])
        self.assertIn("model warm-up (start) done", "\n".join(logs.output))
        self.assertEqual(self.ollama.calls, [])  # nothing is generated

    def test_start_leaves_another_projects_model_alone(self):
        self.ollama.memory = ["other:14b"]
        with self.assertLogs("quill.ollama", level="INFO") as logs:
            self.started()
        self.assertEqual(self.ollama.warms, [])
        self.assertEqual(self.ollama.memory, ["other:14b"])
        self.assertIn("skipped: Ollama holds another model", "\n".join(logs.output))

    def test_only_a_mouse_5_hold_warms_the_model(self):
        self.started()
        self.assertEqual(self.ollama.warms, [])  # already in memory at start
        self.hold(WORDS)  # mouse 4, long: rewritten without a warm-up
        self.hold((1, 2, 3), which=MIDDLE)
        self.assertEqual(self.ollama.warms, [])
        self.assertEqual(len(self.ollama.calls), 1)
        self.into_claude()
        self.ollama.reply = "W1 w2 w3."
        self.hold((1, 2, 3), which=XBUTTON2)
        outcome = self.app.sessions.outcomes[-1]
        self.assertEqual((outcome.action, outcome.reason, outcome.rewrite), ("send_polished", S.SENT_ENTER,
                                                                             R.REWRITTEN))
        # Started by the hold, run on the warm-up thread: never on the hook or session threads.
        self.assertEqual(self.ollama.warms, [("qwen3:8b", "quill-model-warm-up")])

    def test_no_warm_up_without_the_rewrite(self):
        triggers = tuple(dataclasses.replace(t, inputs=()) if t.action == "send_polished" else t
                         for t in self.config.triggers)
        rewrite = dataclasses.replace(self.config.autorewrite, enabled=False)
        self.ollama.memory = []
        quill = self.make_app(self.make_config(autorewrite=rewrite, triggers=triggers), rewrite_client=self.ollama)
        self.assertIsNone(quill.warmer)
        self.start(quill)
        self.hold(WORDS, quill=quill)
        quill.stop()
        self.assertEqual((self.ollama.warms, self.ollama.calls), ([], []))

    def test_with_the_rewrite_off_only_a_mouse_5_hold_warms_the_model(self):
        quill = self.rewriting_app(enabled=False)
        self.ollama.memory = []
        with self.assertLogs("quill", level="INFO") as logs:
            self.start(quill)
            time.sleep(0.2)  # past the moment a start warm-up would begin
        self.assertEqual((self.ollama.warms, self.ollama.lists), ([], 0))
        self.assertNotIn("model warm-up (start)", "\n".join(logs.output))
        self.hold(WORDS, quill=quill)  # mouse 4, long: [autorewrite] is off, no model call
        self.assertEqual((self.ollama.warms, self.ollama.calls), ([], []))
        self.into_claude()
        self.ollama.reply = "W1 w2 w3."
        self.hold((1, 2, 3), which=XBUTTON2, quill=quill)
        self.assertEqual(self.ollama.warms, [("qwen3:8b", "quill-model-warm-up")])
        self.assertEqual(quill.sessions.outcomes[-1].rewrite, R.REWRITTEN)

    def test_a_cold_mouse_5_waits_for_the_load_then_is_corrected(self):
        self.ollama.memory = ["other:14b"]  # left alone at start
        self.started()
        self.into_claude()
        self.ollama.reply = "W1 w2 w3."
        self.ollama.warm_gate = threading.Event()
        self.addCleanup(self.ollama.warm_gate.set)
        done = len(self.app.sessions.outcomes)
        self.hold((1, 2, 3), which=XBUTTON2, wait=False)  # the capture started while the load is held
        wait_for(lambda: self.ollama.warms, "the hold's warm-up")
        time.sleep(0.1)
        self.assertEqual(self.ollama.calls, [])  # the correction waits for the load
        self.ollama.warm_gate.set()
        outcome = self.outcomes(done + 1)[-1]
        self.assertEqual((outcome.reason, outcome.rewrite), (S.SENT_ENTER, R.REWRITTEN))
        self.assertEqual(self.ollama.order, ["warm", "chat"])
        self.assertEqual(sent_segments(self.api), ["W1 w2 w3.", ""])

    def test_a_load_slower_than_the_wait_types_the_dictation_within_the_bound(self):
        quill = self.rewriting_app(load_wait_s=0.3)
        self.ollama.memory = []
        self.ollama.warm_gate = threading.Event()  # the load never ends during the session
        self.addCleanup(self.ollama.warm_gate.set)
        self.ollama.error = TimeoutError("timed out")  # the model call gives up at timeout_s
        self.start(quill)
        wait_for(lambda: self.ollama.warms, "the start warm-up")  # in flight from start
        self.into_claude()
        done = len(quill.sessions.outcomes)
        self.hold((1, 2, 3), which=XBUTTON2, quill=quill, wait=False)
        released = time.monotonic()
        outcome = self.outcomes(done + 1, quill)[-1]
        self.assertLess(time.monotonic() - released, 0.3 + 3.0)  # load_wait_s, then the (fake) timeout
        self.assertEqual((outcome.reason, outcome.rewrite), (S.SENT_ENTER, R.TIMEOUT))
        self.assertEqual(sent_segments(self.api), ["w1 w2 w3.", ""])  # the dictation as it was, then Enter
        self.assertEqual(len(self.ollama.warms), 1)  # at most one warm-up in flight
        self.ollama.warm_gate.set()
        wait_for(lambda: not quill.warmer.busy, "the load to end")
        self.ollama.error = None
        self.ollama.reply = "W1 w2 w3."
        self.hold((1, 2, 3), which=XBUTTON2, quill=quill)
        self.assertEqual(quill.sessions.outcomes[-1].rewrite, R.REWRITTEN)



class ProjectHintsTest(unittest.TestCase):
    """The decoding hints of mouse 5: the project name and pack terms within today's hint budget."""

    NAMES = ("Ana Lima", "nimbus-deck")
    GENERIC = tuple(f"generic{n:02d}" for n in range(40))  # far more than the budget holds

    def setUp(self):
        from quill.profiles import Profiles, WindowInfo
        from quill.projects import Project

        self.vocabulary = V.Vocabulary(names=tuple(V.Entry(name, "name") for name in self.NAMES))
        self.profiles = Profiles(load_config(None, EXAMPLE_CONFIG).profiles)
        self.panel = WindowInfo("Code.exe", "Chrome_WidgetWin_1", "orchard | notes.md - Visual Studio Code [Claude Code]")
        self.editor = WindowInfo("Code.exe", "Chrome_WidgetWin_1", "orchard | notes.md - Visual Studio Code")
        self.found = Project("orchard", Path("C:/invented/orchard"), "vscode_title")
        self.detected = []
        self.pack = SimpleNamespace(summary="", terms=("ledger", "Ledgerly", "invoice", "GlacierPlanCsvTest",
                                                       "build.gradle", "UNAVAILABLE", "API", "hand-off", "generic03"))

    def detector(self, found=True, error=None):
        test = self

        class Detector:
            def detect(self, info, pid, claude_code=False):
                test.detected.append((pid, claude_code))
                if error is not None:
                    raise error
                return test.found if found else None

        return Detector()

    def hints(self, info=None, *, projects=None, pack_for=None):
        return A.project_hints(info or self.panel, 7, profiles=self.profiles,
                               projects=projects if projects is not None else self.detector(),
                               pack_for=pack_for or (lambda folder: self.pack), vocabulary=self.vocabulary,
                               generic_terms=self.GENERIC)

    def words(self, hints):
        return hints.hotwords.split(" ")

    def test_claude_code_gets_the_project_and_its_terms_after_the_names(self):
        from quill.whisper import HINTS_PREFIX, PROJECT_HINT_MAX_CHARS, join_vocabulary

        today = V.whisper_hints(self.vocabulary, (), self.GENERIC)
        hints, reason = self.hints()
        self.assertEqual(reason, A.PROJECT_HINTS)
        self.assertEqual(self.detected, [(7, True)])
        listed = hints.prompt[len(HINTS_PREFIX):-1].split(", ")
        # Names first; then the project and its spoken terms, distinctive spellings first; then today's generic terms.
        self.assertEqual(listed[:7], ["Ana Lima", "nimbus-deck", "orchard", "hand-off", "ledger", "Ledgerly", "invoice"])
        self.assertIn("API", listed)
        for code in ("GlacierPlanCsvTest", "build.gradle", "UNAVAILABLE"):
            self.assertNotIn(code, listed)
        self.assertEqual(listed.count("generic03"), 1)  # already a hint: never twice
        project_part = listed[2:listed.index("generic00")]
        self.assertLessEqual(len(", ".join(project_part)), PROJECT_HINT_MAX_CHARS)
        # Within today's budget: the last generic terms make room.
        self.assertLessEqual(len(", ".join(listed)), V.HINT_MAX_CHARS)
        self.assertEqual(listed[len(listed) - len([w for w in listed if w.startswith("generic")]):],
                         [w for w in today if w.startswith("generic")][: len([w for w in listed if w.startswith("generic")])])
        self.assertLess(len(listed) - len(project_part), len(today))
        self.assertEqual(hints.hotwords, join_vocabulary(listed, separator=" "))
        self.assertIsNone(hints.language)

    def test_the_same_words_decode_exactly_like_the_vocabulary_hints(self):
        from quill.streaming import StreamingTranscriber
        from quill.whisper import session_hints

        today = V.whisper_hints(self.vocabulary, (), self.GENERIC)
        transcriber = StreamingTranscriber(object(), vocabulary=today)
        plain, hinted = transcriber.open(), transcriber.open(hints=session_hints(today))
        self.assertEqual(plain._decode(5, 0, 32000, words=False), hinted._decode(5, 0, 32000, words=False))
        self.assertIsNone(session_hints(()))

    def test_anything_else_gives_todays_hints(self):
        from quill.whisper import SessionHints  # noqa: F401 - the type the app passes on

        cases = {
            "an ordinary editor tab": (dict(info=self.editor), A.HINTS_NOT_CLAUDE_CODE),
            "no window": (dict(info=SimpleNamespace(process="", window_class="", title="", focus=None)),
                          A.HINTS_NOT_CLAUDE_CODE),
            "no project": (dict(projects=self.detector(found=False)), A.HINTS_NO_PROJECT),
            "no pack": (dict(pack_for=lambda folder: None), A.HINTS_NO_PACK),
            "a pack without terms": (dict(pack_for=lambda folder: SimpleNamespace(terms=None)), A.HINTS_NO_PACK),
            "a failing detector": (dict(projects=self.detector(error=OSError("fake"))), A.HINTS_FAILED),
            "a failing pack": (dict(pack_for=mock.Mock(side_effect=OSError("fake"))), A.HINTS_FAILED),
        }
        for label, (kwargs, reason) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.hints(**kwargs), (None, reason))

    def test_project_terms_selection(self):
        from quill.whisper import distinctive_term, project_terms, spoken_term

        self.assertEqual([t for t in ("QuasarSync", "zeta-Ledger", "sha256", "plinth", "Grommet", "API", "x-")
                          if distinctive_term(t)], ["QuasarSync", "zeta-Ledger", "sha256"])
        self.assertEqual([t for t in ("plinth", "HTTP", "HTTPS", "NovaRoute", "GlacierPlanTest", "a.b", "a_b", "a/b")
                          if spoken_term(t)], ["plinth", "HTTP", "NovaRoute"])
        terms = project_terms("orchard", ("plinth", "Plinth", "NovaRoute", "plínth", "", "  ", 42, "table"), known=("table",),
                              max_chars=40)
        self.assertEqual(terms, ["orchard", "NovaRoute", "plinth"])  # folded duplicates and known words skipped
        self.assertEqual(project_terms("orchard", ("one", "two"), known=("Orchard",)), ["one", "two"])
        self.assertEqual(project_terms("orchard", ("abcdefghij",), max_chars=12), ["orchard"])  # the list ends there
        self.assertEqual(project_terms("orchard", (), max_chars=3), [])


if __name__ == "__main__":
    unittest.main()
