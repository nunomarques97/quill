"""Render the indicator offscreen, or run the manual on-screen demo.

Usage: py -3.12 -m quill.indicator --render-frames bench/results/indicator
       py -3.12 -m quill.indicator --demo --allow-desktop-window [--seconds 60] [--position pointer]

``--render-frames`` draws every state at 100 % and 150 % scale, a 12-frame
listening sequence at varying voice levels, reduced-motion frames, long-text
cases, the Claude Code alerts with project names (one finished reply, one
permission request, several projects, a long name and an overflow, with the
words line the sessions build), the mouse 5 enrichment texts (enriching,
enriched and sent, refused) and a contact sheet (each frame over a light and a dark desktop) into
PNG files with GDI+ in memory. It creates no window (it checks that the
process owns none at the end) and prints the offscreen frame time p50/p95,
also written to ``timing.json`` next to the frames. The texts are invented.

``--demo`` is manual only and never part of tests or checks: it shows the
real overlay cycling through the states for ``--seconds`` (or until Ctrl+C)
while checking that the foreground window is never the indicator. It writes
an aggregate (counts and frame times only) to
``local/selftest/indicator-<UTC time>.json``. Without
``--allow-desktop-window`` it exits 2 before creating anything.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from quill.indicator.render import (CLAUDE_DONE, CLAUDE_PERMISSION, COMMAND, ERROR, LISTENING, LOADING, REVIEWING,
                                    SENT, STATES, TRANSCRIBING, VOICE, VOICE_NONE, VOICE_OPEN, Layout, Renderer,
                                    View)
from quill import enrich
from quill.session import ALERT_STATES, ENRICHING, MESSAGES, alert_text
from quill.sound import DONE, PERMISSION

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "local" / "selftest"
RESULT_VERSION = 1
SCALES = (1.0, 1.5)
SECONDS_RANGE = (5, 1800)

REFUSAL = (
    "refusing to run: the demo shows the real indicator window on the desktop.\n"
    "Pass --allow-desktop-window to run it."
)

# Invented sample texts (no real names, projects or dictations).
SAMPLES = {
    LOADING: "",
    LISTENING: "vamos rever o pull request antes do deploy de amanhã",
    TRANSCRIBING: "vamos rever o pull request antes do deploy de amanhã e depois",
    COMMAND: "põe isto mais formal",
    REVIEWING: "revi o módulo de pagamentos e encontrei dois problemas no cache",
    SENT: "corrige o teste de integração e corre a suite outra vez",
    ERROR: "Microfone não encontrado",
    CLAUDE_DONE: "",
    CLAUDE_PERMISSION: "",
    VOICE: "abre VS Code no nimbus deck",
    VOICE_OPEN: "A abrir nimbus-deck",
    VOICE_NONE: "Não sei qual abrir. Parecidos: nimbus-deck, nimbus-deck-public, tarvo-kit",
}
LONG_TEXT = (
    "hoje de manhã revi o módulo de pagamentos e encontrei dois problemas no cache, o primeiro é que o "
    "timeout está demasiado curto para pedidos grandes e o segundo é que o retry não respeita o backoff, "
    "por isso proponho mudar a configuração e acrescentar testes de integração antes do próximo release"
)
LONG_WORD = "supercalifragilisticexpialidocious-configuration-override-value-without-spaces"
SEQUENCE_LEVELS = (0.0, 0.12, 0.35, 0.62, 0.9, 1.0, 0.78, 0.5, 0.3, 0.18, 0.06, 0.0)
# Claude Code alerts waiting together, as (kind, invented project) in arrival order.
ALERT_SAMPLES = (
    ("alert-project-done", [(DONE, "nimbus-deck")]),
    ("alert-project-permission", [(PERMISSION, "tarvo-kit")]),
    ("alert-projects-mixed", [(DONE, "nimbus-deck"), (PERMISSION, "tarvo-kit"), (DONE, "orla-notes")]),
    ("alert-project-long-name", [(PERMISSION, "nimbus-deck-public-documentation-site-archive-x")]),
    ("alert-projects-overflow", [(PERMISSION, "tarvo-kit"), *((DONE, f"nimbus-deck-service-{index:02d}")
                                                               for index in range(1, 9)),
                                 (PERMISSION, "velo-api")]),
)


def frame_set() -> list[tuple[str, View, float]]:
    """(file stem, view, scale) of every rendered frame."""
    frames: list[tuple[str, View, float]] = []
    for scale in SCALES:
        pct = round(scale * 100)
        for state in STATES:
            frames.append((f"state-{state}-{pct}", View(state, SAMPLES[state], 0.55, 0.4), scale))
        frames.append((f"state-listening-empty-{pct}", View(LISTENING, "", 0.2, 0.4), scale))
        frames.append((f"long-text-{pct}", View(LISTENING, LONG_TEXT, 0.7, 0.8), scale))
    words = SAMPLES[LISTENING].split()
    for index, level in enumerate(SEQUENCE_LEVELS):
        shown = " ".join(words[:1 + index * len(words) // len(SEQUENCE_LEVELS)])
        frames.append((f"listening-seq-{index + 1:02d}", View(LISTENING, shown, level, index / 15.0), 1.0))
    frames.append(("reduced-listening-100", View(LISTENING, SAMPLES[LISTENING], 0.6, 1.3, True), 1.0))
    frames.append(("reduced-command-100", View(COMMAND, SAMPLES[COMMAND], 0.35, 1.3, True), 1.0))
    frames.append(("reduced-loading-150", View(LOADING, "", 0.0, 1.3, True), 1.5))
    frames.append(("reduced-claude_permission-150", View(CLAUDE_PERMISSION, "", 0.0, 1.3, True), 1.5))
    frames.append(("reduced-voice-100", View(VOICE, SAMPLES[VOICE], 0.45, 1.3, True), 1.0))
    for scale in SCALES:
        pct = round(scale * 100)
        frames.append((f"state-voice-empty-{pct}", View(VOICE, "", 0.2, 0.4), scale))
        frames.append((f"state-voice_none-unrecognized-{pct}", View(VOICE_NONE, "Comando não reconhecido", 0.0, 0.4),
                       scale))
    frames.append(("long-word-100", View(LISTENING, LONG_WORD, 0.4, 0.5), 1.0))
    for scale in SCALES:
        pct = round(scale * 100)
        for stem, alerts in ALERT_SAMPLES:
            kind, text = alert_text(list(alerts))
            frames.append((f"{stem}-{pct}", View(ALERT_STATES[kind], text, 0.0, 0.4), scale))
    for scale in SCALES:
        pct = round(scale * 100)
        # Mouse 5 into Claude Code: the enrichment running, then its outcome after the Enter.
        frames.append((f"reviewing-enriching-{pct}", View(REVIEWING, ENRICHING, 0.0, 0.4), scale))
        frames.append((f"sent-enriched-{pct}", View(SENT, MESSAGES[enrich.ENRICHED], 0.0, 0.4), scale))
        frames.append((f"sent-enrich-refused-{pct}", View(SENT, MESSAGES[enrich.REFUSED], 0.0, 0.4), scale))
    return frames


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))]


class FrameBuffer:
    """Top-down PARGB pixels of one rendered frame."""

    def __init__(self, lay: Layout) -> None:
        self.width, self.height = lay.width, lay.height
        self.pixels = (ctypes.c_uint32 * (lay.width * lay.height))()

    @property
    def address(self) -> int:
        return ctypes.addressof(self.pixels)


def render_frames(out_dir: Path, out: Callable[[str], None] = print) -> int:
    from quill.indicator.gdiplus import Linear, Solid, png_bytes

    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = Renderer()
    gdip = renderer.gdip
    written: list[Path] = []
    try:
        rendered: list[tuple[str, FrameBuffer]] = []
        for stem, view, scale in frame_set():
            lay = renderer.layout(view, scale)
            frame = FrameBuffer(lay)
            renderer.draw(frame.address, frame.width * 4, lay, view)
            with gdip.bitmap(frame.address, frame.width, frame.height, frame.width * 4) as image:
                rgba = gdip.straight_rgba(image, frame.width, frame.height)
            path = out_dir / f"{stem}.png"
            path.write_bytes(png_bytes(frame.width, frame.height, rgba))
            written.append(path)
            rendered.append((stem, frame))
        written.append(_contact_sheet(renderer, rendered, out_dir / "contact-sheet.png", Linear, Solid, png_bytes))
        timing = _frame_timing(renderer)
    finally:
        renderer.close()
    windows = own_windows()
    for path in written:
        out(_label(path))
    out(f"offscreen frame time over {timing['frames']} frames: p50 {timing['p50_ms']:.2f} ms, "
        f"p95 {timing['p95_ms']:.2f} ms, max {timing['max_ms']:.2f} ms")
    (out_dir / "timing.json").write_text(json.dumps(timing, indent=2) + "\n", "utf-8")
    if windows:
        out(f"error: the process owns {windows} window(s) after rendering")
        return 1
    out(f"{len(written)} PNG files, no window created")
    return 0


def _contact_sheet(renderer: Renderer, frames: list[tuple[str, FrameBuffer]], path: Path, linear: type,
                   solid: type, png_bytes: Callable[[int, int, bytes], bytes]) -> Path:
    """Every frame over a light and a dark desktop, with its name, in one PNG."""
    gdip = renderer.gdip
    caption_w, pad = 250, 12
    cell_w = max(frame.width for _, frame in frames) + 2 * pad
    rows = [frame.height + 2 * pad for _, frame in frames]
    width, height = caption_w + 2 * cell_w, sum(rows)
    sheet = (ctypes.c_uint32 * (width * height))()
    address = ctypes.addressof(sheet)
    label_font, _ = renderer.fonts(1.0)
    with gdip.bitmap(address, width, height, width * 4) as image, gdip.canvas(image) as canvas:
        canvas.clear(0xFF15171C)
        top = 0
        for (stem, frame), row in zip(frames, rows):
            light = linear(0, top, 0, top + row, 0xFFF4F4F2, 0xFFD9DEE6)
            dark = linear(0, top, 0, top + row, 0xFF2B3036, 0xFF101216)
            canvas.round_rect(caption_w, top, cell_w, row, 0, fill=light)
            canvas.round_rect(caption_w + cell_w, top, cell_w, row, 0, fill=dark)
            canvas.round_rect(0, top + row - 1, width, 1, 0, fill=solid(0xFF3A3F47))
            canvas.text(12, top + pad, stem, label_font, renderer._format, 0xFFE8ECF2)  # type: ignore[arg-type]
            with gdip.bitmap(frame.address, frame.width, frame.height, frame.width * 4) as picture:
                canvas.image(picture, caption_w + pad, top + pad, frame.width, frame.height)
                canvas.image(picture, caption_w + cell_w + pad, top + pad, frame.width, frame.height)
            top += row
        rgba = gdip.straight_rgba(image, width, height)
    path.write_bytes(png_bytes(width, height, rgba))
    return path


def _frame_timing(renderer: Renderer, frames: int = 300) -> dict[str, float]:
    """Layout plus paint time of a live listening session, words growing, both scales."""
    words = LONG_TEXT.split()
    times: list[float] = []
    buffers: dict[float, FrameBuffer] = {}
    floor = 0.0
    for index in range(frames):
        scale = SCALES[index % len(SCALES)]
        level = 0.5 + 0.5 * math.sin(index / 3.0)
        view = View(LISTENING, " ".join(words[:index // 6]), level, index / 30.0)
        started = time.perf_counter()
        lay = renderer.layout(view, scale, floor)
        frame = buffers.get(scale)
        if frame is None or frame.width < lay.width or frame.height < lay.height:
            frame = buffers[scale] = FrameBuffer(Layout(scale, lay.width + 64, lay.height + 64, lay.capsule, "", (),
                                                        False, (0, 0), 0, 0, 0, 0))
        renderer.draw(frame.address, frame.width * 4, lay, view)
        times.append((time.perf_counter() - started) * 1000.0)
    return {"frames": len(times), "p50_ms": round(percentile(times, 0.5), 3),
            "p95_ms": round(percentile(times, 0.95), 3), "max_ms": round(max(times), 3)}


def own_windows() -> int:
    """Top-level and message-only windows owned by this process (read-only query)."""
    if sys.platform != "win32":
        return 0
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    pid = os.getpid()
    count = 0

    def owned(hwnd: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return owner.value == pid

    def visit(hwnd: int, _param: int) -> bool:
        nonlocal count
        count += owned(hwnd)
        return True

    user32.EnumWindows(enum_proc(visit), 0)
    child = None
    while True:
        child = user32.FindWindowExW(wintypes.HWND(-3), child, None, None)  # HWND_MESSAGE
        if not child:
            break
        count += owned(child)
    return count


# ---------------------------------------------------------------- manual demo


DEMO_STEPS: tuple[tuple[str, float], ...] = (
    (LOADING, 1.5), (LISTENING, 4.5), (TRANSCRIBING, 1.0), (SENT, 1.5),
    (COMMAND, 3.0), (TRANSCRIBING, 0.8), (REVIEWING, 1.5), (ERROR, 2.0), (VOICE, 3.0), (VOICE_OPEN, 1.5),
    (VOICE_NONE, 2.0), ("", 1.0),
)


def run_demo(position: str, seconds: float, results_dir: Path = RESULTS_DIR,
             out: Callable[[str], None] = print) -> int:
    from quill.indicator.window import Indicator, OverlayWin32

    layer = OverlayWin32()
    indicator = Indicator(position)
    out(f"Showing the indicator for {seconds:.0f} s ({position}); keep working normally, it must never take focus.")
    started = time.monotonic()
    foreground_hits = samples = shown = 0
    indicator.start()
    try:
        words = SAMPLES[LISTENING].split()
        while time.monotonic() - started < seconds:
            for state, duration in DEMO_STEPS:
                step_start = time.monotonic()
                if not state:
                    indicator.hide()
                else:
                    indicator.show(state, "" if state in (LISTENING, LOADING) else SAMPLES[state])
                    shown += 1
                while time.monotonic() - step_start < duration:
                    elapsed = time.monotonic() - step_start
                    if state in (LISTENING, COMMAND, VOICE):
                        indicator.set_level(abs(math.sin(elapsed * 5.1)) * (0.4 + 0.6 * abs(math.sin(elapsed * 1.3))))
                    if state == LISTENING:
                        indicator.set_text(" ".join(words[:int(elapsed / duration * len(words)) + 1]))
                    hwnd = indicator.overlay.hwnd if indicator.overlay is not None else 0
                    samples += 1
                    foreground_hits += bool(hwnd) and layer.foreground_window() == hwnd
                    time.sleep(0.05)
                if time.monotonic() - started >= seconds:
                    break
    except KeyboardInterrupt:
        out("Stopped.")
    finally:
        frame_ms = list(indicator.overlay.frame_ms) if indicator.overlay is not None else []
        indicator.stop()
    finished = datetime.now(timezone.utc)
    data: dict[str, object] = {
        "version": RESULT_VERSION,
        "finished": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "position": position,
        "seconds": round(time.monotonic() - started, 1),
        "states_shown": shown,
        "foreground_samples": samples,
        "foreground_was_indicator": foreground_hits,
        "frames": len(frame_ms),
        "frame_ms_p50": round(percentile(frame_ms, 0.5), 3) if frame_ms else None,
        "frame_ms_p95": round(percentile(frame_ms, 0.95), 3) if frame_ms else None,
        "passed": foreground_hits == 0,
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"indicator-{finished.strftime('%Y%m%dT%H%M%SZ')}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)
    out(f"Focus taken by the indicator: {foreground_hits} of {samples} samples. Result: {_label(path)}")
    return 0 if data["passed"] else 1


def _label(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.indicator", description=__doc__.splitlines()[0])
    parser.add_argument("--render-frames", type=Path, metavar="DIR", help="render every state offscreen to PNG")
    parser.add_argument("--demo", action="store_true", help="manual: show the real overlay cycling states")
    parser.add_argument("--allow-desktop-window", action="store_true",
                        help="required with --demo: allow a window on the desktop")
    parser.add_argument("--seconds", type=float, default=60.0, help="demo length (default 60)")
    parser.add_argument("--position", choices=("pointer", "bottom-center"), default="pointer",
                        help="demo placement (default pointer)")
    args = parser.parse_args(argv)
    if args.demo == bool(args.render_frames):
        parser.print_usage(sys.stderr)
        print("choose exactly one of --render-frames DIR or --demo", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("the indicator needs Windows", file=sys.stderr)
        return 2
    if args.render_frames:
        return render_frames(args.render_frames)
    if not args.allow_desktop_window:
        print(REFUSAL, file=sys.stderr)
        return 2
    low, high = SECONDS_RANGE
    if not low <= args.seconds <= high:
        parser.error(f"--seconds must be from {low} to {high}")
    return run_demo(args.position, args.seconds)


if __name__ == "__main__":
    sys.exit(main())
