"""quill.command tests: command mode with a fake Win32 layer, clipboard and Ollama client.

The clipboard, the input and the model are fakes: nothing here reads or
writes the real clipboard, sends input, opens the microphone or calls
Ollama. The selected texts, instructions and rewrites are invented.
"""

import unittest
from types import SimpleNamespace

from quill import command as C
from quill import inject
from quill import session as S
from quill.clipboard import CF_BITMAP, CF_UNICODETEXT
from quill.indicator.render import COMMAND, ERROR
from quill.inject import NO_TARGET as INJECT_NO_TARGET
from quill.inject import Target
from quill.profiles import window_info
from quill.session import SessionManager
from quill.tests import test_app as app_tests
from quill.tests import test_session as session_tests
from quill.tests.fakes import OTHER_HWND, TARGET, FakeClock, FakeWin32, injector
from quill.tests.test_streaming import speech
from quill.clipboard import VK_C
from quill.win32 import VK_CONTROL, VK_SHIFT

SELECTION = "o relatório fica pronto amanhã de manhã"
REWRITE = "O relatório estará concluído amanhã de manhã."
INSTRUCTION = "põe isto mais formal"
F14 = 0x7D


def utf16(text):
    return (text + "\x00").encode("utf-16-le")


def user_clip():
    return [(CF_UNICODETEXT, utf16("conteúdo do utilizador")), (0xC100, b"rich text of the user")]


class FakeClient:
    """A fake Ollama chat client; ``on_chat`` runs during the call (the rewrite is in flight)."""

    def __init__(self, reply=REWRITE, error=None, on_chat=None):
        self.reply = reply
        self.error = error
        self.on_chat = on_chat
        self.calls = []

    def chat(self, model, system, user, max_tokens=None):
        self.calls.append(SimpleNamespace(model=model, system=system, user=user, max_tokens=max_tokens))
        if self.on_chat is not None:
            self.on_chat()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.reply)


# ---------------------------------------------------------------- validation


class ValidateTest(unittest.TestCase):
    def check(self, reply, selection=SELECTION):
        return C.validate_rewrite(selection, reply)

    def test_plain_rewrite_is_accepted(self):
        self.assertEqual(self.check(REWRITE), (REWRITE, "ok"))
        self.assertEqual(self.check("  " + REWRITE + "\r\n"), (REWRITE, "ok"))

    def test_whitespace_around_the_selection_is_kept(self):
        self.assertEqual(self.check(REWRITE, "  " + SELECTION + "\n"), ("  " + REWRITE + "\n", "ok"))

    def test_quotes_around_the_whole_reply_are_dropped(self):
        for left, right in (('"', '"'), ("«", "»"), ("“", "”")):
            self.assertEqual(self.check(left + REWRITE + right), (REWRITE, "ok"))
        # Quotes the selection had too are kept.
        self.assertEqual(self.check('"' + REWRITE + '"', '"' + SELECTION + '"'), ('"' + REWRITE + '"', "ok"))

    def test_refusals(self):
        cases = {
            "": C.EMPTY,
            "   \n ": C.EMPTY,
            '""': C.EMPTY,
            "Aqui está o texto reescrito: " + REWRITE: C.PREAMBLE,
            "Here is the rewritten text:\n" + REWRITE: C.PREAMBLE,
            "Sure, here is a more formal version.\n" + REWRITE: C.PREAMBLE,
            "Tradução: The report will be ready tomorrow.": C.PREAMBLE,
            "**Texto reescrito:** " + REWRITE: C.PREAMBLE,
            "Versão formal:\n" + REWRITE: C.PREAMBLE,
            REWRITE + "\n\nNota: mudei o tempo verbal.": C.EXPLANATION,
            REWRITE + "\n(Note: I changed the tense.)": C.EXPLANATION,
            REWRITE + "\nAlterações: registo mais formal.": C.EXPLANATION,
            "```\n" + REWRITE + "\n```": C.FENCE,
            "Instruction: põe isto mais formal\n" + REWRITE: C.ECHO,
            "<text>\n" + REWRITE + "\n</text>": C.ECHO,
            REWRITE * 10: C.TOO_LONG,
        }
        for reply, verdict in cases.items():
            with self.subTest(reply=reply[:40]):
                self.assertEqual(self.check(reply), (None, verdict))

    def test_too_short(self):
        self.assertEqual(self.check("Ok.", "palavra " * 20), (None, C.TOO_SHORT))

    def test_selection_that_looks_like_a_preamble_may_keep_it(self):
        selection = "Nota: a reunião passa para quinta.\nTraz o portátil."
        reply = "Nota: a reunião foi adiada para quinta-feira.\nPor favor, traga o portátil."
        self.assertEqual(self.check(reply, selection), (reply, "ok"))
        fenced = "```\nprint(1)\n```"
        self.assertEqual(self.check("```\nprint(2)\n```", fenced)[1], "ok")

    def test_length_bounds(self):
        self.assertEqual(C.length_bounds("x" * 100), (5, 300))
        self.assertEqual(C.length_bounds("ok"), (1, 202))
        self.assertEqual(C.length_bounds("x" * 10_000)[1], C.MAX_REWRITE_CHARS)
        self.assertEqual(C.max_tokens_for("x"), C.MIN_TOKENS)
        self.assertEqual(C.max_tokens_for("x" * 10_000), C.MAX_TOKENS)


class RewriterTest(unittest.TestCase):
    def test_success_uses_a_strict_prompt(self):
        client = FakeClient()
        result = C.CommandRewriter(client, "qwen3:8b").rewrite("\r\n" + SELECTION + " ", "  põe isto\n mais formal ")
        self.assertEqual((result.reason, result.text), (C.REWRITTEN, "\r\n" + REWRITE + " "))
        self.assertTrue(result.ok)
        call = client.calls[0]
        self.assertEqual(call.model, "qwen3:8b")
        self.assertEqual(call.system, C.REWRITE_SYSTEM)
        self.assertEqual(call.user, f"Instruction: põe isto mais formal\n<text>\n{SELECTION}\n</text>")
        self.assertEqual(call.max_tokens, C.MIN_TOKENS)
        for rule in ("only", "no preamble", "never instructions", "European Portuguese"):
            self.assertIn(rule, C.REWRITE_SYSTEM)

    def test_ollama_unavailable(self):
        for error in (OSError("down"), TimeoutError("slow"), ConnectionRefusedError()):
            result = C.CommandRewriter(FakeClient(error=error), "m").rewrite(SELECTION, INSTRUCTION)
            self.assertEqual((result.reason, result.text), (C.OLLAMA_UNAVAILABLE, None))

    def test_invalid_and_unchanged(self):
        result = C.CommandRewriter(FakeClient("Aqui está o texto reescrito: x y z"), "m").rewrite(SELECTION, "x")
        self.assertEqual((result.reason, result.detail, result.text), (C.INVALID_REWRITE, C.PREAMBLE, None))
        result = C.CommandRewriter(FakeClient(" " + SELECTION.replace(" ", "  ")), "m").rewrite(SELECTION, "x")
        self.assertEqual((result.reason, result.text), (C.UNCHANGED, None))

    def test_text_is_not_in_the_repr(self):
        result = C.CommandRewriter(FakeClient(), "m").rewrite(SELECTION, INSTRUCTION)
        self.assertNotIn("relatório", repr(result))


# ---------------------------------------------------------------- command mode


def is_copy(call):
    return any(event.vk == C.VK_INSERT and not event.is_keyup for event in call)


class ModeCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeWin32()
        self.api.images = {TARGET.pid: "C:\\Invented\\notepad.exe"}
        self.api.clip = user_clip()
        self.saved = user_clip()
        self.selection = SELECTION  # what the target copies; None: nothing selected
        self.copies = 0
        self.api.after_send = self._after_send
        self.clock = FakeClock()
        self.client = FakeClient()
        self.mode = self.make_mode(self.client)

    def make_mode(self, client):
        rewriter = C.CommandRewriter(client, "qwen3:8b", clock=self.clock)
        return C.CommandMode(self.api, injector(self.api, self.clock), rewriter,
                             lambda target: window_info(self.api, target.hwnd), clock=self.clock,
                             sleep=self.clock.sleep)

    def _after_send(self, index):
        call = self.api.calls[index]
        if is_copy(call):
            self.copies += 1
            if self.selection is not None:
                # The target puts its selection on the clipboard (a write by another process).
                self.api.clip = [(CF_UNICODETEXT, utf16(self.selection))]
                self.api.sequence += 1

    def run_mode(self, instruction=INSTRUCTION, target=TARGET):
        with _Capture(self) as logs:
            result = self.mode.run(instruction, target)
        for text in (SELECTION, REWRITE, INSTRUCTION, "relatório", "formal"):
            self.assertNotIn(text, logs.text)
        return result

    def typed_calls(self):
        return [call for call in self.api.calls if not is_copy(call)]

    def assert_untouched(self, copies=None):
        """Nothing typed over the selection and the user's clipboard is back as it was."""
        self.assertEqual(self.typed_calls(), [])
        self.assertEqual(self.api.clip, self.saved)
        self.assertFalse(self.api.clip_open)
        if copies is not None:
            self.assertEqual(self.copies, copies)


class _Capture:
    """Collects every log record of ``quill`` (also when there are none)."""

    def __init__(self, case):
        self.case = case
        self.text = ""

    def __enter__(self):
        import logging

        self.handler = logging.Handler()
        self.records = []
        self.handler.emit = lambda record: self.records.append(record.getMessage())
        self.logger = logging.getLogger("quill")
        self.level = self.logger.level
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.level)
        self.text = "\n".join(self.records)
        return False


class ModeSuccessTest(ModeCase):
    def test_rewrites_the_selection_and_restores_the_clipboard(self):
        result = self.run_mode()
        self.assertEqual((result.reason, result.typed), (C.REWRITTEN, len(REWRITE)))
        self.assertTrue(result.ok)
        self.assertEqual(self.api.received_text(), REWRITE)
        self.assertEqual(self.api.clip, self.saved)  # every format back, in order
        self.assertEqual(self.copies, 2)  # the copy and the check before typing
        self.assertEqual(self.api.mouse_calls, [])  # never clicks
        self.assertEqual(set(result.timings), {"copy_s", "rewrite_s", "verify_s", "typing_s"})
        self.assertIn(INSTRUCTION, self.client.calls[0].user)
        self.assertIn(SELECTION, self.client.calls[0].user)

    def test_copy_uses_ctrl_insert_never_ctrl_c(self):
        self.run_mode()
        copy = self.api.calls[0]
        self.assertEqual([(e.vk, e.is_keyup) for e in copy],
                         [(VK_CONTROL, False), (C.VK_INSERT, False), (C.VK_INSERT, True), (VK_CONTROL, True)])
        self.assertNotIn(VK_C, [event.vk for event in self.api.events])

    def test_line_breaks_are_shift_enter_never_enter(self):
        self.client.reply = "Primeira linha revista.\nSegunda linha revista."
        self.selection = "primeira linha\nsegunda linha"
        self.assertEqual(self.run_mode().reason, C.REWRITTEN)
        self.assertEqual(self.api.received_text(), "Primeira linha revista.\nSegunda linha revista.")
        self.assertEqual(self.api.enter_presses(), [True])  # Shift held

    def test_waits_for_modifiers_before_copying(self):
        self.api.keys_down.add(VK_SHIFT)

        def release(seconds):
            self.clock.now += seconds
            self.api.keys_down.clear()

        self.mode.sleep = release
        self.assertEqual(self.run_mode().reason, C.REWRITTEN)


class ModeRefusalTest(ModeCase):
    def test_empty_or_long_instruction(self):
        for instruction, reason in (("", C.EMPTY_INSTRUCTION), ("  \n", C.EMPTY_INSTRUCTION),
                                    ("x " * 300, C.INSTRUCTION_TOO_LONG)):
            self.assertEqual(self.run_mode(instruction).reason, reason)
        self.assert_untouched(copies=0)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.client.calls, [])

    def test_no_target(self):
        for target in (None, Target(hwnd=0, pid=0)):
            self.assertEqual(self.run_mode(target=target).reason, C.NO_TARGET)
        self.assertEqual(self.api.calls, [])

    def test_terminal_windows_are_refused(self):
        self.api.classes[TARGET.hwnd] = "CASCADIA_HOSTING_WINDOW_CLASS"
        self.assertEqual(self.run_mode().reason, C.TERMINAL)
        self.api.classes[TARGET.hwnd] = ""
        self.api.images[TARGET.pid] = "C:\\Invented\\WindowsTerminal.exe"
        self.assertEqual(self.run_mode().reason, C.TERMINAL)
        self.assert_untouched(copies=0)
        self.assertEqual(self.api.calls, [])

    def test_undescribable_window_is_not_assumed_a_terminal(self):
        def broken(target):
            raise RuntimeError("fake")

        self.mode.describe = broken
        self.assertEqual(self.run_mode().reason, C.REWRITTEN)

    def test_target_problems_before_the_copy(self):
        cases = [
            (lambda: setattr(self.api, "foreground", OTHER_HWND), inject.FOREGROUND_CHANGED),
            (lambda: self.api.hung.add(TARGET.hwnd), inject.TARGET_NOT_RESPONDING),
            (lambda: self.api.keys_down.add(VK_CONTROL), inject.MODIFIER_HELD),
        ]
        for change, reason in cases:
            with self.subTest(reason=reason):
                self.setUp()
                change()
                result = self.run_mode()
                self.assertEqual(result.reason, reason)
                self.assertIn(reason, S.MESSAGES)
                self.assert_untouched(copies=0)
                self.assertEqual(self.api.calls, [])

    def test_no_selection(self):
        self.selection = None  # the copy changes nothing
        self.assertEqual(self.run_mode().reason, C.NO_SELECTION)
        self.assert_untouched(copies=1)
        self.assertEqual(self.client.calls, [])
        self.selection = "  \n "
        self.assertEqual(self.run_mode().reason, C.NO_SELECTION)
        self.assert_untouched()

    def test_selection_too_long(self):
        self.selection = "x" * 5000
        self.assertEqual(self.run_mode().reason, C.SELECTION_TOO_LONG)
        self.assert_untouched(copies=1)

    def test_clipboard_busy(self):
        self.api.open_failures = 1000
        self.assertEqual(self.run_mode().reason, C.CLIPBOARD_BUSY)
        self.api.open_failures = 0
        self.assert_untouched(copies=0)
        self.assertEqual(self.api.clipboard_writes, 0)

    def test_clipboard_that_cannot_be_restored_is_left_alone(self):
        self.api.clip = [(CF_BITMAP, None)]
        self.saved = [(CF_BITMAP, None)]
        self.assertEqual(self.run_mode().reason, C.CLIPBOARD_UNSAFE)
        self.assert_untouched(copies=0)
        self.assertEqual(self.api.clipboard_writes, 0)

    def test_ollama_unavailable(self):
        self.mode = self.make_mode(FakeClient(error=OSError("fake: Ollama down")))
        self.assertEqual(self.run_mode().reason, C.OLLAMA_UNAVAILABLE)
        self.assert_untouched(copies=1)

    def test_invalid_rewrites(self):
        for reply in ("", "Aqui está o texto reescrito:\n" + REWRITE, REWRITE + "\nNota: mais formal.", REWRITE * 10):
            with self.subTest(reply=reply[:30]):
                self.setUp()
                self.client.reply = reply
                self.assertEqual(self.run_mode().reason, C.INVALID_REWRITE)
                self.assert_untouched(copies=1)

    def test_unchanged_rewrite_types_nothing(self):
        self.client.reply = SELECTION
        self.assertEqual(self.run_mode().reason, C.UNCHANGED)
        self.assert_untouched(copies=1)

    def test_every_reason_has_a_portuguese_message(self):
        for reason in C.MESSAGES:
            self.assertIn(reason, S.MESSAGES)
            self.assertNotEqual(S.message(reason), S.MESSAGES[S.INTERNAL_ERROR])
        self.assertIn(INJECT_NO_TARGET, S.MESSAGES)


class ModeDuringRewriteTest(ModeCase):
    """Things that change while the model is rewriting."""

    def rewrite_with(self, change):
        self.client.on_chat = change
        return self.run_mode()

    def test_foreground_changed(self):
        result = self.rewrite_with(lambda: setattr(self.api, "foreground", OTHER_HWND))
        self.assertEqual(result.reason, inject.FOREGROUND_CHANGED)
        self.assert_untouched(copies=1)

    def test_target_closed(self):
        result = self.rewrite_with(lambda: self.api.windows.pop(TARGET.hwnd))
        self.assertEqual(result.reason, inject.TARGET_GONE)
        self.assert_untouched(copies=1)

    def test_selection_changed(self):
        result = self.rewrite_with(lambda: setattr(self, "selection", "outro texto selecionado"))
        self.assertEqual(result.reason, C.SELECTION_CHANGED)
        self.assert_untouched(copies=2)

    def test_selection_gone(self):
        result = self.rewrite_with(lambda: setattr(self, "selection", None))
        self.assertEqual(result.reason, C.SELECTION_CHANGED)
        self.assert_untouched(copies=2)

    def test_modifier_held_until_the_end(self):
        result = self.rewrite_with(lambda: self.api.keys_down.add(VK_CONTROL))
        self.assertEqual(result.reason, inject.MODIFIER_HELD)
        self.assert_untouched(copies=1)

    def test_clipboard_written_by_another_program_during_the_rewrite_is_kept(self):
        other = [(CF_UNICODETEXT, utf16("copiado noutro programa"))]

        def another_program():
            self.api.clip = list(other)
            self.api.sequence += 1

        self.assertEqual(self.rewrite_with(another_program).reason, C.REWRITTEN)
        self.assertEqual(self.api.received_text(), REWRITE)
        self.assertEqual(self.api.clip, other)  # the new content is kept, not the older one

    def test_clipboard_written_by_another_program_during_the_copy_is_not_overwritten(self):
        other = [(CF_UNICODETEXT, utf16("copiado noutro programa"))]
        closes = []
        close = self.api.close_clipboard

        def close_then_write():
            close()
            closes.append(1)
            if len(closes) == 3:  # after the selection was read, before the restore
                self.api.clip = list(other)
                self.api.sequence += 1

        self.api.close_clipboard = close_then_write
        result = self.run_mode()
        self.assertEqual(result.reason, C.REWRITTEN)
        self.assertEqual(self.api.clip, other)

    def test_typing_interrupted_reports_the_characters_typed(self):
        self.client.reply = "Uma frase reescrita bastante mais longa para ocupar várias rajadas de escrita."
        typed_bursts = []

        def after(index):
            self._after_send(index)
            if not is_copy(self.api.calls[index]):
                typed_bursts.append(index)
                self.api.foreground = OTHER_HWND

        self.api.after_send = after
        result = self.run_mode()
        self.assertEqual(result.reason, inject.FOREGROUND_CHANGED)
        self.assertEqual(len(typed_bursts), 1)
        self.assertGreater(result.typed, 0)
        self.assertEqual(self.api.clip, self.saved)


# ---------------------------------------------------------------- sessions


class FakeCommand:
    def __init__(self, reason=C.REWRITTEN):
        self.reason = reason
        self.calls = []

    def run(self, instruction, target):
        self.calls.append((instruction, target))
        return C.CommandResult(self.reason, 7 if self.reason == C.REWRITTEN else 0, {"copy_s": 0.01})


class CommandSessionCase(session_tests.SessionCase):
    def setUp(self):
        super().setUp()
        self.manager.stop()
        self.command = FakeCommand()
        self.manager = SessionManager(
            transcriber=self.transcriber, capture_factory=self.captures, focus=self.focus, injector=self.injector,
            indicator=self.indicator, pipeline=self.pipeline, command=self.command, clock=self.clock,
            final_timeout_s=5.0, poll_s=0.05)
        self.manager.start()
        self.addCleanup(self.manager.stop)
        self.manager.loaded()

    def command_hold(self, text=INSTRUCTION, error=None):
        self.dictate(text, action="command", error=error)


class CommandSessionTest(CommandSessionCase):
    def test_success_shows_command_mode_then_hides(self):
        self.press("command", "f14")
        self.assertEqual(self.indicator.last, ("show", COMMAND, ""))
        self.assertEqual(self.focus.calls, [("command", "f14")])
        self.captures.made[-1].push(session_tests.PCM)
        self.transcriber.sessions[-1].partial("põe isto")
        self.release("command", "f14")
        self.transcriber.sessions[-1].handle.resolve(INSTRUCTION)
        outcome = self.wait_outcomes(1)[0]
        self.assertEqual((outcome.action, outcome.reason, outcome.typed), ("command", C.REWRITTEN, 7))
        self.assertEqual(self.command.calls, [(INSTRUCTION, session_tests.TARGET)])
        self.assertEqual(self.indicator.last, ("hide",))
        self.assertEqual(self.pipeline.calls, 0)  # the instruction is not cleaned or typed
        self.assertEqual(self.injector.typed, [])
        self.assert_released()

    def test_failures_show_the_portuguese_message(self):
        for number, reason in enumerate((C.NO_SELECTION, C.OLLAMA_UNAVAILABLE, C.INVALID_REWRITE,
                                         C.EMPTY_INSTRUCTION, inject.FOREGROUND_CHANGED), start=1):
            self.command.reason = reason
            self.command_hold("" if reason == C.EMPTY_INSTRUCTION else INSTRUCTION)
            self.assertEqual(self.wait_outcomes(number)[-1].reason, reason)
            self.assertEqual(self.indicator.last, ("show", ERROR, S.MESSAGES[reason]))
        self.assertEqual(self.command.calls[3], ("", session_tests.TARGET))
        self.assert_released()

    def test_no_target_never_runs_the_command(self):
        self.focus.targets = [None]
        self.press("command", "f14")
        self.release("command", "f14")
        self.assertEqual(self.wait_outcomes(1)[0].reason, C.NO_TARGET)
        self.assertEqual(self.command.calls, [])
        self.assertEqual(self.indicator.last, ("show", ERROR, C.MESSAGES[C.NO_TARGET]))

    def test_engine_error_never_runs_the_command(self):
        self.command_hold(error="fake engine failure")
        self.assertEqual(self.wait_outcomes(1)[0].reason, S.ENGINE_ERROR)
        self.assertEqual(self.command.calls, [])

    def test_a_crashing_command_is_an_internal_error(self):
        def crash(instruction, target):
            raise RuntimeError("fake")

        self.command.run = crash
        with self.assertLogs("quill.session", level="ERROR"):
            self.command_hold()
            self.assertEqual(self.wait_outcomes(1)[0].reason, S.INTERNAL_ERROR)
        self.assert_released()

    def test_command_then_dictation_in_order(self):
        self.command_hold()
        self.dictate()
        self.assertEqual([o.reason for o in self.wait_outcomes(2)], [C.REWRITTEN, S.TYPED])
        self.assertEqual(len(self.injector.typed), 1)


# ---------------------------------------------------------------- app


class CommandAppTest(app_tests.AppCase):
    def setUp(self):
        super().setUp()
        self.api.clip = user_clip()
        self.selection = SELECTION

        def target_copies(index):
            if is_copy(self.api.calls[index]) and self.selection is not None:
                self.api.clip = [(CF_UNICODETEXT, utf16(self.selection))]
                self.api.sequence += 1

        self.api.after_send = target_copies

    def command_app(self, client):
        clock = FakeClock()
        parts = app_tests.A.Parts(
            api=self.api, model=self.model, capture_factory=self.captures, indicator=self.indicator,
            instance=app_tests.A.InstanceLock(self.kernel), hooks_factory=self.new_hooks, command_client=client,
            focus_options={"sleep": clock.sleep, "clock": clock}, inject_options={"sleep": clock.sleep, "clock": clock})
        quill = app_tests.A.QuillApp(self.config, parts)
        self.addCleanup(quill.stop)
        return self.start(quill)

    def command_hold(self, quill):
        done = len(quill.sessions.outcomes)
        self.assertEqual(self.key(True, F14), 1)  # swallowed
        app_tests.wait_for(lambda: self.captures.made, "the capture to start")
        app_tests.wait_for(lambda: quill.sessions._active is not None and quill.sessions._active.target is not None,
                           "the command target")
        self.captures.made[-1].push(speech((1, 2)))
        self.assertEqual(self.key(False, F14), 1)
        return self.outcomes(done + 1, quill)[-1]

    def test_command_mode_rewrites_the_selection(self):
        client = FakeClient()
        quill = self.command_app(client)
        self.assertIsNotNone(quill.command)
        outcome = self.command_hold(quill)
        self.assertEqual(outcome.reason, C.REWRITTEN)
        self.assertEqual(self.api.received_text(), REWRITE)
        self.assertEqual(self.api.mouse_calls, [])  # the command trigger never clicks
        self.assertEqual(self.api.enter_presses(), [])
        self.assertEqual(self.api.clip, user_clip())
        self.assertIn("Instruction: w1 w2\n", client.calls[0].user)
        self.assertEqual(client.calls[0].model, self.config.ollama_model)
        self.assertIn(COMMAND, self.indicator.states)
        self.assertEqual(self.indicator.last, ("hide",))

    def test_ollama_down_leaves_the_selection(self):
        quill = self.command_app(FakeClient(error=OSError("fake: Ollama down")))
        outcome = self.command_hold(quill)
        self.assertEqual(outcome.reason, C.OLLAMA_UNAVAILABLE)
        self.assertEqual(self.typed_events(), [])
        self.assertEqual(self.api.clip, user_clip())
        self.assertEqual(self.indicator.last[:3], ("show", ERROR, C.MESSAGES[C.OLLAMA_UNAVAILABLE]))

    def test_without_a_client_the_trigger_says_unavailable(self):
        quill = self.command_app(None)
        self.assertIsNone(quill.command)
        self.assertEqual(self.key(True, F14), 1)
        self.key(False, F14)
        self.assertEqual(self.outcomes(1, quill)[0].reason, S.COMMAND_UNAVAILABLE)
        self.assertEqual(self.api.calls, [])

    def typed_events(self):
        return [call for call in self.api.calls if not is_copy(call)]


if __name__ == "__main__":
    unittest.main()
