"""The on-screen indicator: a click-through layered window that never takes focus.

The window is WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE |
WS_EX_TOOLWINDOW | WS_EX_TOPMOST, shown with SW_SHOWNOACTIVATE and painted
with UpdateLayeredWindow from a 32-bit premultiplied DIB section. It answers
WM_MOUSEACTIVATE with MA_NOACTIVATE and WM_NCHITTEST with HTTRANSPARENT. No
call here moves the keyboard focus or activates a window: ``WIN32_CALLS``
lists every Win32 function this module binds.

Threads: ``Indicator`` is the thread-safe front. Its methods queue commands
and post a wake message; the UI thread (which owns the window) applies them
in queue order in ``Overlay``. The UI thread is per-monitor DPI aware (v2),
so positions are physical pixels and each frame uses the DPI of the monitor
under the anchor.

Tests drive ``Overlay`` and ``Indicator`` with a fake layer and renderer; the
real ``OverlayWin32`` is only created by the app and the manual demo.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from quill.indicator.render import METRICS, STATES, Layout, Renderer, View, place

log = logging.getLogger(__name__)

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_SETTINGCHANGE = 0x001A
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
WM_TIMER = 0x0113
WM_APP = 0x8000
WM_APP_WAKE = WM_APP + 1
MA_NOACTIVATE = 3
HTTRANSPARENT = -1

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
BI_RGB = 0
MONITOR_DEFAULTTONEAREST = 2
MDT_EFFECTIVE_DPI = 0
SPI_GETCLIENTAREAANIMATION = 0x1042
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
ERROR_CLASS_ALREADY_EXISTS = 1410

TIMER_ID = 1
FRAME_MS = 33
REDUCED_FRAME_MS = 100
CLASS_NAME = "QuillIndicatorOverlay"

# Every Win32 function the real layer binds. None of them activates a window
# or moves the keyboard focus.
WIN32_CALLS = {
    "user32": (
        "RegisterClassExW", "UnregisterClassW", "CreateWindowExW", "DestroyWindow", "DefWindowProcW",
        "ShowWindow", "UpdateLayeredWindow", "SetTimer", "KillTimer", "PostMessageW", "PostQuitMessage",
        "GetMessageW", "TranslateMessage", "DispatchMessageW", "GetCursorPos", "MonitorFromPoint",
        "GetMonitorInfoW", "SystemParametersInfoW", "SetThreadDpiAwarenessContext", "GetForegroundWindow",
    ),
    "gdi32": ("CreateCompatibleDC", "CreateDIBSection", "SelectObject", "DeleteObject", "DeleteDC"),
    "kernel32": ("GetModuleHandleW",),
    "shcore": ("GetDpiForMonitor",),
}


class IndicatorError(RuntimeError):
    """The indicator window could not be created or run."""


@dataclass
class Surface:
    """A memory DC with a selected top-down 32-bit DIB section of ``width`` x ``height``."""

    dc: int
    bitmap: int
    previous: int
    bits: int
    width: int
    height: int

    @property
    def stride(self) -> int:
        return self.width * 4


if sys.platform == "win32":
    from ctypes import wintypes

    LRESULT = wintypes.LPARAM
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]


def _bind_all(dlls: dict[str, object]) -> None:
    w = wintypes
    p = ctypes.POINTER
    signatures = {
        "RegisterClassExW": (w.ATOM, p(WNDCLASSEXW)),
        "UnregisterClassW": (w.BOOL, w.LPCWSTR, w.HINSTANCE),
        "CreateWindowExW": (w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID),
        "DestroyWindow": (w.BOOL, w.HWND),
        "DefWindowProcW": (LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM),
        "ShowWindow": (w.BOOL, w.HWND, ctypes.c_int),
        "UpdateLayeredWindow": (w.BOOL, w.HWND, w.HDC, p(w.POINT), p(w.SIZE), w.HDC, p(w.POINT), w.COLORREF,
                                p(BLENDFUNCTION), w.DWORD),
        "SetTimer": (ctypes.c_size_t, w.HWND, ctypes.c_size_t, w.UINT, ctypes.c_void_p),
        "KillTimer": (w.BOOL, w.HWND, ctypes.c_size_t),
        "PostMessageW": (w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM),
        "PostQuitMessage": (None, ctypes.c_int),
        "GetMessageW": (w.BOOL, p(w.MSG), w.HWND, w.UINT, w.UINT),
        "TranslateMessage": (w.BOOL, p(w.MSG)),
        "DispatchMessageW": (LRESULT, p(w.MSG)),
        "GetCursorPos": (w.BOOL, p(w.POINT)),
        "MonitorFromPoint": (w.HMONITOR, w.POINT, w.DWORD),
        "GetMonitorInfoW": (w.BOOL, w.HMONITOR, p(MONITORINFO)),
        "SystemParametersInfoW": (w.BOOL, w.UINT, w.UINT, ctypes.c_void_p, w.UINT),
        "SetThreadDpiAwarenessContext": (ctypes.c_void_p, ctypes.c_void_p),
        "GetForegroundWindow": (w.HWND,),
        "CreateCompatibleDC": (w.HDC, w.HDC),
        "CreateDIBSection": (w.HBITMAP, w.HDC, ctypes.c_void_p, w.UINT, p(ctypes.c_void_p), w.HANDLE, w.DWORD),
        "SelectObject": (w.HGDIOBJ, w.HDC, w.HGDIOBJ),
        "DeleteObject": (w.BOOL, w.HGDIOBJ),
        "DeleteDC": (w.BOOL, w.HDC),
        "GetModuleHandleW": (w.HMODULE, w.LPCWSTR),
        "GetDpiForMonitor": (ctypes.c_long, w.HMONITOR, ctypes.c_int, p(w.UINT), p(w.UINT)),
    }
    for dll_name, names in WIN32_CALLS.items():
        for name in names:
            function = getattr(dlls[dll_name], name)
            restype, *argtypes = signatures[name]
            function.restype = restype
            function.argtypes = argtypes


class OverlayWin32:
    """Real Win32 layer for the overlay window. Every call except ``post``,
    ``cursor_pos`` and ``foreground_window`` runs on the UI thread."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise IndicatorError("the indicator window needs Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._shcore = ctypes.WinDLL("shcore", use_last_error=True)
        _bind_all({"user32": self._user32, "gdi32": self._gdi32, "kernel32": self._kernel32,
                   "shcore": self._shcore})
        self._instance = self._kernel32.GetModuleHandleW(None)
        self._wndproc: object = None
        self._class_registered = False

    def init_thread(self) -> bool:
        """Make the calling (UI) thread per-monitor DPI aware (v2)."""
        return bool(self._user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))

    def register_class(self, handler: Callable[[int, int, int, int], int | None]) -> None:
        user32 = self._user32

        def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            try:
                result = handler(hwnd or 0, msg, wparam, lparam)
            except Exception:  # an exception must never unwind through Windows
                log.exception("indicator window procedure failed")
                result = None
            if result is None:
                return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))
            return result

        self._wndproc = WNDPROC(wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._instance
        wc.lpszClassName = CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            if error != ERROR_CLASS_ALREADY_EXISTS:
                raise IndicatorError(f"RegisterClassExW failed (error {error})")
        self._class_registered = True

    def unregister_class(self) -> None:
        if self._class_registered:
            self._user32.UnregisterClassW(CLASS_NAME, self._instance)
            self._class_registered = False

    def create_window(self, ex_style: int, style: int) -> int:
        hwnd = self._user32.CreateWindowExW(ex_style, CLASS_NAME, "Quill", style, 0, 0, 1, 1, None, None,
                                            self._instance, None)
        if not hwnd:
            raise IndicatorError(f"CreateWindowExW failed (error {ctypes.get_last_error()})")
        return int(hwnd)

    def destroy_window(self, hwnd: int) -> None:
        self._user32.DestroyWindow(hwnd)

    def show_window(self, hwnd: int, command: int) -> None:
        self._user32.ShowWindow(hwnd, command)

    def set_timer(self, hwnd: int, timer_id: int, ms: int) -> None:
        self._user32.SetTimer(hwnd, timer_id, ms, None)

    def kill_timer(self, hwnd: int, timer_id: int) -> None:
        self._user32.KillTimer(hwnd, timer_id)

    def post(self, hwnd: int, msg: int) -> bool:
        """Post a message to the window; safe from any thread."""
        return bool(self._user32.PostMessageW(hwnd, msg, 0, 0))

    def post_quit(self) -> None:
        self._user32.PostQuitMessage(0)

    def run_loop(self) -> None:
        message = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

    def cursor_pos(self) -> tuple[int, int] | None:
        point = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def monitor(self, x: int, y: int) -> tuple[tuple[int, int, int, int], int]:
        """Work area ``(left, top, right, bottom)`` and effective DPI of the monitor nearest to (x, y)."""
        handle = self._user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not handle or not self._user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            raise IndicatorError("no monitor information")
        work = info.rcWork
        dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
        if self._shcore.GetDpiForMonitor(handle, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0:
            dpi_x.value = 96
        return (work.left, work.top, work.right, work.bottom), int(dpi_x.value)

    def reduced_motion(self) -> bool:
        """True when Windows has client-area animations turned off."""
        enabled = wintypes.BOOL(True)
        if not self._user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0):
            return False
        return not enabled.value

    def create_surface(self, width: int, height: int) -> Surface:
        gdi32 = self._gdi32
        dc = gdi32.CreateCompatibleDC(None)
        if not dc:
            raise IndicatorError("CreateCompatibleDC failed")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            gdi32.DeleteDC(dc)
            raise IndicatorError("CreateDIBSection failed")
        previous = gdi32.SelectObject(dc, bitmap)
        return Surface(int(dc), int(bitmap), int(previous or 0), int(bits.value), width, height)

    def delete_surface(self, surface: Surface) -> None:
        gdi32 = self._gdi32
        if surface.previous:
            gdi32.SelectObject(surface.dc, surface.previous)
        gdi32.DeleteObject(surface.bitmap)
        gdi32.DeleteDC(surface.dc)

    def update_layered(self, hwnd: int, surface: Surface, x: int, y: int, width: int, height: int) -> bool:
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        return bool(self._user32.UpdateLayeredWindow(
            hwnd, None, ctypes.byref(wintypes.POINT(x, y)), ctypes.byref(wintypes.SIZE(width, height)),
            surface.dc, ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA))


# ---------------------------------------------------------------- UI thread


def smooth_level(previous: float, target: float, dt: float) -> float:
    """Voice level follower: fast attack, slower release, so the orb does not flicker."""
    target = max(0.0, min(1.0, target))
    rate = 18.0 if target > previous else 5.0
    share = 1.0 - pow(2.718281828, -rate * max(0.0, dt))
    return previous + (target - previous) * share


class Overlay:
    """Window state owned by the UI thread: applies commands and paints frames."""

    def __init__(self, layer: object, renderer: object, position: str = "pointer",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.layer = layer
        self.renderer = renderer
        self.position = position
        self.clock = clock
        self.hwnd = 0
        self.surface: Surface | None = None
        self.visible = False
        self.state = ""
        self.text = ""
        self.state_started = 0.0
        self.hide_at: float | None = None
        self.anchor: tuple[int, int] = (0, 0)
        self.content_floor = 0.0
        self.level_target = 0.0
        self.level = 0.0
        self.level_at = 0.0
        self.reduced_motion = False
        self.frame_ms: deque[float] = deque(maxlen=2000)
        self.drain: Callable[[], list[tuple[object, ...]]] = lambda: []

    # -------------------------------------------------------- lifecycle

    def create(self) -> None:
        layer = self.layer
        layer.init_thread()
        layer.register_class(self.handle)
        self.hwnd = layer.create_window(EX_STYLE, WS_POPUP)
        self.reduced_motion = layer.reduced_motion()

    def destroy(self) -> None:
        """Release the timer, the surface, the window and the class; safe to call twice."""
        layer = self.layer
        if self.hwnd:
            layer.kill_timer(self.hwnd, TIMER_ID)
            hwnd, self.hwnd = self.hwnd, 0
            layer.destroy_window(hwnd)
        self._free_surface()
        layer.unregister_class()
        self.visible = False

    def _free_surface(self) -> None:
        if self.surface is not None:
            surface, self.surface = self.surface, None
            self.layer.delete_surface(surface)

    # -------------------------------------------------------- messages

    def handle(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int | None:
        """Window procedure; None means DefWindowProc."""
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if msg == WM_NCHITTEST:
            return HTTRANSPARENT
        if msg == WM_APP_WAKE:
            for command in self.drain():
                self.apply(command)
            return 0
        if msg == WM_TIMER:
            self.tick()
            return 0
        if msg == WM_SETTINGCHANGE:
            self.reduced_motion = self.layer.reduced_motion()
            return None
        if msg == WM_CLOSE:
            self.destroy()
            return 0
        if msg == WM_DESTROY:
            self.layer.post_quit()
            return 0
        return None

    def apply(self, command: tuple[object, ...]) -> None:
        kind = command[0]
        now = self.clock()
        if kind == "show":
            _, state, text, hold = command
            if not self.visible:
                self.anchor = self.layer.cursor_pos() or self.anchor
                self.content_floor = 0.0
            if state != self.state or not self.visible:
                self.state_started = now
                self.content_floor = 0.0
            self.state, self.text = str(state), str(text)
            self.hide_at = None if hold is None else now + float(hold)  # type: ignore[arg-type]
            self._paint(now)
            if not self.visible:
                self.layer.show_window(self.hwnd, SW_SHOWNOACTIVATE)
                self.visible = True
                self.layer.set_timer(self.hwnd, TIMER_ID, REDUCED_FRAME_MS if self.reduced_motion else FRAME_MS)
        elif kind == "text":
            self.text = str(command[1])
            if self.visible:
                self._paint(now)
        elif kind == "level":
            self.level_target = float(command[1])  # type: ignore[arg-type]
        elif kind == "hide":
            self.hide()

    def hide(self) -> None:
        if self.visible:
            self.layer.kill_timer(self.hwnd, TIMER_ID)
            self.layer.show_window(self.hwnd, SW_HIDE)
            self.visible = False
        self.hide_at = None
        self.level = self.level_target = 0.0

    def tick(self) -> None:
        if not self.visible:
            return
        now = self.clock()
        if self.hide_at is not None and now >= self.hide_at:
            self.hide()
            return
        self._paint(now)

    # -------------------------------------------------------- painting

    def _paint(self, now: float) -> None:
        started = time.perf_counter()
        self.level = smooth_level(self.level, self.level_target, now - self.level_at)
        self.level_at = now
        work, dpi = self.layer.monitor(*self.anchor)
        scale = max(1.0, dpi / 96.0)
        view = View(self.state, self.text, self.level, now - self.state_started, self.reduced_motion)
        room = work[2] - work[0] - 2 * METRICS.glow * scale
        lay: Layout = self.renderer.layout(view, scale, self.content_floor, room)
        self.content_floor = max(self.content_floor, lay.content_width)
        surface = self._surface_for(lay)
        self.renderer.draw(surface.bits, surface.stride, lay, view)
        x, y = place((lay.width, lay.height), self.anchor, work, scale, self.position)
        self.layer.update_layered(self.hwnd, surface, x, y, lay.width, lay.height)
        self.frame_ms.append((time.perf_counter() - started) * 1000.0)

    def _surface_for(self, lay: Layout) -> Surface:
        """One surface big enough for any frame at this scale; recreated only when that grows."""
        m, s = METRICS, lay.scale
        width = max(lay.width, int((m.max_width + 2 * m.glow) * s) + 2)
        height = max(lay.height, int((m.pad_top + m.label_line + m.gap + 2 * m.words_line + m.pad_bottom
                                      + 2 * m.glow) * s) + 2)
        current = self.surface
        if current is not None and (current.width, current.height) == (width, height):
            return current
        self._free_surface()
        self.surface = self.layer.create_surface(width, height)
        return self.surface


# ---------------------------------------------------------------- thread-safe front


class Indicator:
    """Thread-safe indicator. ``start`` creates the UI thread and window; every
    other method may be called from any thread and is applied in call order."""

    def __init__(self, position: str = "pointer", *, layer_factory: Callable[[], object] = OverlayWin32,
                 renderer_factory: Callable[[], object] = Renderer,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.position = position
        self._layer_factory = layer_factory
        self._renderer_factory = renderer_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._commands: deque[tuple[object, ...]] = deque()
        self._thread: threading.Thread | None = None
        self._layer: object = None
        self._hwnd = 0
        self._error: BaseException | None = None
        self.overlay: Overlay | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 10.0) -> None:
        with self._lock:
            if self._thread is not None:
                return
            ready = threading.Event()
            self._error = None
            self._thread = threading.Thread(target=self._run, args=(ready,), name="quill-indicator", daemon=True)
            self._thread.start()
        if not ready.wait(timeout):
            raise IndicatorError("the indicator did not start in time")
        if self._error is not None:
            self._thread.join(timeout)
            self._thread = None
            raise IndicatorError(f"the indicator could not start: {self._error}") from self._error

    def _run(self, ready: threading.Event) -> None:
        layer = renderer = overlay = None
        try:
            layer = self._layer_factory()
            renderer = self._renderer_factory()
            overlay = Overlay(layer, renderer, self.position, self._clock)
            overlay.drain = self._drain
            overlay.create()
            with self._lock:
                self._layer, self._hwnd, self.overlay = layer, overlay.hwnd, overlay
                pending = bool(self._commands)
            if pending:
                layer.post(overlay.hwnd, WM_APP_WAKE)
        except BaseException as exc:  # reported to start()
            self._error = exc
            if overlay is not None:
                overlay.destroy()
            if renderer is not None:
                renderer.close()
            ready.set()
            return
        ready.set()
        try:
            layer.run_loop()
        except Exception:
            log.exception("indicator message loop failed")
        finally:
            with self._lock:
                self._layer, self._hwnd = None, 0
            overlay.destroy()
            renderer.close()

    def _drain(self) -> list[tuple[object, ...]]:
        with self._lock:
            commands = list(self._commands)
            self._commands.clear()
        return commands

    def _post(self, command: tuple[object, ...]) -> None:
        with self._lock:
            self._commands.append(command)
            layer, hwnd = self._layer, self._hwnd
        if layer is not None and hwnd:
            layer.post(hwnd, WM_APP_WAKE)

    def show(self, state: str, text: str = "", hide_after_s: float | None = None) -> None:
        """Show ``state`` (one of ``render.STATES``) with its text; optionally hide after a delay."""
        if state not in STATES:
            raise ValueError(f"unknown indicator state {state!r}")
        self._post(("show", state, text, hide_after_s))

    def set_text(self, text: str) -> None:
        self._post(("text", text))

    def set_level(self, level: float) -> None:
        """Voice level from 0 (silence) to 1 (loud)."""
        self._post(("level", max(0.0, min(1.0, float(level)))))

    def hide(self) -> None:
        self._post(("hide",))

    def stop(self, timeout: float = 10.0) -> None:
        """Close the window and end the UI thread; ``start`` may be called again."""
        with self._lock:
            thread, layer, hwnd = self._thread, self._layer, self._hwnd
            self._commands.clear()
        if thread is None:
            return
        if layer is not None and hwnd:
            layer.post(hwnd, WM_CLOSE)
        thread.join(timeout)
        if thread.is_alive():
            raise IndicatorError("the indicator thread did not stop in time")
        with self._lock:
            self._thread = None
            self._commands.clear()
