"""Push-to-talk trigger state machine: pure, with no Win32 and no threads.

The hook layer turns every keyboard event and mouse-button event into an
``InputEvent`` and the worker feeds it here. The machine emits ``Signal``s per
action (``dictation``, ``command``, ``send_claude``):

- ``start`` when a bound input goes down (audio capture can begin at once);
- ``confirm`` when the hold passes ``min_hold_ms`` (click-to-focus happens here);
- ``stop`` when a confirmed hold ends (reason ``release``, ``max_hold`` or
  ``release_missed``): transcribe and type;
- ``cancel`` when a hold ends before it counts (``short_hold``), when another
  key or button is pressed while a keyboard trigger is held (``combo``, also
  after the confirm), when an unconfirmed hold loses its release
  (``release_missed``) or when the hooks stop (``stopped``): nothing is typed,
  and before the confirm nothing is clicked either.

Only one hold is active at a time. Ignored: injected events (flagged by
Windows or carrying Quill's marker), key auto-repeat, a release without a
press, a second trigger while one is held and a keyboard trigger pressed while
another key is already down. A missed release is recovered so the machine
never stays stuck: an input that passes through to Windows is checked against
the live key state, a second down of a mouse button or a key that stopped
auto-repeating counts as a new press, and every hold ends at ``max_hold_ms``.

``suppresses`` holds the static suppression policy the hook applies: bound
mouse buttons (so Back/Forward never fire) and bound non-modifier keys such as
F13-F24 are swallowed; modifier keys (Right Ctrl and friends) pass through so
Ctrl shortcuts keep working.

Decisions name the trigger input only; other keys are never reported.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from quill.config import ACTIONS, Config, Input
from quill.win32 import QUILL_EXTRA_INFO

BUTTON = "button"
KEY = "key"

START = "start"
CONFIRM = "confirm"
CANCEL = "cancel"
STOP = "stop"

# Reasons carried by signals and ignore notices.
RELEASE = "release"
SHORT_HOLD = "short_hold"
COMBO = "combo"
MAX_HOLD = "max_hold"
RELEASE_MISSED = "release_missed"
INJECTED = "injected"
REPEAT = "repeat"
RELEASE_WITHOUT_PRESS = "release_without_press"
BUSY = "busy"
NOT_ACTIVE = "not_active"
STOPPED = "stopped"

DEFAULT_MAX_HOLD_MS = 120_000.0
DEFAULT_VERIFY_MS = 250.0
# Windows auto-repeat starts at most 1 s after the press and then repeats at
# least every ~400 ms, so a held key sends a down at least this often.
REPEAT_GAP_MS = 1_500.0

# Keys that stay usable as modifiers: passed through to Windows, never suppressed.
PASS_THROUGH_KEYS = frozenset({0xA1, 0xA3, 0xA5})  # right Shift, right Ctrl, right Alt


@dataclass(frozen=True)
class InputEvent:
    """One normalized hook event: a mouse button or a key going down or up.

    ``time_ms`` is when the event happened; ``age_ms`` (diagnostics only) is
    how long before the hook callback that was, or None when unknown.
    """

    kind: str  # BUTTON or KEY
    vk: int
    down: bool
    time_ms: float
    injected: bool = False
    extra_info: int = 0
    age_ms: float | None = None

    @property
    def is_injected(self) -> bool:
        """Flagged as injected by Windows, or carrying Quill's own marker."""
        return self.injected or self.extra_info == QUILL_EXTRA_INFO


@dataclass(frozen=True)
class Binding:
    """A bound input, the action it triggers and whether the hook swallows it."""

    action: str
    input: Input
    suppress: bool

    @property
    def name(self) -> str:
        return self.input.name


@dataclass(frozen=True)
class Signal:
    kind: str  # START, CONFIRM, CANCEL or STOP
    action: str
    trigger: str  # name of the bound input, for example "xbutton1"
    at_ms: float
    reason: str = ""
    held_ms: float = 0.0


def suppresses(item: Input) -> bool:
    """Static policy: bound mouse buttons and bound non-modifier keys are swallowed."""
    return item.kind == BUTTON or item.vk not in PASS_THROUGH_KEYS


def bindings_from(config: Config) -> dict[tuple[str, int], Binding]:
    """Every bound input of the enabled triggers, keyed by (kind, vk)."""
    bindings: dict[tuple[str, int], Binding] = {}
    for trigger in config.triggers:
        if trigger.action not in ACTIONS:
            raise ValueError(f"unknown trigger action: {trigger.action}")
        for item in trigger.inputs:
            bindings[(item.kind, item.vk)] = Binding(trigger.action, item, suppresses(item))
    return bindings


@dataclass
class _Hold:
    binding: Binding
    key: tuple[str, int]
    down_ms: float
    confirmed: bool = False
    next_verify_ms: float = 0.0


IgnoreObserver = Callable[[str, Binding], None]


class TriggerMachine:
    """Turns normalized input events into start/confirm/cancel/stop signals.

    ``key_state(vk)`` (optional) returns whether a key is down right now; it
    is used only for inputs that pass through to Windows, because a
    suppressed event never reaches the live key state. ``on_ignore(reason,
    binding)`` is told about ignored events of bound inputs only.
    """

    def __init__(
        self,
        bindings: Mapping[tuple[str, int], Binding],
        min_hold_ms: float,
        *,
        max_hold_ms: float = DEFAULT_MAX_HOLD_MS,
        verify_ms: float = DEFAULT_VERIFY_MS,
        key_state: Callable[[int], bool] | None = None,
        on_ignore: IgnoreObserver | None = None,
    ) -> None:
        if not 0 < min_hold_ms < max_hold_ms:
            raise ValueError("min_hold_ms must be positive and below max_hold_ms")
        if verify_ms <= 0:
            raise ValueError("verify_ms must be positive")
        self.bindings = dict(bindings)
        self.min_hold_ms = float(min_hold_ms)
        self.max_hold_ms = float(max_hold_ms)
        self.verify_ms = float(verify_ms)
        self.key_state = key_state
        self.on_ignore = on_ignore
        self._active: _Hold | None = None
        # Bound inputs we saw go down and not up yet: time of the latest down.
        self._held: dict[tuple[str, int], float] = {}
        # Other keys currently down (for combos); never reported.
        self._other_keys: set[int] = set()

    # ------------------------------------------------------------ state

    @property
    def active(self) -> str | None:
        """The action of the current hold, or None."""
        return self._active.binding.action if self._active else None

    @property
    def idle(self) -> bool:
        return self._active is None

    def next_deadline(self) -> float | None:
        """When ``tick`` must run next (ms, same clock as events), or None while idle."""
        hold = self._active
        if hold is None:
            return None
        deadlines = [hold.down_ms + self.max_hold_ms]
        if not hold.confirmed:
            deadlines.append(hold.down_ms + self.min_hold_ms)
        if self._verifiable(hold.binding):
            deadlines.append(hold.next_verify_ms)
        return min(deadlines)

    # ------------------------------------------------------------ input

    def feed(self, event: InputEvent) -> list[Signal]:
        """Process one event; returns the signals it causes, in order."""
        key = (event.kind, event.vk)
        binding = self.bindings.get(key)
        if event.is_injected:
            if binding is not None:
                self._ignore(INJECTED, binding)
            return []
        # Deadlines that passed before this event apply first. The live key
        # state is not consulted here: it describes now, not the event's time.
        signals = self.tick(event.time_ms, verify=False)
        if binding is None:
            signals += self._other(event)
        elif event.down:
            signals += self._press(event, key, binding)
        else:
            signals += self._release(event, key, binding)
        return signals

    def tick(self, now_ms: float, *, verify: bool = True) -> list[Signal]:
        """Apply time: confirm a hold, end it at max hold, verify a passed-through key.

        Call it with ``verify`` only once every queued event has been fed:
        Windows updates the key state after the hook saw the release.
        """
        hold = self._active
        if hold is None:
            return []
        signals: list[Signal] = []
        confirm_at = hold.down_ms + self.min_hold_ms
        if not hold.confirmed and now_ms >= confirm_at:
            hold.confirmed = True
            signals.append(self._signal(CONFIRM, hold, confirm_at))
        end_at = hold.down_ms + self.max_hold_ms
        if now_ms >= end_at:
            # The input may still be down: keep it in _held so its repeats and release are ignored.
            signals.append(self._end(hold, end_at, MAX_HOLD))
            return signals
        if verify and self._verifiable(hold.binding) and now_ms >= hold.next_verify_ms:
            hold.next_verify_ms = now_ms + self.verify_ms
            if not self._is_down(hold.binding.input.vk):
                self._held.pop(hold.key, None)
                signals.append(self._end(hold, now_ms, RELEASE_MISSED))
        return signals

    def reset(self, now_ms: float) -> list[Signal]:
        """Forget every hold (for example when the hooks stop); an active hold is cancelled."""
        signals: list[Signal] = []
        if self._active is not None:
            hold = self._active
            self._active = None
            signals.append(self._signal(CANCEL, hold, now_ms, STOPPED))
        self._held.clear()
        self._other_keys.clear()
        return signals

    # ------------------------------------------------------------ handlers

    def _other(self, event: InputEvent) -> list[Signal]:
        """A key or button that no trigger uses: tracked for combos, never reported."""
        if event.kind == KEY:
            if event.down:
                self._other_keys.add(event.vk)
            else:
                self._other_keys.discard(event.vk)
        hold = self._active
        if event.down and hold is not None and hold.binding.input.kind == KEY:
            return [self._end(hold, event.time_ms, COMBO)]
        return []

    def _press(self, event: InputEvent, key: tuple[str, int], binding: Binding) -> list[Signal]:
        signals: list[Signal] = []
        last = self._held.get(key)
        if last is not None:
            if event.kind == KEY and event.time_ms - last <= REPEAT_GAP_MS:
                self._held[key] = event.time_ms
                self._ignore(REPEAT, binding)
                return signals
            # A mouse button went down twice, or a key stopped repeating: its release was missed.
            del self._held[key]
            hold = self._active
            if hold is not None and hold.key == key:
                signals.append(self._end(hold, event.time_ms, RELEASE_MISSED))
        self._held[key] = event.time_ms
        if self._active is not None:
            self._ignore(BUSY, binding)
            return signals
        if event.kind == KEY and self._keys_down():
            self._ignore(COMBO, binding)
            return signals
        hold = _Hold(binding, key, event.time_ms, next_verify_ms=event.time_ms + self.verify_ms)
        self._active = hold
        signals.append(self._signal(START, hold, event.time_ms))
        return signals

    def _release(self, event: InputEvent, key: tuple[str, int], binding: Binding) -> list[Signal]:
        if self._held.pop(key, None) is None:
            self._ignore(RELEASE_WITHOUT_PRESS, binding)
            return []
        hold = self._active
        if hold is None or hold.key != key:
            self._ignore(NOT_ACTIVE, binding)
            return []
        return [self._end(hold, event.time_ms, RELEASE)]

    # ------------------------------------------------------------ helpers

    def _end(self, hold: _Hold, at_ms: float, reason: str) -> Signal:
        """Close the active hold: stop when confirmed, cancel otherwise (short holds report short_hold).

        A combo always cancels, also after the confirm.
        """
        self._active = None
        if hold.confirmed and reason != COMBO:
            return self._signal(STOP, hold, at_ms, reason)
        return self._signal(CANCEL, hold, at_ms, SHORT_HOLD if reason == RELEASE else reason)

    def _signal(self, kind: str, hold: _Hold, at_ms: float, reason: str = "") -> Signal:
        return Signal(kind, hold.binding.action, hold.binding.name, at_ms, reason, max(0.0, at_ms - hold.down_ms))

    def _verifiable(self, binding: Binding) -> bool:
        return self.key_state is not None and not binding.suppress

    def _is_down(self, vk: int) -> bool:
        try:
            return bool(self.key_state(vk)) if self.key_state is not None else True
        except Exception:  # noqa: BLE001 - an unreadable state never ends a hold by itself
            return True

    def _keys_down(self) -> bool:
        """Whether another key is down; entries Windows reports as up are dropped (missed releases)."""
        if self.key_state is not None:
            self._other_keys = {vk for vk in self._other_keys if self._is_down(vk)}
        return bool(self._other_keys)

    def _ignore(self, reason: str, binding: Binding) -> None:
        if self.on_ignore is not None:
            self.on_ignore(reason, binding)
