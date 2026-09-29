"""Low-level keyboard and mouse hooks that feed the trigger state machine.

Three pieces, so the hook callbacks stay cheap enough that Windows never
drops them (it silently removes a low-level hook that is too slow):

- ``HookRouter``: called by the WH_KEYBOARD_LL / WH_MOUSE_LL callbacks. It
  classifies the event (a key, or a mouse button going down or up; pointer
  moves and the wheel are dropped), enqueues it and returns the static
  suppression decision. Constant work: no locks, no I/O, no logging.
- ``TriggerWorker``: a thread that drains the queue into ``TriggerMachine``
  (``quill.triggers``), runs its timers and hands the signals to the
  application's handler. Handler errors are logged and never stop it.
- ``HookThread``: installs both hooks on its own thread and pumps its
  messages until stopped. It creates the real ``LowLevelHooks`` only through
  the factory it is given; tests pass a fake and never install a hook.

``TriggerHooks`` wires them together and can be started, stopped and
started again. An optional ``on_event`` observer sees every key and button
event on the worker thread (the app's manual-edit detection and correction
key); it keeps what it needs in memory. Logs name trigger inputs and reasons
only, never other keys.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from quill.config import Config
from quill.triggers import BUTTON, KEY, Binding, InputEvent, Signal, TriggerMachine, bindings_from
from quill.win32 import (
    HC_ACTION,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    QUILL_EXTRA_INFO,
    VK_LBUTTON,
    VK_MBUTTON,
    VK_RBUTTON,
    VK_XBUTTON1,
    VK_XBUTTON2,
    WH_KEYBOARD_LL,
    WH_MOUSE_LL,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MBUTTONDOWN,
    WM_MBUTTONUP,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    XBUTTON1,
    XBUTTON2,
)

log = logging.getLogger("quill.hooks")

KEY_MESSAGES = {WM_KEYDOWN: True, WM_SYSKEYDOWN: True, WM_KEYUP: False, WM_SYSKEYUP: False}
BUTTON_MESSAGES = {
    WM_LBUTTONDOWN: (VK_LBUTTON, True),
    WM_LBUTTONUP: (VK_LBUTTON, False),
    WM_RBUTTONDOWN: (VK_RBUTTON, True),
    WM_RBUTTONUP: (VK_RBUTTON, False),
    WM_MBUTTONDOWN: (VK_MBUTTON, True),
    WM_MBUTTONUP: (VK_MBUTTON, False),
}
XBUTTON_MESSAGES = {WM_XBUTTONDOWN: True, WM_XBUTTONUP: False}
XBUTTON_VKS = {XBUTTON1: VK_XBUTTON1, XBUTTON2: VK_XBUTTON2}

START_TIMEOUT_S = 5.0
STOP_TIMEOUT_S = 5.0


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def classify_key(wparam: int, vk: int, flags: int, extra: int, now_ms: float) -> InputEvent | None:
    """A keyboard hook event as an InputEvent, or None for other messages."""
    down = KEY_MESSAGES.get(wparam)
    if down is None:
        return None
    return InputEvent(KEY, vk, down, now_ms, bool(flags & LLKHF_INJECTED), extra)


def classify_mouse(wparam: int, mouse_data: int, flags: int, extra: int, now_ms: float) -> InputEvent | None:
    """A mouse-button hook event as an InputEvent; moves, wheels and unknown buttons give None."""
    button = BUTTON_MESSAGES.get(wparam)
    if button is not None:
        vk, down = button
    else:
        down = XBUTTON_MESSAGES.get(wparam)
        if down is None:
            return None
        vk = XBUTTON_VKS.get((mouse_data >> 16) & 0xFFFF)
        if vk is None:
            return None
    return InputEvent(BUTTON, vk, down, now_ms, bool(flags & LLMHF_INJECTED), extra)


class HookRouter:
    """What the hook callbacks do: classify, enqueue, return whether to swallow the event.

    Suppression is static (it depends on the bound input only), so the
    callback never waits for the worker. Events carrying Quill's own marker
    are never swallowed.
    """

    def __init__(self, bindings: Mapping[tuple[str, int], Binding], events: queue.SimpleQueue,
                 clock_ms: Callable[[], float] = monotonic_ms) -> None:
        self._suppressed = frozenset(key for key, binding in bindings.items() if binding.suppress)
        self._events = events
        self._clock_ms = clock_ms

    def _route(self, event: InputEvent | None) -> bool:
        if event is None:
            return False
        suppress = (event.kind, event.vk) in self._suppressed and event.extra_info != QUILL_EXTRA_INFO
        self._events.put((event, suppress))
        return suppress

    def keyboard(self, wparam: int, vk: int, flags: int, extra: int) -> bool:
        return self._route(classify_key(wparam, vk, flags, extra, self._clock_ms()))

    def mouse(self, wparam: int, mouse_data: int, flags: int, extra: int) -> bool:
        return self._route(classify_mouse(wparam, mouse_data, flags, extra, self._clock_ms()))


SignalHandler = Callable[[Signal], None]
InputObserver = Callable[[InputEvent, Binding, bool], None]
# Sees every key and mouse-button event (manual-edit detection, correction key).
# It keeps what it needs in memory and never logs keys.
EventObserver = Callable[[InputEvent], None]

_STOP = object()


class TriggerWorker:
    """Runs the trigger machine on its own thread and dispatches its signals in order."""

    def __init__(self, machine: TriggerMachine, handler: SignalHandler, events: queue.SimpleQueue,
                 clock_ms: Callable[[], float] = monotonic_ms, on_input: InputObserver | None = None,
                 on_event: EventObserver | None = None) -> None:
        self.machine = machine
        self.handler = handler
        self.events = events
        self.clock_ms = clock_ms
        self.on_input = on_input
        self.on_event = on_event
        self.observer_errors = 0
        self.handler_errors = 0
        self.machine_errors = 0
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("trigger worker already started")
        self._thread = threading.Thread(target=self._run, name="quill-triggers", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """Process what is queued, cancel an active hold, and end the thread."""
        if self._thread is None:
            return
        self.events.put(_STOP)
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            log.error("trigger worker did not stop in time")

    def _run(self) -> None:
        while True:
            deadline = self.machine.next_deadline()
            timeout = None if deadline is None else max(0.0, (deadline - self.clock_ms()) / 1000.0)
            try:
                item = self.events.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            signals: list[Signal] = []
            try:
                if item is not None:
                    event, suppressed = item
                    self._observe(event, suppressed)
                    self._notify(event)
                    signals += self.machine.feed(event)
                # Timers (and the live key-state check) run once the queue is drained.
                deadline = self.machine.next_deadline()
                if deadline is not None and self.events.empty():
                    now = self.clock_ms()
                    if now >= deadline:
                        signals += self.machine.tick(now)
            except Exception:  # noqa: BLE001 - a bug here must not leave a hold stuck or kill the thread
                self.machine_errors += 1
                log.exception("trigger machine failed; holds reset")
                signals += self.machine.reset(self.clock_ms())
            self._dispatch(signals)
        self._dispatch(self.machine.reset(self.clock_ms()))

    def _observe(self, event: InputEvent, suppressed: bool) -> None:
        if self.on_input is None:
            return
        binding = self.machine.bindings.get((event.kind, event.vk))
        if binding is None:
            return  # other keys are never reported
        try:
            self.on_input(event, binding, suppressed)
        except Exception:  # noqa: BLE001
            log.exception("input observer failed")

    def _notify(self, event: InputEvent) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as exc:  # noqa: BLE001 - neither the event nor the message is logged: they may carry keys
            self.observer_errors += 1
            log.error("event observer failed (%s)", type(exc).__name__)

    def _dispatch(self, signals: list[Signal]) -> None:
        for signal in signals:
            log.debug("trigger %s %s (%s) %s", signal.action, signal.kind, signal.trigger, signal.reason)
            try:
                self.handler(signal)
            except Exception:  # noqa: BLE001 - one failed session never stops the triggers
                self.handler_errors += 1
                log.exception("trigger handler failed on %s %s", signal.action, signal.kind)


class HookThread:
    """Installs WH_KEYBOARD_LL and WH_MOUSE_LL on a dedicated thread with a message loop.

    ``stop`` (also after a start that timed out) cancels an installation that
    is still running: the thread checks the flag after each hook and removes
    what it installed, so no hook outlives a failed start. The flag is set
    before ``_api`` is read and ``_api`` before the flag is checked, so either
    ``stop`` posts the quit message or the thread sees the cancellation.
    """

    def __init__(self, router: HookRouter, factory: Callable[[], object]) -> None:
        self.router = router
        self.factory = factory
        self.callback_errors = 0
        self._api: object | None = None
        self._thread_id = 0
        self._callbacks: tuple[object, ...] = ()
        self._ready = threading.Event()
        self._cancelled = threading.Event()
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self, timeout_s: float = START_TIMEOUT_S) -> None:
        if self._thread is not None:
            raise RuntimeError("hook thread already started")
        self._thread = threading.Thread(target=self._run, name="quill-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            self._cancelled.set()
            raise TimeoutError("input hooks were not installed in time")
        if self._error is not None:
            self._thread.join(timeout_s)
            raise self._error

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        if self._thread is None:
            return
        self._cancelled.set()
        if self._api is not None and self._thread_id:
            self._api.post_quit(self._thread_id)
        self._thread.join(timeout_s)
        if self._thread.is_alive():
            log.error("hook thread did not stop in time")

    # The callbacks run inside the message loop of the hook thread.

    def keyboard_proc(self, code: int, wparam: int, lparam: int) -> int:
        api = self._api
        try:
            if code == HC_ACTION and self.router.keyboard(wparam, *api.keyboard_fields(lparam)):
                return 1
        except Exception:  # noqa: BLE001 - never block input because of a bug here
            self.callback_errors += 1
        return api.call_next(code, wparam, lparam)

    def mouse_proc(self, code: int, wparam: int, lparam: int) -> int:
        api = self._api
        try:
            if code == HC_ACTION and self.router.mouse(wparam, *api.mouse_fields(lparam)):
                return 1
        except Exception:  # noqa: BLE001
            self.callback_errors += 1
        return api.call_next(code, wparam, lparam)

    def _run(self) -> None:
        handles: list[int] = []
        try:
            api = self.factory()
            api.ensure_queue()
            self._api = api
            self._thread_id = api.current_thread_id()
            self._callbacks = (api.make_callback(self.keyboard_proc), api.make_callback(self.mouse_proc))
            for kind, callback in ((WH_KEYBOARD_LL, self._callbacks[0]), (WH_MOUSE_LL, self._callbacks[1])):
                if self._cancelled.is_set():
                    break
                handles.append(api.set_hook(kind, callback))
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self._error = exc
            self._unhook(handles)
            self._ready.set()
            return
        if self._cancelled.is_set():
            log.warning("hook installation cancelled")
            self._unhook(handles)
            self._ready.set()
            return
        self._ready.set()
        try:
            api.run_loop()
        finally:
            self._unhook(handles)

    def _unhook(self, handles: list[int]) -> None:
        for handle in handles:
            try:
                self._api.unhook(handle)
            except Exception:  # noqa: BLE001
                log.exception("unhook failed")


def real_hooks() -> object:
    """The real installer; imported late so tests can never reach it by accident."""
    from quill.win32 import LowLevelHooks

    return LowLevelHooks()


@dataclass
class TriggerHooks:
    """Hooks, queue, machine and worker for one run; ``start``/``stop`` can repeat.

    ``key_state`` (for example ``User32.key_down``) lets the machine recover
    a missed release of an input that passes through to Windows.
    """

    bindings: Mapping[tuple[str, int], Binding]
    min_hold_ms: float
    handler: SignalHandler
    factory: Callable[[], object] = real_hooks
    key_state: Callable[[int], bool] | None = None
    clock_ms: Callable[[], float] = monotonic_ms
    on_ignore: Callable[[str, Binding], None] | None = None
    on_input: InputObserver | None = None
    machine_options: Mapping[str, float] | None = None
    on_event: EventObserver | None = None

    def __post_init__(self) -> None:
        self.worker: TriggerWorker | None = None
        self.hooks: HookThread | None = None

    @classmethod
    def from_config(cls, config: Config, handler: SignalHandler, **options: object) -> TriggerHooks:
        return cls(bindings_from(config), config.min_hold_ms, handler, **options)

    @property
    def running(self) -> bool:
        return self.worker is not None

    def start(self) -> None:
        if self.running:
            raise RuntimeError("trigger hooks already running")
        events: queue.SimpleQueue = queue.SimpleQueue()
        machine = TriggerMachine(self.bindings, self.min_hold_ms, key_state=self.key_state,
                                 on_ignore=self.on_ignore, **dict(self.machine_options or {}))
        worker = TriggerWorker(machine, self.handler, events, self.clock_ms, self.on_input, self.on_event)
        hooks = HookThread(HookRouter(self.bindings, events, self.clock_ms), self.factory)
        worker.start()
        try:
            hooks.start()
        except BaseException:
            hooks.stop()
            worker.stop()
            raise
        self.worker, self.hooks = worker, hooks
        log.info("trigger hooks installed (%d inputs)", len(self.bindings))

    def stop(self) -> None:
        """Remove the hooks first, then let the worker finish what was queued."""
        if not self.running:
            return
        hooks, worker = self.hooks, self.worker
        self.hooks = self.worker = None
        try:
            hooks.stop()
        finally:
            worker.stop()
        log.info("trigger hooks removed")
