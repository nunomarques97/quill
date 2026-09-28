"""Microphone capture through MME (winmm waveIn) with ctypes, 16 kHz mono PCM16.

MME is the path proven for the target headset: it records at 16 kHz in real
time. DirectSound is never used, and no audio package is needed.

``WinMM`` is the only class that touches Windows. ``Capture`` drives any
object with the same methods, so tests use a fake and never open the
microphone. Listing devices (``input_devices``) reads their names only; it
does not open any device.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

# MME truncates device names to 31 characters. A truncated name only matches
# the configured one by prefix when it has at least 10 characters, so a short
# name such as "Mic" never matches by accident.
MME_NAME_LIMIT = 31
MIN_TRUNCATED_NAME = 10

WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0
MAXPNAMELEN = 32


class AudioError(OSError):
    """The microphone could not be listed, opened or read."""


# ---------------------------------------------------------------- devices


def name_matches(device_name: str, configured: str) -> bool:
    """The device is the configured one: contains it, or is its MME-truncated prefix."""
    target = configured.strip().casefold()
    name = device_name.strip().casefold()
    if not target or not name:
        return False
    if target in name:
        return True
    return MIN_TRUNCATED_NAME <= len(name) <= MME_NAME_LIMIT and target.startswith(name)


def select_device(names: list[str], configured: str) -> int:
    """Index of the one MME input matching ``configured``; AudioError otherwise."""
    matches = [index for index, name in enumerate(names) if name_matches(name, configured)]
    if not matches:
        raise AudioError("configured microphone not found among MME inputs (see --list-devices)")
    if len(matches) > 1:
        raise AudioError("configured microphone name matches several MME inputs; use a longer name")
    return matches[0]


# ---------------------------------------------------------------- winmm


if sys.platform == "win32":
    from ctypes import wintypes

    class WAVEINCAPSW(ctypes.Structure):
        _fields_ = [
            ("wMid", wintypes.WORD),
            ("wPid", wintypes.WORD),
            ("vDriverVersion", wintypes.UINT),
            ("szPname", wintypes.WCHAR * MAXPNAMELEN),
            ("dwFormats", wintypes.DWORD),
            ("wChannels", wintypes.WORD),
            ("wReserved1", wintypes.WORD),
        ]

    class WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ("wFormatTag", wintypes.WORD),
            ("nChannels", wintypes.WORD),
            ("nSamplesPerSec", wintypes.DWORD),
            ("nAvgBytesPerSec", wintypes.DWORD),
            ("nBlockAlign", wintypes.WORD),
            ("wBitsPerSample", wintypes.WORD),
            ("cbSize", wintypes.WORD),
        ]

    class WAVEHDR(ctypes.Structure):
        pass

    WAVEHDR._fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.POINTER(WAVEHDR)),
        ("reserved", ctypes.c_size_t),
    ]


@dataclass
class MmeBuffer:
    """One capture buffer: its memory and header stay alive while queued."""

    size: int
    data: object = field(default=None, repr=False)
    header: object = field(default=None, repr=False)


class WinMM:
    """Thin wrapper over the winmm waveIn functions (MME)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise AudioError("MME capture is only available on Windows")
        dll = ctypes.WinDLL("winmm")
        self._dll = dll
        handle_p = ctypes.POINTER(ctypes.c_void_p)
        header_p = ctypes.POINTER(WAVEHDR)
        dll.waveInGetNumDevs.restype = wintypes.UINT
        dll.waveInGetNumDevs.argtypes = []
        dll.waveInGetDevCapsW.restype = wintypes.UINT
        dll.waveInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(WAVEINCAPSW), wintypes.UINT]
        dll.waveInOpen.restype = wintypes.UINT
        dll.waveInOpen.argtypes = [
            handle_p, wintypes.UINT, ctypes.POINTER(WAVEFORMATEX), ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD,
        ]
        for name in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
            function = getattr(dll, name)
            function.restype = wintypes.UINT
            function.argtypes = [ctypes.c_void_p, header_p, wintypes.UINT]
        for name in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
            function = getattr(dll, name)
            function.restype = wintypes.UINT
            function.argtypes = [ctypes.c_void_p]
        dll.waveInGetErrorTextW.restype = wintypes.UINT
        dll.waveInGetErrorTextW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]

    def _check(self, result: int, action: str) -> None:
        if result != MMSYSERR_NOERROR:
            text = ctypes.create_unicode_buffer(256)
            self._dll.waveInGetErrorTextW(result, text, 256)
            raise AudioError(f"MME {action} failed ({result}): {text.value or 'unknown error'}")

    def device_count(self) -> int:
        return int(self._dll.waveInGetNumDevs())

    def device_name(self, index: int) -> str:
        caps = WAVEINCAPSW()
        self._check(self._dll.waveInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)), "device query")
        return caps.szPname

    def open(self, index: int, rate: int = SAMPLE_RATE) -> object:
        fmt = WAVEFORMATEX(
            WAVE_FORMAT_PCM, CHANNELS, rate, rate * CHANNELS * SAMPLE_WIDTH, CHANNELS * SAMPLE_WIDTH, 8 * SAMPLE_WIDTH, 0
        )
        handle = ctypes.c_void_p()
        self._check(self._dll.waveInOpen(ctypes.byref(handle), index, ctypes.byref(fmt), 0, 0, CALLBACK_NULL), "open")
        return handle

    def new_buffer(self, handle: object, size: int) -> MmeBuffer:
        data = ctypes.create_string_buffer(size)
        header = WAVEHDR()
        header.lpData = ctypes.cast(data, ctypes.c_void_p)
        header.dwBufferLength = size
        self._check(self._dll.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header)), "prepare")
        return MmeBuffer(size, data, header)

    def add(self, handle: object, buffer: MmeBuffer) -> None:
        buffer.header.dwFlags &= ~WHDR_DONE
        buffer.header.dwBytesRecorded = 0
        self._check(self._dll.waveInAddBuffer(handle, ctypes.byref(buffer.header), ctypes.sizeof(buffer.header)), "add buffer")

    def is_done(self, buffer: MmeBuffer) -> bool:
        return bool(buffer.header.dwFlags & WHDR_DONE)

    def recorded(self, buffer: MmeBuffer) -> bytes:
        return ctypes.string_at(buffer.data, buffer.header.dwBytesRecorded)

    def start(self, handle: object) -> None:
        self._check(self._dll.waveInStart(handle), "start")

    def reset(self, handle: object) -> None:
        # Stops input and marks every queued buffer done, keeping partial data.
        self._check(self._dll.waveInReset(handle), "reset")

    def unprepare(self, handle: object, buffer: MmeBuffer) -> None:
        self._dll.waveInUnprepareHeader(handle, ctypes.byref(buffer.header), ctypes.sizeof(buffer.header))

    def close(self, handle: object) -> None:
        self._dll.waveInClose(handle)


def input_devices(api: object | None = None) -> list[str]:
    """Names of the MME inputs, by device index. Does not open any device."""
    api = api if api is not None else WinMM()
    return [api.device_name(index) for index in range(api.device_count())]


# ---------------------------------------------------------------- capture


@dataclass(frozen=True)
class CaptureResult:
    pcm: bytes = field(repr=False)
    elapsed_s: float

    @property
    def audio_s(self) -> float:
        return len(self.pcm) / BYTES_PER_SECOND


class Capture:
    """Queue MME buffers, collect them in order and re-queue them.

    With ``background`` a thread collects every ``poll_s``; without it the
    caller (or ``stop``) collects. ``stop`` always releases the device.
    """

    def __init__(
        self,
        api: object,
        device_index: int,
        *,
        buffer_s: float = 0.05,
        buffers: int = 16,
        poll_s: float = 0.01,
        background: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api = api
        self.device_index = device_index
        self.buffer_bytes = max(SAMPLE_WIDTH, int(buffer_s * SAMPLE_RATE) * SAMPLE_WIDTH)
        self.buffer_count = buffers
        self.poll_s = poll_s
        self.background = background
        self.clock = clock
        self.sleep = sleep
        self._handle: object | None = None
        self._queue: deque[MmeBuffer] = deque()
        self._all: list[MmeBuffer] = []
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self.error: Exception | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def start(self) -> None:
        if self._handle is not None:
            raise AudioError("capture already started")
        self._chunks = []
        self.error = None
        handle = self.api.open(self.device_index, SAMPLE_RATE)
        self._handle = handle
        try:
            for _ in range(self.buffer_count):
                buffer = self.api.new_buffer(handle, self.buffer_bytes)
                self._all.append(buffer)
                self.api.add(handle, buffer)
                self._queue.append(buffer)
            self.api.start(handle)
        except Exception:
            self._release()
            raise
        self._started = self.clock()
        self._running = True
        if self.background:
            self._thread = threading.Thread(target=self._run, name="mme-capture", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while self._running:
            try:
                self.pump()
            except Exception as exc:  # reported by stop(); the thread must not die silently
                self.error = exc
                self._running = False
                return
            self.sleep(self.poll_s)

    def pump(self, requeue: bool = True) -> int:
        """Collect the finished buffers at the head of the queue; bytes collected."""
        collected = 0
        with self._lock:
            while self._queue and self.api.is_done(self._queue[0]):
                buffer = self._queue.popleft()
                data = self.api.recorded(buffer)
                self._chunks.append(data)
                collected += len(data)
                if requeue and self._handle is not None:
                    self.api.add(self._handle, buffer)
                    self._queue.append(buffer)
        return collected

    def stop(self) -> CaptureResult:
        """Stop, collect everything recorded and release the device."""
        if self._handle is None:
            raise AudioError("capture not started")
        self._running = False
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        try:
            if self.error is None:
                while self.pump():
                    pass
                self.api.reset(self._handle)
                self.pump(requeue=False)
            elapsed = self.clock() - self._started
        finally:
            self._release()
        if self.error is not None:
            raise AudioError(f"capture failed: {self.error}")
        return CaptureResult(b"".join(self._chunks), elapsed)

    def _release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self.api.reset(handle)
        except Exception:
            pass
        for buffer in self._all:
            self.api.unprepare(handle, buffer)
        self._all = []
        self._queue.clear()
        self.api.close(handle)


# ---------------------------------------------------------------- checks

# Accepted gap between captured audio and wall clock: 0.25 s plus 10 %.
CLOCK_TOLERANCE_S = 0.25
CLOCK_TOLERANCE_RATIO = 0.10
MAX_ZERO_RUN_S = 0.5
FRAME_S = 0.02
# Mean RMS (full scale 1.0) of the loudest 20 % of 20 ms frames: speech sits
# well above this; a muted or unplugged microphone or pure room noise below.
MIN_SPEECH_RMS = 0.01
MIN_TAKE_S = 1.0


def longest_zero_run_s(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    longest = current = 0
    for sample in samples:
        current = current + 1 if sample == 0 else 0
        longest = max(longest, current)
    return longest / SAMPLE_RATE


def speech_level(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    frame = int(FRAME_S * SAMPLE_RATE)
    levels = []
    for start in range(0, len(samples) - frame + 1, frame):
        chunk = samples[start : start + frame]
        levels.append((sum(s * s for s in chunk) / frame) ** 0.5 / 32768.0)
    if not levels:
        return 0.0
    levels.sort(reverse=True)
    top = levels[: max(1, len(levels) // 5)]
    return sum(top) / len(top)


def take_problem(result: CaptureResult) -> str | None:
    """Why a capture must be rejected, or None. English, no audio content."""
    audio_s, real_s = result.audio_s, result.elapsed_s
    if audio_s < MIN_TAKE_S:
        return f"take too short ({audio_s:.2f} s)"
    zeros = longest_zero_run_s(result.pcm)
    if zeros >= MAX_ZERO_RUN_S:
        return f"{zeros:.2f} s of exact digital zeros (microphone muted or driver stalled)"
    tolerance = CLOCK_TOLERANCE_S + CLOCK_TOLERANCE_RATIO * real_s
    if audio_s < real_s - tolerance:
        return f"capture fell behind the clock ({audio_s:.2f} s of audio in {real_s:.2f} s)"
    if audio_s > real_s + tolerance:
        return f"capture ran faster than the clock ({audio_s:.2f} s of audio in {real_s:.2f} s)"
    level = speech_level(result.pcm)
    if level < MIN_SPEECH_RMS:
        return f"level too low ({level:.4f}); check the microphone and speak closer"
    return None
