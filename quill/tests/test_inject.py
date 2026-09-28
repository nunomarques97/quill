"""Injector and clipboard tests with a fake Win32 layer; nothing is injected."""

from __future__ import annotations

import unittest
from unittest import mock

from quill import clipboard, inject
from quill.inject import (
    NEWLINE_SHIFT_ENTER,
    NEWLINE_SPACE,
    InjectOptions,
    Injector,
    Target,
    bursts,
    events_for,
    normalize_text,
)
from quill.tests.fakes import OTHER_HWND, OTHER_PID, TARGET, FakeClock, FakeWin32, injector
from quill.win32 import (
    INTEGRITY_HIGH,
    INTEGRITY_LOW,
    INTEGRITY_SYSTEM,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_CONTROL,
    VK_LWIN,
    VK_MENU,
    VK_RETURN,
    VK_SHIFT,
    KeyEvent,
    User32,
)

ACCENTED = "ã õ ç á é ê í ó ú à Ã Õ Ç Á É Ê Í Ó Ú À â ô «aspas» “curvas” 'simples' \"duplas\" (a) [b] {c} <d> € § º ª"


class GuardTest(unittest.TestCase):
    def test_real_win32_layer_is_disabled_in_tests(self) -> None:
        with self.assertRaises(AssertionError):
            User32()


class NormalizeTest(unittest.TestCase):
    def test_space_policy_turns_line_breaks_into_one_space(self) -> None:
        self.assertEqual(normalize_text("uma linha\nduas\r\n\r\ntrês \n  quatro", NEWLINE_SPACE),
                         "uma linha duas três quatro")

    def test_shift_enter_policy_keeps_each_break(self) -> None:
        self.assertEqual(normalize_text("a\r\nb\rc\n\nd e", NEWLINE_SHIFT_ENTER), "a\nb\nc\n\nd\ne")

    def test_controls_are_dropped_and_tabs_become_spaces(self) -> None:
        text = "ok\x03\x1b[2J\x00\x7f\x85x\tfim\ud800"
        self.assertEqual(normalize_text(text, NEWLINE_SHIFT_ENTER), "ok[2J\nx fim")
        self.assertEqual(normalize_text(text, NEWLINE_SPACE), "ok[2J x fim")

    def test_bidirectional_overrides_are_dropped(self) -> None:
        self.assertEqual(normalize_text("a‮b⁦c⁩d‪e"), "abcde")

    def test_unicode_is_kept(self) -> None:
        self.assertEqual(normalize_text(ACCENTED + " 😀 👩‍💻"), ACCENTED + " 😀 👩‍💻")

    def test_unknown_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_text("x", "enter")
        with self.assertRaises(ValueError):
            InjectOptions(newline="enter")
        with self.assertRaises(ValueError):
            InjectOptions(chunk_chars=0)


class EventsTest(unittest.TestCase):
    def test_bmp_character_is_one_unicode_down_up(self) -> None:
        self.assertEqual(events_for("ç"), [
            KeyEvent(0, 0xE7, KEYEVENTF_UNICODE),
            KeyEvent(0, 0xE7, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ])

    def test_astral_character_is_a_surrogate_pair(self) -> None:
        events = events_for("😀")
        self.assertEqual([event.scan for event in events], [0xD83D, 0xD83D, 0xDE00, 0xDE00])
        self.assertTrue(all(event.is_unicode for event in events))
        self.assertEqual([event.is_keyup for event in events], [False, True, False, True])

    def test_newline_is_shift_enter_never_bare_enter(self) -> None:
        events = events_for("\n")
        self.assertEqual([(event.vk, event.is_keyup) for event in events],
                         [(VK_SHIFT, False), (VK_RETURN, False), (VK_RETURN, True), (VK_SHIFT, True)])
        self.assertFalse(any(event.is_unicode for event in events))

    def test_bursts_split_by_character_and_keep_pairs_together(self) -> None:
        text = "ab😀cd😀e"
        pieces = list(bursts(text, 3))
        self.assertEqual([count for count, _ in pieces], [3, 3, 1])
        for _, events in pieces:
            units = [event.scan for event in events if event.is_unicode and not event.is_keyup]
            self.assertFalse(units and 0xDC00 <= units[0] <= 0xDFFF, "burst starts with a low surrogate")
            self.assertFalse(units and 0xD800 <= units[-1] <= 0xDBFF, "burst ends with a high surrogate")
        with self.assertRaises(ValueError):
            list(bursts(text, 0))


class InjectTest(unittest.TestCase):
    def test_accents_and_surrogate_pairs_travel_in_one_sendinput_call(self) -> None:
        api = FakeWin32()
        text = "Olá, ação 😀 é já 👍"
        result = injector(api).inject(text, TARGET)
        self.assertTrue(result.ok, result)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.received_text(), text)
        self.assertTrue(all(event.is_unicode for event in api.calls[0]))

    def test_types_accented_text_in_chunks_without_touching_clipboard(self) -> None:
        api = FakeWin32()
        api.clip = [(13, "antes".encode("utf-16-le") + b"\0\0")]
        before = clipboard.snapshot(api)
        writes = api.clipboard_writes
        text = (ACCENTED + " ") * 12
        result = injector(api).inject(text, TARGET, InjectOptions(chunk_chars=16))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.typed, len(text))
        self.assertEqual(api.received_text(), text)
        self.assertEqual(len(api.calls), -(-len(text) // 16))
        self.assertTrue(all(len(call) <= 16 * 4 for call in api.calls))
        self.assertFalse(result.clipboard_changed)
        self.assertEqual(api.clipboard_writes, writes)
        self.assertTrue(before.identical(clipboard.snapshot(api)))

    def test_surrogate_pairs_arrive_whole(self) -> None:
        api = FakeWin32()
        text = "😀" * 5 + "x"
        self.assertTrue(injector(api).inject(text, TARGET, InjectOptions(chunk_chars=2)).ok)
        self.assertEqual(api.received_text(), text)

    def test_newline_policy_per_target(self) -> None:
        text = "primeira linha\nsegunda linha"
        api = FakeWin32()
        injector(api).inject(text, TARGET, InjectOptions(newline=NEWLINE_SHIFT_ENTER))
        self.assertEqual(api.received_text(), text)
        self.assertEqual(api.enter_presses(), [True])
        api = FakeWin32()
        injector(api).inject(text, TARGET)  # default policy is space
        self.assertEqual(api.received_text(), "primeira linha segunda linha")
        self.assertFalse(any(event.vk == VK_RETURN for event in api.events))

    def test_no_bare_enter_for_any_line_break(self) -> None:
        for policy in (NEWLINE_SPACE, NEWLINE_SHIFT_ENTER):
            api = FakeWin32()
            injector(api).inject("a\rb\r\nc\nd\x0d e\x0b", TARGET, InjectOptions(newline=policy))
            api.received_text()  # raises on a bare Enter
            self.assertTrue(all(api.enter_presses()), policy)

    def test_control_characters_are_never_sent(self) -> None:
        api = FakeWin32()
        result = injector(api).inject("a\x03b\x1bc\x08d\x7fe\x9bf", TARGET)
        self.assertTrue(result.ok)
        self.assertEqual(api.received_text(), "abcdef")
        self.assertEqual(result.total, 6)
        self.assertTrue(all(event.is_unicode and event.scan >= 0x20 for event in api.events))

    def test_inject_never_presses_enter(self) -> None:
        api = FakeWin32()
        tool = injector(api)
        with mock.patch.object(Injector, "press_enter", side_effect=AssertionError("press_enter called")):
            self.assertTrue(tool.inject("linha\nlinha\r\n", TARGET, InjectOptions(newline=NEWLINE_SHIFT_ENTER)).ok)
            self.assertTrue(tool.inject("linha\nlinha", TARGET).ok)

    def test_foreground_change_between_bursts_aborts_with_typed_count(self) -> None:
        api = FakeWin32()

        def switch(index: int) -> None:
            if index == 1:
                api.foreground = OTHER_HWND

        api.after_send = switch
        result = injector(api).inject("x" * 100, TARGET, InjectOptions(chunk_chars=10))
        self.assertEqual(result.reason, inject.FOREGROUND_CHANGED)
        self.assertEqual((result.typed, result.total), (20, 100))
        self.assertEqual(len(api.calls), 2)
        self.assertNotIn("x", result.detail)

    def test_foreground_change_during_modifier_wait_aborts(self) -> None:
        api = FakeWin32()
        api.keys_down.add(VK_CONTROL)
        clock = FakeClock()
        tool = injector(api, clock)

        def user_switches_window(seconds: float) -> None:
            api.keys_down.clear()
            api.foreground = OTHER_HWND
            clock.now += seconds

        tool.sleep = user_switches_window
        result = tool.inject("abc", TARGET)
        self.assertEqual((result.reason, result.typed, api.calls), (inject.FOREGROUND_CHANGED, 0, []))

    def test_foreground_differs_at_start_types_nothing(self) -> None:
        api = FakeWin32()
        api.foreground = OTHER_HWND
        result = injector(api).inject("olá", TARGET)
        self.assertEqual((result.reason, result.typed, api.calls), (inject.FOREGROUND_CHANGED, 0, []))

    def test_missing_or_reused_window_aborts(self) -> None:
        api = FakeWin32()
        self.assertEqual(injector(api).inject("a", None).reason, inject.NO_TARGET)
        self.assertEqual(injector(api).inject("a", Target(0, 0)).reason, inject.NO_TARGET)
        self.assertEqual(injector(api).inject("a", Target(300, 7)).reason, inject.TARGET_GONE)
        api.windows[TARGET.hwnd] = 9  # handle reused by another process
        self.assertEqual(injector(api).inject("a", TARGET).reason, inject.TARGET_GONE)
        self.assertEqual(api.calls, [])

    def test_window_closed_mid_text_aborts(self) -> None:
        api = FakeWin32()
        api.after_send = lambda index: api.windows.pop(TARGET.hwnd, None)
        result = injector(api).inject("y" * 30, TARGET, InjectOptions(chunk_chars=10))
        self.assertEqual((result.reason, result.typed, len(api.calls)), (inject.TARGET_GONE, 10, 1))

    def test_handle_reused_mid_text_aborts(self) -> None:
        api = FakeWin32()

        def reuse(index: int) -> None:
            # Same handle and still in front, but now owned by another process.
            api.windows[TARGET.hwnd] = OTHER_PID

        api.after_send = reuse
        result = injector(api).inject("y" * 30, TARGET, InjectOptions(chunk_chars=10))
        self.assertEqual((result.reason, result.typed, len(api.calls)), (inject.TARGET_GONE, 10, 1))
        self.assertIn("another process", result.detail)

    def test_elevated_target_is_refused(self) -> None:
        for level in (INTEGRITY_HIGH, INTEGRITY_SYSTEM):
            api = FakeWin32()
            api.integrity[TARGET.pid] = level
            result = injector(api).inject("a", TARGET)
            self.assertEqual((result.reason, api.calls), (inject.TARGET_ELEVATED, []))

    def test_unreadable_integrity_is_refused_unless_elevated(self) -> None:
        api = FakeWin32()
        del api.integrity[TARGET.pid]
        self.assertEqual(injector(api).inject("a", TARGET).reason, inject.TARGET_ACCESS_DENIED)
        api.own = INTEGRITY_HIGH
        self.assertTrue(injector(api).inject("a", TARGET).ok)

    def test_unreadable_own_integrity_is_refused(self) -> None:
        api = FakeWin32()
        api.own = None
        result = injector(api).inject("a", TARGET)
        self.assertEqual((result.reason, api.calls), (inject.TARGET_ACCESS_DENIED, []))

    def test_lower_integrity_target_is_allowed(self) -> None:
        api = FakeWin32()
        api.integrity[TARGET.pid] = INTEGRITY_LOW
        self.assertTrue(injector(api).inject("a", TARGET).ok)

    def test_hung_target_is_refused(self) -> None:
        api = FakeWin32()
        api.hung.add(TARGET.hwnd)
        result = injector(api).inject("a", TARGET)
        self.assertEqual((result.reason, api.calls), (inject.TARGET_NOT_RESPONDING, []))

    def test_target_hanging_mid_text_aborts(self) -> None:
        api = FakeWin32()
        api.after_send = lambda index: api.hung.add(TARGET.hwnd)
        result = injector(api).inject("z" * 25, TARGET, InjectOptions(chunk_chars=10))
        self.assertEqual((result.reason, result.typed, len(api.calls)), (inject.TARGET_NOT_RESPONDING, 10, 1))

    def test_short_sendinput_count_is_reported(self) -> None:
        api = FakeWin32()
        api.short_by = 1
        result = injector(api).inject("abc", TARGET)
        self.assertEqual((result.reason, result.typed), (inject.SENDINPUT_FAILED, 0))
        self.assertIn("error 5", result.detail)
        self.assertEqual(len(api.calls), 1)

    def test_short_sendinput_count_in_later_burst_keeps_typed_count(self) -> None:
        api = FakeWin32()
        api.after_send = lambda index: setattr(api, "short_by", 1 if index == 1 else 0)
        result = injector(api).inject("q" * 20, TARGET, InjectOptions(chunk_chars=10))
        self.assertEqual((result.reason, result.typed, len(api.calls)), (inject.SENDINPUT_FAILED, 10, 2))

    def test_held_modifier_waits_then_aborts(self) -> None:
        for vk in (VK_CONTROL, VK_MENU, VK_LWIN):
            api = FakeWin32()
            api.keys_down.add(vk)
            clock = FakeClock()
            result = injector(api, clock).inject("abc", TARGET, InjectOptions(modifier_wait_s=0.1))
            self.assertEqual((result.reason, api.calls), (inject.MODIFIER_HELD, []))
            self.assertGreaterEqual(clock.now, 0.1)

    def test_shift_does_not_block_unicode_typing(self) -> None:
        api = FakeWin32()
        api.keys_down.add(VK_SHIFT)
        self.assertTrue(injector(api).inject("abc", TARGET).ok)

    def test_modifier_released_during_wait_types(self) -> None:
        api = FakeWin32()
        api.keys_down.add(VK_CONTROL)
        clock = FakeClock()
        tool = injector(api, clock)

        def release(seconds: float) -> None:
            api.keys_down.clear()
            clock.now += seconds

        tool.sleep = release
        self.assertTrue(tool.inject("abc", TARGET).ok)
        self.assertEqual(api.received_text(), "abc")

    def test_clipboard_change_during_typing_is_reported(self) -> None:
        api = FakeWin32()

        def other_program_copies(index: int) -> None:
            api.sequence += 1

        api.after_send = other_program_copies
        result = injector(api).inject("abc", TARGET)
        self.assertTrue(result.ok)
        self.assertTrue(result.clipboard_changed)

    def test_clipboard_change_is_reported_on_abort(self) -> None:
        api = FakeWin32()

        def copy_and_switch(index: int) -> None:
            api.sequence += 1
            api.foreground = OTHER_HWND

        api.after_send = copy_and_switch
        result = injector(api).inject("w" * 20, TARGET, InjectOptions(chunk_chars=10))
        self.assertEqual(result.reason, inject.FOREGROUND_CHANGED)
        self.assertTrue(result.clipboard_changed)

    def test_empty_text_types_nothing(self) -> None:
        api = FakeWin32()
        result = injector(api).inject("\x00\x07", TARGET)
        self.assertEqual((result.reason, result.total, api.calls), (inject.OK, 0, []))

    def test_logs_and_results_never_contain_the_text(self) -> None:
        api = FakeWin32()
        api.after_send = lambda index: setattr(api, "foreground", OTHER_HWND)
        with self.assertLogs("quill.inject", level="INFO") as logs:
            result = injector(api).inject("palavra reservada " * 4, TARGET, InjectOptions(chunk_chars=8))
            injector(FakeWin32()).inject("palavra reservada", TARGET)
        self.assertEqual(result.reason, inject.FOREGROUND_CHANGED)
        self.assertFalse(any("palavra" in line for line in logs.output))
        self.assertNotIn("palavra", repr(result))

    def test_capture_target_reads_foreground(self) -> None:
        api = FakeWin32()
        self.assertEqual(injector(api).capture_target(), TARGET)
        api.foreground = 0
        self.assertIsNone(injector(api).capture_target())


class PressEnterTest(unittest.TestCase):
    def test_presses_one_plain_enter(self) -> None:
        api = FakeWin32()
        result = injector(api).press_enter(TARGET)
        self.assertTrue(result.ok, result)
        self.assertEqual(api.calls, [[KeyEvent(VK_RETURN, 0x1C, 0), KeyEvent(VK_RETURN, 0x1C, KEYEVENTF_KEYUP)]])
        self.assertEqual(api.enter_presses(), [False])

    def test_refused_when_target_is_not_the_foreground(self) -> None:
        api = FakeWin32()
        api.foreground = OTHER_HWND
        result = injector(api).press_enter(TARGET)
        self.assertEqual((result.reason, api.calls), (inject.FOREGROUND_CHANGED, []))

    def test_refused_when_a_modifier_is_held(self) -> None:
        for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN):
            api = FakeWin32()
            api.keys_down.add(vk)
            clock = FakeClock()
            result = injector(api, clock).press_enter(TARGET)
            self.assertEqual((result.reason, api.calls), (inject.MODIFIER_HELD, []), vk)
            self.assertEqual(clock.sleeps, [], "press_enter must refuse without waiting")

    def test_refused_for_missing_gone_reused_hung_or_elevated_targets(self) -> None:
        cases = {
            inject.NO_TARGET: lambda api: None,
            inject.TARGET_GONE: lambda api: api.windows.pop(TARGET.hwnd),
            inject.TARGET_NOT_RESPONDING: lambda api: api.hung.add(TARGET.hwnd),
            inject.TARGET_ELEVATED: lambda api: api.integrity.__setitem__(TARGET.pid, INTEGRITY_HIGH),
        }
        for reason, setup in cases.items():
            api = FakeWin32()
            setup(api)
            target = None if reason == inject.NO_TARGET else TARGET
            result = injector(api).press_enter(target)
            self.assertEqual((result.reason, api.calls), (reason, []), reason)
        api = FakeWin32()
        api.windows[TARGET.hwnd] = OTHER_PID
        self.assertEqual(injector(api).press_enter(TARGET).reason, inject.TARGET_GONE)
        self.assertEqual(api.calls, [])

    def test_short_sendinput_is_reported(self) -> None:
        api = FakeWin32()
        api.short_by = 2
        self.assertEqual(injector(api).press_enter(TARGET).reason, inject.SENDINPUT_FAILED)


class ClipboardTest(unittest.TestCase):
    def filled(self) -> FakeWin32:
        api = FakeWin32()
        api.clip = [
            (13, "texto é".encode("utf-16-le") + b"\0\0"),
            (clipboard.CF_BITMAP, None),
            (8, b"DIB-bytes"),
            (0xC123, b"<html/>"),
        ]
        return api

    def test_snapshot_saves_every_format(self) -> None:
        api = self.filled()
        saved = clipboard.snapshot(api)
        self.assertEqual(saved.format_ids, (13, clipboard.CF_BITMAP, 8, 0xC123))
        self.assertIsNone(saved.formats[1].data)
        self.assertEqual(saved.formats[3].name, "Registered49443")
        self.assertFalse(api.clip_open)

    def test_snapshot_reads_the_sequence_while_the_clipboard_is_open(self) -> None:
        api = self.filled()
        clipboard.snapshot(api)
        self.assertEqual(api.sequence_reads_while_closed, 0)

    def test_handle_and_private_formats_are_recorded_without_data(self) -> None:
        api = self.filled()
        api.clip += [(clipboard.CF_ENHMETAFILE, None), (0x0200, None), (0x02FF, None), (0x0350, None)]
        saved = clipboard.snapshot(api)
        for fmt in (clipboard.CF_BITMAP, clipboard.CF_ENHMETAFILE, 0x0200, 0x02FF, 0x0350):
            self.assertTrue(clipboard.is_handle_format(fmt), fmt)
            self.assertIsNone(next(item.data for item in saved.formats if item.fmt == fmt))
        self.assertFalse(clipboard.is_handle_format(13))
        self.assertFalse(clipboard.is_handle_format(0xC123))

    def test_restore_puts_back_contents_and_formats(self) -> None:
        api = self.filled()
        saved = clipboard.snapshot(api)
        with clipboard.opened(api):
            api.empty_clipboard()
            api.set_clipboard_bytes(1, b"outro")
        self.assertFalse(saved.same_content(clipboard.snapshot(api)))
        skipped = clipboard.restore(api, saved)
        self.assertEqual(skipped, [clipboard.CF_BITMAP])
        after = clipboard.snapshot(api)
        self.assertEqual(after.format_ids, (13, 8, 0xC123))
        self.assertEqual([item.data for item in after.formats], [saved.formats[0].data, b"DIB-bytes", b"<html/>"])
        self.assertFalse(api.clip_open)

    def test_restore_reports_refused_formats(self) -> None:
        api = self.filled()
        saved = clipboard.snapshot(api)
        api.refuse_set.add(8)
        self.assertEqual(clipboard.restore(api, saved), [clipboard.CF_BITMAP, 8])
        self.assertFalse(api.clip_open)

    def test_restore_closes_the_clipboard_when_emptying_fails(self) -> None:
        api = self.filled()
        saved = clipboard.snapshot(api)
        api.empty_clipboard = lambda: False
        with self.assertRaises(clipboard.ClipboardError):
            clipboard.restore(api, saved)
        self.assertFalse(api.clip_open)

    def test_difference_names_formats_only(self) -> None:
        api = self.filled()
        before = clipboard.snapshot(api)
        with clipboard.opened(api):
            api.empty_clipboard()
            api.set_clipboard_bytes(13, "conteúdo novo".encode("utf-16-le"))
        after = clipboard.snapshot(api)
        text = clipboard.describe_difference(before, after)
        self.assertIn("format data changed 13", text)
        self.assertIn("formats removed", text)
        self.assertNotIn("conteúdo", text)
        self.assertEqual(clipboard.describe_difference(before, before), "identical")

    def test_sequence_change_alone_is_not_identical(self) -> None:
        api = self.filled()
        before = clipboard.snapshot(api)
        api.sequence += 1
        after = clipboard.snapshot(api)
        self.assertTrue(before.same_content(after))
        self.assertFalse(before.identical(after))
        self.assertEqual(clipboard.describe_difference(before, after), "sequence number changed")

    def test_busy_clipboard_is_retried_then_fails(self) -> None:
        api = self.filled()
        api.open_failures = 2
        self.assertEqual(len(clipboard.snapshot(api, sleep=lambda _: None).formats), 4)
        api.open_failures = 5
        with self.assertRaises(clipboard.ClipboardError):
            clipboard.snapshot(api, attempts=3, sleep=lambda _: None)
        self.assertFalse(api.clip_open)


if __name__ == "__main__":
    unittest.main()
