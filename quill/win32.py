"""Thin ctypes wrapper over the Win32 calls Quill needs.

``User32`` is the only product class that touches Windows input, windows,
processes and the clipboard. The injector and the clipboard module drive any
object with the same methods, so unit tests use a fake and never inject input
or read the real clipboard.

The product layer never moves the keyboard focus: it has no
SetForegroundWindow, SetFocus or AttachThreadInput. Only the manual typing
self-test, which creates its own windows, adds those calls in a subclass. Its
only mouse input is a button click at the current pointer position
(click-to-focus); it never moves the pointer. ``LowLevelHooks`` installs the
trigger hooks and, like ``User32``, is never created by tests.

``Processes`` lists processes and reads, read-only, the current directory of
one process of the same user (``quill.projects`` finds the project of a
Claude Code session in a terminal with it). It opens a process with
query-limited and VM-read access only, never enables a privilege, and is
never created by tests either.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

# Marks every event Quill injects, so Quill's own keyboard hook can skip them.
QUILL_EXTRA_INFO = 0x5155494C  # "QUIL"

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
SCAN_SHIFT = 0x2A
SCAN_RETURN = 0x1C

# Modifiers that turn typed characters into shortcuts while held.
SHORTCUT_MODIFIERS = (VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)

# Low-level hooks.
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

GA_ROOT = 2
SM_SWAPBUTTON = 23

# Mandatory integrity levels (RID of the token's integrity SID).
INTEGRITY_UNTRUSTED = 0x0000
INTEGRITY_LOW = 0x1000
INTEGRITY_MEDIUM = 0x2000
INTEGRITY_HIGH = 0x3000
INTEGRITY_SYSTEM = 0x4000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
TOKEN_QUERY = 0x0008
TOKEN_USER = 1
TOKEN_INTEGRITY_LEVEL = 25
MAX_TOKEN_INFORMATION = 4096
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_BASIC_INFORMATION_CLASS = 0
# 64-bit layout: PEB.ProcessParameters, and RTL_USER_PROCESS_PARAMETERS.CurrentDirectory.DosPath.
PEB_PROCESS_PARAMETERS = 0x20
PARAMETERS_CURRENT_DIRECTORY = 0x38
MAX_PROCESSES = 8192  # entries of one process snapshot
MAX_DIRECTORY_CHARS = 4096  # characters of a current directory worth reading

GMEM_MOVEABLE = 0x0002


class Win32Error(OSError):
    """A Win32 call failed."""


@dataclass(frozen=True)
class MouseEvent:
    """One mouse INPUT record: button flags only, never a pointer move."""

    flags: int


@dataclass(frozen=True)
class KeyEvent:
    """One keyboard INPUT record: a Unicode code unit or a virtual key."""

    vk: int = 0
    scan: int = 0
    flags: int = 0

    @property
    def is_unicode(self) -> bool:
        return bool(self.flags & KEYEVENTF_UNICODE)

    @property
    def is_keyup(self) -> bool:
        return bool(self.flags & KEYEVENTF_KEYUP)


if sys.platform == "win32":
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ULONG_PTR),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("ExitStatus", wintypes.LONG),
            ("PebBaseAddress", ctypes.c_void_p),
            ("AffinityMask", ULONG_PTR),
            ("BasePriority", wintypes.LONG),
            ("UniqueProcessId", ULONG_PTR),
            ("InheritedFromUniqueProcessId", ULONG_PTR),
        ]

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


def bind(dll: object, name: str, restype: object, *argtypes: object) -> None:
    """Declare the signature of one exported function."""
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = list(argtypes)


def _bind_tokens(kernel32: object, advapi32: object) -> None:
    """The process and token calls ``User32`` and ``Processes`` share."""
    w = wintypes
    bind(kernel32, "GetCurrentProcess", w.HANDLE)
    bind(kernel32, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    bind(kernel32, "CloseHandle", w.BOOL, w.HANDLE)
    bind(advapi32, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    bind(advapi32, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
         ctypes.POINTER(w.DWORD))
    bind(advapi32, "GetSidSubAuthorityCount", ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p)
    bind(advapi32, "GetSidSubAuthority", ctypes.POINTER(w.DWORD), ctypes.c_void_p, w.DWORD)


def _token_information(kernel32: object, advapi32: object, process: int, kind: int) -> object | None:
    """One ``GetTokenInformation`` buffer of a process token, or None when Windows refuses it."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        if not size.value or size.value > MAX_TOKEN_INFORMATION:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, kind, buffer, size, ctypes.byref(size)):
            return None
        return buffer
    finally:
        kernel32.CloseHandle(token)


def _token_integrity(kernel32: object, advapi32: object, process: int) -> int | None:
    """Integrity RID of a process token; None when Windows refuses to tell."""
    buffer = _token_information(kernel32, advapi32, process, TOKEN_INTEGRITY_LEVEL)
    if buffer is None:
        return None
    sid = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents.Label.Sid
    count = advapi32.GetSidSubAuthorityCount(sid).contents.value
    if not count:
        return None
    return int(advapi32.GetSidSubAuthority(sid, count - 1).contents.value)


class User32:
    """Real Win32 layer: input, windows, process integrity and clipboard."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise Win32Error("Win32 input is only available on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._user32, self._kernel32, self._advapi32 = user32, kernel32, advapi32
        w = wintypes
        bind(user32, "SendInput", w.UINT, w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        bind(user32, "GetForegroundWindow", w.HWND)
        bind(user32, "IsWindow", w.BOOL, w.HWND)
        bind(user32, "IsWindowVisible", w.BOOL, w.HWND)
        bind(user32, "IsHungAppWindow", w.BOOL, w.HWND)
        bind(user32, "GetWindowThreadProcessId", w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
        bind(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
        bind(user32, "GetCursorPos", w.BOOL, ctypes.POINTER(w.POINT))
        bind(user32, "WindowFromPoint", w.HWND, w.POINT)
        bind(user32, "GetAncestor", w.HWND, w.HWND, w.UINT)
        bind(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
        bind(user32, "EnumWindows", w.BOOL, WNDENUMPROC, w.LPARAM)
        bind(user32, "GetWindowTextW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        bind(user32, "GetClassNameW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        bind(user32, "OpenClipboard", w.BOOL, w.HWND)
        bind(user32, "CloseClipboard", w.BOOL)
        bind(user32, "EmptyClipboard", w.BOOL)
        bind(user32, "EnumClipboardFormats", w.UINT, w.UINT)
        bind(user32, "GetClipboardData", w.HANDLE, w.UINT)
        bind(user32, "SetClipboardData", w.HANDLE, w.UINT, w.HANDLE)
        bind(user32, "GetClipboardSequenceNumber", w.DWORD)
        bind(user32, "GetClipboardFormatNameW", ctypes.c_int, w.UINT, w.LPWSTR, ctypes.c_int)
        _bind_tokens(kernel32, advapi32)
        bind(kernel32, "GetCurrentProcessId", w.DWORD)
        bind(kernel32, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
        bind(kernel32, "GlobalAlloc", w.HGLOBAL, w.UINT, ctypes.c_size_t)
        bind(kernel32, "GlobalFree", w.HGLOBAL, w.HGLOBAL)
        bind(kernel32, "GlobalLock", ctypes.c_void_p, w.HGLOBAL)
        bind(kernel32, "GlobalUnlock", w.BOOL, w.HGLOBAL)
        bind(kernel32, "GlobalSize", ctypes.c_size_t, w.HGLOBAL)

    # ------------------------------------------------------------ input

    def send_input(self, events: list[KeyEvent]) -> int:
        """Inject keyboard events in one call, tagged with the Quill marker; returns how many were inserted."""
        if not events:
            return 0
        array = (INPUT * len(events))()
        for record, event in zip(array, events):
            record.type = INPUT_KEYBOARD
            record.u.ki = KEYBDINPUT(event.vk, event.scan, event.flags, 0, QUILL_EXTRA_INFO)
        return int(self._user32.SendInput(len(events), array, ctypes.sizeof(INPUT)))

    def send_mouse(self, events: list[MouseEvent]) -> int:
        """Inject mouse-button events at the current pointer position, tagged with the Quill marker.

        Records carry no coordinates and never MOUSEEVENTF_MOVE or
        MOUSEEVENTF_ABSOLUTE, so the pointer does not move.
        """
        if not events:
            return 0
        if any(event.flags & (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE) for event in events):
            raise ValueError("Quill never moves the pointer")
        array = (INPUT * len(events))()
        for record, event in zip(array, events):
            record.type = INPUT_MOUSE
            record.u.mi = MOUSEINPUT(0, 0, 0, event.flags, 0, QUILL_EXTRA_INFO)
        return int(self._user32.SendInput(len(events), array, ctypes.sizeof(INPUT)))

    def last_error(self) -> int:
        return ctypes.get_last_error()

    def cursor_pos(self) -> tuple[int, int] | None:
        point = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def window_from_point(self, x: int, y: int) -> int:
        return int(self._user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)

    def root_window(self, hwnd: int) -> int:
        return int(self._user32.GetAncestor(hwnd, GA_ROOT) or 0)

    def buttons_swapped(self) -> bool:
        return bool(self._user32.GetSystemMetrics(SM_SWAPBUTTON))

    def own_process_id(self) -> int:
        return int(self._kernel32.GetCurrentProcessId())

    def key_down(self, vk: int) -> bool:
        """The key is physically or logically down right now."""
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    # ------------------------------------------------------------ windows

    def foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def is_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self._user32.IsWindow(hwnd))

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def is_hung(self, hwnd: int) -> bool:
        return bool(self._user32.IsHungAppWindow(hwnd))

    def window_thread_process(self, hwnd: int) -> tuple[int, int]:
        pid = wintypes.DWORD()
        thread = self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(thread), int(pid.value)

    def window_process_id(self, hwnd: int) -> int:
        return self.window_thread_process(hwnd)[1]

    def window_text(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(512)
        self._user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def window_class(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def top_level_windows(self) -> list[int]:
        found: list[int] = []

        def collect(hwnd: int, _param: int) -> bool:
            found.append(int(hwnd))
            return True

        self._user32.EnumWindows(WNDENUMPROC(collect), 0)
        return found

    # ------------------------------------------------------------ processes

    def _token_integrity(self, process: int) -> int | None:
        return _token_integrity(self._kernel32, self._advapi32, process)

    def own_integrity(self) -> int | None:
        return self._token_integrity(self._kernel32.GetCurrentProcess())

    def process_integrity(self, pid: int) -> int | None:
        """Integrity RID of a process; None when Windows refuses to tell."""
        process = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None
        try:
            return self._token_integrity(process)
        finally:
            self._kernel32.CloseHandle(process)

    def process_image(self, pid: int) -> str:
        """Full path of a process image, or '' when it cannot be read."""
        process = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if not self._kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            self._kernel32.CloseHandle(process)

    # ------------------------------------------------------------ clipboard

    def clipboard_sequence(self) -> int:
        return int(self._user32.GetClipboardSequenceNumber())

    def open_clipboard(self) -> bool:
        return bool(self._user32.OpenClipboard(None))

    def close_clipboard(self) -> None:
        self._user32.CloseClipboard()

    def clipboard_formats(self) -> list[int]:
        """Formats on the open clipboard, in the order Windows lists them."""
        formats: list[int] = []
        current = 0
        while True:
            current = int(self._user32.EnumClipboardFormats(current))
            if not current:
                return formats
            formats.append(current)

    def clipboard_format_name(self, fmt: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        length = self._user32.GetClipboardFormatNameW(fmt, buffer, len(buffer))
        return buffer.value[:length] if length else ""

    def clipboard_bytes(self, fmt: int) -> bytes | None:
        """Bytes of an HGLOBAL clipboard format on the open clipboard; None when unreadable."""
        handle = self._user32.GetClipboardData(fmt)
        if not handle:
            return None
        size = int(self._kernel32.GlobalSize(handle))
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self._kernel32.GlobalUnlock(handle)

    def empty_clipboard(self) -> bool:
        return bool(self._user32.EmptyClipboard())

    def set_clipboard_bytes(self, fmt: int, data: bytes) -> bool:
        handle = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
        if not handle:
            return False
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        self._kernel32.GlobalUnlock(handle)
        if not self._user32.SetClipboardData(fmt, handle):
            self._kernel32.GlobalFree(handle)
            return False
        return True


class Processes:
    """Real process listing and read-only current-directory reads (``quill.projects``).

    Every open asks for ``PROCESS_QUERY_LIMITED_INFORMATION`` (plus
    ``PROCESS_VM_READ`` for the directory) and nothing more; no privilege is
    ever enabled, so a process of another user or a protected one cannot be
    opened. Tests never create this class; they pass a fake.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise Win32Error("process reads are only available on Windows")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        self._kernel32, self._advapi32, self._ntdll = kernel32, advapi32, ntdll
        w = wintypes
        _bind_tokens(kernel32, advapi32)
        bind(kernel32, "CreateToolhelp32Snapshot", w.HANDLE, w.DWORD, w.DWORD)
        bind(kernel32, "Process32FirstW", w.BOOL, w.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        bind(kernel32, "Process32NextW", w.BOOL, w.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        bind(kernel32, "GetProcessTimes", w.BOOL, w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
             ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME))
        bind(kernel32, "IsWow64Process", w.BOOL, w.HANDLE, ctypes.POINTER(w.BOOL))
        bind(kernel32, "ReadProcessMemory", w.BOOL, w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
             ctypes.POINTER(ctypes.c_size_t))
        bind(ntdll, "NtQueryInformationProcess", w.LONG, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.ULONG,
             ctypes.POINTER(w.ULONG))
        bind(advapi32, "EqualSid", w.BOOL, ctypes.c_void_p, ctypes.c_void_p)

    def processes(self, limit: int = MAX_PROCESSES) -> list[tuple[int, int, str]]:
        """(pid, parent pid, image file name) of the running processes, at most ``limit``."""
        snapshot = self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            raise Win32Error("process snapshot failed")
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            found: list[tuple[int, int, str]] = []
            more = self._kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more and len(found) < limit:
                found.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
                more = self._kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return found
        finally:
            self._kernel32.CloseHandle(snapshot)

    def _open(self, pid: int, access: int) -> int:
        process = self._kernel32.OpenProcess(access, False, pid)
        if not process:
            raise Win32Error("process not open")
        return process

    def created(self, pid: int) -> int | None:
        """The creation time of a process (FILETIME units), or None when it cannot be read."""
        try:
            process = self._open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
        except Win32Error:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self._kernel32.GetProcessTimes(process, *(ctypes.byref(item) for item in times)):
                return None
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            self._kernel32.CloseHandle(process)

    def same_user(self, pid: int) -> bool:
        """Whether the process runs as the user Quill runs as; False when it cannot be told."""
        try:
            process = self._open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
        except Win32Error:
            return False
        try:
            theirs = _token_information(self._kernel32, self._advapi32, process, TOKEN_USER)
            own = _token_information(self._kernel32, self._advapi32, self._kernel32.GetCurrentProcess(), TOKEN_USER)
            if theirs is None or own is None:
                return False
            sids = [ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents.Sid for buffer in (theirs, own)]
            return bool(self._advapi32.EqualSid(*sids))
        finally:
            self._kernel32.CloseHandle(process)

    def own_integrity(self) -> int | None:
        return _token_integrity(self._kernel32, self._advapi32, self._kernel32.GetCurrentProcess())

    def process_integrity(self, pid: int) -> int | None:
        """Integrity RID of a process; None when Windows refuses to tell."""
        try:
            process = self._open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
        except Win32Error:
            return None
        try:
            return _token_integrity(self._kernel32, self._advapi32, process)
        finally:
            self._kernel32.CloseHandle(process)

    def _read(self, process: int, address: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not address or not self._kernel32.ReadProcessMemory(process, ctypes.c_void_p(address), buffer, size,
                                                               ctypes.byref(done)) or done.value != size:
            raise Win32Error("process memory not readable")
        return buffer.raw

    def current_directory(self, pid: int, max_chars: int = MAX_DIRECTORY_CHARS) -> str:
        """The current directory of a 64-bit process, read from its process parameters.

        Raises ``Win32Error`` when the process cannot be opened or read, is a
        32-bit process, or the directory is empty, longer than ``max_chars``
        or not UTF-16. The directory is never logged.
        """
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise Win32Error("process reads need a 64-bit Quill")
        process = self._open(pid, PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ)
        try:
            wow64 = wintypes.BOOL()
            if not self._kernel32.IsWow64Process(process, ctypes.byref(wow64)) or wow64.value:
                raise Win32Error("not a 64-bit process")
            info = PROCESS_BASIC_INFORMATION()
            size = wintypes.ULONG()
            if self._ntdll.NtQueryInformationProcess(process, PROCESS_BASIC_INFORMATION_CLASS, ctypes.byref(info),
                                                     ctypes.sizeof(info), ctypes.byref(size)) != 0:
                raise Win32Error("process information not readable")
            peb = info.PebBaseAddress or 0
            parameters = int.from_bytes(self._read(process, peb + PEB_PROCESS_PARAMETERS if peb else 0, 8), "little")
            raw = self._read(process, parameters + PARAMETERS_CURRENT_DIRECTORY if parameters else 0,
                             ctypes.sizeof(UNICODE_STRING))
            path = UNICODE_STRING.from_buffer_copy(raw)
            if not path.Length or path.Length % 2 or path.Length > 2 * max_chars:
                raise Win32Error("current directory out of bounds")
            data = self._read(process, path.Buffer or 0, path.Length)
            try:
                return data.decode("utf-16-le")
            except UnicodeDecodeError:
                raise Win32Error("current directory not text") from None
        finally:
            self._kernel32.CloseHandle(process)


class LowLevelHooks:
    """Real WH_KEYBOARD_LL / WH_MOUSE_LL installer and the message loop they need.

    Every call runs on the thread that owns the hooks. Tests never create this
    class; they drive ``quill.hooks`` with a fake installer.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise Win32Error("Win32 hooks are only available on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32, self._kernel32 = user32, kernel32
        w = wintypes
        bind(user32, "SetWindowsHookExW", w.HHOOK, ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD)
        bind(user32, "UnhookWindowsHookEx", w.BOOL, w.HHOOK)
        bind(user32, "CallNextHookEx", w.LPARAM, w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM)
        bind(user32, "GetMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
        bind(user32, "PeekMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        bind(user32, "PostThreadMessageW", w.BOOL, w.DWORD, w.UINT, w.WPARAM, w.LPARAM)
        bind(kernel32, "GetCurrentThreadId", w.DWORD)
        bind(kernel32, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
        bind(kernel32, "GetTickCount", w.DWORD)

    def make_callback(self, function: object) -> object:
        """Wrap a Python function as a HOOKPROC; the caller keeps it alive while hooked."""
        return HOOKPROC(function)

    def set_hook(self, kind: int, callback: object) -> int:
        handle = self._user32.SetWindowsHookExW(kind, callback, self._kernel32.GetModuleHandleW(None), 0)
        if not handle:
            raise Win32Error(f"SetWindowsHookExW({kind}) failed (error {ctypes.get_last_error()})")
        return int(handle)

    def unhook(self, handle: int) -> None:
        self._user32.UnhookWindowsHookEx(handle)

    def call_next(self, code: int, wparam: int, lparam: int) -> int:
        return int(self._user32.CallNextHookEx(None, code, wparam, lparam))

    def current_thread_id(self) -> int:
        return int(self._kernel32.GetCurrentThreadId())

    def ensure_queue(self) -> None:
        """Create this thread's message queue, so a WM_QUIT posted early is not lost."""
        message = wintypes.MSG()
        self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)

    def run_loop(self) -> None:
        """Pump messages (the hook callbacks run inside) until WM_QUIT."""
        message = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass

    def post_quit(self, thread_id: int) -> bool:
        return bool(self._user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0))

    def tick_count(self) -> int:
        """Milliseconds since boot (32 bits, wraps): the clock of the hook structures' ``time`` field."""
        return int(self._kernel32.GetTickCount())

    @staticmethod
    def keyboard_fields(lparam: int) -> tuple[int, int, int, int]:
        """(vk, flags, extra info, event time) of the KBDLLHOOKSTRUCT at ``lparam``."""
        info = KBDLLHOOKSTRUCT.from_address(lparam)
        return int(info.vkCode), int(info.flags), int(info.dwExtraInfo), int(info.time)

    @staticmethod
    def mouse_fields(lparam: int) -> tuple[int, int, int, int]:
        """(mouseData, flags, extra info, event time) of the MSLLHOOKSTRUCT at ``lparam``."""
        info = MSLLHOOKSTRUCT.from_address(lparam)
        return int(info.mouseData), int(info.flags), int(info.dwExtraInfo), int(info.time)
