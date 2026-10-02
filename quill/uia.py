"""Read-only UI Automation: what kind of element has the keyboard focus in a window.

The send triggers press Enter only in Claude Code. In VS Code the Claude Code
editor tab (a webview panel) cannot be told apart from any other tab by the
window title, so ``FocusProbe`` asks Windows UI Automation which element has
the keyboard focus and gives one verdict:

- ``claude_code_input``: the message input of the Claude Code extension (the
  sidebar view or the editor tab);
- ``text_editor``: a VS Code text editor (Monaco);
- ``terminal``: the VS Code integrated terminal (xterm.js);
- ``other``: anything else (another webview such as a Markdown preview or the
  settings, a list, a button, focus in another window);
- ``unavailable``: UI Automation cannot be used or describes nothing (VS Code
  without accessibility, an error);
- ``timeout``: no answer within the time bound.

Only ``claude_code_input`` may lead to an Enter. Of each element the reader
reads the control type, the class name, the automation id and the native
window handle, and of its ancestors also the name, walking up at most
``MAX_ANCESTORS`` parents until the first element backed by a window. It
never reads the focused element's own name, a value or a text, never sets the
focus, never calls a control pattern, and switches UI Automation's automatic
focus setting off. Names stay in memory: nothing read here is logged or
printed, only the verdict.

The COM calls (raw ctypes over ``IUIAutomation2``, no package) run on one
worker thread of their own, never on the hook thread, with UI Automation's
connection and transaction timeouts set; ``FocusProbe`` also waits at most
``budget_s`` for the answer. While a timed-out read is still running, a new
request gets ``timeout`` at once: at most one read is in flight.

Identifiers of Claude Code for VS Code, read in the installed extension
version 2.1.287 (``webview/index.js``) and checked live in VS Code with
``editor.accessibilitySupport`` "on": the message input is a contenteditable
``div`` with role ``textbox`` (UI Automation control type Edit) whose class is
the CSS module class ``messageInput_<hash>``, inside a ``div`` of class
``messageInputContainer_<hash>``; above them VS Code's webview frame (a document
with automation id ``active-frame``, inside an iframe of class ``webview``).
The hash changes between builds, so only the ``messageInput_`` and
``messageInputContainer_`` prefixes are compared. The input is about 18
ancestors below its window, the same in the sidebar view and the editor tab.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

log = logging.getLogger("quill.uia")

CLAUDE_CODE_INPUT = "claude_code_input"
TEXT_EDITOR = "text_editor"
TERMINAL = "terminal"
OTHER = "other"
UNAVAILABLE = "unavailable"
TIMEOUT = "timeout"
VERDICTS = (CLAUDE_CODE_INPUT, TEXT_EDITOR, TERMINAL, OTHER, UNAVAILABLE, TIMEOUT)

# UI Automation control type ids (UIAutomationClient.h).
EDIT = 50004
DOCUMENT = 50030

MAX_ANCESTORS = 64  # parents walked up from the focused element
MAX_CLASS_CHARS = 256  # longer class names, automation ids and names are cut
DEFAULT_BUDGET_S = 0.8  # how long a caller waits for a verdict
CONNECTION_TIMEOUT_MS = 500
TRANSACTION_TIMEOUT_MS = 400

# Class names of Claude Code for VS Code 2.1.287 (CSS modules: "<name>_<hash>").
CLAUDE_INPUT_PREFIX = "messageInput_"
CLAUDE_CONTAINER_PREFIX = "messageInputContainer_"
CLAUDE_CONTAINER_DEPTH = 3  # the container is the input's parent; allow a wrapper or two
# VS Code 1.10x webviews: the extension's page is the document with this automation id (the inner
# frame), inside an iframe of class "webview".
WEBVIEW_FRAME_ID = "active-frame"
WEBVIEW_CLASS = "webview"
# Class names of VS Code's own editors.
MONACO_INPUTS = frozenset({"inputarea", "native-edit-context"})
MONACO_EDITOR = "monaco-editor"
XTERM_INPUT = "xterm-helper-textarea"
XTERM = "xterm"


@dataclass(frozen=True)
class Node:
    """What is read of one element: identifiers only (``name`` for ancestors, never for the focused element)."""

    control_type: int = 0
    class_name: str = ""
    automation_id: str = ""
    name: str = field(default="", repr=False)
    hwnd: int = 0  # native window handle; 0 for an element inside a web page


@dataclass(frozen=True)
class Focus:
    """The focused element and its ancestors (nearest first) up to the first one backed by a window.

    ``root`` is the top-level window of that window (0 when none was reached
    within ``MAX_ANCESTORS``).
    """

    element: Node
    ancestors: tuple[Node, ...] = ()
    root: int = 0


def _classes(node: Node) -> list[str]:
    return node.class_name.split()


def _has_class(node: Node, wanted: frozenset[str] | set[str]) -> bool:
    return any(item in wanted for item in _classes(node))


def _has_prefix(node: Node, prefix: str) -> bool:
    return any(item.startswith(prefix) and len(item) > len(prefix) for item in _classes(node))


def _in_webview(ancestors: Sequence[Node]) -> bool:
    """Inside a VS Code webview: the extension's frame document, then the webview iframe above it."""
    for index, node in enumerate(ancestors):
        if node.control_type == DOCUMENT and node.automation_id == WEBVIEW_FRAME_ID:
            return any(_has_class(above, {WEBVIEW_CLASS}) for above in ancestors[index + 1:])
    return False


def classify(focus: Focus | None, hwnd: int) -> str:
    """The verdict for a focus read in window ``hwnd`` (a pure function of what was read)."""
    if focus is None:
        return UNAVAILABLE
    if not focus.root:
        return UNAVAILABLE
    if focus.root != hwnd:
        return OTHER  # the focus is in another window
    element, ancestors = focus.element, focus.ancestors
    if (element.control_type == EDIT and _has_prefix(element, CLAUDE_INPUT_PREFIX)
            and not _has_prefix(element, CLAUDE_CONTAINER_PREFIX)
            and any(_has_prefix(node, CLAUDE_CONTAINER_PREFIX) for node in ancestors[:CLAUDE_CONTAINER_DEPTH])
            and _in_webview(ancestors)):
        return CLAUDE_CODE_INPUT
    if _has_class(element, {XTERM_INPUT}) or any(_has_class(node, {XTERM}) for node in ancestors):
        return TERMINAL
    if _has_class(element, MONACO_INPUTS) and any(_has_class(node, {MONACO_EDITOR}) for node in ancestors):
        return TEXT_EDITOR
    return OTHER


# ---------------------------------------------------------------- COM (ctypes)


class UiaError(OSError):
    """UI Automation is not available or a call failed."""


def _guid(text: str) -> object:
    from ctypes import wintypes  # noqa: F401 - Windows only

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    hexa = text.strip("{}").replace("-", "")
    data4 = bytes.fromhex(hexa[16:])
    return GUID(int(hexa[:8], 16), int(hexa[8:12], 16), int(hexa[12:16], 16), (ctypes.c_ubyte * 8)(*data4))


CLSID_CUIAUTOMATION8 = "{E22AD333-B25F-460C-83D0-0581107395C9}"
IID_IUIAUTOMATION2 = "{34723AFF-0C9D-49D0-9896-7AB52DF8CD8A}"
COINIT_MULTITHREADED = 0x0
CLSCTX_INPROC_SERVER = 0x1
RPC_E_CHANGED_MODE = -2147417850

# Vtable slots (IUnknown takes 0-2), from UIAutomationClient.h.
RELEASE = 2
# IUIAutomation / IUIAutomation2
GET_FOCUSED_ELEMENT = 8
GET_RAW_VIEW_WALKER = 16
PUT_AUTO_SET_FOCUS = 59
PUT_CONNECTION_TIMEOUT = 61
PUT_TRANSACTION_TIMEOUT = 63
# IUIAutomationTreeWalker
GET_PARENT_ELEMENT = 3
# IUIAutomationElement (only "get_Current*" identifier reads)
GET_CURRENT_CONTROL_TYPE = 21
GET_CURRENT_NAME = 23
GET_CURRENT_AUTOMATION_ID = 29
GET_CURRENT_CLASS_NAME = 30
GET_CURRENT_NATIVE_WINDOW_HANDLE = 36


class ComUia:
    """The real reader: ``IUIAutomation2`` through ctypes. Create and use it on one thread only.

    Only the vtable slots named above are ever called: GetFocusedElement,
    the raw-view walker's GetParentElement and the identifier reads of an
    element, plus the AutoSetFocus (set to off) and timeout settings.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise UiaError("UI Automation is only available on Windows")
        from ctypes import wintypes

        self._ole32 = ctypes.WinDLL("ole32")
        self._oleaut32 = ctypes.WinDLL("oleaut32")
        self._user32 = ctypes.WinDLL("user32")
        self._ole32.CoInitializeEx.restype = ctypes.c_long
        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self._ole32.CoCreateInstance.restype = ctypes.c_long
        self._ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_void_p)]
        self._ole32.CoUninitialize.restype = None
        self._oleaut32.SysStringLen.restype = ctypes.c_uint
        self._oleaut32.SysStringLen.argtypes = [ctypes.c_void_p]
        self._oleaut32.SysFreeString.restype = None
        self._oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
        self._user32.GetAncestor.restype = ctypes.c_void_p
        self._user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        hr = self._ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            raise UiaError(f"CoInitializeEx failed (0x{hr & 0xFFFFFFFF:08X})")
        self._initialized = hr >= 0
        self._automation = ctypes.c_void_p()
        self._walker = ctypes.c_void_p()
        clsid, iid = _guid(CLSID_CUIAUTOMATION8), _guid(IID_IUIAUTOMATION2)
        try:
            hr = self._ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid),
                                              ctypes.byref(self._automation))
            if hr < 0 or not self._automation:
                raise UiaError(f"UI Automation not created (0x{hr & 0xFFFFFFFF:08X})")
            self._call(self._automation, PUT_AUTO_SET_FOCUS, (ctypes.c_int,), 0)
            self._call(self._automation, PUT_CONNECTION_TIMEOUT, (ctypes.c_ulong,), CONNECTION_TIMEOUT_MS)
            self._call(self._automation, PUT_TRANSACTION_TIMEOUT, (ctypes.c_ulong,), TRANSACTION_TIMEOUT_MS)
            self._call(self._automation, GET_RAW_VIEW_WALKER, (ctypes.POINTER(ctypes.c_void_p),),
                       ctypes.byref(self._walker))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _call(pointer: ctypes.c_void_p, slot: int, argtypes: tuple, *args: object) -> None:
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
        hr = function(pointer, *args)
        if hr < 0:
            raise UiaError(f"UI Automation call {slot} failed (0x{hr & 0xFFFFFFFF:08X})")

    @staticmethod
    def _release(pointer: ctypes.c_void_p) -> None:
        if pointer:
            vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[RELEASE])(pointer)

    def _string(self, element: ctypes.c_void_p, slot: int) -> str:
        bstr = ctypes.c_void_p()
        self._call(element, slot, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(bstr))
        if not bstr:
            return ""
        try:
            size = min(int(self._oleaut32.SysStringLen(bstr)), MAX_CLASS_CHARS)
            return ctypes.wstring_at(bstr, size)
        finally:
            self._oleaut32.SysFreeString(bstr)

    def _node(self, element: ctypes.c_void_p, with_name: bool) -> Node:
        control = ctypes.c_int()
        self._call(element, GET_CURRENT_CONTROL_TYPE, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(control))
        handle = ctypes.c_void_p()
        self._call(element, GET_CURRENT_NATIVE_WINDOW_HANDLE, (ctypes.POINTER(ctypes.c_void_p),),
                   ctypes.byref(handle))
        return Node(control_type=int(control.value), class_name=self._string(element, GET_CURRENT_CLASS_NAME),
                    automation_id=self._string(element, GET_CURRENT_AUTOMATION_ID),
                    name=self._string(element, GET_CURRENT_NAME) if with_name else "",
                    hwnd=int(handle.value or 0))

    def read(self) -> Focus | None:
        """The focused element and its ancestors up to the first window-backed one; None without a focus."""
        element = ctypes.c_void_p()
        self._call(self._automation, GET_FOCUSED_ELEMENT, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(element))
        if not element:
            return None
        ancestors: list[Node] = []
        current = element
        try:
            focused = self._node(element, with_name=False)
            window = focused.hwnd
            while not window and len(ancestors) < MAX_ANCESTORS:
                parent = ctypes.c_void_p()
                self._call(self._walker, GET_PARENT_ELEMENT, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                           current, ctypes.byref(parent))
                if current is not element:
                    self._release(current)
                current = parent
                if not parent:
                    break
                node = self._node(parent, with_name=True)
                ancestors.append(node)
                window = node.hwnd
        finally:
            if current is not element:
                self._release(current)  # also after a failed call: no parent is left referenced
            self._release(element)
        root = int(self._user32.GetAncestor(window, 2) or 0) if window else 0  # GA_ROOT
        return Focus(focused, tuple(ancestors), root)

    def close(self) -> None:
        self._release(self._walker)
        self._release(self._automation)
        self._walker = self._automation = ctypes.c_void_p()
        if self._initialized:
            self._initialized = False
            self._ole32.CoUninitialize()


# ---------------------------------------------------------------- bounded probe


class FocusProbe:
    """``probe(hwnd)``: the focus verdict of window ``hwnd`` within ``budget_s`` seconds.

    ``reader_factory`` builds the reader (``ComUia``; tests pass a fake with
    ``read()`` and ``close()``) on the probe's own worker thread, the first
    time a verdict is asked; a reader that cannot be built gives
    ``unavailable`` from then on. Never raises.
    """

    def __init__(self, reader_factory: Callable[[], object] = ComUia, budget_s: float = DEFAULT_BUDGET_S) -> None:
        if not budget_s > 0:
            raise ValueError("budget_s must be positive")
        self.reader_factory = reader_factory
        self.budget_s = budget_s
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue | None = None
        self._thread: threading.Thread | None = None
        self._busy = False
        self._broken = False

    def __call__(self, hwnd: int) -> str:
        return self.probe(hwnd)

    def probe(self, hwnd: int) -> str:
        if not hwnd:
            return UNAVAILABLE
        with self._lock:
            if self._broken:
                return UNAVAILABLE
            if self._busy:
                return TIMEOUT  # an earlier read has not answered yet: never two at once
            self._busy = True
            if self._thread is None:
                self._jobs = queue.SimpleQueue()
                self._thread = threading.Thread(target=self._run, args=(self._jobs,), name="quill-uia", daemon=True)
                self._thread.start()
            jobs = self._jobs
        done = threading.Event()
        answer: list[str] = []
        started = time.perf_counter()
        jobs.put((hwnd, answer, done))
        if not done.wait(self.budget_s):
            log.warning("focus check timed out after %.0f ms", (time.perf_counter() - started) * 1000)
            return TIMEOUT
        return answer[0] if answer else UNAVAILABLE

    def _run(self, jobs: queue.SimpleQueue) -> None:
        reader = None
        while True:
            job = jobs.get()
            if job is None:
                break
            hwnd, answer, done = job
            try:
                if reader is None:
                    reader = self.reader_factory()
                answer.append(classify(reader.read(), hwnd))
            except Exception as exc:  # noqa: BLE001 - any failure is "unavailable", never an Enter
                log.warning("focus check unavailable (%s)", type(exc).__name__)
                if reader is None:
                    with self._lock:
                        self._broken = True
                answer.append(UNAVAILABLE)
            finally:
                with self._lock:
                    self._busy = False
                done.set()
        if reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """Ends the worker thread (after a read in flight); a later ``probe`` starts a new one."""
        with self._lock:
            jobs, self._jobs, self._thread = self._jobs, None, None
            self._broken = False
        if jobs is not None:
            jobs.put(None)

