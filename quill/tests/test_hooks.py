"""Hook layer: classification, suppression, the worker thread and start/stop, all with a fake installer.

No test installs a real hook: ``FakeHooks`` records the callbacks and the
tests call them directly, the way Windows would on the hook thread.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from quill import hooks, win32
from quill.config import load_config
from quill.hooks import HookRouter, HookThread, TriggerHooks, classify_key, classify_mouse
from quill.tests.fakes import FakeHooks, FakeWin32
from quill.triggers import BUTTON, CANCEL, CONFIRM, KEY, START, STOP, STOPPED, InputEvent, Signal, bindings_from
from quill.win32 import (
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    QUILL_EXTRA_INFO,
    WH_KEYBOARD_LL,
    WH_MOUSE_LL,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MBUTTONDOWN,
    WM_RBUTTONUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
)

WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x0200, 0x020A
X1, X2 = 0x0001 << 16, 0x0002 << 16
F13, F14, F16, RCTRL, KEY_C = 0x7C, 0x7D, 0x7F, 0xA3, 0x43
WAIT_S = 5.0

CONFIG = load_config(None)
BINDINGS = bindings_from(CONFIG)


class ClassifyTest(unittest.TestCase):
    def test_keyboard_messages(self) -> None:
        self.assertEqual(classify_key(WM_KEYDOWN, F13, 0, 0, 5.0), InputEvent(KEY, F13, True, 5.0))
        self.assertEqual(classify_key(WM_SYSKEYDOWN, RCTRL, 0, 0, 1.0).down, True)
        self.assertEqual(classify_key(WM_KEYUP, F13, 0, 0, 1.0).down, False)
        self.assertEqual(classify_key(WM_SYSKEYUP, F13, 0, 0, 1.0).down, False)
        self.assertIsNone(classify_key(0x0102, F13, 0, 0, 1.0))  # WM_CHAR never reaches a LL hook
        event = classify_key(WM_KEYDOWN, F13, LLKHF_INJECTED, QUILL_EXTRA_INFO, 1.0)
        self.assertTrue(event.injected)
        self.assertEqual(event.extra_info, QUILL_EXTRA_INFO)

    def test_mouse_messages(self) -> None:
        self.assertEqual(classify_mouse(WM_XBUTTONDOWN, X1, 0, 0, 2.0), InputEvent(BUTTON, 0x05, True, 2.0))
        self.assertEqual(classify_mouse(WM_XBUTTONUP, X2, 0, 0, 2.0), InputEvent(BUTTON, 0x06, False, 2.0))
        self.assertEqual(classify_mouse(WM_LBUTTONDOWN, 0, 0, 0, 2.0).vk, 0x01)
        self.assertEqual(classify_mouse(WM_RBUTTONUP, 0, 0, 0, 2.0).vk, 0x02)
        self.assertEqual(classify_mouse(WM_MBUTTONDOWN, 0, 0, 0, 2.0).vk, 0x04)
        self.assertTrue(classify_mouse(WM_LBUTTONUP, 0, LLMHF_INJECTED, 0, 2.0).injected)
        for message, data in ((WM_MOUSEMOVE, 0), (WM_MOUSEWHEEL, 120 << 16), (WM_XBUTTONDOWN, 3 << 16)):
            self.assertIsNone(classify_mouse(message, data, 0, 0, 2.0), hex(message))

    def test_real_struct_readers(self) -> None:
        # Pure memory reads of structures built here; no hook is involved.
        key = win32.KBDLLHOOKSTRUCT(F13, 0x64, LLKHF_INJECTED, 1234, QUILL_EXTRA_INFO)
        self.assertEqual(win32.LowLevelHooks.keyboard_fields(ctypes.addressof(key)),
                         (F13, LLKHF_INJECTED, QUILL_EXTRA_INFO))
        mouse = win32.MSLLHOOKSTRUCT(win32.wintypes.POINT(3, 4), X2, LLMHF_INJECTED, 99, 7)
        self.assertEqual(win32.LowLevelHooks.mouse_fields(ctypes.addressof(mouse)), (X2, LLMHF_INJECTED, 7))


class RouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.router = HookRouter(BINDINGS, self.events, clock_ms=lambda: 42.0)

    def drain(self) -> list[tuple[InputEvent, bool]]:
        items = []
        while not self.events.empty():
            items.append(self.events.get_nowait())
        return items

    def test_bound_mouse_buttons_are_swallowed_down_and_up(self) -> None:
        for message in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            for data in (X1, X2):
                self.assertTrue(self.router.mouse(message, data, 0, 0), (message, data))
        self.assertEqual([suppressed for _, suppressed in self.drain()], [True] * 4)

    def test_bound_function_keys_are_swallowed(self) -> None:
        for vk in (F13, F14, 0x7E):
            self.assertTrue(self.router.keyboard(WM_KEYDOWN, vk, 0, 0))
            self.assertTrue(self.router.keyboard(WM_KEYUP, vk, 0, 0))

    def test_right_ctrl_other_keys_and_other_buttons_pass(self) -> None:
        self.assertFalse(self.router.keyboard(WM_KEYDOWN, RCTRL, 0, 0))
        self.assertFalse(self.router.keyboard(WM_KEYUP, RCTRL, 0, 0))
        self.assertFalse(self.router.keyboard(WM_KEYDOWN, KEY_C, 0, 0))
        self.assertFalse(self.router.keyboard(WM_KEYDOWN, F16, 0, 0))  # F16 is not bound in the example
        self.assertFalse(self.router.mouse(WM_LBUTTONDOWN, 0, 0, 0))
        items = self.drain()
        self.assertEqual(len(items), 5)
        self.assertFalse(any(suppressed for _, suppressed in items))

    def test_quill_marked_events_are_never_swallowed(self) -> None:
        self.assertFalse(self.router.mouse(WM_XBUTTONDOWN, X1, LLMHF_INJECTED, QUILL_EXTRA_INFO))
        self.assertFalse(self.router.keyboard(WM_KEYDOWN, F13, LLKHF_INJECTED, QUILL_EXTRA_INFO))
        # Injected by another program: still a bound input, so Back never fires.
        self.assertTrue(self.router.mouse(WM_XBUTTONDOWN, X1, LLMHF_INJECTED, 0))

    def test_moves_and_wheel_are_not_queued(self) -> None:
        self.assertFalse(self.router.mouse(WM_MOUSEMOVE, 0, 0, 0))
        self.assertFalse(self.router.mouse(WM_MOUSEWHEEL, 120 << 16, 0, 0))
        self.assertEqual(self.drain(), [])

    def test_events_are_stamped_with_the_router_clock(self) -> None:
        self.router.keyboard(WM_KEYDOWN, F13, 0, 0)
        self.assertEqual(self.drain()[0][0].time_ms, 42.0)


class Recorder:
    """Signal handler that records the thread it ran on."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.signals: queue.Queue[Signal] = queue.Queue()
        self.threads: set[str] = set()
        self.fail_on = fail_on

    def __call__(self, signal: Signal) -> None:
        self.threads.add(threading.current_thread().name)
        self.signals.put(signal)
        if signal.kind == self.fail_on:
            raise RuntimeError("handler failure (test)")

    def next(self) -> Signal:
        return self.signals.get(timeout=WAIT_S)

    def kinds(self, count: int) -> list[str]:
        return [self.next().kind for _ in range(count)]


class ServiceCase(unittest.TestCase):
    def service(self, recorder: Recorder, fake: FakeHooks | None = None, min_hold_ms: float = 50,
                **options: object) -> TriggerHooks:
        self.fake = fake or FakeHooks()
        service = TriggerHooks(BINDINGS, min_hold_ms, recorder, factory=lambda: self.fake, **options)
        self.addCleanup(service.stop)
        return service


class ServiceTest(ServiceCase):
    def test_callbacks_return_the_decision_and_the_worker_emits_the_signals(self) -> None:
        recorder = Recorder()
        service = self.service(recorder)
        service.start()
        self.assertEqual(set(self.fake.hooks), {WH_KEYBOARD_LL, WH_MOUSE_LL})
        self.assertEqual(self.fake.mouse(WM_XBUTTONDOWN, X1), 1)
        self.assertEqual(self.fake.next_calls, [])  # swallowed: not passed on
        self.assertEqual(recorder.kinds(2), [START, CONFIRM])
        self.assertEqual(self.fake.mouse(WM_XBUTTONUP, X1), 1)
        signal = recorder.next()
        self.assertEqual((signal.kind, signal.action, signal.trigger), (STOP, "dictation", "xbutton1"))
        self.assertEqual(recorder.threads, {"quill-triggers"})
        self.assertEqual(self.fake.next_calls, [])

    def test_passed_through_events_call_the_next_hook(self) -> None:
        recorder = Recorder()
        service = self.service(recorder, min_hold_ms=10_000)  # a tap, however slow the test machine is
        service.start()
        self.assertEqual(self.fake.key(WM_KEYDOWN, RCTRL), 0)
        self.assertEqual(self.fake.key(WM_KEYUP, RCTRL), 0)
        self.assertEqual([call[:2] for call in self.fake.next_calls], [(0, WM_KEYDOWN), (0, WM_KEYUP)])
        self.assertEqual(recorder.kinds(2), [START, CANCEL])

    def test_negative_codes_are_passed_on_untouched(self) -> None:
        recorder = Recorder()
        service = self.service(recorder)
        service.start()
        self.assertEqual(self.fake.mouse(WM_XBUTTONDOWN, X1, code=-1), 0)
        self.assertEqual(self.fake.key(WM_KEYDOWN, F13, code=-1), 0)
        self.assertEqual(len(self.fake.next_calls), 2)
        service.stop()
        self.assertTrue(recorder.signals.empty())

    def test_callback_error_passes_the_event_on(self) -> None:
        recorder = Recorder()
        service = self.service(recorder)
        service.start()
        hook = self.fake.hooks[WH_KEYBOARD_LL]
        self.assertEqual(hook(0, WM_KEYDOWN, 999_999), 0)  # unknown lparam: the field reader raises
        self.assertEqual(service.hooks.callback_errors, 1)
        self.assertEqual(len(self.fake.next_calls), 1)

    def test_handler_failure_does_not_stop_the_worker(self) -> None:
        recorder = Recorder(fail_on=START)
        service = self.service(recorder)
        service.start()
        self.fake.mouse(WM_XBUTTONDOWN, X2)
        self.assertEqual(recorder.kinds(2), [START, CONFIRM])
        self.fake.mouse(WM_XBUTTONUP, X2)
        self.assertEqual(recorder.next().kind, STOP)
        self.assertEqual(service.worker.handler_errors, 1)

    def test_machine_failure_resets_the_holds_and_the_worker_goes_on(self) -> None:
        recorder = Recorder()
        service = self.service(recorder)
        service.start()
        machine = service.worker.machine
        original = machine.feed
        calls = [0]

        def feed(event: InputEvent) -> list[Signal]:
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("machine bug (test)")
            return original(event)

        machine.feed = feed  # type: ignore[method-assign]
        self.fake.key(WM_KEYDOWN, F14)
        self.assertEqual(recorder.next().kind, START)
        self.fake.key(WM_KEYUP, F14)  # this one fails
        signal = recorder.next()
        self.assertEqual((signal.kind, signal.reason), (CANCEL, STOPPED))
        self.assertEqual(service.worker.machine_errors, 1)
        self.fake.mouse(WM_XBUTTONDOWN, X1)
        self.assertEqual(recorder.kinds(2), [START, CONFIRM])

    def test_input_observer_sees_bound_inputs_only(self) -> None:
        seen: list[tuple[str, bool, bool]] = []
        recorder = Recorder()
        service = self.service(recorder, on_input=lambda event, bound, suppressed: seen.append(
            (bound.name, event.down, suppressed)))
        service.start()
        self.fake.key(WM_KEYDOWN, KEY_C)
        self.fake.key(WM_KEYUP, KEY_C)
        self.fake.key(WM_KEYDOWN, F14)
        self.assertEqual(recorder.next().kind, START)
        self.fake.key(WM_KEYUP, F14)
        recorder.next()
        self.assertEqual(seen, [("f14", True, True), ("f14", False, True)])

    def test_stop_cancels_an_active_hold_and_removes_the_hooks(self) -> None:
        recorder = Recorder()
        service = self.service(recorder)
        service.start()
        self.fake.key(WM_KEYDOWN, F13)
        self.assertEqual(recorder.kinds(2), [START, CONFIRM])
        service.stop()
        signal = recorder.next()
        self.assertEqual((signal.kind, signal.reason), (CANCEL, STOPPED))
        self.assertEqual(sorted(self.fake.unhooked), sorted(self.fake.handles.values()))
        self.assertFalse(service.running)
        service.stop()  # a second stop is harmless

    def test_start_stop_start(self) -> None:
        recorder = Recorder()
        fakes = [FakeHooks(), FakeHooks()]
        service = TriggerHooks(BINDINGS, 50, recorder, factory=lambda: fakes.pop(0))
        self.addCleanup(service.stop)
        for _ in range(2):
            service.start()
            with self.assertRaises(RuntimeError):
                service.start()
            hook_thread, worker = service.hooks, service.worker
            fake = hook_thread._api
            fake.mouse(WM_XBUTTONDOWN, X1)
            self.assertEqual(recorder.kinds(2), [START, CONFIRM])
            fake.mouse(WM_XBUTTONUP, X1)
            self.assertEqual(recorder.next().kind, STOP)
            service.stop()
            self.assertFalse(hook_thread._thread.is_alive())
            self.assertFalse(worker.alive)
            self.assertEqual(len(fake.unhooked), 2)
        self.assertEqual(fakes, [])

    def test_failed_install_cleans_up_and_a_later_start_works(self) -> None:
        recorder = Recorder()
        broken = FakeHooks(fail_kind=WH_MOUSE_LL)
        attempts = [broken, FakeHooks()]
        service = TriggerHooks(BINDINGS, 50, recorder, factory=lambda: attempts.pop(0))
        self.addCleanup(service.stop)
        with self.assertRaises(OSError):
            service.start()
        self.assertFalse(service.running)
        self.assertEqual(broken.unhooked, [broken.handles[WH_KEYBOARD_LL]])
        service.start()
        self.assertTrue(service.running)

    def test_missed_release_is_recovered_through_the_key_state(self) -> None:
        live = {RCTRL}
        recorder = Recorder()
        service = self.service(recorder, key_state=lambda vk: vk in live, machine_options={"verify_ms": 20})
        service.start()
        self.fake.key(WM_KEYDOWN, RCTRL)
        self.assertEqual(recorder.kinds(2), [START, CONFIRM])
        live.clear()  # the key-up never reaches the hook
        signal = recorder.next()
        self.assertEqual((signal.kind, signal.reason), (STOP, "release_missed"))

    def test_from_config(self) -> None:
        service = TriggerHooks.from_config(CONFIG, Recorder(), factory=FakeHooks)
        self.assertEqual(service.min_hold_ms, CONFIG.min_hold_ms)
        self.assertEqual(dict(service.bindings), BINDINGS)


class SlowHooks(FakeHooks):
    """An installer whose ``set_hook`` of one kind waits for a gate (a slow SetWindowsHookExW)."""

    def __init__(self, slow_kind: int, gate: threading.Event) -> None:
        super().__init__()
        self.slow_kind = slow_kind
        self.gate = gate
        self.loop_ran = False

    def set_hook(self, kind: int, callback: object) -> int:
        if kind == self.slow_kind:
            self.gate.wait(WAIT_S)
        return super().set_hook(kind, callback)

    def run_loop(self) -> None:
        self.loop_ran = True
        super().run_loop()


class CancelledStartTest(unittest.TestCase):
    """A start that timed out never leaves hooks installed once the installer catches up."""

    def finish(self, thread: HookThread, gate: threading.Event) -> None:
        with self.assertRaises(TimeoutError):
            thread.start(timeout_s=0.02)
        thread.stop(timeout_s=0.01)  # the installation is still blocked: nothing to post yet
        gate.set()
        thread._thread.join(WAIT_S)
        self.assertFalse(thread._thread.is_alive())

    def test_slow_factory(self) -> None:
        gate = threading.Event()
        installer = FakeHooks()

        def factory() -> FakeHooks:
            gate.wait(WAIT_S)
            return installer

        self.finish(HookThread(HookRouter(BINDINGS, queue.SimpleQueue()), factory), gate)
        self.assertEqual(installer.hooks, {})
        self.assertEqual(installer.loop_thread, 0)  # the message loop never ran

    def test_slow_first_hook_is_removed_and_the_second_never_installed(self) -> None:
        gate = threading.Event()
        installer = SlowHooks(WH_KEYBOARD_LL, gate)
        self.finish(HookThread(HookRouter(BINDINGS, queue.SimpleQueue()), lambda: installer), gate)
        self.assertEqual(list(installer.hooks), [WH_KEYBOARD_LL])
        self.assertEqual(installer.unhooked, [installer.handles[WH_KEYBOARD_LL]])
        self.assertFalse(installer.loop_ran)

    def test_slow_last_hook_is_removed(self) -> None:
        gate = threading.Event()
        installer = SlowHooks(WH_MOUSE_LL, gate)
        self.finish(HookThread(HookRouter(BINDINGS, queue.SimpleQueue()), lambda: installer), gate)
        self.assertEqual(sorted(installer.unhooked), sorted(installer.handles.values()))
        self.assertEqual(len(installer.unhooked), 2)
        self.assertFalse(installer.loop_ran)


class RealInstallerTest(unittest.TestCase):
    def test_default_factory_is_the_real_installer_and_tests_cannot_reach_it(self) -> None:
        service = TriggerHooks(BINDINGS, 50, lambda signal: None)
        self.assertIs(service.factory, hooks.real_hooks)
        with self.assertRaises(AssertionError):
            hooks.real_hooks()



class SelftestTest(unittest.TestCase):
    """``python -m quill.selftest.triggers``: refusal without its flag and an aggregate of counts only."""

    def test_without_flag_exits_two_and_installs_nothing(self) -> None:
        from quill.selftest import triggers as selftest

        used: list[str] = []

        def trap(name: str) -> mock.Mock:
            return mock.Mock(side_effect=lambda *args, **kwargs: used.append(name))

        for args in ([], ["--seconds", "10"], ["--click-to-focus"]):
            with mock.patch.object(selftest, "run", trap("run")), \
                    mock.patch.object(hooks, "real_hooks", trap("installer")), \
                    mock.patch.object(TriggerHooks, "start", trap("start")), \
                    mock.patch.object(win32, "User32", trap("win32")), \
                    mock.patch.object(threading.Thread, "start", trap("thread")), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = selftest.main(args)
            self.assertEqual(code, 2, args)
            self.assertIn("--allow-desktop-input", err.getvalue())
        self.assertEqual(used, [])

    def test_run_prints_and_writes_trigger_names_and_counts_only(self) -> None:
        from quill.selftest import triggers as selftest

        now = [0.0]
        fake = FakeHooks()
        api = FakeWin32()
        lines: list[str] = []

        def session(seconds: float) -> None:
            steps = [
                (1000, lambda: fake.mouse(WM_XBUTTONDOWN, X1)),
                (1100, lambda: fake.mouse(WM_XBUTTONUP, X1)),  # short tap
                (2000, lambda: fake.mouse(WM_XBUTTONDOWN, X2)),
                (2500, lambda: fake.key(WM_KEYDOWN, KEY_C)),  # other keys are never reported
                (2510, lambda: fake.key(WM_KEYUP, KEY_C)),
                (3000, lambda: fake.mouse(WM_XBUTTONUP, X2)),
                (4000, lambda: fake.key(WM_KEYDOWN, F13, flags=LLKHF_INJECTED)),
                (4001, lambda: fake.key(WM_KEYUP, F13, flags=LLKHF_INJECTED)),
                (5000, lambda: fake.key(WM_KEYDOWN, RCTRL)),
                (5100, lambda: fake.key(WM_KEYDOWN, KEY_C)),  # combo
                (5200, lambda: fake.key(WM_KEYUP, RCTRL)),
                (6000, lambda: fake.key(WM_KEYDOWN, F14)),  # still held when the test ends
            ]
            for at, action in steps:
                now[0] = at
                action()
            now[0] = 6100  # before F14's confirm deadline, so the worker's timer cannot race the stop

        with tempfile.TemporaryDirectory() as folder:
            code = selftest.run(CONFIG, 60, True, Path(folder), api=api, factory=lambda: fake, wait=session,
                                clock_ms=lambda: now[0], out=lines.append)
            files = list(Path(folder).glob("triggers-*.json"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text("utf-8")
        data = json.loads(text)
        self.assertEqual(code, 0)
        self.assertTrue(data["passed"])
        self.assertEqual(data["totals"]["starts"], 4)
        self.assertEqual(data["totals"]["ends"], 4)
        signals = {(row["action"], row["signal"], row["reason"]): row["count"] for row in data["signals"]}
        self.assertEqual(signals, {
            ("dictation", "start", ""): 2,
            ("dictation", "cancel", "short_hold"): 1,
            ("dictation", "cancel", "combo"): 1,
            ("send_claude", "start", ""): 1,
            ("send_claude", "confirm", ""): 1,
            ("send_claude", "stop", "release"): 1,
            ("command", "start", ""): 1,
            ("command", "cancel", "stopped"): 1,
        })
        self.assertEqual(data["clicks"], [{"action": "send_claude", "reason": "clicked", "count": 1}])
        self.assertEqual(len(api.mouse_calls), 1)
        self.assertEqual(api.calls, [])  # nothing typed
        ignored = {(row["trigger"], row["reason"]) for row in data["ignored"]}
        self.assertEqual(ignored, {("f13", "injected"), ("right_ctrl", "not_active")})
        inputs = {(row["trigger"], row["event"]): row["count"] for row in data["inputs"]}
        self.assertEqual(inputs[("xbutton1", "down_suppressed")], 1)
        self.assertEqual(inputs[("right_ctrl", "up_passed")], 1)
        self.assertEqual(inputs[("f13", "injected")], 2)
        output = "\n".join(lines) + text
        for private in ("0x43", '"67"', " 67", "KEY_C", "640", "360"):
            self.assertNotIn(private, output)
        self.assertTrue(any("send_claude xbutton2" in line and "click-to-focus: clicked" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
