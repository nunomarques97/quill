"""Thin ctypes wrapper over the GDI+ flat API, for drawing into memory only.

GDI+ ships with Windows, so nothing is installed. It is started with
``SuppressBackgroundThread``: GDI+ then creates no hidden notification window,
so rendering offscreen frames never creates a window. Every GDI+ object made
here is counted in ``Gdiplus.live`` and deleted by the call that made it, so a
test can check that drawing leaks nothing.

Colours are ``0xAARRGGBB`` integers, not premultiplied; GDI+ premultiplies
them when it draws into a 32-bit premultiplied (PARGB) bitmap.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
PIXEL_FORMAT_32BPP_PARGB = 0x000E200B

UNIT_PIXEL = 2
SMOOTHING_ANTIALIAS = 4
PIXEL_OFFSET_HALF = 4
# Gamma-corrected (high quality) compositing is about 7x slower per frame.
COMPOSITING_QUALITY_HIGH_SPEED = 1
TEXT_RENDERING_ANTIALIAS_GRIDFIT = 3
FONT_STYLE_REGULAR = 0
FONT_STYLE_BOLD = 1
STRING_FORMAT_MEASURE_TRAILING_SPACES = 0x00000800
STRING_FORMAT_NO_WRAP = 0x00001000
STRING_FORMAT_NO_CLIP = 0x00004000
LINE_CAP_ROUND = 2
LINE_JOIN_ROUND = 2
DASH_STYLE_DASH = 1
FILL_MODE_ALTERNATE = 0
WRAP_MODE_TILE_FLIP_XY = 3
IMAGE_LOCK_MODE_READ = 1


class GdiplusError(OSError):
    """A GDI+ call failed."""


@dataclass(frozen=True)
class Solid:
    argb: int


@dataclass(frozen=True)
class Linear:
    """A linear gradient from (x1, y1) to (x2, y2)."""

    x1: float
    y1: float
    x2: float
    y2: float
    start: int
    end: int


@dataclass(frozen=True)
class Radial:
    """An elliptical gradient: ``center`` in the middle fading to ``edge`` at the rim."""

    cx: float
    cy: float
    rx: float
    ry: float
    center: int
    edge: int


Fill = Solid | Linear | Radial


@dataclass(frozen=True)
class Stroke:
    width: float
    fill: Fill
    dashed: bool = False


if sys.platform == "win32":
    from ctypes import wintypes

    class _StartupInput(ctypes.Structure):
        _fields_ = [
            ("GdiplusVersion", ctypes.c_uint32),
            ("DebugEventCallback", ctypes.c_void_p),
            ("SuppressBackgroundThread", wintypes.BOOL),
            ("SuppressExternalCodecs", wintypes.BOOL),
        ]

    class _StartupOutput(ctypes.Structure):
        _fields_ = [("NotificationHook", ctypes.c_void_p), ("NotificationUnhook", ctypes.c_void_p)]

    class PointF(ctypes.Structure):
        _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float)]

    class RectF(ctypes.Structure):
        _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float), ("Width", ctypes.c_float),
                    ("Height", ctypes.c_float)]

    class _Rect(ctypes.Structure):
        _fields_ = [("X", ctypes.c_int), ("Y", ctypes.c_int), ("Width", ctypes.c_int), ("Height", ctypes.c_int)]

    class _BitmapData(ctypes.Structure):
        _fields_ = [
            ("Width", ctypes.c_uint),
            ("Height", ctypes.c_uint),
            ("Stride", ctypes.c_int),
            ("PixelFormat", ctypes.c_int),
            ("Scan0", ctypes.c_void_p),
            ("Reserved", ctypes.c_size_t),
        ]


_P = ctypes.c_void_p
_PP = ctypes.POINTER(ctypes.c_void_p)


def _signatures() -> dict[str, tuple[object, ...]]:
    f, i = ctypes.c_float, ctypes.c_int
    return {
        "GdiplusStartup": (ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(_StartupInput),
                           ctypes.POINTER(_StartupOutput)),
        "GdiplusShutdown": (ctypes.c_size_t,),
        "GdipCreateBitmapFromScan0": (i, i, i, i, _P, _PP),
        "GdipDisposeImage": (_P,),
        "GdipGetImageGraphicsContext": (_P, _PP),
        "GdipDeleteGraphics": (_P,),
        "GdipGraphicsClear": (_P, ctypes.c_uint32),
        "GdipSetSmoothingMode": (_P, i),
        "GdipSetPixelOffsetMode": (_P, i),
        "GdipSetCompositingQuality": (_P, i),
        "GdipSetTextRenderingHint": (_P, i),
        "GdipCreateSolidFill": (ctypes.c_uint32, _PP),
        "GdipCreateLineBrush": (ctypes.POINTER(PointF), ctypes.POINTER(PointF), ctypes.c_uint32, ctypes.c_uint32,
                                i, _PP),
        "GdipCreatePathGradientFromPath": (_P, _PP),
        "GdipSetPathGradientCenterColor": (_P, ctypes.c_uint32),
        "GdipSetPathGradientSurroundColorsWithCount": (_P, ctypes.POINTER(ctypes.c_uint32),
                                                       ctypes.POINTER(i)),
        "GdipDeleteBrush": (_P,),
        "GdipCreatePen2": (_P, f, i, _PP),
        "GdipSetPenLineCap197819": (_P, i, i, i),
        "GdipSetPenLineJoin": (_P, i),
        "GdipSetPenDashStyle": (_P, i),
        "GdipDeletePen": (_P,),
        "GdipCreatePath": (i, _PP),
        "GdipDeletePath": (_P,),
        "GdipAddPathArc": (_P, f, f, f, f, f, f),
        "GdipAddPathLine": (_P, f, f, f, f),
        "GdipAddPathEllipse": (_P, f, f, f, f),
        "GdipAddPathLine2": (_P, ctypes.POINTER(PointF), i),
        "GdipAddPathClosedCurve2": (_P, ctypes.POINTER(PointF), i, f),
        "GdipClosePathFigure": (_P,),
        "GdipFillPath": (_P, _P, _P),
        "GdipDrawPath": (_P, _P, _P),
        "GdipDrawImageRectI": (_P, _P, i, i, i, i),
        "GdipCreateFontFamilyFromName": (ctypes.c_wchar_p, _P, _PP),
        "GdipDeleteFontFamily": (_P,),
        "GdipCreateFont": (_P, f, i, i, _PP),
        "GdipDeleteFont": (_P,),
        "GdipStringFormatGetGenericTypographic": (_PP,),
        "GdipCloneStringFormat": (_P, _PP),
        "GdipSetStringFormatFlags": (_P, i),
        "GdipDeleteStringFormat": (_P,),
        "GdipDrawString": (_P, ctypes.c_wchar_p, i, _P, ctypes.POINTER(RectF), _P, _P),
        "GdipMeasureString": (_P, ctypes.c_wchar_p, i, _P, ctypes.POINTER(RectF), _P, ctypes.POINTER(RectF),
                              ctypes.POINTER(i), ctypes.POINTER(i)),
        "GdipBitmapLockBits": (_P, ctypes.POINTER(_Rect), ctypes.c_uint, i, ctypes.POINTER(_BitmapData)),
        "GdipBitmapUnlockBits": (_P, ctypes.POINTER(_BitmapData)),
    }


class Gdiplus:
    """GDI+ started for this process; ``close`` shuts it down.

    ``live`` counts the GDI+ objects created through this wrapper and not yet
    deleted; it is back to zero after every drawing call.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise GdiplusError("GDI+ is only available on Windows")
        self._dll = ctypes.WinDLL("gdiplus")
        for name, argtypes in _signatures().items():
            function = getattr(self._dll, name)
            function.restype = ctypes.c_int
            function.argtypes = list(argtypes)
        self.live = 0
        self._token = ctypes.c_size_t()
        startup = _StartupInput(1, None, True, True)
        self._output = _StartupOutput()
        self._check("GdiplusStartup", ctypes.byref(self._token), ctypes.byref(startup), ctypes.byref(self._output))
        self._closed = False

    def _check(self, name: str, *args: object) -> None:
        status = getattr(self._dll, name)(*args)
        if status != 0:
            raise GdiplusError(f"{name} failed (status {status})")

    def call(self, name: str, *args: object) -> None:
        self._check(name, *args)

    def create(self, name: str, *args: object) -> ctypes.c_void_p:
        """Call a GDI+ constructor whose last argument receives the new object."""
        handle = ctypes.c_void_p()
        self._check(name, *args, ctypes.byref(handle))
        self.live += 1
        return handle

    def delete(self, name: str, handle: ctypes.c_void_p) -> None:
        if handle:
            getattr(self._dll, name)(handle)
            self.live -= 1

    @contextmanager
    def owned(self, create: str, delete: str, *args: object) -> Iterator[ctypes.c_void_p]:
        handle = self.create(create, *args)
        try:
            yield handle
        finally:
            self.delete(delete, handle)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._dll.GdiplusShutdown(self._token)

    # ------------------------------------------------------------ bitmaps

    @contextmanager
    def bitmap(self, bits: int, width: int, height: int, stride: int) -> Iterator[ctypes.c_void_p]:
        """A PARGB bitmap over caller-owned top-down pixel memory at ``bits``."""
        with self.owned("GdipCreateBitmapFromScan0", "GdipDisposeImage", width, height, stride,
                        PIXEL_FORMAT_32BPP_PARGB, ctypes.c_void_p(bits)) as image:
            yield image

    @contextmanager
    def canvas(self, image: ctypes.c_void_p) -> Iterator[Canvas]:
        with self.owned("GdipGetImageGraphicsContext", "GdipDeleteGraphics", image) as graphics:
            self.call("GdipSetSmoothingMode", graphics, SMOOTHING_ANTIALIAS)
            self.call("GdipSetPixelOffsetMode", graphics, PIXEL_OFFSET_HALF)
            self.call("GdipSetCompositingQuality", graphics, COMPOSITING_QUALITY_HIGH_SPEED)
            self.call("GdipSetTextRenderingHint", graphics, TEXT_RENDERING_ANTIALIAS_GRIDFIT)
            yield Canvas(self, graphics)

    def straight_rgba(self, image: ctypes.c_void_p, width: int, height: int) -> bytes:
        """Top-down RGBA rows with straight (not premultiplied) alpha, for PNG."""
        data = _BitmapData()
        rect = _Rect(0, 0, width, height)
        self.call("GdipBitmapLockBits", image, ctypes.byref(rect), IMAGE_LOCK_MODE_READ, PIXEL_FORMAT_32BPP_ARGB,
                  ctypes.byref(data))
        try:
            rows = [ctypes.string_at(data.Scan0 + y * data.Stride, width * 4) for y in range(height)]
        finally:
            self.call("GdipBitmapUnlockBits", image, ctypes.byref(data))
        bgra = b"".join(rows)
        rgba = bytearray(len(bgra))
        rgba[0::4] = bgra[2::4]
        rgba[1::4] = bgra[1::4]
        rgba[2::4] = bgra[0::4]
        rgba[3::4] = bgra[3::4]
        return bytes(rgba)

    # ------------------------------------------------------------ fonts

    def font_family(self, names: Sequence[str]) -> ctypes.c_void_p:
        """The first installed family of ``names``; the caller deletes it with ``delete_font_family``."""
        for name in names:
            handle = ctypes.c_void_p()
            if self._dll.GdipCreateFontFamilyFromName(name, None, ctypes.byref(handle)) == 0:
                self.live += 1
                return handle
        raise GdiplusError(f"none of the fonts {list(names)} is installed")

    def delete_font_family(self, family: ctypes.c_void_p) -> None:
        self.delete("GdipDeleteFontFamily", family)

    def font(self, family: ctypes.c_void_p, size_px: float, bold: bool) -> ctypes.c_void_p:
        style = FONT_STYLE_BOLD if bold else FONT_STYLE_REGULAR
        return self.create("GdipCreateFont", family, ctypes.c_float(size_px), style, UNIT_PIXEL)

    def delete_font(self, font: ctypes.c_void_p) -> None:
        self.delete("GdipDeleteFont", font)

    def string_format(self) -> ctypes.c_void_p:
        """A typographic single-line format that measures trailing spaces."""
        generic = ctypes.c_void_p()
        self._check("GdipStringFormatGetGenericTypographic", ctypes.byref(generic))
        handle = self.create("GdipCloneStringFormat", generic)
        self.call("GdipSetStringFormatFlags", handle,
                  STRING_FORMAT_MEASURE_TRAILING_SPACES | STRING_FORMAT_NO_WRAP | STRING_FORMAT_NO_CLIP)
        return handle

    def delete_string_format(self, fmt: ctypes.c_void_p) -> None:
        self.delete("GdipDeleteStringFormat", fmt)


class Canvas:
    """Drawing operations on one GDI+ graphics; every brush, pen and path is freed per call."""

    def __init__(self, gdip: Gdiplus, graphics: ctypes.c_void_p) -> None:
        self.gdip = gdip
        self.graphics = graphics

    def clear(self, argb: int = 0) -> None:
        self.gdip.call("GdipGraphicsClear", self.graphics, argb)

    @contextmanager
    def _brush(self, fill: Fill) -> Iterator[ctypes.c_void_p]:
        g = self.gdip
        if isinstance(fill, Solid):
            with g.owned("GdipCreateSolidFill", "GdipDeleteBrush", fill.argb) as brush:
                yield brush
        elif isinstance(fill, Linear):
            start, end = PointF(fill.x1, fill.y1), PointF(fill.x2, fill.y2)
            with g.owned("GdipCreateLineBrush", "GdipDeleteBrush", ctypes.byref(start), ctypes.byref(end),
                         fill.start, fill.end, WRAP_MODE_TILE_FLIP_XY) as brush:
                yield brush
        else:
            with self._path() as path:
                g.call("GdipAddPathEllipse", path, fill.cx - fill.rx, fill.cy - fill.ry, 2 * fill.rx, 2 * fill.ry)
                with g.owned("GdipCreatePathGradientFromPath", "GdipDeleteBrush", path) as brush:
                    g.call("GdipSetPathGradientCenterColor", brush, fill.center)
                    colours = (ctypes.c_uint32 * 1)(fill.edge)
                    count = ctypes.c_int(1)
                    g.call("GdipSetPathGradientSurroundColorsWithCount", brush, colours, ctypes.byref(count))
                    yield brush

    @contextmanager
    def _pen(self, stroke: Stroke) -> Iterator[ctypes.c_void_p]:
        g = self.gdip
        with self._brush(stroke.fill) as brush:
            with g.owned("GdipCreatePen2", "GdipDeletePen", brush, ctypes.c_float(stroke.width),
                         UNIT_PIXEL) as pen:
                g.call("GdipSetPenLineCap197819", pen, LINE_CAP_ROUND, LINE_CAP_ROUND, 0)
                g.call("GdipSetPenLineJoin", pen, LINE_JOIN_ROUND)
                if stroke.dashed:
                    g.call("GdipSetPenDashStyle", pen, DASH_STYLE_DASH)
                yield pen

    @contextmanager
    def _path(self) -> Iterator[ctypes.c_void_p]:
        with self.gdip.owned("GdipCreatePath", "GdipDeletePath", FILL_MODE_ALTERNATE) as path:
            yield path

    def _paint(self, path: ctypes.c_void_p, fill: Fill | None, stroke: Stroke | None) -> None:
        if fill is not None:
            with self._brush(fill) as brush:
                self.gdip.call("GdipFillPath", self.graphics, brush, path)
        if stroke is not None:
            with self._pen(stroke) as pen:
                self.gdip.call("GdipDrawPath", self.graphics, pen, path)

    def round_rect(self, x: float, y: float, w: float, h: float, radius: float, fill: Fill | None = None,
                   stroke: Stroke | None = None) -> None:
        g = self.gdip
        d = max(0.5, min(2 * radius, w, h))
        with self._path() as path:
            g.call("GdipAddPathArc", path, x, y, d, d, 180.0, 90.0)
            g.call("GdipAddPathArc", path, x + w - d, y, d, d, 270.0, 90.0)
            g.call("GdipAddPathArc", path, x + w - d, y + h - d, d, d, 0.0, 90.0)
            g.call("GdipAddPathArc", path, x, y + h - d, d, d, 90.0, 90.0)
            g.call("GdipClosePathFigure", path)
            self._paint(path, fill, stroke)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, fill: Fill | None = None,
                stroke: Stroke | None = None) -> None:
        with self._path() as path:
            self.gdip.call("GdipAddPathEllipse", path, cx - rx, cy - ry, 2 * rx, 2 * ry)
            self._paint(path, fill, stroke)

    def arc(self, cx: float, cy: float, r: float, start_deg: float, sweep_deg: float, stroke: Stroke) -> None:
        with self._path() as path:
            self.gdip.call("GdipAddPathArc", path, cx - r, cy - r, 2 * r, 2 * r, start_deg, sweep_deg)
            self._paint(path, None, stroke)

    def polyline(self, points: Sequence[tuple[float, float]], closed: bool, fill: Fill | None = None,
                 stroke: Stroke | None = None, smooth: bool = False) -> None:
        array = (PointF * len(points))(*(PointF(x, y) for x, y in points))
        with self._path() as path:
            if smooth:
                self.gdip.call("GdipAddPathClosedCurve2", path, array, len(points), ctypes.c_float(0.5))
            else:
                self.gdip.call("GdipAddPathLine2", path, array, len(points))
                if closed:
                    self.gdip.call("GdipClosePathFigure", path)
            self._paint(path, fill, stroke)

    def text(self, x: float, y: float, text: str, font: ctypes.c_void_p, fmt: ctypes.c_void_p, argb: int) -> None:
        if not text:
            return
        layout = RectF(x, y, 0.0, 0.0)
        with self._brush(Solid(argb)) as brush:
            self.gdip.call("GdipDrawString", self.graphics, text, len(text), font, ctypes.byref(layout), fmt, brush)

    def measure(self, text: str, font: ctypes.c_void_p, fmt: ctypes.c_void_p) -> float:
        """Advance width of ``text`` in pixels."""
        if not text:
            return 0.0
        layout = RectF(0.0, 0.0, 100000.0, 1000.0)
        box = RectF()
        self.gdip.call("GdipMeasureString", self.graphics, text, len(text), font, ctypes.byref(layout), fmt,
                       ctypes.byref(box), None, None)
        return float(box.Width)

    def image(self, image: ctypes.c_void_p, x: int, y: int, w: int, h: int) -> None:
        self.gdip.call("GdipDrawImageRectI", self.graphics, image, x, y, w, h)


def png_bytes(width: int, height: int, rgba: bytes) -> bytes:
    """Encode straight-alpha RGBA rows as a PNG (stdlib zlib, deterministic)."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    row = width * 4
    raw = b"".join(b"\x00" + rgba[y * row:(y + 1) * row] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))
