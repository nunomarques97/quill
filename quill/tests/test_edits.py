"""quill.edits with fake keyboards and invented text; no hook, no real key."""

import tempfile
import unittest
from pathlib import Path

from quill import edits, inject
from quill.tests import fakes
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
    RewriteUndo,
    compose,
)
from quill.inject import NEWLINE_SHIFT_ENTER, InjectOptions
from quill.tests.fakes import OTHER_HWND, TARGET, FakeClock, FakeWin32
from quill.triggers import BUTTON, KEY, InputEvent
from quill.win32 import QUILL_EXTRA_INFO, VK_CONTROL

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


class IntactTest(unittest.TestCase):
    """``EditTracker.intact``: proof that a typed span is untouched with the caret at its end."""

    def test_untouched_span(self):
        tracker = EditTracker(window_s=10.0)
        dictation = typed("olá mundo")
        tracker.start(dictation, 100.0)
        self.assertEqual(tracker.intact(dictation, 101.0), "")

    def test_edits_caret_and_window(self):
        cases = {
            edits.EDITED: [ch("x")],
            edits.CARET_MOVED: [k(LEFT)],
            "retyped": [k(BACKSPACE), ch("o")],  # the same text again: intact
        }
        for name, keys in cases.items():
            with self.subTest(name):
                tracker = EditTracker(window_s=10.0)
                dictation = typed("olá mundo")
                tracker.start(dictation, 100.0)
                for key in keys:
                    tracker.key(key, HWND, 101.0)
                self.assertEqual(tracker.intact(dictation, 102.0), "" if name == "retyped" else name)
        tracker = EditTracker(window_s=10.0)
        dictation = typed("olá")
        tracker.start(dictation, 100.0)
        self.assertEqual(tracker.intact(dictation, 110.0), edits.WINDOW_OVER)

    def test_a_finished_or_abandoned_span_says_why(self):
        for reason, keys in ((ENTER, [k(ENTER)]), (edits.CLICK, [k(edits.CLICK)]), (edits.EDITED, [ch("x"), k(ENTER)]),
                             (edits.OUTSIDE, [k(RIGHT)])):
            with self.subTest(reason):
                tracker = EditTracker()
                dictation = typed("abc")
                tracker.start(dictation, 0.0)
                for key in keys:
                    tracker.key(key, HWND, 1.0)
                expected = ENTER if reason == edits.EDITED and keys[-1].kind == ENTER else reason
                self.assertEqual(tracker.intact(dictation, 2.0), expected)
        tracker = EditTracker()
        dictation = typed("abc")
        tracker.start(dictation, 0.0)
        tracker.key(ch("x"), OTHER_HWND, 1.0)
        self.assertEqual(tracker.intact(dictation, 2.0), edits.FOCUS)
        tracker.start(dictation, 3.0)
        tracker.key(ch("x"), HWND, 4.0)
        tracker.finish()  # edited, then finished (next dictation, stop)
        self.assertEqual(tracker.intact(dictation, 5.0), edits.EDITED)

    def test_another_dictation_is_not_followed(self):
        tracker = EditTracker()
        self.assertEqual(tracker.intact(typed("abc"), 0.0), edits.NOT_FOLLOWED)
        tracker.start(typed("abc"), 0.0)
        tracker.start(typed("def", "d2"), 1.0)
        self.assertNotEqual(tracker.intact(typed("abc"), 2.0), "")
        self.assertEqual(tracker.intact(typed("abc", "d9"), 2.0), edits.NOT_FOLLOWED)


class CountableTest(unittest.TestCase):
    def test_line_breaks_and_precomposed_accents_count_as_one(self):
        self.assertTrue(edits.countable("Olá, coração!\n- ação à noite"))

    def test_characters_an_editor_may_count_differently(self):
        for text in ("café", "a‍b", "\U0001F600", "a\tb", "a­b", "️"):
            with self.subTest(text=repr(text)):
                self.assertFalse(edits.countable(text))


class UndoCase(unittest.TestCase):
    """``RewriteUndo`` with a real injector on the fake Win32 layer and a real tracker."""

    REWRITE = "Olá, coração!\nNova linha com mais umas palavras."  # longer than one burst
    ORIGINAL = "ola coraçao nova linha com mais umas palavras"

    def setUp(self):
        self.api = FakeWin32()
        self.clock = FakeClock()
        self.clock.now = 1000.0
        self.injector = fakes.injector(self.api)
        self.tracker = EditTracker(window_s=30.0)
        self.undo = RewriteUndo(self.injector, self.tracker, self.api.foreground_window, 20.0, clock=self.clock)
        self.dictation = self.type_rewrite()

    def type_rewrite(self, text=None, dictation_id="d1"):
        text = text or self.REWRITE
        self.assertTrue(self.injector.inject(text, TARGET, InjectOptions(newline=NEWLINE_SHIFT_ENTER)).ok)
        dictation = Dictation(dictation_id, text, TARGET.hwnd, 0.0)
        self.tracker.start(dictation, self.clock())
        self.assertTrue(self.undo.remember(dictation, self.ORIGINAL, TARGET, NEWLINE_SHIFT_ENTER))
        return dictation


class UndoTest(UndoCase):
    def test_undo_erases_the_rewrite_and_types_the_original(self):
        self.assertEqual(self.api.received_text(), self.REWRITE)
        with self.assertLogs("quill", "INFO") as logs:
            outcome = self.undo.undo()
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.message)
        self.assertEqual((outcome.erased, outcome.original, outcome.target), (len(self.REWRITE), self.ORIGINAL, TARGET))
        self.assertEqual(self.api.backspaces(), len(self.REWRITE))  # the line break and each accent: one each
        self.assertEqual(self.api.received_text(), self.ORIGINAL)
        self.assertEqual(self.api.enter_presses(), [True])  # the rewrite's line break: Shift+Enter, never Enter
        text = "".join(logs.output)
        for word in ("coração", "linha", "ola"):
            self.assertNotIn(word, text)
        self.assertNotIn(self.ORIGINAL, repr(outcome))
        # One undo per rewrite.
        self.assertEqual(self.undo.undo().reason, edits.UNDO_NOTHING)

    def test_nothing_to_undo(self):
        self.undo.forget()
        outcome = self.undo.undo()
        self.assertEqual(outcome.reason, edits.UNDO_NOTHING)
        self.assertEqual(outcome.message, "Não há reescrita para desfazer")

    def assert_refused(self, reason, keep=False):
        calls = len(self.api.calls)
        outcome = self.undo.undo()
        self.assertEqual(outcome.reason, reason)
        self.assertIn(outcome.message, edits.UNDO_MESSAGES.values())
        self.assertEqual(len(self.api.calls), calls)  # nothing sent
        self.assertEqual(self.api.received_text(), self.REWRITE)
        self.assertEqual(self.undo.pending, keep)

    def test_refused_after_an_edit(self):
        self.tracker.key(ch("x"), TARGET.hwnd, self.clock())
        self.assert_refused(edits.UNDO_EDITED)

    def test_refused_after_the_caret_moved(self):
        self.tracker.key(k(LEFT), TARGET.hwnd, self.clock())
        self.assert_refused(edits.UNDO_CARET)

    def test_refused_after_enter(self):
        self.tracker.key(k(ENTER), TARGET.hwnd, self.clock())
        self.assert_refused(edits.UNDO_ENTERED)

    def test_refused_after_a_click(self):
        self.tracker.key(k(edits.CLICK), TARGET.hwnd, self.clock())
        self.assert_refused(edits.UNDO_CLICKED)

    def test_refused_in_another_window_but_kept_for_when_it_comes_back(self):
        self.api.foreground = OTHER_HWND
        self.assert_refused(edits.UNDO_OTHER_WINDOW, keep=True)
        self.api.foreground = TARGET.hwnd
        self.assertTrue(self.undo.undo().ok)

    def test_refused_after_a_key_in_another_window(self):
        self.tracker.key(ch("x"), OTHER_HWND, self.clock())
        self.assert_refused(edits.UNDO_OTHER_WINDOW)

    def test_refused_after_the_time_window(self):
        self.clock.now += 20.0
        self.assert_refused(edits.UNDO_EXPIRED)

    def test_refused_after_the_tracker_window(self):
        self.undo.window_s = 60.0
        self.clock.now += 30.0
        self.assert_refused(edits.UNDO_EXPIRED)

    def test_refused_when_the_tracker_follows_something_else(self):
        self.tracker.start(Dictation("d2", "outro", TARGET.hwnd, 0.0), self.clock())
        self.assert_refused(edits.UNDO_UNSURE)

    def test_refused_without_a_tracker(self):
        undo = RewriteUndo(self.injector, None, self.api.foreground_window, 20.0, clock=self.clock)
        undo.remember(self.dictation, self.ORIGINAL, TARGET, NEWLINE_SHIFT_ENTER)
        self.undo = undo
        self.assert_refused(edits.UNDO_UNSURE)

    def test_uncountable_text_is_never_erased(self):
        dictation = Dictation("d3", "café bom", TARGET.hwnd, 0.0)
        self.assertFalse(self.undo.remember(dictation, "cafe bom", TARGET))
        self.assertEqual(self.undo.undo().reason, edits.UNDO_NOTHING)

    def test_target_gone_while_erasing_stops_and_says_so(self):
        self.api.after_send = lambda index: self.api.windows.pop(TARGET.hwnd, None)
        outcome = self.undo.undo()
        self.assertEqual(outcome.reason, edits.UNDO_FAILED)
        self.assertEqual(outcome.message, "A reposição foi interrompida; verifique o texto")
        self.assertFalse(self.tracker.tracking)
        self.assertLess(outcome.erased, len(self.REWRITE))

    def test_modifier_held_while_erasing_stops(self):
        self.api.keys_down.add(VK_CONTROL)  # Ctrl+Backspace would delete a word
        outcome = self.undo.undo()
        self.assertEqual((outcome.reason, outcome.erased), (edits.UNDO_FAILED, 0))
        self.assertEqual(self.api.received_text(), self.REWRITE)

    def test_typing_the_original_interrupted_stops_and_says_so(self):
        calls = len(self.api.calls)
        erase_calls = -(-len(self.REWRITE) // InjectOptions().chunk_chars)

        def change_focus(index):
            if index == calls + erase_calls - 1:  # the last Backspace burst
                self.api.foreground = OTHER_HWND

        self.api.after_send = change_focus
        outcome = self.undo.undo()
        self.assertEqual((outcome.reason, outcome.erased), (edits.UNDO_FAILED, len(self.REWRITE)))
        self.assertFalse(self.tracker.tracking)


class EraseTest(unittest.TestCase):
    def test_erase_sends_backspace_in_checked_bursts(self):
        api = FakeWin32()
        injector = fakes.injector(api)
        injector.inject("abcdefghij", TARGET)
        result = injector.erase(7, TARGET, InjectOptions(chunk_chars=3))
        self.assertEqual((result.reason, result.typed, result.total), (inject.OK, 7, 7))
        self.assertEqual(api.received_text(), "abc")
        self.assertEqual([len(call) for call in api.calls[1:]], [6, 6, 2])
        self.assertEqual(injector.erase(0, TARGET).typed, 0)
        with self.assertRaises(ValueError):
            injector.erase(-1, TARGET)

    def test_erase_refuses_a_gone_or_other_target(self):
        api = FakeWin32()
        injector = fakes.injector(api)
        self.assertEqual(injector.erase(3, None).reason, inject.NO_TARGET)
        api.foreground = OTHER_HWND
        self.assertEqual(injector.erase(3, TARGET).reason, inject.FOREGROUND_CHANGED)
        self.assertEqual(api.calls, [])


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
