"""quill.edits with fake keyboards and invented text; no hook, no real key."""

import tempfile
import unittest
from pathlib import Path

from quill import edits
from quill.corrections import CorrectionStore, Dictation, Learner
from quill.edits import (
    BACKSPACE,
    CHAR,
    CTRL_BACKSPACE,
    DELETE,
    END,
    ENTER,
    HOME,
    LEFT,
    RIGHT,
    EditKey,
    EditTracker,
    KeyTranslator,
    ManualEdits,
    compose,
)
from quill.tests.fakes import OTHER_HWND, TARGET, FakeClock
from quill.triggers import BUTTON, KEY, InputEvent
from quill.win32 import QUILL_EXTRA_INFO

HWND = TARGET.hwnd


def typed(text, dictation_id="d1"):
    return Dictation(dictation_id, text, HWND, 0.0)


def ch(text):
    return EditKey(CHAR, text)


def k(kind):
    return EditKey(kind)


class TrackerCase(unittest.TestCase):
    def run_keys(self, text, keys, window_s=30.0, finish=True):
        tracker = EditTracker(window_s)
        tracker.start(typed(text), 0.0)
        outcome = None
        for index, key in enumerate(keys):
            outcome = tracker.key(key, HWND, 1.0 + index * 0.1) or outcome
        if finish and outcome is None:
            outcome = tracker.finish()
        return tracker, outcome


class MirrorTest(TrackerCase):
    def edited(self, text, keys):
        tracker, outcome = self.run_keys(text, keys)
        self.assertEqual(tracker.last_reason, "finished" if outcome is None or outcome.edited else "")
        return None if outcome is None else outcome.edited

    def test_backspace_and_retype_at_the_end(self):
        self.assertEqual(self.edited("abre o velatriz", [k(BACKSPACE)] + [ch("x")]), "abre o velatrix")

    def test_arrows_delete_and_insert_in_the_middle(self):
        keys = [k(LEFT)] * 10 + [k(DELETE)] * 4 + [ch("kube")]
        self.assertEqual(self.edited("usa o velo agora", keys), "usa o kube agora")
        self.assertEqual(self.edited("ab", [k(LEFT), k(LEFT), k(RIGHT), ch("x")]), "axb")

    def test_ctrl_backspace_deletes_the_last_word(self):
        self.assertEqual(self.edited("corre o ram ", [k(CTRL_BACKSPACE), ch("run")]), "corre o run")
        self.assertEqual(self.edited("corre o ram", [k(CTRL_BACKSPACE), ch("run")]), "corre o run")

    def test_home_and_end_within_lines_of_the_span(self):
        keys = [k(HOME), ch("> ")]
        self.assertEqual(self.edited("linha um\nlinha dois", keys), "linha um\n> linha dois")
        keys = [k(LEFT)] * 12 + [k(END), ch(";")]
        self.assertEqual(self.edited("linha um\nlinha dois", keys), "linha um;\nlinha dois")

    def test_unchanged_text_gives_no_outcome(self):
        self.assertIsNone(self.edited("igual", [ch("x"), k(BACKSPACE)]))

    def test_enter_finishes_with_the_edits_before_it(self):
        tracker, outcome = self.run_keys("olá mundo", [k(BACKSPACE), ch("O"), k(ENTER), ch("zzz")], finish=False)
        self.assertEqual(outcome.edited, "olá mundO")
        self.assertEqual(tracker.last_reason, "enter")
        self.assertFalse(tracker.tracking)

    def test_outcome_repr_hides_text(self):
        _, outcome = self.run_keys("segredo", [ch("s")])
        self.assertNotIn("segredo", repr(outcome))


class AbandonTest(TrackerCase):
    def abandoned(self, text, keys):
        tracker, outcome = self.run_keys(text, keys)
        self.assertIsNone(outcome)
        self.assertFalse(tracker.tracking)
        return tracker.last_reason

    def test_keys_that_leave_the_span(self):
        cases = {
            "backspace at start": ("ab", [k(LEFT), k(LEFT), k(BACKSPACE)]),
            "delete at end": ("ab", [ch("x"), k(DELETE)]),
            "left at start": ("ab", [k(LEFT)] * 3),
            "right at end": ("ab", [ch("x"), k(RIGHT)]),
            "home on one line": ("ab", [ch("x"), k(HOME)]),
            "end on the last line": ("ab", [ch("x"), k(LEFT), k(END)]),
            "ctrl backspace to the span start": ("palavra", [ch("x"), k(CTRL_BACKSPACE)]),
            "ctrl backspace after punctuation": ("a b.", [ch("x"), k(LEFT), k(CTRL_BACKSPACE)]),
        }
        for name, (text, keys) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.abandoned(text, keys), edits.OUTSIDE)

    def test_click_paste_undo_injected_unknown(self):
        for reason in (edits.CLICK, edits.PASTE, edits.UNDO, edits.INJECTED, edits.UNKNOWN):
            with self.subTest(reason):
                self.assertEqual(self.abandoned("abc", [ch("x"), k(reason), ch("y")]), reason)

    def test_focus_change_abandons(self):
        tracker = EditTracker()
        tracker.start(typed("abc"), 0.0)
        tracker.key(ch("x"), HWND, 1.0)
        self.assertIsNone(tracker.key(ch("y"), OTHER_HWND, 1.1))
        self.assertEqual(tracker.last_reason, edits.FOCUS)
        self.assertIsNone(tracker.finish())
        tracker.start(typed("abc"), 2.0)
        tracker.abandon(edits.FOCUS)
        self.assertFalse(tracker.tracking)

    def test_runaway_growth_abandons(self):
        self.assertEqual(self.abandoned("a", [ch("x" * (edits.MAX_GROWTH + 1))]), edits.TOO_LONG)

    def test_empty_character_is_not_guessed(self):
        self.assertEqual(self.abandoned("abc", [ch("")]), edits.UNKNOWN)


class WindowTest(unittest.TestCase):
    def test_keys_after_the_window_finish_with_what_came_before(self):
        tracker = EditTracker(window_s=10.0)
        tracker.start(typed("olá"), 100.0)
        tracker.key(ch("!"), HWND, 105.0)
        outcome = tracker.key(ch("?"), HWND, 110.0)
        self.assertEqual((outcome.edited, tracker.last_reason), ("olá!", "window over"))
        self.assertIsNone(tracker.key(ch("x"), HWND, 111.0))

    def test_poll_finishes_after_the_window(self):
        tracker = EditTracker(window_s=10.0)
        tracker.start(typed("olá"), 100.0)
        tracker.key(ch("!"), HWND, 101.0)
        self.assertIsNone(tracker.poll(109.9))
        self.assertEqual(tracker.poll(110.0).edited, "olá!")
        self.assertIsNone(tracker.poll(120.0))

    def test_a_new_dictation_finishes_the_previous_one(self):
        tracker = EditTracker()
        tracker.start(typed("um"), 0.0)
        tracker.key(ch("s"), HWND, 1.0)
        previous = tracker.start(typed("dois", "d2"), 2.0)
        self.assertEqual((previous.dictation.id, previous.edited), ("d1", "ums"))
        self.assertTrue(tracker.tracking)

    def test_keys_without_tracking_are_ignored(self):
        tracker = EditTracker()
        self.assertIsNone(tracker.key(ch("x"), HWND, 0.0))
        self.assertIsNone(tracker.finish())
        self.assertIsNone(tracker.poll(100.0))


class FakeLayout:
    """A tiny Portuguese-like layout: letters, digits, space, ´ and ~ as dead keys, AltGr+2 = @."""

    def __init__(self):
        self.caps = False
        self.calls = 0

    def caps_lock(self):
        return self.caps

    def to_unicode(self, vk, shift, altgr, caps):
        self.calls += 1
        if altgr:
            return ("@", False) if vk == 0x32 else ("", False)
        if 0x41 <= vk <= 0x5A:
            letter = chr(vk)
            return (letter if shift != caps else letter.lower()), False
        if 0x30 <= vk <= 0x39:
            return chr(vk), False
        if vk == 0x20:
            return " ", False
        if vk == 0xBA:
            return ("`" if shift else "´"), True
        if vk == 0xBF:
            return ("^" if shift else "~"), True
        if vk == 0x1B:
            return "\x1b", False
        if vk == 0xDC:
            raise OSError("layout failed")
        return "", False


def press(vk, down=True, **extra):
    return InputEvent(KEY, vk, down, 0.0, **extra)


class TranslatorTest(unittest.TestCase):
    def setUp(self):
        self.layout = FakeLayout()
        self.translator = KeyTranslator(self.layout, ignore=frozenset({0x7F, 0xA3}))

    def keys(self, *events):
        return [key for key in map(self.translator.translate, events) if key is not None]

    def test_characters_and_shift(self):
        keys = self.keys(press(0x41), press(0xA0), press(0x42), press(0xA0, False), press(0x43), press(0x43, False))
        self.assertEqual([key.text for key in keys], ["a", "B", "c"])
        self.layout.caps = True
        self.assertEqual(self.keys(press(0x41))[0].text, "A")

    def test_dead_keys_compose_like_windows(self):
        self.assertEqual(self.keys(press(0xBA), press(0x45))[0].text, "é")
        self.assertEqual(self.keys(press(0xBF), press(0x41))[0].text, "ã")
        self.assertEqual(self.keys(press(0xA0), press(0xBF), press(0xA0, False), press(0x4F))[0].text, "ô")
        self.assertEqual(self.keys(press(0xBA), press(0x20))[0].text, "´")
        self.assertEqual(self.keys(press(0xBF), press(0x42))[0].text, "~b")
        self.assertEqual([k.kind for k in self.keys(press(0xBA), press(0xBA))], [edits.UNKNOWN])
        self.assertEqual([k.kind for k in self.keys(press(0xBA), press(0x08), press(0x45))], [BACKSPACE, CHAR])
        self.assertEqual(self.keys(press(0x45))[0].text, "e")  # the dead key was dropped by Backspace

    def test_editing_and_navigation_keys(self):
        kinds = [key.kind for key in self.keys(press(0x08), press(0x2E), press(0x25), press(0x27), press(0x24),
                                                press(0x23), press(0x0D))]
        self.assertEqual(kinds, [BACKSPACE, DELETE, LEFT, RIGHT, HOME, END, ENTER])

    def test_shortcuts(self):
        def with_ctrl(vk):
            return self.keys(press(0xA2), press(vk), press(0xA2, False))[0].kind

        self.assertEqual(with_ctrl(0x08), CTRL_BACKSPACE)
        self.assertEqual(with_ctrl(0x56), edits.PASTE)
        self.assertEqual(with_ctrl(0x5A), edits.UNDO)
        self.assertEqual(with_ctrl(0x59), edits.UNDO)
        self.assertEqual(with_ctrl(0x41), edits.UNKNOWN)  # select all
        self.assertEqual(with_ctrl(0x25), edits.UNKNOWN)  # word jump
        self.assertEqual(self.keys(press(0xA0), press(0x2D))[0].kind, edits.PASTE)  # Shift+Insert
        self.assertEqual(self.keys(press(0x25))[0].kind, edits.UNKNOWN)  # Shift+Left selects

    def test_altgr_types_and_alt_or_win_are_unknown(self):
        self.assertEqual(self.keys(press(0xA2), press(0xA5), press(0x32))[0].text, "@")
        self.translator.reset()
        self.assertEqual(self.keys(press(0xA4), press(0x41))[0].kind, edits.UNKNOWN)
        self.translator.reset()
        self.assertEqual(self.keys(press(0x5B), press(0x41))[0].kind, edits.UNKNOWN)

    def test_unknown_keys(self):
        for vk in (0x09, 0x26, 0x28, 0x14, 0x1B, 0x70, 0xDC):
            with self.subTest(vk=hex(vk)):
                self.translator.reset()
                self.assertEqual(self.keys(press(vk))[0].kind, edits.UNKNOWN)

    def test_clicks_injection_and_own_input(self):
        self.assertEqual(self.keys(InputEvent(BUTTON, 0x01, True, 0.0))[0].kind, edits.CLICK)
        self.assertEqual(self.keys(InputEvent(BUTTON, 0x02, True, 0.0))[0].kind, edits.CLICK)
        self.assertEqual(self.keys(InputEvent(BUTTON, 0x01, False, 0.0), InputEvent(BUTTON, 0x05, True, 0.0)), [])
        self.assertEqual(self.keys(press(0x41, injected=True))[0].kind, edits.INJECTED)
        self.assertEqual(self.keys(press(0x41, injected=True, extra_info=QUILL_EXTRA_INFO)), [])
        self.assertEqual(self.keys(InputEvent(BUTTON, 0x01, True, 0.0, extra_info=QUILL_EXTRA_INFO)), [])

    def test_trigger_keys_are_ignored_but_right_ctrl_stays_a_modifier(self):
        self.assertEqual(self.keys(press(0x7F), press(0x7F, False)), [])
        self.assertEqual(self.keys(press(0xA3), press(0x56))[0].kind, edits.PASTE)

    def test_compose(self):
        self.assertEqual((compose("´", "a"), compose("^", "e"), compose("¨", "u"), compose("?", "a")), ("á", "ê", "ü", "?a"))


class ManualEditsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.clock.now = 1_800_000_000.0
        self.learner = Learner(CorrectionStore(Path(self.dir.name) / "c.json", self.clock), self.clock)
        self.foreground = HWND
        self.edits = ManualEdits(self.learner, KeyTranslator(FakeLayout()), EditTracker(30.0),
                                 lambda: self.foreground, self.clock)

    def tearDown(self):
        self.dir.cleanup()

    def fix_last_word(self, text, dictation_id):
        self.edits.typed(Dictation(dictation_id, text, HWND, self.clock()))
        for _ in range(3):
            self.edits.on_input(press(0x08))
        for vk in (0x52, 0x55, 0x4E):  # r u n
            self.edits.on_input(press(vk))

    def test_learns_from_manual_edits_after_two_dictations(self):
        self.fix_last_word("corre o ram", "d1")
        self.clock.now += 31
        self.edits.poll()
        self.assertEqual(self.learner.corrections.counts()["pending"], 1)
        self.fix_last_word("depois ram", "d2")
        self.edits.stop()
        self.assertEqual(self.learner.apply("o ram"), "o run")

    def test_abandoned_edits_learn_nothing(self):
        self.fix_last_word("corre o ram", "d1")
        self.edits.on_input(InputEvent(BUTTON, 0x01, True, 0.0))
        self.edits.stop()
        self.assertEqual(self.learner.corrections.entries, [])
        self.foreground = OTHER_HWND
        self.fix_last_word("corre o ram", "d2")
        self.edits.stop()
        self.assertEqual(self.learner.corrections.entries, [])

    def test_keys_before_any_dictation_do_nothing(self):
        self.edits.on_input(press(0x41))
        self.edits.poll()
        self.edits.stop()
        self.assertEqual(self.learner.corrections.entries, [])

    def test_nothing_about_keys_is_logged(self):
        with self.assertLogs("quill", "INFO") as logs:
            self.fix_last_word("corre o ram", "d1")
            self.edits.stop()
        text = "".join(logs.output)
        self.assertNotIn("ram", text.replace("quill.", ""))
        self.assertNotIn("run", text)


if __name__ == "__main__":
    unittest.main()
