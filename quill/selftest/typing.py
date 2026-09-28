"""Automated typing test: type a fixed corpus into real apps and read it back.

Usage: py -3.12 -m quill.selftest.typing --allow-desktop-input [--targets edit,console]

Manual only: it takes over the keyboard focus and types for about a minute, so
it is never part of automated tests or task checks. Without
``--allow-desktop-input`` it exits 2 before creating any window, process,
hook or input.

The test creates every window it types into and never touches another one:

- ``edit``: a Win32 multi-line edit window owned by this process;
- ``edge-textarea`` and ``edge-contenteditable``: Edge ``--app`` windows with
  a temporary profile, served from 127.0.0.1 by this process; each page posts
  its value back to the server;
- ``console``: a conhost window (launched explicitly, never a shared
  terminal) running a raw key reader that records what it receives;
- ``vscode``: VS Code with a temporary user data folder, no extensions and
  auto-save, editing a temporary file.

A window is used only when its process is one this test started and its title
carries this run's random nonce. Before each case the window is brought to the
foreground; the injector then checks before every burst that it is still the
foreground window and aborts otherwise. Each target receives every case in
turn and its whole content must equal the expected text: any lost, extra or
changed character fails the run. The clipboard is compared (formats and bytes)
before and after each target and restored if anything changed it.

The VS Code target turns off auto-closing, suggestions and auto-indent: those
editor features rewrite typed text and are not part of the input path.

Everything opened is closed and temporary folders are removed. An aggregate
result (per target and case: counts of typed, lost, extra and changed
characters, reason codes and times; never the typed text) is written to
``local/selftest/typing-<UTC time>.json``. Exit codes: 0 all targets match,
1 any mismatch or failure, 2 refused (flag missing) or usage error. Text typed
here is invented; nothing personal is involved.
"""

from __future__ import annotations

import argparse
import ctypes
import difflib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from quill import clipboard
from quill.inject import NEWLINE_SHIFT_ENTER, NEWLINE_SPACE, InjectOptions, Injector, Target, normalize_text
from quill.win32 import ULONG_PTR, User32, Win32Error, bind

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "local" / "selftest"
RESULT_VERSION = 1
TARGET_NAMES = ("edit", "edge-textarea", "edge-contenteditable", "console", "vscode")
READ_TIMEOUT_S = 15.0
SETTLE_S = 0.6
TITLE_PREFIX = "Quill typing selftest"

REFUSAL = (
    "refusing to run: this self-test opens windows, takes the keyboard focus and types into them.\n"
    "Pass --allow-desktop-input to run it, and do not use the keyboard or mouse until it ends."
)


# ---------------------------------------------------------------- desktop layer


class SelftestUser32(User32):
    """The product Win32 layer plus the calls this self-test needs for windows it created.

    Moving the foreground and closing windows are deliberately absent from
    ``User32``: the product never steals the focus.
    """

    WM_CLOSE = 0x0010
    WM_GETTEXT = 0x000D
    WM_GETTEXTLENGTH = 0x000E
    SW_RESTORE = 9

    def __init__(self) -> None:
        super().__init__()
        from ctypes import wintypes as w

        user32, kernel32 = self._user32, self._kernel32
        bind(user32, "SetForegroundWindow", w.BOOL, w.HWND)
        bind(user32, "BringWindowToTop", w.BOOL, w.HWND)
        bind(user32, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
        bind(user32, "AttachThreadInput", w.BOOL, w.DWORD, w.DWORD, w.BOOL)
        bind(user32, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        bind(user32, "SendMessageTimeoutW", w.LPARAM, w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
             ctypes.POINTER(ULONG_PTR))
        bind(kernel32, "GetCurrentThreadId", w.DWORD)

    def post_close(self, hwnd: int) -> bool:
        return bool(self._user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))

    def read_window_text(self, hwnd: int, timeout_ms: int = 2000) -> str:
        """Text of a window through WM_GETTEXT (for example an edit control)."""
        result = ULONG_PTR()
        if not self._user32.SendMessageTimeoutW(hwnd, self.WM_GETTEXTLENGTH, 0, 0, 0, timeout_ms,
                                                ctypes.byref(result)):
            raise Win32Error("window did not answer WM_GETTEXTLENGTH")
        buffer = ctypes.create_unicode_buffer(int(result.value) + 1)
        address = ctypes.cast(buffer, ctypes.c_void_p).value or 0
        if not self._user32.SendMessageTimeoutW(hwnd, self.WM_GETTEXT, len(buffer), address, 0, timeout_ms,
                                                ctypes.byref(result)):
            raise Win32Error("window did not answer WM_GETTEXT")
        return buffer.value

    def focus_window(self, hwnd: int, timeout_s: float = 3.0) -> bool:
        """Bring a window to the foreground, attaching to the current foreground thread if needed."""
        own_thread = int(self._kernel32.GetCurrentThreadId())
        deadline = time.monotonic() + timeout_s
        while True:
            self._user32.ShowWindow(hwnd, self.SW_RESTORE)
            if not self._user32.SetForegroundWindow(hwnd):
                current = self.foreground_window()
                other = self.window_thread_process(current)[0] if current else 0
                attached = bool(other) and other != own_thread and bool(
                    self._user32.AttachThreadInput(own_thread, other, True))
                try:
                    self._user32.BringWindowToTop(hwnd)
                    self._user32.SetForegroundWindow(hwnd)
                finally:
                    if attached:
                        self._user32.AttachThreadInput(own_thread, other, False)
            if self.foreground_window() == hwnd:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


# ---------------------------------------------------------------- corpus


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    newline: str | None = None  # None: the target's policy


_LONG = (
    "Bom dia equipa, segue o resumo da revisão de ontem ao módulo de pagamentos.\n"
    "Primeiro, o endpoint de checkout devolve um erro quinhentos quando o carrinho "
    "vem vazio; proponho validar isso no controller e responder com um bad request claro. "
    "Segundo, o pipeline de CI demora demasiado porque corre os testes end-to-end em série; "
    "convém reparti-los por quatro jobs paralelos. Terceiro, a migração do esquema "
    "exige um rollback ensaiado antes do deploy de sexta-feira.\n"
    "Resta rever o changelog; as notas da API ficam comigo. Obrigado!"
)
LONG_TEXT = (_LONG + " " + "Até já." * 40)[:600]

CORPUS = (
    Case("short", "Olá! Já está a funcionar, não é?"),
    Case(
        "accented",
        " ã õ ç á é ê í ó ú à â ô Ã Õ Ç Á É Ê Í Ó Ú À; “curvas” «angulares» 'simples' \"duplas\" "
        "(parênteses) [colchetes] {chavetas} <menor> @ # $ % & * + = / \\ | ~ ^ ` _ - € £ § º ª ! ? 😀 ",
    ),
    Case("long-600", LONG_TEXT),
    Case("newline-as-space", " fim da linha\nlinha seguinte", NEWLINE_SPACE),
)


# ---------------------------------------------------------------- results


@dataclass
class CaseResult:
    """One case on one target. Counts and reasons only, never the text."""

    target: str
    case: str
    ok: bool
    typed: int
    expected: int
    detail: str
    seconds: float
    lost: int = 0
    extra: int = 0
    changed: int = 0


@dataclass
class TargetOutcome:
    name: str
    ok: bool = False
    attempts: int = 0
    clipboard_identical: bool = True
    cases: list[CaseResult] = field(default_factory=list)


def diff_counts(expected: str, actual: str) -> tuple[int, int, int]:
    """Characters lost, extra and changed between the expected and the received text."""
    lost = extra = changed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, expected, actual, autojunk=False).get_opcodes():
        if tag == "delete":
            lost += i2 - i1
        elif tag == "insert":
            extra += j2 - j1
        elif tag == "replace":
            changed += max(i2 - i1, j2 - j1)
    return lost, extra, changed


def compare(expected: str, actual: str) -> str:
    """Empty when equal; otherwise counts of lost, extra and changed characters."""
    if expected == actual:
        return ""
    lost, extra, changed = diff_counts(expected, actual)
    first = next((i for i, (a, b) in enumerate(zip(expected, actual)) if a != b), min(len(expected), len(actual)))
    return (f"mismatch: expected {len(expected)} chars, got {len(actual)}; lost {lost}, extra {extra}, "
            f"changed {changed}; first difference at {first}")


def aggregate(outcomes: list[TargetOutcome], finished: datetime) -> dict[str, object]:
    """The result file: per-target and per-case counts, never typed text."""
    cases = [case for outcome in outcomes for case in outcome.cases]
    return {
        "selftest": "typing",
        "version": RESULT_VERSION,
        "finished_utc": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": bool(outcomes) and all(outcome.ok for outcome in outcomes),
        "totals": {
            "targets": len(outcomes),
            "targets_passed": sum(outcome.ok for outcome in outcomes),
            "cases": len(cases),
            "cases_passed": sum(case.ok for case in cases),
            "characters_expected": sum(case.expected for case in cases),
            "characters_typed": sum(case.typed for case in cases),
            "lost": sum(case.lost for case in cases),
            "extra": sum(case.extra for case in cases),
            "changed": sum(case.changed for case in cases),
            "clipboard_changed_targets": sum(not outcome.clipboard_identical for outcome in outcomes),
        },
        "targets": [
            {
                "name": outcome.name,
                "ok": outcome.ok,
                "attempts": outcome.attempts,
                "clipboard_identical": outcome.clipboard_identical,
                "cases": [{key: value for key, value in asdict(case).items() if key != "target"}
                          for case in outcome.cases],
            }
            for outcome in outcomes
        ],
    }


def write_result(data: dict[str, object], folder: Path, finished: datetime) -> Path:
    """Write the aggregate under ``folder`` (inside the ignored local/ folder)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"typing-{finished.strftime('%Y%m%dT%H%M%SZ')}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    os.replace(temporary, path)
    return path


# ---------------------------------------------------------------- helpers


def kill_tree(pid: int) -> None:
    """Force-close a process this test started, with its children."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


def remove_tree(path: Path) -> None:
    for _ in range(20):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.25)


def wait_for(condition: Callable[[], object], timeout_s: float, step_s: float = 0.1) -> object:
    deadline = time.monotonic() + timeout_s
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() >= deadline:
            return value
        time.sleep(step_s)


def clean_env() -> dict[str, str]:
    """Environment without host-editor variables that would reroute VS Code or Electron."""
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("VSCODE_", "ELECTRON_"))}


class TargetError(RuntimeError):
    """A target could not be created or verified."""


class Base:
    name = ""
    newline = NEWLINE_SHIFT_ENTER

    def __init__(self, api: SelftestUser32, nonce: str) -> None:
        self.api = api
        self.nonce = nonce
        self.hwnd = 0
        self.owned_pids: set[int] = set()

    @property
    def title(self) -> str:
        return f"{TITLE_PREFIX} {self.nonce} {self.name}"

    def find_window(self, timeout_s: float, title_part: str | None = None, cls: str | None = None) -> int:
        """A visible top-level window of an owned process whose title has the marker."""
        marker = title_part or self.title

        def search() -> int:
            for hwnd in self.api.top_level_windows():
                if not self.api.is_visible(hwnd) or self.api.window_process_id(hwnd) not in self.owned_pids:
                    continue
                if marker in self.api.window_text(hwnd) and (cls is None or self.api.window_class(hwnd) == cls):
                    return hwnd
            return 0

        hwnd = int(wait_for(search, timeout_s, 0.2) or 0)
        if not hwnd:
            raise TargetError(f"{self.name}: window did not appear")
        return hwnd

    def verify_owned(self) -> Target:
        pid = self.api.window_process_id(self.hwnd)
        if pid not in self.owned_pids or self.nonce not in self.api.window_text(self.hwnd):
            raise TargetError(f"{self.name}: window is not one this test created")
        return Target(self.hwnd, pid)

    def open(self) -> None:
        raise NotImplementedError

    def input_focused(self) -> bool:
        """The element that should receive the text has the keyboard focus (when observable)."""
        return True

    def focus_losses(self) -> int:
        """How often the receiving element lost the focus so far (when observable)."""
        return 0

    def read(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------- Win32 edit


class EditTarget(Base):
    name = "edit"

    def __init__(self, api: SelftestUser32, nonce: str) -> None:
        super().__init__(api, nonce)
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._error = ""
        self._procedure: object = None
        self.edit = 0

    def open(self) -> None:
        self.owned_pids = {os.getpid()}
        self._thread = threading.Thread(target=self._run, name="edit-target", daemon=True)
        self._thread.start()
        if not self._ready.wait(10) or not self.hwnd:
            raise TargetError(f"edit: window not created {self._error}".strip())

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wndproc_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                          wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", wndproc_type), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.DefWindowProcW.restype = wintypes.LPARAM
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.SendMessageW.restype = wintypes.LPARAM
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SetFocus.argtypes = [wintypes.HWND]
        user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.BOOL]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        ws_overlappedwindow, ws_visible, ws_child, ws_vscroll = 0x00CF0000, 0x10000000, 0x40000000, 0x00200000
        es_multiline, es_autovscroll, es_wantreturn = 0x0004, 0x0040, 0x1000
        wm_destroy, wm_size, wm_setfocus, em_setlimittext = 0x0002, 0x0005, 0x0007, 0x00C5
        instance = kernel32.GetModuleHandleW(None)
        edit = [0]

        def procedure(hwnd: int, message: int, wparam: int, lparam: int) -> int:
            if message == wm_setfocus and edit[0]:
                user32.SetFocus(edit[0])
                return 0
            if message == wm_size and edit[0]:
                user32.MoveWindow(edit[0], 0, 0, lparam & 0xFFFF, (lparam >> 16) & 0xFFFF, True)
                return 0
            if message == wm_destroy:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._procedure = wndproc_type(procedure)  # kept alive while the window exists
        class_name = f"QuillTypingSelftest{self.nonce}"
        window_class = WNDCLASSW(lpfnWndProc=self._procedure, hInstance=instance, lpszClassName=class_name,
                                 hbrBackground=6)  # COLOR_WINDOW + 1
        self._thread_id = int(kernel32.GetCurrentThreadId())
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            self._error = f"(register error {ctypes.get_last_error()})"
            self._ready.set()
            return
        frame = user32.CreateWindowExW(0, class_name, self.title, ws_overlappedwindow | ws_visible,
                                       120, 120, 900, 600, None, None, instance, None)
        edit[0] = frame and user32.CreateWindowExW(
            0, "EDIT", "", ws_child | ws_visible | ws_vscroll | es_multiline | es_autovscroll | es_wantreturn,
            0, 0, 880, 560, frame, None, instance, None,
        )
        if not frame or not edit[0]:
            self._error = f"(error {ctypes.get_last_error()})"
            self._ready.set()
            return
        user32.SendMessageW(edit[0], em_setlimittext, 0, 0)
        user32.SetFocus(edit[0])
        self.edit = int(edit[0])
        self.hwnd = int(frame)
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def read(self) -> str:
        return self.api.read_window_text(self.edit).replace("\r\n", "\n")

    def close(self) -> None:
        if self.hwnd and self.api.is_window(self.hwnd):
            self.api.post_close(self.hwnd)
        if self._thread_id:
            wm_quit = 0x0012
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, wm_quit, 0, 0)
        if self._thread is not None:
            self._thread.join(5)


# ---------------------------------------------------------------- Edge


PAGE = """<!doctype html>
<html lang="pt-PT"><head><meta charset="utf-8"><title>{title}</title>
<style>
html, body {{ margin: 0; height: 100%; }}
#field {{ box-sizing: border-box; width: 100%; height: 100%; border: 0; padding: 12px;
  font: 16px/1.4 sans-serif; white-space: pre-wrap; outline: none; overflow: auto; }}
</style></head>
<body>{element}
<script>
const nonce = {nonce_json}, field = {field_json};
const el = document.getElementById("field");
const value = () => (field === "textarea" ? el.value : el.innerText);
let seq = 0;
function send() {{
  seq += 1;
  fetch("/state", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ nonce, field, seq, value: value(), blurs,
      focused: document.hasFocus() && document.activeElement === el }}) }}).catch(() => {{}});
}}
let blurs = 0;
el.addEventListener("input", send);
el.addEventListener("focus", send);
el.addEventListener("blur", () => {{ blurs += 1; send(); }});
window.addEventListener("focus", () => el.focus());
el.focus();
send();
setInterval(send, 250);
</script></body></html>
"""

ELEMENTS = {
    "textarea": '<textarea id="field" spellcheck="false" autocomplete="off" autocorrect="off" '
                'autocapitalize="off"></textarea>',
    "contenteditable": '<div id="field" contenteditable="true" spellcheck="false" autocorrect="off" '
                       'autocapitalize="off"></div>',
}


class PageServer:
    """Serves the two test pages on 127.0.0.1 and keeps the latest value each page posts."""

    MAX_BODY = 1_000_000

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        # field -> (seq, value, focused, blur count), latest seq wins
        self.values: dict[str, tuple[int, str, bool, int]] = {}
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                field = self.path.strip("/").split("?")[0]
                expected = f"{server.nonce}/"
                if not field.startswith(expected) or field[len(expected):] not in ELEMENTS:
                    self.send_error(404)
                    return
                name = field[len(expected):]
                body = PAGE.format(
                    title=f"{TITLE_PREFIX} {server.nonce} edge-{name}", element=ELEMENTS[name],
                    nonce_json=json.dumps(server.nonce), field_json=json.dumps(name),
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if self.path != "/state" or not 0 < length <= PageServer.MAX_BODY:
                    self.send_error(400)
                    return
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    ok = data["nonce"] == server.nonce and data["field"] in ELEMENTS
                    seq, value = int(data["seq"]), str(data["value"])
                    focused, blurs = bool(data["focused"]), int(data["blurs"])
                except (ValueError, KeyError, TypeError):
                    ok = False
                if not ok:
                    self.send_error(403)
                    return
                with server.lock:
                    if seq > server.values.get(data["field"], (0,))[0]:
                        server.values[data["field"]] = (seq, value, focused, blurs)
                self.send_response(204)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="page-server", daemon=True)
        self.thread.start()

    def url(self, field: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{self.nonce}/{field}"

    def value(self, field: str) -> str | None:
        with self.lock:
            entry = self.values.get(field)
        return None if entry is None else entry[1]

    def focus(self, field: str) -> tuple[bool, int]:
        """Whether the page field has the keyboard focus, and how often it lost it."""
        with self.lock:
            entry = self.values.get(field)
        return (False, 0) if entry is None else (entry[2], entry[3])

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def edge_path() -> Path | None:
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                path = Path(winreg.QueryValue(key, None))
                if path.is_file():
                    return path
        except OSError:
            pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (Path(base) / "Microsoft/Edge/Application/msedge.exe").is_file():
            return Path(base) / "Microsoft/Edge/Application/msedge.exe"
    return None


class EdgeBrowser:
    """One Edge instance with a temporary profile and its own page server."""

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        self.server: PageServer | None = None
        self.process: subprocess.Popen | None = None
        self.profile: Path | None = None

    def launch(self, field: str) -> int:
        """Open an app window for ``field``; returns the browser process id."""
        executable = edge_path()
        if executable is None:
            raise TargetError("edge: msedge.exe not found")
        if self.server is None:
            self.server = PageServer(self.nonce)
            self.profile = Path(tempfile.mkdtemp(prefix="quill-edge-"))
        args = [
            str(executable), f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-sync", "--disable-extensions", "--disable-background-networking",
            "--disable-features=Translate,msEdgeTranslate,msUndersideButton", "--new-window",
            "--window-size=900,600", "--window-position=160,160", f"--app={self.server.url(field)}",
        ]
        process = subprocess.Popen(args, env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.process is None:
            self.process = process
        return self.process.pid

    def close(self, windows: list[int], api: SelftestUser32) -> None:
        for hwnd in windows:
            if hwnd and api.is_window(hwnd):
                api.post_close(hwnd)
        if self.process is not None:
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                kill_tree(self.process.pid)
                self.process.wait(10)
        if self.server is not None:
            self.server.close()
        if self.profile is not None:
            remove_tree(self.profile)


class EdgeTarget(Base):
    def __init__(self, api: SelftestUser32, nonce: str, field: str) -> None:
        super().__init__(api, nonce)
        self.browser = EdgeBrowser(nonce)
        self.field = field
        self.name = f"edge-{field}"

    def open(self) -> None:
        self.owned_pids = {self.browser.launch(self.field)}
        self.hwnd = self.find_window(40)
        if wait_for(lambda: self.browser.server.value(self.field) is not None, 20) is False:
            raise TargetError(f"{self.name}: page did not report")
        time.sleep(3)  # a fresh browser settles its windows and focus for a few seconds after load

    def input_focused(self) -> bool:
        return self.browser.server.focus(self.field)[0]

    def focus_losses(self) -> int:
        return self.browser.server.focus(self.field)[1]

    def read(self) -> str:
        value = self.browser.server.value(self.field) or ""
        # Chromium keeps runs of typed spaces as no-break spaces inside contenteditable.
        return value.replace("\u00a0", " ") if self.field == "contenteditable" else value

    def close(self) -> None:
        self.browser.close([self.hwnd], self.api)


# ---------------------------------------------------------------- console


class ConsoleTarget(Base):
    name = "console"

    def __init__(self, api: SelftestUser32, nonce: str) -> None:
        super().__init__(api, nonce)
        self.folder = Path(tempfile.mkdtemp(prefix="quill-console-"))
        self.process: subprocess.Popen | None = None

    def open(self) -> None:
        conhost = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "conhost.exe"
        args = [str(conhost), sys.executable, "-m", "quill.selftest.typing", "--allow-desktop-input",
                "--console-reader", str(self.folder), "--nonce", self.nonce]
        self.process = subprocess.Popen(args, cwd=REPO_ROOT, env=clean_env())
        ready = self.folder / "ready.json"
        if not wait_for(ready.exists, 20):
            raise TargetError("console: reader did not start")
        info = json.loads(ready.read_text("utf-8"))
        self.owned_pids = {self.process.pid, int(info["pid"])}
        self.hwnd = int(info["hwnd"])
        if self.api.window_class(self.hwnd) != "ConsoleWindowClass":
            raise TargetError("console: reader is not in a conhost window")
        wait_for(lambda: self.nonce in self.api.window_text(self.hwnd), 5)

    def read(self) -> str:
        path = self.folder / "received.bin"
        data = path.read_bytes() if path.exists() else b""
        return data[: len(data) // 2 * 2].decode("utf-16-le", "surrogatepass")

    def close(self) -> None:
        (self.folder / "stop").write_text("stop", "utf-8")
        if self.process is not None:
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                kill_tree(self.process.pid)
                self.process.wait(10)
        remove_tree(self.folder)


def console_reader(folder: Path, nonce: str) -> int:
    """Runs inside the conhost window: records every character key it receives."""
    from ctypes import wintypes

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("padding", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD), wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    key_event, shift_pressed, enter = 0x0001, 0x0010, 0x0D
    handle = kernel32.GetStdHandle(-10)
    kernel32.SetConsoleMode(handle, 0)  # raw: no echo, no line editing, no Ctrl+C processing
    kernel32.SetConsoleTitleW(f"{TITLE_PREFIX} {nonce} console")
    print("Quill typing self-test: console reader. This window closes by itself.")
    received = folder / "received.bin"
    received.write_bytes(b"")
    (folder / "ready.json").write_text(
        json.dumps({"pid": os.getpid(), "hwnd": int(kernel32.GetConsoleWindow() or 0)}), "utf-8")
    stop = folder / "stop"
    records = (INPUT_RECORD * 128)()
    count = wintypes.DWORD()
    deadline = time.monotonic() + 600
    with received.open("ab") as out:
        while not stop.exists() and time.monotonic() < deadline:
            if kernel32.WaitForSingleObject(handle, 100) != 0:
                continue
            if not kernel32.ReadConsoleInputW(handle, records, len(records), ctypes.byref(count)):
                return 1
            units = bytearray()
            for record in records[: count.value]:
                if record.EventType != key_event:
                    continue
                key = record.Event.KeyEvent
                char = ord(key.UnicodeChar)
                if not key.bKeyDown or not char:
                    continue
                if char == enter:
                    # Shift+Enter is a line break; a bare Enter is kept as CR so it fails the comparison.
                    char = 0x0A if key.dwControlKeyState & shift_pressed else 0x0D
                units += char.to_bytes(2, "little") * max(1, key.wRepeatCount)
            if units:
                out.write(units)
                out.flush()
    return 0


# ---------------------------------------------------------------- VS Code


VSCODE_SETTINGS = {
    "workbench.startupEditor": "none",
    "workbench.enableExperiments": False,
    "workbench.tips.enabled": False,
    "workbench.secondarySideBar.defaultVisibility": "hidden",
    "chat.disableAIFeatures": True,
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "extensions.autoCheckUpdates": False,
    "extensions.autoUpdate": False,
    "security.workspace.trust.enabled": False,
    "window.restoreWindows": "none",
    "git.enabled": False,
    "files.autoSave": "afterDelay",
    "files.autoSaveDelay": 100,
    "files.eol": "\n",
    "files.encoding": "utf8",
    "files.insertFinalNewline": False,
    "files.trimTrailingWhitespace": False,
    "editor.autoClosingBrackets": "never",
    "editor.autoClosingQuotes": "never",
    "editor.autoClosingComments": "never",
    "editor.autoSurround": "never",
    "editor.autoIndent": "none",
    "editor.formatOnType": False,
    "editor.quickSuggestions": {"other": "off", "comments": "off", "strings": "off"},
    "editor.suggestOnTriggerCharacters": False,
    "editor.wordBasedSuggestions": "off",
    "editor.parameterHints.enabled": False,
    "editor.inlineSuggest.enabled": False,
    "editor.acceptSuggestionOnEnter": "off",
    "editor.unicodeHighlight.ambiguousCharacters": False,
    "editor.unicodeHighlight.invisibleCharacters": False,
    "editor.wordWrap": "on",
}


def vscode_path() -> Path | None:
    command = shutil.which("code")
    candidates = [Path(command).resolve().parent.parent / "Code.exe"] if command else []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe")
    return next((path for path in candidates if path.is_file()), None)


class VsCodeTarget(Base):
    name = "vscode"

    def __init__(self, api: SelftestUser32, nonce: str) -> None:
        super().__init__(api, nonce)
        self.folder = Path(tempfile.mkdtemp(prefix="quill-vscode-"))
        self.file = self.folder / "work" / f"quill-typing-{nonce}.txt"
        self.process: subprocess.Popen | None = None

    def open(self) -> None:
        executable = vscode_path()
        if executable is None:
            raise TargetError("vscode: Code.exe not found")
        user = self.folder / "data" / "User"
        user.mkdir(parents=True)
        (user / "settings.json").write_text(json.dumps(VSCODE_SETTINGS, indent=2), "utf-8")
        self.file.parent.mkdir()
        self.file.write_bytes(b"")
        args = [
            str(executable), "--user-data-dir", str(self.folder / "data"),
            "--extensions-dir", str(self.folder / "extensions"), "--disable-extensions",
            "--disable-workspace-trust", "--skip-release-notes", "--skip-welcome", "--new-window", str(self.file),
        ]
        self.process = subprocess.Popen(args, env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.owned_pids = {self.process.pid}
        self.hwnd = self.find_window(60, title_part=self.file.name)
        time.sleep(4)  # let the workbench restore and give the editor the focus

    def read(self) -> str:
        return self.file.read_text("utf-8") if self.file.exists() else ""

    def close(self) -> None:
        if self.hwnd and self.api.is_window(self.hwnd):
            self.api.post_close(self.hwnd)
        if self.process is not None:
            try:
                self.process.wait(15)
            except subprocess.TimeoutExpired:
                kill_tree(self.process.pid)
                self.process.wait(10)
        remove_tree(self.folder)


# ---------------------------------------------------------------- physical input


class PhysicalInputMonitor:
    """Counts keyboard and mouse-button events that were not injected.

    People using the desktop during the test move the focus and add keys, so
    the runner waits for a quiet desktop before each case and retries a target
    that physical input disturbed. Only counts and times are kept, never keys.
    """

    def __init__(self) -> None:
        self.count = 0
        self.last = 0.0
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="input-monitor", daemon=True)
        self._callbacks: list[object] = []

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hook_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_type, wintypes.HINSTANCE, wintypes.DWORD]
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        wh_keyboard_ll, wh_mouse_ll = 13, 14
        llkhf_injected, llmhf_injected = 0x10, 0x01
        buttons = {0x0201, 0x0204, 0x0207, 0x020A, 0x020B, 0x020E}  # button downs and wheels
        # KBDLLHOOKSTRUCT.flags sits at offset 8; MSLLHOOKSTRUCT.flags at offset 12.

        def keyboard(code: int, wparam: int, lparam: int) -> int:
            if code >= 0 and not ctypes.c_uint32.from_address(lparam + 8).value & llkhf_injected:
                self.count += 1
                self.last = time.monotonic()
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def mouse(code: int, wparam: int, lparam: int) -> int:
            if code >= 0 and wparam in buttons and not ctypes.c_uint32.from_address(lparam + 12).value & llmhf_injected:
                self.count += 1
                self.last = time.monotonic()
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._callbacks = [hook_type(keyboard), hook_type(mouse)]
        self._thread_id = int(kernel32.GetCurrentThreadId())
        hooks = [user32.SetWindowsHookExW(kind, callback, None, 0)
                 for kind, callback in zip((wh_keyboard_ll, wh_mouse_ll), self._callbacks)]
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        for hook in hooks:
            if hook:
                user32.UnhookWindowsHookEx(hook)

    def wait_idle(self, quiet_s: float = 1.0, timeout_s: float = 60.0) -> bool:
        """Wait until nobody has touched the keyboard or mouse buttons for ``quiet_s``."""
        return bool(wait_for(lambda: time.monotonic() - self.last >= quiet_s, timeout_s, 0.1))

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            self._thread.join(5)


# ---------------------------------------------------------------- runner


def read_settled(target: Base, expected: str) -> str:
    """Wait for the expected content, then for it to stay put (late duplicates show up)."""
    value = wait_for(lambda: target.read() == expected, READ_TIMEOUT_S, 0.1)
    if value:
        time.sleep(SETTLE_S)
        return target.read()
    last, stable_since = target.read(), time.monotonic()
    while time.monotonic() - stable_since < 1.0:
        time.sleep(0.2)
        current = target.read()
        if current != last:
            last, stable_since = current, time.monotonic()
    return last


class Disturbed(Exception):
    """Physical keyboard or mouse input happened while a case ran."""


def run_cases(target: Base, api: SelftestUser32, injector: Injector, monitor: PhysicalInputMonitor,
              report: Callable[[CaseResult], None]) -> bool:
    target.open()
    owned = target.verify_owned()
    expected = ""
    for case in CORPUS:
        policy = case.newline or target.newline
        if not monitor.wait_idle():
            report(CaseResult(target.name, case.name, False, 0, 0, "keyboard or mouse kept being used", 0))
            return False
        mark = monitor.count
        if not api.focus_window(owned.hwnd) or api.foreground_window() != owned.hwnd:
            if monitor.count != mark:
                raise Disturbed
            report(CaseResult(target.name, case.name, False, 0, 0, "could not bring the window to the front", 0))
            return False
        if not wait_for(target.input_focused, 5):
            if monitor.count != mark:
                raise Disturbed
            report(CaseResult(target.name, case.name, False, 0, 0, "text field did not get the keyboard focus", 0))
            return False
        time.sleep(0.15)
        losses = target.focus_losses()
        started = time.monotonic()
        result = injector.inject(case.text, owned, InjectOptions(newline=policy))
        elapsed = time.monotonic() - started
        before = expected
        expected += normalize_text(case.text, policy)
        counts = (0, 0, 0)
        if result.ok:
            received = read_settled(target, expected)
            detail = compare(expected, received)
            # Only this case's share of the content: earlier cases already passed.
            counts = diff_counts(expected[len(before):], received[len(before):]) if detail else counts
        else:
            detail = f"injection {result.reason}: {result.detail}"
            counts = (result.total - result.typed, 0, 0)
        if detail and monitor.count != mark:
            raise Disturbed
        if detail and target.focus_losses() != losses:
            detail += "; the text field lost the keyboard focus while typing"
        if result.clipboard_changed:
            detail = (detail + "; " if detail else "") + "clipboard changed during typing"
        report(CaseResult(target.name, case.name, not detail, result.typed, result.total, detail, elapsed, *counts))
        if detail:
            return False
    return True


def run_target(make: Callable[[], Base], api: SelftestUser32, injector: Injector, monitor: PhysicalInputMonitor,
               report: Callable[[CaseResult], None], attempts: int = 3) -> TargetOutcome:
    """Run every case on a fresh target; start over when physical input disturbed a case."""
    outcome = TargetOutcome("")
    for attempt in range(1, attempts + 1):
        target = make()
        outcome = TargetOutcome(target.name, attempts=attempt)

        def record(result: CaseResult) -> None:
            outcome.cases.append(result)
            report(result)

        before = clipboard.snapshot(api)
        disturbed = False
        try:
            ok = run_cases(target, api, injector, monitor, record)
        except Disturbed:
            disturbed, ok = True, False
        except (TargetError, OSError) as error:
            record(CaseResult(target.name, "open", False, 0, 0, str(error), 0))
            ok = False
        finally:
            target.close()
        after = clipboard.snapshot(api)
        outcome.clipboard_identical = before.identical(after)
        if outcome.clipboard_identical:
            print(f"  {target.name:<22} clipboard identical ({len(after.formats)} formats)", flush=True)
        else:
            # Quill never writes the clipboard, so a change came from someone else: report it and
            # leave the newer contents alone rather than overwrite them.
            print(f"  {target.name:<22} clipboard CHANGED: {clipboard.describe_difference(before, after)}",
                  flush=True)
            ok = False
        outcome.ok = ok
        if not disturbed:
            return outcome
        print(f"  {target.name:<22} keyboard or mouse input during a case: starting this target again "
              f"({attempt} of {attempts} attempts used)", flush=True)
    outcome.ok = False
    outcome.cases.append(
        CaseResult(outcome.name, "all", False, 0, 0, "disturbed by keyboard or mouse input on every attempt", 0))
    report(outcome.cases[-1])
    return outcome


def print_result(result: CaseResult) -> None:
    status = "ok" if result.ok else "FAIL"
    line = (f"  {result.target:<22} {result.case:<18} {status:<5} {result.typed:>4}/{result.expected:<4} chars "
            f"{result.seconds:6.2f} s")
    print(line + (f"  {result.detail}" if result.detail else ""), flush=True)


def run(names: list[str], results_dir: Path = RESULTS_DIR) -> int:
    api = SelftestUser32()
    injector = Injector(api)
    previous = api.foreground_window()
    factories: dict[str, Callable[[], Base]] = {
        "edit": lambda: EditTarget(api, secrets.token_hex(4)),
        "edge-textarea": lambda: EdgeTarget(api, secrets.token_hex(4), "textarea"),
        "edge-contenteditable": lambda: EdgeTarget(api, secrets.token_hex(4), "contenteditable"),
        "console": lambda: ConsoleTarget(api, secrets.token_hex(4)),
        "vscode": lambda: VsCodeTarget(api, secrets.token_hex(4)),
    }
    print(f"Quill typing self-test: {len(names)} targets, {len(CORPUS)} cases each "
          f"({', '.join(case.name for case in CORPUS)}). Do not use the keyboard or mouse until it ends.",
          flush=True)
    monitor = PhysicalInputMonitor()
    monitor.start()
    outcomes: list[TargetOutcome] = []
    try:
        for name in names:
            outcomes.append(run_target(factories[name], api, injector, monitor, print_result))
    finally:
        monitor.stop()
        if previous and api.is_window(previous):
            api.focus_window(previous, timeout_s=1.0)
        finished = datetime.now(timezone.utc)
        path = write_result(aggregate(outcomes, finished), results_dir, finished)
        print(f"Aggregate result (no typed text): {path.relative_to(REPO_ROOT).as_posix()}", flush=True)
    passed = sum(outcome.ok for outcome in outcomes)
    print(f"Result: {passed} of {len(names)} targets passed.", flush=True)
    return 0 if passed == len(names) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.selftest.typing", description=__doc__.splitlines()[0])
    parser.add_argument("--allow-desktop-input", action="store_true",
                        help="required: allow the test to open windows, take the focus and type into them")
    parser.add_argument("--targets", default=",".join(TARGET_NAMES),
                        help="comma-separated subset of: " + ", ".join(TARGET_NAMES))
    parser.add_argument("--console-reader", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--nonce", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.allow_desktop_input:
        print(REFUSAL, file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("this self-test needs Windows", file=sys.stderr)
        return 2
    if args.console_reader is not None:
        return console_reader(args.console_reader, args.nonce)
    names = [name.strip() for name in args.targets.split(",") if name.strip()]
    unknown = [name for name in names if name not in TARGET_NAMES]
    if unknown or not names:
        parser.error("unknown targets: " + ", ".join(unknown) if unknown else "no targets given")
    return run(names)


if __name__ == "__main__":
    sys.exit(main())
