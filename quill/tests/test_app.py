"""End-to-end tests of the Quill app with fakes.

The real hook service, trigger machine, click-to-focus, streaming
transcriber, text pipeline, session manager and injector run; the hooks,
Win32 layer, microphone, model, indicator, kernel objects and registry are
fakes. Nothing sends real input, opens a window or the microphone, or reads
the Sponsor's personal files: the config's personal paths point to a
temporary folder. The spoken words are invented bursts (``w1``, ``w2``...).
"""

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from quill import app as A
from quill import inject
from quill import session as S
from quill import startup
from quill.config import EXAMPLE_CONFIG, load_config
from quill.indicator.render import ERROR, LISTENING, LOADING, SENT
from quill.ollama import OllamaError
from quill.tests.fakes import (
    OTHER_HWND,
    TARGET,
    FakeCaptures,
    FakeClock,
    FakeHooks,
    FakeIndicator,
    FakeKernel,
    FakeRegistry,
    FakeWin32,
)
from quill.tests.test_streaming import FakeModel, speech
from quill.win32 import (
    INTEGRITY_MEDIUM,
    VK_RCONTROL,
    VK_XBUTTON1,
    VK_XBUTTON2,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    XBUTTON1,
    XBUTTON2,
)

CLAUDE_HWND = 500
CLAUDE_PID = 50
F13 = 0x7C
F16 = 0x7F
KEY_C = 0x43
WAIT_S = 10.0


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

    def make_app(self, config=None, client=None, kernel=None):
        focus_clock, inject_clock = FakeClock(), FakeClock()
        parts = A.Parts(api=self.api, model=self.model, capture_factory=self.captures, indicator=self.indicator,
                        instance=A.InstanceLock(kernel or self.kernel), hooks_factory=self.new_hooks,
                        client=client, focus_options={"sleep": focus_clock.sleep, "clock": focus_clock},
                        inject_options={"sleep": inject_clock.sleep, "clock": inject_clock})
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
        vk = VK_XBUTTON1 if which == XBUTTON1 else VK_XBUTTON2
        if down:
            self.api.keys_down.add(vk)
        else:
            self.api.keys_down.discard(vk)
        return self.hooks.mouse(WM_XBUTTONDOWN if down else WM_XBUTTONUP, which << 16)

    def key(self, down, vk):
        (self.api.keys_down.add if down else self.api.keys_down.discard)(vk)
        return self.hooks.key(WM_KEYDOWN if down else WM_KEYUP, vk)

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

    def test_overlapping_sessions_capture_at_once_and_type_in_order(self):
        self.start()
        gate = threading.Event()
        self.model.gate, self.model.gate_when = gate, lambda call: True
        self.hold((1,), wait=False)
        wait_for(lambda: self.model.entered.is_set(), "the engine to be busy")
        # The first session is being finalized: the next press records at once.
        second = self.hold((2,), wait=False)
        self.assertTrue(second.started)
        self.assertEqual(len(self.app.sessions.outcomes), 0)
        gate.set()
        self.assertEqual([o.reason for o in self.outcomes(2)], [S.TYPED, S.TYPED])
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


class CorrectionKeyTest(AppCase):
    def test_correction_key_learns_from_the_selection(self):
        self.start()
        self.hold((1, 2))
        with mock.patch.object(self.app.correction_key, "read_selection", return_value="w1 w9."):
            self.assertEqual(self.key(True, F16), 0)  # not swallowed
            self.key(False, F16)
            wait_for(lambda: self.config.corrections_path.exists(), "the learned correction")
        data = json.loads(self.config.corrections_path.read_text(encoding="utf-8"))
        self.assertTrue(data)


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


if __name__ == "__main__":
    unittest.main()
