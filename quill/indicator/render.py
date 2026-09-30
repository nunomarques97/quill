"""What the indicator draws: states, text layout, placement and the scene.

Direction (docs/research/FASE2.md, recorded decision): a dark translucent
glass capsule beside the place where the text will appear. The live words are
the dominant element (large, near-white, at most two lines, older words cut
at the left with an ellipsis); the state is secondary: a small European
Portuguese label in the state colour and an orb on the left whose glyph and
motion differ per state. Motion only shows the voice level and the state.
With reduced motion the rings are static and a level bar shows the voice.

Everything here except ``Renderer`` is pure: layout takes a text measuring
function, ``scene`` returns drawing operations, and ``place`` computes the
window position, so tests check them without GDI+. ``Renderer`` replays the
scene on a GDI+ canvas over a 32-bit premultiplied bitmap.
"""

from __future__ import annotations

import ctypes
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from quill.indicator.gdiplus import (PIXEL_FORMAT_32BPP_PARGB, TEXT_RENDERING_ANTIALIAS_GRIDFIT, Canvas, Gdiplus,
                                     Linear, Radial, Solid, Stroke)

LOADING = "loading"
LISTENING = "listening"
TRANSCRIBING = "transcribing"
COMMAND = "command"
REVIEWING = "reviewing"
SENT = "sent"
ERROR = "error"
CLAUDE_DONE = "claude_done"
CLAUDE_PERMISSION = "claude_permission"
# Voice commands (quill.voice): listening to a command, a project opening, nothing opened.
VOICE = "voice"
VOICE_OPEN = "voice_open"
VOICE_NONE = "voice_none"
STATES = (LOADING, LISTENING, TRANSCRIBING, COMMAND, REVIEWING, SENT, ERROR, CLAUDE_DONE, CLAUDE_PERMISSION,
          VOICE, VOICE_OPEN, VOICE_NONE)

LABELS = {
    LOADING: "A carregar",
    LISTENING: "A ouvir",
    TRANSCRIBING: "A transcrever",
    COMMAND: "Modo comando",
    REVIEWING: "A rever o texto",
    SENT: "Enviado para o Claude Code",
    ERROR: "Erro",
    CLAUDE_DONE: "Claude Code terminou",
    CLAUDE_PERMISSION: "Claude Code pede permissão",
    VOICE: "Comando de voz",
    VOICE_OPEN: "Comando executado",
    VOICE_NONE: "Nada foi aberto",
}
# Shown in the words line, in the secondary colour, while a state has no text.
PLACEHOLDERS = {
    LOADING: "A preparar o reconhecimento de voz…",
    LISTENING: "Pode falar…",
    TRANSCRIBING: "A finalizar o texto…",
    COMMAND: "Diga a instrução…",
    REVIEWING: "A corrigir palavras mal ouvidas…",
    SENT: "Texto enviado com Enter",
    ERROR: "Não foi possível concluir",
    CLAUDE_DONE: "A resposta está pronta; é a sua vez",
    CLAUDE_PERMISSION: "Aprove ou recuse o pedido no Claude Code",
    VOICE: "Diga, por exemplo: abre VS Code no projeto…",
    VOICE_OPEN: "A abrir o projeto…",
    VOICE_NONE: "Comando não reconhecido",
}
# State colours (0xRRGGBB): distinct hues, each >= 4.5:1 on the worst backdrop.
STATE_COLOURS = {
    LOADING: 0x9FB7D9,
    LISTENING: 0x4FE3FF,
    TRANSCRIBING: 0xC4A6FF,
    COMMAND: 0xFFC857,
    REVIEWING: 0xFF8AD8,
    SENT: 0x5CF2A0,
    ERROR: 0xFF9494,
    CLAUDE_DONE: 0xBDF26B,
    CLAUDE_PERMISSION: 0xFFA25F,
    VOICE: 0x8FB0FF,
    VOICE_OPEN: 0x6FF0D8,
    VOICE_NONE: 0xFFB0C4,
}
# The Claude Code alerts: an orb that calls for attention, not for the voice.
ALERT_STATES = (CLAUDE_DONE, CLAUDE_PERMISSION)
# States whose orb follows the voice level.
LEVEL_STATES = (LISTENING, COMMAND, VOICE)

HOLO_CYAN = 0x4FE3FF
HOLO_VIOLET = 0xA46BFF
WORDS_COLOUR = 0xF4F8FF
SECONDARY_COLOUR = 0xAEB9D6
GLYPH_COLOUR = 0x0B1020
# Glass: top and bottom of the capsule gradient (0xAARRGGBB).
GLASS_TOP = 0xF0161C38
GLASS_BOTTOM = 0xF50A0E1F
# Top reflection over the glass, the lightest layer behind the text.
SHEEN_TOP = 0x14FFFFFF
ELLIPSIS = "…"

WORDS_FONTS = ("Segoe UI Variable Display", "Segoe UI")
LABEL_FONTS = ("Segoe UI Variable Text", "Segoe UI")


@dataclass(frozen=True)
class Metrics:
    """Sizes in pixels at 100 % scale."""

    glow: float = 16.0
    pad_top: float = 11.0
    label_size: float = 12.0
    label_line: float = 16.0
    gap: float = 3.0
    words_size: float = 17.0
    words_line: float = 23.0
    pad_bottom: float = 11.0
    orb_area: float = 66.0
    pad_right: float = 20.0
    radius: float = 22.0
    min_width: float = 240.0
    max_width: float = 520.0
    # Pointer placement: capsule left edge right of the hotspot, or right edge left of it.
    gap_right: float = 30.0
    gap_left: float = 22.0
    gap_below: float = 34.0
    bottom_margin: float = 40.0


METRICS = Metrics()


@dataclass(frozen=True)
class View:
    """One frame's input: the state, its text, the voice level (0..1) and time in the state."""

    state: str
    text: str = ""
    level: float = 0.0
    t: float = 0.0
    reduced_motion: bool = False


@dataclass(frozen=True)
class Layout:
    scale: float
    width: int
    height: int
    capsule: tuple[float, float, float, float]
    label: str
    lines: tuple[str, ...]
    placeholder: bool
    orb: tuple[float, float]
    label_y: float
    words_x: float
    words_y: float
    line_height: float
    content_width: float = 0.0


@dataclass(frozen=True)
class Op:
    """One drawing call on a ``Canvas``: method name, positional and keyword arguments."""

    name: str
    args: tuple[object, ...]
    kwargs: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------- colour


def argb(rgb: int, alpha: float) -> int:
    a = max(0, min(255, round(alpha * 255)))
    return (a << 24) | (rgb & 0xFFFFFF)


def _channels(colour: int) -> tuple[float, float, float, float]:
    return ((colour >> 24) & 0xFF) / 255, ((colour >> 16) & 0xFF) / 255, ((colour >> 8) & 0xFF) / 255, \
        (colour & 0xFF) / 255


def over(top: int, bottom_rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """``top`` (ARGB) composited over an opaque colour (RGB 0..1)."""
    a, r, g, b = _channels(top)
    return tuple(a * c + (1 - a) * d for c, d in zip((r, g, b), bottom_rgb))  # type: ignore[return-value]


def luminance(rgb: tuple[float, float, float]) -> float:
    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def rgb_of(colour: int) -> tuple[float, float, float]:
    return ((colour >> 16) & 0xFF) / 255, ((colour >> 8) & 0xFF) / 255, (colour & 0xFF) / 255


def backdrops() -> list[tuple[float, float, float]]:
    """The text backdrop over a black and over a white desktop (the two extremes)."""
    found = []
    for desktop in ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)):
        for glass in (GLASS_TOP, GLASS_BOTTOM):
            found.append(over(SHEEN_TOP, over(glass, desktop)))
    return found


def min_contrast(colour: int) -> float:
    return min(contrast(rgb_of(colour), backdrop) for backdrop in backdrops())


# ---------------------------------------------------------------- text


def fit_words(text: str, max_width: float, measure: Callable[[str], float], max_lines: int = 2) -> list[str]:
    """Wrap ``text`` into at most ``max_lines`` lines of ``max_width``.

    When everything fits, lines are filled from the start. Otherwise the
    newest words win: lines are filled from the end and the first line
    starts with an ellipsis, so the latest words are always visible.
    """
    words = text.split()
    if not words:
        return []
    words = [_fit_word(word, max_width, measure) for word in words]
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and measure(candidate) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    lines.append(" ".join(current))
    if len(lines) <= max_lines:
        return lines
    remaining = list(words)
    tail: list[str] = []
    for index in range(max_lines):
        first_line = index == max_lines - 1
        line: list[str] = []
        while remaining:
            candidate = [remaining[-1]] + line
            shown = " ".join(candidate)
            if first_line and len(remaining) > 1:
                shown = ELLIPSIS + " " + shown
            if line and measure(shown) > max_width:
                break
            line.insert(0, remaining.pop())
        if first_line and remaining:
            while len(line) > 1 and measure(ELLIPSIS + " " + " ".join(line)) > max_width:
                line.pop(0)
            text_line = ELLIPSIS + " " + " ".join(line)
            if measure(text_line) > max_width:
                text_line = _fit_word(ELLIPSIS + line[-1], max_width, measure)
            tail.insert(0, text_line)
        else:
            tail.insert(0, " ".join(line))
    return tail


def _fit_word(word: str, max_width: float, measure: Callable[[str], float]) -> str:
    """A word wider than the line keeps its end, after an ellipsis."""
    if measure(word) <= max_width:
        return word
    kept = word.lstrip(ELLIPSIS)
    while len(kept) > 1 and measure(ELLIPSIS + kept) > max_width:
        kept = kept[1:]
    return ELLIPSIS + kept


def layout(view: View, scale: float, measure_label: Callable[[str], float],
           measure_words: Callable[[str], float], min_content: float = 0.0, max_width: float | None = None,
           metrics: Metrics = METRICS) -> Layout:
    """Sizes and positions of one frame; measuring functions work in pixels at ``scale``.

    ``max_width`` (pixels) narrows the capsule on a monitor too small for it.
    """
    m, s = metrics, scale
    limit = m.max_width * s if max_width is None else max(m.min_width * s, min(m.max_width * s, max_width))
    label = LABELS[view.state]
    text = " ".join(view.text.split())
    placeholder = not text
    shown = PLACEHOLDERS[view.state] if placeholder else text
    words_room = limit - (m.orb_area + m.pad_right) * s
    lines = tuple(fit_words(shown, words_room, measure_words))
    content = max([measure_label(label)] + [measure_words(line) for line in lines] + [min_content])
    width = max(m.min_width * s, min(limit, m.orb_area * s + content + m.pad_right * s))
    height = (m.pad_top + m.label_line + m.gap + m.words_line * max(1, len(lines)) + m.pad_bottom) * s
    g = m.glow * s
    capsule = (g, g, float(math.ceil(width)), float(math.ceil(height)))
    return Layout(
        scale=s,
        width=int(math.ceil(width + 2 * g)),
        height=int(math.ceil(height + 2 * g)),
        capsule=capsule,
        label=label,
        lines=lines,
        placeholder=placeholder,
        orb=(g + m.orb_area * s / 2, g + capsule[3] / 2),
        label_y=g + m.pad_top * s,
        words_x=g + m.orb_area * s,
        words_y=g + (m.pad_top + m.label_line + m.gap) * s,
        line_height=m.words_line * s,
        content_width=content,
    )


# ---------------------------------------------------------------- placement


def place(size: tuple[int, int], hotspot: tuple[int, int], work: tuple[int, int, int, int], scale: float,
          position: str = "pointer", metrics: Metrics = METRICS) -> tuple[int, int]:
    """Top-left of the indicator window inside the monitor work area ``(left, top, right, bottom)``.

    ``pointer``: right of the pointer, vertically centred on it; flips to the
    left at the right edge, and goes below (or above) the pointer when
    neither side fits. The window never contains the pointer hotspot.
    ``bottom-center``: centred near the bottom of the work area.
    """
    width, height = size
    left, top, right, bottom = work
    g, s = metrics.glow * scale, scale
    if position == "bottom-center":
        x = left + (right - left - width) // 2
        y = bottom - height - round((metrics.bottom_margin - metrics.glow) * s)
        return _clamp(x, left, right - width), _clamp(y, top, bottom - height)
    px, py = hotspot
    y = _clamp(py - height // 2, top, bottom - height)
    x_right = px + round(metrics.gap_right * s - g)
    x_left = px - round(metrics.gap_left * s - g) - width
    if x_right + width <= right:
        return x_right, y
    if x_left >= left:
        return x_left, y
    x = _clamp(px - width // 2, left, right - width)
    y_below = py + round(metrics.gap_below * s - g)
    if y_below + height <= bottom:
        return x, y_below
    return x, _clamp(py - round(metrics.gap_left * s - g) - height, top, py - height - 1)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high)) if high >= low else low


# ---------------------------------------------------------------- scene


def scene(view: View, lay: Layout) -> list[Op]:
    """Drawing operations for one frame, back to front."""
    s = lay.scale
    colour = STATE_COLOURS[view.state]
    x, y, w, h = lay.capsule
    radius = min(METRICS.radius * s, h / 2)
    ops: list[Op] = []
    # Outer holographic glow, outside the glass.
    for grow, alpha in ((12.0, 0.04), (7.0, 0.07), (3.0, 0.12)):
        d = grow * s
        ops.append(Op("round_rect", (x - d, y - d, w + 2 * d, h + 2 * d, radius + d),
                      {"fill": Solid(argb(colour, alpha))}))
    # Glass body, reflection and holographic rim.
    ops.append(Op("round_rect", (x, y, w, h, radius), {"fill": Linear(x, y, x, y + h, GLASS_TOP, GLASS_BOTTOM)}))
    ops.append(Op("round_rect", (x + 1 * s, y + 1 * s, w - 2 * s, h * 0.5, radius - 1 * s),
                  {"fill": Linear(x, y, x, y + h * 0.5, SHEEN_TOP, 0x00FFFFFF)}))
    rim = Linear(x, y, x + w, y + h, argb(colour, 0.75), argb(HOLO_VIOLET, 0.45))
    ops.append(Op("round_rect", (x + 0.5, y + 0.5, w - 1, h - 1, radius), {"stroke": Stroke(1.2 * s, rim)}))
    ops.extend(_orb(view, lay))
    ops.append(Op("text", (lay.words_x, lay.label_y, lay.label, "label", argb(colour, 1.0))))
    words = SECONDARY_COLOUR if lay.placeholder else WORDS_COLOUR
    for index, line in enumerate(lay.lines):
        ops.append(Op("text", (lay.words_x, lay.words_y + index * lay.line_height, line, "words",
                               argb(words, 1.0))))
    return ops


def _orb(view: View, lay: Layout) -> list[Op]:
    s = lay.scale
    cx, cy = lay.orb
    colour = STATE_COLOURS[view.state]
    still = view.reduced_motion
    t = 0.0 if still else view.t
    level = max(0.0, min(1.0, view.level)) if view.state in LEVEL_STATES else 0.0
    # Motion uses the level; with reduced motion only the level bar does.
    motion_level = 0.0 if still else level
    ops: list[Op] = []
    aura = 0.22 + 0.30 * motion_level
    ops.append(Op("ellipse", (cx, cy, 29 * s, 29 * s), {"fill": Radial(cx, cy, 29 * s, 29 * s,
                                                                         argb(colour, aura), argb(colour, 0.0))}))
    if view.state in LEVEL_STATES:
        ops.extend(_voice_rings(cx, cy, s, colour, t, motion_level, still))
        if view.state == COMMAND:
            ops.append(_diamond(cx, cy, 20 * s, 30.0 * t + 45.0, Stroke(1.8 * s, Solid(argb(colour, 0.95)))))
        elif view.state == VOICE:
            # Two brackets around the core, like a command line waiting: a spoken command.
            bracket = Stroke(1.8 * s, Solid(argb(colour, 0.95)))
            for side in (-1.0, 1.0):
                x0, x1 = cx + side * 20.5 * s, cx + side * 23.5 * s
                ops.append(Op("polyline", ([(x0, cy - 7.0 * s), (x1, cy - 7.0 * s), (x1, cy + 7.0 * s),
                                            (x0, cy + 7.0 * s)], False), {"stroke": bracket}))
        if still:
            ops.extend(_level_bar(cx, cy, s, colour, level))
    elif view.state == LOADING:
        ops.append(Op("ellipse", (cx, cy, 17 * s, 17 * s), {"stroke": Stroke(1.5 * s, Solid(argb(colour, 0.25)))}))
        ops.append(Op("arc", (cx, cy, 17 * s, (330.0 * t) % 360.0 - 90.0, 110.0,
                              Stroke(2.4 * s, Solid(argb(colour, 0.95))))))
    elif view.state == REVIEWING:
        # A faint track with two solid arcs sweeping it: the text being read through.
        ops.append(Op("ellipse", (cx, cy, 19 * s, 19 * s), {"stroke": Stroke(1.2 * s, Solid(argb(colour, 0.3)))}))
        for offset in (0.0, 180.0):
            ops.append(Op("arc", (cx, cy, 19 * s, (200.0 * t + offset) % 360.0, 70.0,
                                  Stroke(2.2 * s, Solid(argb(colour, 0.95))))))
    elif view.state in ALERT_STATES:
        # A steady ring and a slower halo that breathes: someone is waiting for you.
        breath = 0.5 if still else 0.5 + 0.5 * math.sin(2 * math.pi * 0.8 * t)
        ops.append(Op("ellipse", (cx, cy, 18 * s, 18 * s), {"stroke": Stroke(1.6 * s, Solid(argb(colour, 0.8)))}))
        ops.append(Op("ellipse", (cx, cy, 23 * s, 23 * s),
                      {"stroke": Stroke(1.2 * s, Solid(argb(colour, 0.2 + 0.4 * breath)))}))
    elif view.state == TRANSCRIBING:
        ops.append(Op("arc", (cx, cy, 15 * s, (140.0 * t) % 360.0, 250.0,
                              Stroke(1.6 * s, Solid(argb(colour, 0.85)), dashed=True))))
        ops.append(Op("arc", (cx, cy, 21 * s, (-90.0 * t) % 360.0 + 180.0, 200.0,
                              Stroke(1.6 * s, Solid(argb(HOLO_CYAN, 0.6)), dashed=True))))
    else:
        burst = 0.0 if still else min(1.0, view.t / 0.6)
        if burst < 1.0:
            r = (14 + 14 * burst) * s
            ops.append(Op("ellipse", (cx, cy, r, r), {"stroke": Stroke(1.5 * s, Solid(argb(colour, 0.8 * (1 - burst))))}))
        ops.append(Op("ellipse", (cx, cy, 18 * s, 18 * s), {"stroke": Stroke(1.6 * s, Solid(argb(colour, 0.7)))}))
    pulse = 0.0 if still or view.state != TRANSCRIBING else 1.5 * math.sin(2 * math.pi * 1.2 * t)
    core = (10.0 + 2.5 * motion_level + pulse) * s
    if view.state in (SENT, ERROR, REVIEWING, VOICE_OPEN, VOICE_NONE, *ALERT_STATES):
        core = 12.0 * s
    ops.append(Op("ellipse", (cx, cy, core, core), {"fill": Radial(cx - core * 0.3, cy - core * 0.35, core * 1.6,
                                                                     core * 1.6, argb(0xFFFFFF, 1.0),
                                                                     argb(colour, 1.0))}))
    if view.state == SENT:
        mark = [(cx - 5.0 * s, cy + 0.2 * s), (cx - 1.5 * s, cy + 3.8 * s), (cx + 5.2 * s, cy - 3.8 * s)]
        ops.append(Op("polyline", (mark, False), {"stroke": Stroke(2.4 * s, Solid(argb(GLYPH_COLOUR, 1.0)))}))
    elif view.state == REVIEWING:
        # Three lines of text on the core.
        for dy, width in ((-3.6, 10.0), (0.0, 10.0), (3.6, 6.0)):
            y = cy + dy * s
            ops.append(Op("polyline", ([(cx - 5.0 * s, y), (cx - 5.0 * s + width * s, y)], False),
                          {"stroke": Stroke(1.8 * s, Solid(argb(GLYPH_COLOUR, 1.0)))}))
    elif view.state == CLAUDE_DONE:
        # Three dots: the conversation waits for your turn.
        for dx in (-4.2, 0.0, 4.2):
            ops.append(Op("ellipse", (cx + dx * s, cy, 1.6 * s, 1.6 * s), {"fill": Solid(argb(GLYPH_COLOUR, 1.0))}))
    elif view.state == CLAUDE_PERMISSION:
        # A padlock: a permission is needed.
        shackle = Stroke(1.8 * s, Solid(argb(GLYPH_COLOUR, 1.0)))
        ops.append(Op("arc", (cx, cy - 1.2 * s, 3.2 * s, 180.0, 180.0, shackle)))
        ops.append(Op("round_rect", (cx - 4.6 * s, cy - 1.2 * s, 9.2 * s, 7.0 * s, 1.4 * s),
                      {"fill": Solid(argb(GLYPH_COLOUR, 1.0))}))
    elif view.state == VOICE:
        # A prompt chevron on the core.
        ops.append(Op("polyline", ([(cx - 3.2 * s, cy - 4.2 * s), (cx + 1.8 * s, cy), (cx - 3.2 * s, cy + 4.2 * s)],
                                   False), {"stroke": Stroke(2.2 * s, Solid(argb(GLYPH_COLOUR, 1.0)))}))
    elif view.state == VOICE_OPEN:
        # An arrow leaving a corner: something opens elsewhere.
        glyph = Stroke(2.0 * s, Solid(argb(GLYPH_COLOUR, 1.0)))
        ops.append(Op("polyline", ([(cx - 4.4 * s, cy + 4.4 * s), (cx + 4.0 * s, cy - 4.0 * s)], False),
                      {"stroke": glyph}))
        ops.append(Op("polyline", ([(cx - 1.2 * s, cy - 4.4 * s), (cx + 4.4 * s, cy - 4.4 * s),
                                    (cx + 4.4 * s, cy + 1.2 * s)], False), {"stroke": glyph}))
    elif view.state == VOICE_NONE:
        # A question mark: nothing was opened, choose a name.
        glyph = Stroke(2.0 * s, Solid(argb(GLYPH_COLOUR, 1.0)))
        ops.append(Op("arc", (cx, cy - 2.6 * s, 3.4 * s, 180.0, 270.0, glyph)))
        ops.append(Op("polyline", ([(cx, cy + 0.8 * s), (cx, cy + 2.4 * s)], False), {"stroke": glyph}))
        ops.append(Op("ellipse", (cx, cy + 5.4 * s, 1.4 * s, 1.4 * s), {"fill": Solid(argb(GLYPH_COLOUR, 1.0))}))
    elif view.state == ERROR:
        ops.append(Op("polyline", ([(cx, cy - 6.0 * s), (cx, cy + 1.5 * s)], False),
                      {"stroke": Stroke(2.6 * s, Solid(argb(GLYPH_COLOUR, 1.0)))}))
        ops.append(Op("ellipse", (cx, cy + 5.2 * s, 1.5 * s, 1.5 * s), {"fill": Solid(argb(GLYPH_COLOUR, 1.0))}))
    return ops


def _voice_rings(cx: float, cy: float, s: float, colour: int, t: float, level: float, still: bool) -> list[Op]:
    ops: list[Op] = []
    if still:
        for r, alpha in ((16.0, 0.75), (21.0, 0.4)):
            ops.append(Op("ellipse", (cx, cy, r * s, r * s), {"stroke": Stroke(1.4 * s, Solid(argb(colour, alpha)))}))
        return ops
    # Sonar rings: expand outwards, faster and brighter with the voice.
    for index in range(3):
        phase = (t * (0.55 + 0.6 * level) + index / 3.0) % 1.0
        r = (13.0 + phase * (9.0 + 8.0 * level)) * s
        alpha = (1.0 - phase) * (0.25 + 0.6 * level)
        ops.append(Op("ellipse", (cx, cy, r, r), {"stroke": Stroke(1.3 * s, Solid(argb(colour, alpha)))}))
    # Holographic ribbon: a closed curve whose ripple follows the voice.
    points = []
    for step in range(28):
        theta = 2 * math.pi * step / 28
        ripple = level * 5.0 * (0.6 * math.sin(3 * theta + 2.3 * t) + 0.4 * math.sin(5 * theta - 3.1 * t))
        r = (17.0 + 0.8 * math.sin(2 * theta + 1.1 * t) + ripple) * s
        points.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))
    ribbon = Linear(cx - 20 * s, cy - 20 * s, cx + 20 * s, cy + 20 * s, argb(colour, 0.95),
                    argb(HOLO_VIOLET, 0.95))
    ops.append(Op("polyline", (points, True), {"stroke": Stroke(1.8 * s, ribbon), "smooth": True}))
    return ops


def _diamond(cx: float, cy: float, r: float, angle_deg: float, stroke: Stroke) -> Op:
    points = []
    for corner in range(4):
        a = math.radians(angle_deg + 90.0 * corner)
        points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return Op("polyline", (points, True), {"stroke": stroke})


def _level_bar(cx: float, cy: float, s: float, colour: int, level: float) -> list[Op]:
    x, y, w, h = cx - 15 * s, cy + 23.5 * s, 30 * s, 3 * s
    ops = [Op("round_rect", (x, y, w, h, h / 2), {"fill": Solid(argb(0xFFFFFF, 0.18))})]
    if level > 0.0:
        ops.append(Op("round_rect", (x, y, max(h, w * level), h, h / 2), {"fill": Solid(argb(colour, 1.0))}))
    return ops


# ---------------------------------------------------------------- GDI+ renderer


class Renderer:
    """Lays out and paints frames with GDI+; ``close`` frees its fonts and GDI+ itself."""

    def __init__(self, gdip: Gdiplus | None = None) -> None:
        self.gdip = gdip if gdip is not None else Gdiplus()
        self._owns = gdip is None
        g = self.gdip
        self._words_family = g.font_family(WORDS_FONTS)
        self._label_family = g.font_family(LABEL_FONTS)
        self._format = g.string_format()
        self._fonts: dict[float, tuple[object, object]] = {}
        # A 1 x 1 bitmap whose graphics measures text between frames.
        self._scratch = (ctypes.c_uint32 * 1)()
        self._measure_image = g.create("GdipCreateBitmapFromScan0", 1, 1, 4, PIXEL_FORMAT_32BPP_PARGB,
                                       ctypes.cast(self._scratch, ctypes.c_void_p))
        self._measure_graphics = g.create("GdipGetImageGraphicsContext", self._measure_image)
        g.call("GdipSetTextRenderingHint", self._measure_graphics, TEXT_RENDERING_ANTIALIAS_GRIDFIT)
        self._measure = Canvas(g, self._measure_graphics)
        self._widths: dict[tuple[str, float, str], float] = {}

    def fonts(self, scale: float) -> tuple[object, object]:
        key = round(scale, 3)
        if key not in self._fonts:
            label = self.gdip.font(self._label_family, METRICS.label_size * scale, bold=True)
            words = self.gdip.font(self._words_family, METRICS.words_size * scale, bold=False)
            self._fonts[key] = (label, words)
        return self._fonts[key]

    def _measurer(self, scale: float, role: str) -> Callable[[str], float]:
        label, words = self.fonts(scale)
        font = label if role == "label" else words

        def measure(text: str) -> float:
            key = (role, round(scale, 3), text)
            if key not in self._widths:
                if len(self._widths) > 4096:
                    self._widths.clear()
                self._widths[key] = self._measure.measure(text, font, self._format)  # type: ignore[arg-type]
            return self._widths[key]
        return measure

    def layout(self, view: View, scale: float, min_content: float = 0.0, max_width: float | None = None) -> Layout:
        return layout(view, scale, self._measurer(scale, "label"), self._measurer(scale, "words"), min_content,
                      max_width)

    def paint(self, canvas: Canvas, ops: Sequence[Op], scale: float) -> None:
        label, words = self.fonts(scale)
        for op in ops:
            if op.name == "text":
                x, y, text, role, colour = op.args
                font = label if role == "label" else words
                canvas.text(x, y, text, font, self._format, colour)  # type: ignore[arg-type]
            else:
                getattr(canvas, op.name)(*op.args, **op.kwargs)

    def draw(self, bits: int, stride: int, lay: Layout, view: View) -> None:
        """Clear and paint one frame into top-down PARGB memory at ``bits`` (``lay.width`` x ``lay.height``)."""
        with self.gdip.bitmap(bits, lay.width, lay.height, stride) as image, self.gdip.canvas(image) as canvas:
            canvas.clear(0)
            self.paint(canvas, scene(view, lay), lay.scale)

    def close(self) -> None:
        g = self.gdip
        for label, words in self._fonts.values():
            g.delete_font(label)  # type: ignore[arg-type]
            g.delete_font(words)  # type: ignore[arg-type]
        self._fonts.clear()
        g.delete("GdipDeleteGraphics", self._measure_graphics)
        g.delete("GdipDisposeImage", self._measure_image)
        g.delete_string_format(self._format)
        g.delete_font_family(self._words_family)
        g.delete_font_family(self._label_family)
        if self._owns:
            g.close()

