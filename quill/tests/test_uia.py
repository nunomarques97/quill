"""quill.uia tests with fake UI Automation trees and fake readers.

No real UI Automation is used: ``ComUia`` cannot be created in tests (see
``quill.tests``). The trees follow the shape read live, read-only, in VS Code
1.140 with Claude Code for VS Code 2.1.287 (class names and ids only); names
are invented.
"""

from __future__ import annotations

import re
import threading
import time
import unittest
from pathlib import Path

from quill import uia
from quill.uia import (CLAUDE_CODE_INPUT, DOCUMENT, EDIT, OTHER, TERMINAL, TEXT_EDITOR, TIMEOUT, UNAVAILABLE, Focus,
                       FocusProbe, Node, classify)

HWND = 500
OTHER_WINDOW = 600
GROUP = 50026
PANE = 50033


def webview(*inner: Node, frame_name: str = "Invented topic") -> tuple[Node, ...]:
    """Ancestors from inside a VS Code webview page up to the window, nearest first."""
    return (*inner,
            Node(GROUP, "", "root"),
            Node(DOCUMENT, "vscode-dark vscode-using-screen-reader"),
            Node(DOCUMENT, "", "RootWebArea"),
            Node(DOCUMENT, "", "active-frame", frame_name),
            Node(DOCUMENT),
            Node(DOCUMENT, "", "RootWebArea"),
            Node(DOCUMENT, "webview ready"),
            Node(GROUP, "webview-overlay-content webview-overlay-outer-right", "00000000-invented"),
            Node(GROUP),
            Node(PANE, "file-icons-enabled monaco-enable-motion"),
            Node(DOCUMENT, "", "RootWebArea", "invented - Visual Studio Code"),
            Node(PANE, "Chrome_RenderWidgetHostHWND", "26", "Chrome Legacy Window", hwnd=7))


CLAUDE_INPUT = Node(EDIT, "messageInput_cKsPxg")
CLAUDE_CHAIN = webview(Node(GROUP, "messageInputContainer_cKsPxg"), Node(GROUP, "inputContainer_cKsPxg"), Node(GROUP),
                       Node(GROUP, "inputContainer_07S1Yg "), Node(GROUP, "chatContainer_07S1Yg"))


def workbench(*inner: Node) -> tuple[Node, ...]:
    """Ancestors inside the VS Code workbench page (no webview), nearest first."""
    return (*inner, Node(PANE, "file-icons-enabled monaco-enable-motion"),
            Node(DOCUMENT, "", "RootWebArea", "invented - Visual Studio Code"),
            Node(PANE, "Chrome_RenderWidgetHostHWND", "26", hwnd=7))


def focus(element: Node, ancestors: tuple[Node, ...], root: int = HWND) -> Focus:
    return Focus(element, ancestors, root)


class ClassifyTest(unittest.TestCase):
    def test_the_claude_code_message_input(self):
        self.assertEqual(classify(focus(CLAUDE_INPUT, CLAUDE_CHAIN), HWND), CLAUDE_CODE_INPUT)
        # The sidebar view and a renamed editor tab: the same page in another overlay, any frame name.
        sidebar = webview(*CLAUDE_CHAIN[:5], frame_name="Claude Code")
        self.assertEqual(classify(focus(CLAUDE_INPUT, sidebar), HWND), CLAUDE_CODE_INPUT)
        # Another build of the extension: another CSS module hash.
        rebuilt = (Node(GROUP, "messageInputContainer_Zz9"), *CLAUDE_CHAIN[1:])
        self.assertEqual(classify(focus(Node(EDIT, "messageInput_Zz9 focused"), rebuilt), HWND), CLAUDE_CODE_INPUT)

    def test_the_input_of_another_window_is_never_this_window(self):
        self.assertEqual(classify(focus(CLAUDE_INPUT, CLAUDE_CHAIN, root=OTHER_WINDOW), HWND), OTHER)

    def test_nothing_read_or_no_window_reached_is_unavailable(self):
        self.assertEqual(classify(None, HWND), UNAVAILABLE)
        self.assertEqual(classify(focus(CLAUDE_INPUT, CLAUDE_CHAIN, root=0), HWND), UNAVAILABLE)

    def test_look_alikes_are_other(self):
        container = CLAUDE_CHAIN[0]
        cases = {
            "not an edit control": focus(Node(GROUP, "messageInput_cKsPxg"), CLAUDE_CHAIN),
            "a bare prefix": focus(Node(EDIT, "messageInput_"), (Node(GROUP, "messageInputContainer_"),
                                                                *CLAUDE_CHAIN[1:])),
            "no container": focus(CLAUDE_INPUT, CLAUDE_CHAIN[1:]),
            "container too far up": focus(CLAUDE_INPUT, (Node(GROUP), Node(GROUP), Node(GROUP), *CLAUDE_CHAIN)),
            "the container itself focused": focus(Node(EDIT, "messageInputContainer_cKsPxg messageInput_x"),
                                                  CLAUDE_CHAIN),
            "outside a webview": focus(CLAUDE_INPUT, workbench(container)),
            "frame without the webview iframe": focus(CLAUDE_INPUT, tuple(
                node for node in CLAUDE_CHAIN if "webview" not in node.class_name.split())),
            "webview class only below the frame": focus(CLAUDE_INPUT, (container, Node(GROUP, "webview"),
                                                                       Node(DOCUMENT, "", "active-frame"),
                                                                       Node(PANE, hwnd=7))),
            "the side question box": focus(Node(EDIT, "input_RY1"), webview(Node(GROUP, "inputContainer_RY1"))),
            "a markdown preview": focus(Node(DOCUMENT, "vscode-body showEditorSelection"), webview()),
            "a link in the settings editor": focus(Node(50005, "monaco-link"),
                                                   workbench(Node(GROUP, "settings-editor"))),
            "a ProseMirror editor in a webview": focus(Node(EDIT, "ProseMirror"), webview(Node(GROUP, "editor"))),
            "a list": focus(Node(50008, "monaco-list"), workbench()),
        }
        for label, read in cases.items():
            self.assertEqual(classify(read, HWND), OTHER, label)

    def test_the_text_editor(self):
        editor = workbench(Node(GROUP, "overflow-guard"), Node(GROUP, "monaco-editor no-user-select vs-dark"))
        self.assertEqual(classify(focus(Node(GROUP, "native-edit-context"), editor), HWND), TEXT_EDITOR)
        self.assertEqual(classify(focus(Node(EDIT, "inputarea monaco-mouse-cursor-text"), editor), HWND), TEXT_EDITOR)
        # The same class outside a Monaco editor is not a text editor.
        self.assertEqual(classify(focus(Node(EDIT, "inputarea"), workbench()), HWND), OTHER)

    def test_the_integrated_terminal(self):
        terminal = workbench(Node(GROUP, "xterm-screen"), Node(GROUP, "terminal xterm focus"))
        self.assertEqual(classify(focus(Node(EDIT, "xterm-helper-textarea"), terminal), HWND), TERMINAL)
        self.assertEqual(classify(focus(Node(EDIT, "xterm-helper-textarea"), workbench()), HWND), TERMINAL)


class FakeReader:
    """``read()`` gives ``focus``, or waits for ``gate``, or raises ``error``; records its thread."""

    def __init__(self, read=None, gate=None, error=None):
        self.focus = read
        self.gate = gate
        self.error = error
        self.reads = 0
        self.threads = set()
        self.closed = False
        self.started = threading.Event()

    def read(self):
        self.reads += 1
        self.threads.add(threading.get_ident())
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.error is not None:
            raise self.error
        return self.focus

    def close(self):
        self.closed = True


class FocusProbeTest(unittest.TestCase):
    def probe(self, reader, budget_s=2.0):
        made = []

        def factory():
            made.append(threading.get_ident())
            return reader

        probe = FocusProbe(factory, budget_s=budget_s)
        self.addCleanup(probe.close)
        return probe, made

    def test_verdict_read_on_its_own_thread(self):
        reader = FakeReader(focus(CLAUDE_INPUT, CLAUDE_CHAIN))
        probe, made = self.probe(reader)
        self.assertEqual(probe(HWND), CLAUDE_CODE_INPUT)
        self.assertEqual(probe.probe(OTHER_WINDOW), OTHER)
        self.assertEqual(reader.reads, 2)
        self.assertEqual(len(made), 1)  # one reader, built once
        self.assertNotIn(threading.get_ident(), reader.threads | set(made))
        self.assertEqual(set(made), reader.threads)  # built and used on the same worker thread

    def test_no_window_is_unavailable_without_a_read(self):
        reader = FakeReader(focus(CLAUDE_INPUT, CLAUDE_CHAIN))
        probe, made = self.probe(reader)
        self.assertEqual(probe(0), UNAVAILABLE)
        self.assertEqual((made, reader.reads), ([], 0))

    def test_a_slow_read_times_out_and_blocks_no_second_read(self):
        gate = threading.Event()
        reader = FakeReader(focus(CLAUDE_INPUT, CLAUDE_CHAIN), gate=gate)
        probe, _ = self.probe(reader, budget_s=0.05)
        started = time.perf_counter()
        with self.assertLogs("quill.uia", level="WARNING") as logs:
            self.assertEqual(probe(HWND), TIMEOUT)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertIn("focus check timed out", "\n".join(logs.output))
        # While the first read is still running, a new request is not queued behind it.
        self.assertEqual(probe(HWND), TIMEOUT)
        self.assertEqual(reader.reads, 1)
        gate.set()
        deadline = time.monotonic() + 5
        while probe._busy and time.monotonic() < deadline:
            time.sleep(0.01)
        reader.gate = None
        self.assertEqual(probe(HWND), CLAUDE_CODE_INPUT)  # the late answer is not reused: a fresh read
        self.assertEqual(reader.reads, 2)

    def test_a_failing_read_is_unavailable_and_the_next_one_works(self):
        reader = FakeReader(focus(CLAUDE_INPUT, CLAUDE_CHAIN), error=OSError("fake: COM failure"))
        probe, _ = self.probe(reader)
        with self.assertLogs("quill.uia", level="WARNING") as logs:
            self.assertEqual(probe(HWND), UNAVAILABLE)
        self.assertIn("focus check unavailable (OSError)", "\n".join(logs.output))
        reader.error = None
        self.assertEqual(probe(HWND), CLAUDE_CODE_INPUT)

    def test_a_reader_that_cannot_be_built_stays_unavailable(self):
        calls = []

        def factory():
            calls.append(1)
            raise OSError("fake: no UI Automation")

        probe = FocusProbe(factory, budget_s=2.0)
        self.addCleanup(probe.close)
        with self.assertLogs("quill.uia", level="WARNING"):
            self.assertEqual(probe(HWND), UNAVAILABLE)
        self.assertEqual(probe(HWND), UNAVAILABLE)
        self.assertEqual(len(calls), 1)

    def test_close_releases_the_reader(self):
        reader = FakeReader(focus(CLAUDE_INPUT, CLAUDE_CHAIN))
        probe, _ = self.probe(reader)
        probe(HWND)
        probe.close()
        deadline = time.monotonic() + 5
        while not reader.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(reader.closed)

    def test_budget_must_be_positive(self):
        for budget in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                FocusProbe(FakeReader, budget_s=budget)

    def test_the_real_reader_is_never_built_in_tests(self):
        with self.assertRaises(AssertionError):
            uia.ComUia()


class ReadOnlyTest(unittest.TestCase):
    """The real reader only calls the read slots it names: never SetFocus, a pattern, a value or a text."""

    def test_only_identifier_reads_and_settings_are_called(self):
        source = Path(uia.__file__).read_text(encoding="utf-8")
        called = set(re.findall(r"self\._call\([^,]+,\s*([A-Z_]+)", source))
        self.assertEqual(called, {"PUT_AUTO_SET_FOCUS", "PUT_CONNECTION_TIMEOUT", "PUT_TRANSACTION_TIMEOUT",
                                  "GET_RAW_VIEW_WALKER", "GET_FOCUSED_ELEMENT", "GET_PARENT_ELEMENT",
                                  "GET_CURRENT_CONTROL_TYPE", "GET_CURRENT_NATIVE_WINDOW_HANDLE"})
        strings = set(re.findall(r"self\._string\([^,]+,\s*([A-Z_]+)", source))
        self.assertEqual(strings, {"GET_CURRENT_CLASS_NAME", "GET_CURRENT_AUTOMATION_ID", "GET_CURRENT_NAME"})
        # Slots from UIAutomationClient.h: IUIAutomationElement SetFocus is 3, the value/text reads are patterns.
        self.assertEqual((uia.GET_FOCUSED_ELEMENT, uia.GET_RAW_VIEW_WALKER, uia.PUT_AUTO_SET_FOCUS,
                          uia.PUT_CONNECTION_TIMEOUT, uia.PUT_TRANSACTION_TIMEOUT), (8, 16, 59, 61, 63))
        self.assertEqual((uia.GET_PARENT_ELEMENT, uia.GET_CURRENT_CONTROL_TYPE, uia.GET_CURRENT_NAME,
                          uia.GET_CURRENT_AUTOMATION_ID, uia.GET_CURRENT_CLASS_NAME,
                          uia.GET_CURRENT_NATIVE_WINDOW_HANDLE), (3, 21, 23, 29, 30, 36))
        self.assertNotIn("SetFocus", re.sub(r'"""[\s\S]*?"""|#.*', "", source))

    def test_the_focused_element_name_is_never_read(self):
        source = Path(uia.__file__).read_text(encoding="utf-8")
        self.assertIn("focused = self._node(element, with_name=False)", source)
        self.assertEqual(source.count("with_name=True"), 1)  # the ancestors only


if __name__ == "__main__":
    unittest.main()
