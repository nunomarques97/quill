"""Trigger state machine: pure tests, no Win32, no threads."""

from __future__ import annotations

import random
import unittest

from quill.config import Input, load_config
from quill.triggers import (
    BUSY,
    BUTTON,
    CANCEL,
    COMBO,
    CONFIRM,
    INJECTED,
    KEY,
    MAX_HOLD,
    NOT_ACTIVE,
    RELEASE,
    RELEASE_MISSED,
    RELEASE_WITHOUT_PRESS,
    REPEAT,
    REPEAT_GAP_MS,
    SHORT_HOLD,
    START,
    STOP,
    STOPPED,
    Binding,
    InputEvent,
    Signal,
    TriggerMachine,
    bindings_from,
    suppresses,
)
from quill.win32 import QUILL_EXTRA_INFO

XB1, XB2, MIDDLE, LEFT = 0x05, 0x06, 0x04, 0x01
F13, F14, F15 = 0x7C, 0x7D, 0x7E
RCTRL, LCTRL, KEY_C, KEY_A = 0xA3, 0xA2, 0x43, 0x41


def binding(action: str, kind: str, name: str, vk: int) -> Binding:
    item = Input(kind, name, vk)
    return Binding(action, item, suppresses(item))


BINDINGS = {
    (BUTTON, XB1): binding("dictation", BUTTON, "xbutton1", XB1),
    (KEY, F13): binding("dictation", KEY, "f13", F13),
    (KEY, RCTRL): binding("dictation", KEY, "right_ctrl", RCTRL),
    (KEY, F14): binding("command", KEY, "f14", F14),
    (BUTTON, XB2): binding("send_claude", BUTTON, "xbutton2", XB2),
}


def down(kind: str, vk: int, t: float, **extra: object) -> InputEvent:
    return InputEvent(kind, vk, True, t, **extra)


def up(kind: str, vk: int, t: float, **extra: object) -> InputEvent:
    return InputEvent(kind, vk, False, t, **extra)


class Case(unittest.TestCase):
    def machine(self, **options: object) -> TriggerMachine:
        self.ignored: list[tuple[str, str]] = []
        options.setdefault("on_ignore", lambda reason, bound: self.ignored.append((reason, bound.name)))
        return TriggerMachine(BINDINGS, 250, **options)

    @staticmethod
    def kinds(signals: list[Signal]) -> list[tuple[str, str, str]]:
        return [(signal.kind, signal.action, signal.reason) for signal in signals]

    def run_events(self, machine: TriggerMachine, events: list[InputEvent]) -> list[Signal]:
        signals: list[Signal] = []
        for event in events:
            signals += machine.feed(event)
        return signals


class HoldTest(Case):
    def test_long_mouse_hold_starts_confirms_and_stops(self) -> None:
        machine = self.machine()
        signals = machine.feed(down(BUTTON, XB1, 1000))
        self.assertEqual(self.kinds(signals), [(START, "dictation", "")])
        self.assertEqual(signals[0].trigger, "xbutton1")
        self.assertEqual(machine.next_deadline(), 1250)
        self.assertEqual(machine.tick(1249), [])
        confirm = machine.tick(1250)
        self.assertEqual(self.kinds(confirm), [(CONFIRM, "dictation", "")])
        self.assertEqual(confirm[0].held_ms, 250)
        stop = machine.feed(up(BUTTON, XB1, 3000))
        self.assertEqual(self.kinds(stop), [(STOP, "dictation", RELEASE)])
        self.assertEqual(stop[0].held_ms, 2000)
        self.assertTrue(machine.idle)
        self.assertIsNone(machine.next_deadline())

    def test_confirm_is_applied_before_a_late_event(self) -> None:
        # The worker may see the release before its timer fired: the confirm still comes first.
        machine = self.machine()
        machine.feed(down(BUTTON, XB2, 0))
        signals = machine.feed(up(BUTTON, XB2, 900))
        self.assertEqual(self.kinds(signals), [(CONFIRM, "send_claude", ""), (STOP, "send_claude", RELEASE)])
        self.assertEqual(signals[0].at_ms, 250)

    def test_short_hold_cancels_without_confirm(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [down(BUTTON, XB1, 0), up(BUTTON, XB1, 249)])
        self.assertEqual(self.kinds(signals), [(START, "dictation", ""), (CANCEL, "dictation", SHORT_HOLD)])
        self.assertNotIn(CONFIRM, [signal.kind for signal in signals])
        self.assertTrue(machine.idle)

    def test_each_action_uses_its_own_trigger(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [down(KEY, F14, 0), up(KEY, F14, 500)])
        self.assertEqual([signal.action for signal in signals], ["command"] * 3)
        self.assertEqual(signals[0].trigger, "f14")

    def test_invalid_timings_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TriggerMachine(BINDINGS, 0)
        with self.assertRaises(ValueError):
            TriggerMachine(BINDINGS, 500, max_hold_ms=400)
        with self.assertRaises(ValueError):
            TriggerMachine(BINDINGS, 250, verify_ms=0)


class IgnoreTest(Case):
    def test_key_auto_repeat_is_ignored(self) -> None:
        machine = self.machine()
        events = [down(KEY, F13, 0)] + [down(KEY, F13, t) for t in range(500, 2000, 33)] + [up(KEY, F13, 2000)]
        signals = self.run_events(machine, events)
        self.assertEqual(self.kinds(signals), [(START, "dictation", ""), (CONFIRM, "dictation", ""),
                                               (STOP, "dictation", RELEASE)])
        self.assertTrue(all(reason == REPEAT for reason, _ in self.ignored))
        self.assertTrue(self.ignored)

    def test_release_without_press_is_ignored(self) -> None:
        machine = self.machine()
        self.assertEqual(machine.feed(up(BUTTON, XB1, 10)), [])
        self.assertEqual(machine.feed(up(KEY, F13, 20)), [])
        self.assertEqual(self.ignored, [(RELEASE_WITHOUT_PRESS, "xbutton1"), (RELEASE_WITHOUT_PRESS, "f13")])
        self.assertTrue(machine.idle)

    def test_second_trigger_while_one_is_active_is_ignored(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [
            down(BUTTON, XB1, 0),
            down(BUTTON, XB2, 100),  # send_claude while dictation is held
            down(KEY, F14, 150),  # command too
            up(BUTTON, XB2, 400),
            up(KEY, F14, 450),
            up(BUTTON, XB1, 600),
        ])
        self.assertEqual(self.kinds(signals), [(START, "dictation", ""), (CONFIRM, "dictation", ""),
                                               (STOP, "dictation", RELEASE)])
        self.assertEqual(self.ignored, [(BUSY, "xbutton2"), (BUSY, "f14"), (NOT_ACTIVE, "xbutton2"),
                                        (NOT_ACTIVE, "f14")])

    def test_trigger_held_through_the_end_of_another_does_not_start(self) -> None:
        # XButton2 went down while XButton1 was active: after XButton1 ends it is still ignored until released.
        machine = self.machine()
        signals = self.run_events(machine, [down(BUTTON, XB1, 0), down(BUTTON, XB2, 100), up(BUTTON, XB1, 500),
                                            up(BUTTON, XB2, 900), down(BUTTON, XB2, 1000)])
        self.assertEqual([signal.kind for signal in signals], [START, CONFIRM, STOP, START])
        self.assertEqual(signals[-1].action, "send_claude")

    def test_injected_events_are_ignored(self) -> None:
        for extra in ({"injected": True}, {"extra_info": QUILL_EXTRA_INFO}):
            with self.subTest(extra=extra):
                machine = self.machine()
                signals = self.run_events(machine, [down(BUTTON, XB1, 0, **extra), up(BUTTON, XB1, 900, **extra),
                                                    down(KEY, RCTRL, 1000, **extra), up(KEY, RCTRL, 2000, **extra)])
                self.assertEqual(signals, [])
                self.assertEqual([reason for reason, _ in self.ignored], [INJECTED] * 4)
                self.assertTrue(machine.idle)

    def test_injected_other_input_is_not_a_combo(self) -> None:
        # Quill's own click-to-focus (marked, injected) arrives while a keyboard trigger is held.
        machine = self.machine()
        signals = self.run_events(machine, [
            down(KEY, F13, 0),
            down(BUTTON, LEFT, 260, injected=True, extra_info=QUILL_EXTRA_INFO),
            up(BUTTON, LEFT, 261, injected=True, extra_info=QUILL_EXTRA_INFO),
            down(KEY, KEY_A, 300, injected=True),
            up(KEY, F13, 900),
        ])
        self.assertEqual([signal.kind for signal in signals], [START, CONFIRM, STOP])
        self.assertEqual(self.ignored, [])  # other inputs are never reported


class ComboTest(Case):
    def test_other_key_while_keyboard_trigger_is_held_cancels(self) -> None:
        for release_at in (100, 800):  # before and after the confirm
            with self.subTest(release_at=release_at):
                machine = self.machine()
                signals = self.run_events(machine, [down(KEY, RCTRL, 0), down(KEY, KEY_C, release_at),
                                                    up(KEY, KEY_C, release_at + 50), up(KEY, RCTRL, release_at + 90)])
                self.assertEqual(signals[-1].kind, CANCEL)
                self.assertEqual(signals[-1].reason, COMBO)
                self.assertNotIn(STOP, [signal.kind for signal in signals])
                self.assertEqual(self.ignored, [(NOT_ACTIVE, "right_ctrl")])
                self.assertTrue(machine.idle)

    def test_mouse_button_while_keyboard_trigger_is_held_cancels(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [down(KEY, F13, 0), down(BUTTON, LEFT, 400)])
        self.assertEqual(self.kinds(signals)[-1], (CANCEL, "dictation", COMBO))

    def test_keys_while_mouse_trigger_is_held_do_not_cancel(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [down(BUTTON, XB1, 0), down(KEY, LCTRL, 300), up(KEY, LCTRL, 350),
                                            up(BUTTON, XB1, 900)])
        self.assertEqual([signal.kind for signal in signals], [START, CONFIRM, STOP])

    def test_keyboard_trigger_pressed_with_another_key_down_is_ignored(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [down(KEY, LCTRL, 0), down(KEY, F13, 10), up(KEY, F13, 600),
                                            up(KEY, LCTRL, 700)])
        self.assertEqual(signals, [])
        self.assertEqual(self.ignored, [(COMBO, "f13"), (NOT_ACTIVE, "f13")])
        signals = self.run_events(machine, [down(KEY, F13, 1000), up(KEY, F13, 1500)])
        self.assertEqual([signal.kind for signal in signals], [START, CONFIRM, STOP])

    def test_stale_other_key_is_dropped_by_the_live_key_state(self) -> None:
        # A key whose release the hook never saw (for example across the lock screen) must not block triggers.
        live: set[int] = set()
        machine = self.machine(key_state=lambda vk: vk in live)
        machine.feed(down(KEY, KEY_A, 0))
        signals = self.run_events(machine, [down(KEY, F13, 5000), up(KEY, F13, 5600)])
        self.assertEqual([signal.kind for signal in signals], [START, CONFIRM, STOP])


class RecoveryTest(Case):
    def test_missed_release_of_a_passed_through_key_is_recovered_from_the_key_state(self) -> None:
        live = {RCTRL}
        machine = self.machine(key_state=lambda vk: vk in live)
        machine.feed(down(KEY, RCTRL, 0))
        self.assertEqual(machine.next_deadline(), 250)
        self.assertEqual([s.kind for s in machine.tick(250)], [CONFIRM])
        self.assertEqual(machine.tick(500), [])
        live.clear()  # released, but the hook never delivered the key-up
        signals = machine.tick(750)
        self.assertEqual(self.kinds(signals), [(STOP, "dictation", RELEASE_MISSED)])
        self.assertTrue(machine.idle)
        self.assertEqual(machine.feed(up(KEY, RCTRL, 800)), [])
        self.assertEqual(self.ignored, [(RELEASE_WITHOUT_PRESS, "right_ctrl")])

    def test_unconfirmed_missed_release_cancels(self) -> None:
        machine = self.machine(key_state=lambda vk: False, verify_ms=100)
        machine.feed(down(KEY, RCTRL, 0))
        self.assertEqual(self.kinds(machine.tick(100)), [(CANCEL, "dictation", RELEASE_MISSED)])

    def test_feed_never_consults_the_key_state(self) -> None:
        # By the time the worker handles a release, Windows already reports the key as up.
        machine = self.machine(key_state=lambda vk: False)
        signals = self.run_events(machine, [down(KEY, RCTRL, 0), up(KEY, RCTRL, 900)])
        self.assertEqual(self.kinds(signals)[-1], (STOP, "dictation", RELEASE))

    def test_suppressed_inputs_are_not_checked_against_the_key_state(self) -> None:
        # A swallowed event never reaches the key state, which therefore always reads "up".
        machine = self.machine(key_state=lambda vk: False)
        machine.feed(down(BUTTON, XB1, 0))
        machine.feed(down(KEY, F13, 1))  # busy
        self.assertEqual(machine.next_deadline(), 250)
        self.assertEqual([s.kind for s in machine.tick(250)], [CONFIRM])
        self.assertEqual(machine.next_deadline(), 120_000)
        self.assertEqual(machine.tick(5000), [])
        self.assertEqual(machine.active, "dictation")

    def test_second_mouse_down_recovers_a_missed_release(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [down(BUTTON, XB1, 0), down(BUTTON, XB1, 2000), up(BUTTON, XB1, 2100)])
        self.assertEqual(self.kinds(signals), [
            (START, "dictation", ""), (CONFIRM, "dictation", ""), (STOP, "dictation", RELEASE_MISSED),
            (START, "dictation", ""), (CANCEL, "dictation", SHORT_HOLD),
        ])

    def test_key_that_stopped_repeating_counts_as_a_new_press(self) -> None:
        machine = self.machine()
        signals = self.run_events(machine, [down(KEY, F13, 0), down(KEY, F13, 500),
                                            down(KEY, F13, 500 + REPEAT_GAP_MS + 1)])
        self.assertEqual([s.kind for s in signals], [START, CONFIRM, STOP, START])
        self.assertEqual(signals[2].reason, RELEASE_MISSED)

    def test_max_hold_stops_and_the_late_release_is_ignored(self) -> None:
        machine = self.machine(max_hold_ms=10_000)
        machine.feed(down(BUTTON, XB1, 0))
        machine.tick(250)
        self.assertEqual(machine.next_deadline(), 10_000)
        self.assertEqual(self.kinds(machine.tick(10_000)), [(STOP, "dictation", MAX_HOLD)])
        self.assertEqual(machine.feed(up(BUTTON, XB1, 12_000)), [])
        self.assertEqual(self.ignored, [(NOT_ACTIVE, "xbutton1")])
        self.assertEqual([s.kind for s in self.run_events(machine, [down(BUTTON, XB1, 13_000)])], [START])

    def test_max_hold_of_a_repeating_key_ignores_the_repeats(self) -> None:
        machine = self.machine(max_hold_ms=1_000)
        signals = self.run_events(machine, [down(KEY, F13, t) for t in range(0, 3000, 30)] + [up(KEY, F13, 3000)])
        self.assertEqual(self.kinds(signals), [(START, "dictation", ""), (CONFIRM, "dictation", ""),
                                               (STOP, "dictation", MAX_HOLD)])

    def test_reset_cancels_the_active_hold(self) -> None:
        machine = self.machine()
        machine.feed(down(KEY, F14, 0))
        machine.tick(300)
        self.assertEqual(self.kinds(machine.reset(400)), [(CANCEL, "command", STOPPED)])
        self.assertTrue(machine.idle)
        self.assertEqual(machine.feed(up(KEY, F14, 500)), [])

    def test_random_sequences_never_leave_the_machine_stuck(self) -> None:
        inputs = [(BUTTON, XB1), (BUTTON, XB2), (KEY, F13), (KEY, F14), (KEY, RCTRL), (KEY, KEY_A),
                  (BUTTON, LEFT), (KEY, LCTRL)]
        for seed in range(200):
            rng = random.Random(seed)
            live: set[int] = set()
            machine = self.machine(key_state=lambda vk: vk in live, max_hold_ms=20_000)
            signals: list[Signal] = []
            now = 0.0
            for _ in range(rng.randint(1, 40)):
                now += rng.choice((1, 20, 30, 200, 600, 2000))
                kind, vk = rng.choice(inputs)
                is_down = rng.random() < 0.55
                if is_down:
                    live.add(vk)
                else:
                    live.discard(vk)
                injected = rng.random() < 0.1
                signals += machine.feed(InputEvent(kind, vk, is_down, now, injected=injected))
                if rng.random() < 0.3:
                    signals += machine.tick(now)
            live.clear()
            signals += machine.tick(now + 30_000)
            self.assertTrue(machine.idle, seed)
            self.assert_well_formed(signals, seed)

    def assert_well_formed(self, signals: list[Signal], seed: int) -> None:
        """Every hold is start, at most one confirm, then exactly one stop (confirmed) or cancel."""
        state = "idle"
        for signal in signals:
            if signal.kind == START:
                self.assertEqual(state, "idle", seed)
                state = "started"
            elif signal.kind == CONFIRM:
                self.assertEqual(state, "started", seed)
                state = "confirmed"
            elif signal.kind == STOP:
                self.assertEqual(state, "confirmed", seed)
                state = "idle"
            else:
                self.assertIn(state, ("started", "confirmed"), seed)
                state = "idle"
        self.assertEqual(state, "idle", seed)


class SuppressionTest(unittest.TestCase):
    def test_policy(self) -> None:
        for name, vk in (("xbutton1", XB1), ("xbutton2", XB2), ("middle", MIDDLE)):
            self.assertTrue(suppresses(Input(BUTTON, name, vk)), name)
        for number in range(13, 25):
            self.assertTrue(suppresses(Input(KEY, f"f{number}", 0x6F + number)), number)
        for name, vk in (("right_ctrl", 0xA3), ("right_shift", 0xA1), ("right_alt", 0xA5)):
            self.assertFalse(suppresses(Input(KEY, name, vk)), name)

    def test_bindings_from_the_example_config(self) -> None:
        bindings = bindings_from(load_config(None))
        summary = {binding.name: (binding.action, binding.suppress) for binding in bindings.values()}
        self.assertEqual(summary, {
            "xbutton1": ("dictation", True),
            "f13": ("dictation", True),
            "right_ctrl": ("dictation", False),
            "f14": ("command", True),
            "xbutton2": ("send_claude", True),
            "f15": ("send_claude", True),
        })


if __name__ == "__main__":
    unittest.main()
