"""Indicator tests: layout, placement, contrast, scene, and the window with a fake Win32 layer.

The window tests drive ``Indicator`` and ``Overlay`` with ``FakeLayer`` and
``FakeRenderer``: no window is created and nothing is drawn on screen. The
GDI+ tests render into memory only and check that the process owns no window.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import re
import sys
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from quill.indicator import __main__ as cli
from quill.indicator import render, window
from quill.indicator.render import (COMMAND, ERROR, LABELS, LISTENING, LOADING, METRICS, REVIEWING, SENT, STATES,
                                    TRANSCRIBING, View)
from quill.indicator.window import (EX_STYLE, HTTRANSPARENT, MA_NOACTIVATE, SW_HIDE, SW_SHOWNOACTIVATE,
                                    WM_DESTROY, WM_MOUSEACTIVATE, WM_NCHITTEST, WM_TIMER, WS_EX_LAYERED,
                                    WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP,
                                    Indicator, IndicatorError, Overlay, Surface)

FOCUS_CALLS = {"SetForegroundWindow", "SetFocus", "SetActiveWindow", "BringWindowToTop", "SwitchToThisWindow",
               "AttachThreadInput", "SetWindowPos", "SendInput", "SetCursorPos"}


def measure(text: str) -> float:
    return 10.0 * len(text)


# ---------------------------------------------------------------- fakes


class FakeLayer:
    """In-memory stand-in for ``OverlayWin32`` that counts GDI handles and windows."""

    def __init__(self, dpi: int = 96, work: tuple[int, int, int, int] = (0, 0, 1920, 1040),
                 cursor: tuple[int, int] = (400, 300), reduced: bool = False, fail_create: bool = False) -> None:
        self.dpi, self.work, self.cursor, self.reduced = dpi, work, cursor, reduced
        self.fail_create = fail_create
        self.calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.handler = None
        self.messages: list[tuple[int, int]] = []
        self.cond = threading.Condition()
        self.quit = False
        self.ids = itertools.count(100)
        self.live: Counter[str] = Counter()
        self.ui_thread = 0

    def _call(self, name: str, *args: object) -> None:
        self.calls.append((name, threading.get_ident(), args))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def init_thread(self) -> bool:
        self._call("init_thread")
        self.ui_thread = threading.get_ident()
        return True

    def register_class(self, handler: object) -> None:
        self._call("register_class")
        self.handler = handler
        self.live["class"] += 1

    def unregister_class(self) -> None:
        self._call("unregister_class")
        if self.live["class"]:
            self.live["class"] -= 1

    def create_window(self, ex_style: int, style: int) -> int:
        self._call("create_window", ex_style, style)
        if self.fail_create:
            raise IndicatorError("fake create failure")
        self.live["window"] += 1
        return next(self.ids)

    def destroy_window(self, hwnd: int) -> None:
        self._call("destroy_window", hwnd)
        self.live["window"] -= 1
        self.handler(hwnd, WM_DESTROY, 0, 0)

    def show_window(self, hwnd: int, command: int) -> None:
        self._call("show_window", hwnd, command)

    def set_timer(self, hwnd: int, timer_id: int, ms: int) -> None:
        self._call("set_timer", hwnd, timer_id, ms)

    def kill_timer(self, hwnd: int, timer_id: int) -> None:
        self._call("kill_timer", hwnd, timer_id)

    def post(self, hwnd: int, msg: int) -> bool:
        with self.cond:
            self.messages.append((hwnd, msg))
            self.cond.notify_all()
        return True

    def post_quit(self) -> None:
        self._call("post_quit")
        with self.cond:
            self.quit = True
            self.cond.notify_all()

    def run_loop(self) -> None:
        while True:
            with self.cond:
                while not self.messages and not self.quit:
                    self.cond.wait()
                if self.quit and not self.messages:
                    return
                hwnd, msg = self.messages.pop(0)
            self.handler(hwnd, msg, 0, 0)

    def cursor_pos(self) -> tuple[int, int] | None:
        self._call("cursor_pos")
        return self.cursor

    def monitor(self, x: int, y: int) -> tuple[tuple[int, int, int, int], int]:
        return self.work, self.dpi

    def reduced_motion(self) -> bool:
        return self.reduced

    def create_surface(self, width: int, height: int) -> Surface:
        self._call("create_surface", width, height)
        self.live["dc"] += 1
        self.live["bitmap"] += 1
        return Surface(next(self.ids), next(self.ids), next(self.ids), 0, width, height)

    def delete_surface(self, surface: Surface) -> None:
        self._call("delete_surface", surface.width, surface.height)
        self.live["dc"] -= 1
        self.live["bitmap"] -= 1

    def update_layered(self, hwnd: int, surface: Surface, x: int, y: int, width: int, height: int) -> bool:
        self._call("update_layered", x, y, width, height)
        assert width <= surface.width and height <= surface.height
        return True


class FakeRenderer:
    """Uses the real pure layout with a fake measure; records every drawn view and its thread."""

    instances: list[FakeRenderer] = []

    def __init__(self) -> None:
        self.drawn: list[tuple[int, View, render.Layout]] = []
        self.closed = 0
        FakeRenderer.instances.append(self)

    def layout(self, view: View, scale: float, min_content: float = 0.0,
               max_width: float | None = None) -> render.Layout:
        return render.layout(view, scale, measure, measure, min_content, max_width)

    def draw(self, bits: int, stride: int, lay: render.Layout, view: View) -> None:
        self.drawn.append((threading.get_ident(), view, lay))

    def close(self) -> None:
        self.closed += 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def wait_for(condition: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():  # type: ignore[operator]
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.002)


class IndicatorCase(unittest.TestCase):
    def make(self, layer: FakeLayer | None = None, position: str = "pointer",
             clock: FakeClock | None = None) -> tuple[Indicator, FakeLayer]:
        layer = layer or FakeLayer()
        FakeRenderer.instances.clear()
        indicator = Indicator(position, layer_factory=lambda: layer, renderer_factory=FakeRenderer,
                              clock=clock or FakeClock())
        self.addCleanup(indicator.stop)
        return indicator, layer

    def drawn(self) -> list[tuple[int, View, render.Layout]]:
        return [item for renderer in FakeRenderer.instances for item in renderer.drawn]


# ---------------------------------------------------------------- window


class NoFocusTest(IndicatorCase):
    def test_window_styles_and_show_never_activate(self) -> None:
        indicator, layer = self.make()
        indicator.start()
        indicator.show(LISTENING, "olá")
        wait_for(lambda: "update_layered" in layer.names())
        create = next(args for name, _, args in layer.calls if name == "create_window")
        ex_style, style = create
        for flag in (WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST):
            self.assertTrue(ex_style & flag, hex(flag))
        self.assertEqual(ex_style, EX_STYLE)
        self.assertEqual(style, WS_POPUP)
        shows = [args[1] for name, _, args in layer.calls if name == "show_window"]
        self.assertEqual(shows, [SW_SHOWNOACTIVATE])
        # The first frame is painted before the window is shown.
        self.assertLess(layer.names().index("update_layered"), layer.names().index("show_window"))
        self.assertFalse(FOCUS_CALLS & set(layer.names()))

    def test_window_procedure_refuses_activation_and_clicks(self) -> None:
        overlay = Overlay(FakeLayer(), FakeRenderer())
        self.assertEqual(overlay.handle(1, WM_MOUSEACTIVATE, 0, 0), MA_NOACTIVATE)
        self.assertEqual(overlay.handle(1, WM_NCHITTEST, 0, 0), HTTRANSPARENT)
        self.assertIsNone(overlay.handle(1, 0x0006, 0, 0))  # WM_ACTIVATE goes to DefWindowProc

    def test_real_layer_binds_no_focus_or_activation_call(self) -> None:
        bound = {name for names in window.WIN32_CALLS.values() for name in names}
        self.assertFalse(FOCUS_CALLS & bound)
        self.assertIn("UpdateLayeredWindow", bound)
        self.assertIn("SetThreadDpiAwarenessContext", bound)
        source = Path(window.__file__).read_text("utf-8")
        used = set(re.findall(r"self\._(?:user32|gdi32|kernel32|shcore)\.(\w+)", source))
        self.assertTrue(used)
        self.assertLessEqual(used, bound)
        for name in FOCUS_CALLS:
            self.assertNotIn(name + "(", source)

    def test_ui_thread_is_per_monitor_dpi_aware_before_the_window(self) -> None:
        indicator, layer = self.make()
        indicator.start()
        names = layer.names()
        self.assertLess(names.index("init_thread"), names.index("create_window"))
        self.assertEqual(window.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, -4)

    def test_frames_follow_the_monitor_dpi(self) -> None:
        indicator, layer = self.make(FakeLayer(dpi=144))
        indicator.start()
        indicator.show(LISTENING)
        wait_for(lambda: self.drawn())
        self.assertEqual(self.drawn()[0][2].scale, 1.5)

    def test_the_real_layer_is_forbidden_in_tests(self) -> None:
        with self.assertRaises(AssertionError):
            window.OverlayWin32()


class OrderTest(IndicatorCase):
    def test_commands_from_many_threads_apply_in_order_on_the_ui_thread(self) -> None:
        indicator, layer = self.make()
        indicator.start()
        indicator.show(LISTENING, "start")
        wait_for(lambda: len(self.drawn()) == 1)
        per_thread = 60

        def speak(name: str) -> None:
            for index in range(per_thread):
                indicator.set_text(f"{name} {index}")

        threads = [threading.Thread(target=speak, args=(f"t{n}",)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wait_for(lambda: len(self.drawn()) == 1 + 4 * per_thread)
        drawn = self.drawn()
        self.assertEqual({ident for ident, _, _ in drawn}, {layer.ui_thread})
        self.assertNotEqual(layer.ui_thread, threading.get_ident())
        seen: dict[str, list[int]] = {}
        for _, view, _ in drawn[1:]:
            name, index = view.text.split()
            seen.setdefault(name, []).append(int(index))
        self.assertEqual(seen, {f"t{n}": list(range(per_thread)) for n in range(4)})

    def test_state_sequence_is_applied_in_call_order(self) -> None:
        indicator, _ = self.make()
        indicator.start()
        sequence = [LOADING, LISTENING, TRANSCRIBING, SENT, COMMAND, ERROR]
        for state in sequence:
            indicator.show(state, state)
        wait_for(lambda: len(self.drawn()) == len(sequence))
        self.assertEqual([view.state for _, view, _ in self.drawn()], sequence)

    def test_commands_before_start_are_applied_after_start(self) -> None:
        indicator, _ = self.make()
        indicator.show(LOADING)
        indicator.start()
        wait_for(lambda: self.drawn())
        self.assertEqual(self.drawn()[0][1].state, LOADING)

    def test_unknown_state_is_rejected(self) -> None:
        indicator, _ = self.make()
        with self.assertRaises(ValueError):
            indicator.show("dancing")


class LifecycleTest(IndicatorCase):
    def test_show_hide_show_and_restart_leak_no_handles(self) -> None:
        layer = FakeLayer()
        indicator, _ = self.make(layer)
        for _ in range(2):
            indicator.start()
            indicator.show(LISTENING, "um")
            indicator.hide()
            indicator.show(COMMAND, "dois")
            wait_for(lambda: layer.names().count("update_layered") >= 1 and not indicator._commands)
            layer.dpi = 144 if layer.dpi == 96 else 96  # a new scale recreates the surface
            indicator.show(ERROR, "três")
            wait_for(lambda: not indicator._commands)
            self.assertLessEqual(layer.live["dc"], 1)
            self.assertLessEqual(layer.live["bitmap"], 1)
            indicator.stop()
            self.assertEqual(+layer.live, Counter(), layer.live)
            layer.quit = False
        self.assertEqual([r.closed for r in FakeRenderer.instances], [1, 1])
        self.assertEqual(layer.names().count("create_surface"), layer.names().count("delete_surface"))
        self.assertGreaterEqual(layer.names().count("create_surface"), 2)
        self.assertEqual(layer.names().count("create_window"), 2)
        self.assertEqual(layer.names().count("destroy_window"), 2)

    def test_hide_kills_the_timer_and_hides_without_activation(self) -> None:
        indicator, layer = self.make()
        indicator.start()
        indicator.show(LISTENING)
        indicator.hide()
        wait_for(lambda: SW_HIDE in [args[1] for name, _, args in layer.calls if name == "show_window"])
        names = layer.names()
        self.assertIn("kill_timer", names)
        self.assertLess(names.index("set_timer"), names.index("kill_timer"))

    def test_hide_after_delay(self) -> None:
        clock = FakeClock()
        indicator, layer = self.make(clock=clock)
        indicator.start()
        indicator.show(SENT, "feito", hide_after_s=1.5)
        wait_for(lambda: self.drawn())
        clock.now += 1.0
        layer.post(indicator.overlay.hwnd, WM_TIMER)
        wait_for(lambda: len(self.drawn()) == 2)
        self.assertTrue(indicator.overlay.visible)
        clock.now += 1.0
        layer.post(indicator.overlay.hwnd, WM_TIMER)
        wait_for(lambda: not indicator.overlay.visible)
        self.assertEqual(len(self.drawn()), 2)

    def test_start_failure_releases_everything(self) -> None:
        layer = FakeLayer(fail_create=True)
        indicator, _ = self.make(layer)
        with self.assertRaises(IndicatorError):
            indicator.start()
        self.assertEqual(+layer.live, Counter())
        self.assertEqual(FakeRenderer.instances[0].closed, 1)
        self.assertFalse(indicator.running)

    def test_stop_without_start_and_twice(self) -> None:
        indicator, _ = self.make()
        indicator.stop()
        indicator.start()
        indicator.stop()
        indicator.stop()
        self.assertFalse(indicator.running)

    def test_close_message_destroys_then_quits(self) -> None:
        indicator, layer = self.make()
        indicator.start()
        indicator.stop()
        names = layer.names()
        self.assertLess(names.index("destroy_window"), names.index("post_quit"))


class MotionTest(IndicatorCase):
    def test_reduced_motion_uses_a_slower_timer_and_static_frames(self) -> None:
        indicator, layer = self.make(FakeLayer(reduced=True))
        indicator.start()
        indicator.show(LISTENING)
        wait_for(lambda: self.drawn())
        timers = [args[2] for name, _, args in layer.calls if name == "set_timer"]
        self.assertEqual(timers, [window.REDUCED_FRAME_MS])
        self.assertTrue(self.drawn()[0][1].reduced_motion)

    def test_level_is_smoothed_between_frames(self) -> None:
        self.assertEqual(window.smooth_level(0.0, 1.0, 0.0), 0.0)
        rise = window.smooth_level(0.0, 1.0, 0.033)
        fall = window.smooth_level(1.0, 0.0, 0.033)
        self.assertGreater(rise, 1.0 - fall)  # fast attack, slower release
        self.assertLessEqual(window.smooth_level(0.5, 7.0, 10.0), 1.0)

    def test_pointer_anchor_is_taken_when_the_indicator_appears(self) -> None:
        layer = FakeLayer(cursor=(500, 500))
        indicator, _ = self.make(layer)
        indicator.start()
        indicator.show(LISTENING)
        wait_for(lambda: self.drawn())
        layer.cursor = (1500, 900)
        indicator.set_text("mais")
        wait_for(lambda: len(self.drawn()) == 2)
        updates = [args for name, _, args in layer.calls if name == "update_layered"]
        self.assertEqual(updates[0][:2], updates[1][:2])
        self.assertEqual(layer.names().count("cursor_pos"), 1)


# ---------------------------------------------------------------- pure layout


class FitWordsTest(unittest.TestCase):
    def test_short_text_is_one_line(self) -> None:
        self.assertEqual(render.fit_words("olá mundo", 200, measure), ["olá mundo"])

    def test_two_lines_fill_from_the_start(self) -> None:
        self.assertEqual(render.fit_words("aaaa bbbb cccc dddd", 100, measure), ["aaaa bbbb", "cccc dddd"])

    def test_overflow_keeps_the_newest_words_with_a_left_ellipsis(self) -> None:
        text = " ".join(f"w{n:02d}" for n in range(40))
        lines = render.fit_words(text, 160, measure)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(render.ELLIPSIS + " "))
        self.assertTrue(lines[1].endswith("w39"))
        self.assertTrue(all(measure(line) <= 160 for line in lines))
        shown = " ".join(lines).replace(render.ELLIPSIS, "").split()
        self.assertEqual(shown, text.split()[-len(shown):])

    def test_a_word_wider_than_the_line_keeps_its_end(self) -> None:
        lines = render.fit_words("x" * 50 + "END", 120, measure)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(render.ELLIPSIS))
        self.assertTrue(lines[0].endswith("END"))
        self.assertLessEqual(measure(lines[0]), 120)

    def test_empty_text(self) -> None:
        self.assertEqual(render.fit_words("   ", 100, measure), [])


class LayoutTest(unittest.TestCase):
    def test_placeholder_when_there_is_no_text(self) -> None:
        lay = render.layout(View(LISTENING), 1.0, measure, measure)
        self.assertTrue(lay.placeholder)
        self.assertEqual(lay.lines, (render.PLACEHOLDERS[LISTENING],))

    def test_long_text_is_two_lines_within_the_capsule(self) -> None:
        for scale in (1.0, 1.25, 1.5, 2.0):
            lay = render.layout(View(LISTENING, "palavra " * 200), scale, measure, measure)
            self.assertEqual(len(lay.lines), 2)
            x, y, w, h = lay.capsule
            self.assertLessEqual(w, METRICS.max_width * scale + 1)
            for line in lay.lines:
                self.assertLessEqual(lay.words_x + measure(line), x + w - METRICS.pad_right * scale + 1)
            self.assertLessEqual(lay.words_y + 2 * lay.line_height, y + h)
            self.assertLessEqual(x + w, lay.width)
            self.assertLessEqual(y + h, lay.height)

    def test_small_monitor_narrows_the_capsule(self) -> None:
        lay = render.layout(View(LISTENING, "palavra " * 50), 2.0, measure, measure, max_width=600)
        self.assertLessEqual(lay.capsule[2], 600)
        self.assertTrue(all(lay.words_x + measure(line) <= lay.capsule[0] + 600 for line in lay.lines))

    def test_content_floor_stops_the_capsule_shrinking(self) -> None:
        wide = render.layout(View(LISTENING, "uma frase bastante comprida"), 1.0, measure, measure)
        narrow = render.layout(View(LISTENING, "ok"), 1.0, measure, measure, wide.content_width)
        self.assertEqual(narrow.width, wide.width)


class PlaceTest(unittest.TestCase):
    SIZE = (420, 110)

    def test_right_of_the_pointer_and_vertically_centred(self) -> None:
        x, y = render.place(self.SIZE, (500, 400), (0, 0, 1920, 1040), 1.0)
        self.assertGreater(x, 500)
        self.assertEqual(y, 400 - 55)

    def test_flips_left_at_the_right_edge(self) -> None:
        x, _ = render.place(self.SIZE, (1800, 400), (0, 0, 1920, 1040), 1.0)
        self.assertLess(x + self.SIZE[0], 1800)

    def test_clamped_to_the_work_area_vertically(self) -> None:
        _, y = render.place(self.SIZE, (500, 1035), (0, 0, 1920, 1040), 1.0)
        self.assertEqual(y + self.SIZE[1], 1040)
        _, y = render.place(self.SIZE, (500, 2), (0, 0, 1920, 1040), 1.0)
        self.assertEqual(y, 0)

    def test_narrow_monitor_goes_below_or_above(self) -> None:
        work = (0, 0, 600, 800)
        x, y = render.place(self.SIZE, (300, 100), work, 1.0)
        self.assertGreater(y, 100)
        x, y = render.place(self.SIZE, (300, 780), work, 1.0)
        self.assertLess(y + self.SIZE[1], 780)

    def test_never_covers_the_hotspot_and_stays_on_the_monitor(self) -> None:
        monitors = [((0, 0, 1920, 1040), 1.0), ((-2560, -200, 0, 1240), 1.5), ((0, 0, 1000, 700), 2.0)]
        for work, scale in monitors:
            size = (int(self.SIZE[0] * scale), int(self.SIZE[1] * scale))
            left, top, right, bottom = work
            for px in range(left, right, 37):
                for py in range(top, bottom, 29):
                    x, y = render.place(size, (px, py), work, scale)
                    inside = x <= px < x + size[0] and y <= py < y + size[1]
                    self.assertFalse(inside, (work, px, py, x, y))
                    self.assertGreaterEqual(x, left)
                    self.assertGreaterEqual(y, top)
                    self.assertLessEqual(x + size[0], right)
                    self.assertLessEqual(y + size[1], bottom)

    def test_bottom_center(self) -> None:
        x, y = render.place(self.SIZE, (5, 5), (100, 0, 2020, 1040), 1.0, "bottom-center")
        self.assertEqual(x, 100 + (1920 - 420) // 2)
        self.assertLess(y + self.SIZE[1], 1040)
        self.assertGreater(y, 800)


class StateStyleTest(unittest.TestCase):
    def test_labels_are_european_portuguese(self) -> None:
        self.assertEqual(LABELS, {
            LOADING: "A carregar", LISTENING: "A ouvir", TRANSCRIBING: "A transcrever", COMMAND: "Modo comando",
            REVIEWING: "A rever o texto", SENT: "Enviado para o Claude Code", ERROR: "Erro",
        })

    def test_states_differ_by_label_colour_and_glyph(self) -> None:
        self.assertEqual(len(set(LABELS.values())), len(STATES))
        self.assertEqual(len(set(render.STATE_COLOURS.values())), len(STATES))
        lay = render.layout(View(LISTENING, "x"), 1.0, measure, measure)
        orbs = {state: repr([(op.name, op.args, op.kwargs) for op in render._orb(View(state, level=0.5), lay)
                             if op.name != "ellipse" or "fill" not in op.kwargs])
                for state in STATES}
        self.assertEqual(len(set(orbs.values())), len(STATES))

    def test_text_contrast_is_at_least_4_5_on_its_backdrop(self) -> None:
        for colour in list(render.STATE_COLOURS.values()) + [render.WORDS_COLOUR, render.SECONDARY_COLOUR]:
            self.assertGreaterEqual(render.min_contrast(colour), 4.5, hex(colour))

    def test_contrast_formula(self) -> None:
        self.assertAlmostEqual(render.contrast((1, 1, 1), (0, 0, 0)), 21.0, places=3)


class SceneTest(unittest.TestCase):
    def ops(self, view: View, scale: float = 1.0) -> list[render.Op]:
        return render.scene(view, render.layout(view, scale, measure, measure))

    def test_reduced_motion_is_static_over_time(self) -> None:
        for state in STATES:
            a = self.ops(View(state, "texto", 0.5, 0.1, True))
            b = self.ops(View(state, "texto", 0.5, 2.7, True))
            self.assertEqual(a, b, state)

    def test_reduced_motion_shows_the_level_as_a_bar_only(self) -> None:
        quiet = self.ops(View(LISTENING, "texto", 0.1, 1.0, True))
        loud = self.ops(View(LISTENING, "texto", 0.9, 1.0, True))
        changed = [(a, b) for a, b in zip(quiet, loud) if a != b]
        self.assertEqual(len(quiet), len(loud))
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0][0].name, "round_rect")
        self.assertLess(changed[0][0].args[2], changed[0][1].args[2])

    def test_animation_follows_time_and_level(self) -> None:
        self.assertNotEqual(self.ops(View(LISTENING, "t", 0.5, 0.1)), self.ops(View(LISTENING, "t", 0.5, 0.6)))
        self.assertNotEqual(self.ops(View(LISTENING, "t", 0.1, 0.5)), self.ops(View(LISTENING, "t", 0.9, 0.5)))
        self.assertEqual(self.ops(View(TRANSCRIBING, "t", 0.1, 0.5)), self.ops(View(TRANSCRIBING, "t", 0.9, 0.5)))

    def test_orb_never_reaches_the_text_column(self) -> None:
        for state, scale, level in itertools.product(STATES, (1.0, 1.5), (0.0, 1.0)):
            view = View(state, "texto", level, 0.37)
            lay = render.layout(view, scale, measure, measure)
            for op in render._orb(view, lay):
                right = _right_edge(op)
                self.assertLess(right, lay.words_x, (state, op.name))

    def test_text_is_drawn_after_the_glass_with_the_state_colour_label(self) -> None:
        ops = self.ops(View(ERROR, "Microfone não encontrado"))
        texts = [op for op in ops if op.name == "text"]
        self.assertEqual(texts[0].args[2:], ("Erro", "label", render.argb(render.STATE_COLOURS[ERROR], 1.0)))
        self.assertEqual(texts[1].args[2], "Microfone não encontrado")
        self.assertGreater(ops.index(texts[0]), 3)


def _right_edge(op: render.Op) -> float:
    if op.name == "ellipse":
        cx, _, rx, _ = op.args[:4]
        stroke = op.kwargs.get("stroke")
        return cx + rx + (stroke.width / 2 if stroke else 0)
    if op.name == "arc":
        cx, _, r = op.args[:3]
        return cx + r + op.args[5].width / 2
    if op.name == "polyline":
        points = op.args[0]
        return max(x for x, _ in points) + op.kwargs["stroke"].width / 2 + 5  # smoothing overshoot
    if op.name == "round_rect":
        return op.args[0] + op.args[2]
    raise AssertionError(op.name)


# ---------------------------------------------------------------- GDI+ offscreen


@unittest.skipUnless(sys.platform == "win32", "GDI+ needs Windows")
class OffscreenRenderTest(unittest.TestCase):
    def test_every_state_renders_offscreen_without_leaks_or_windows(self) -> None:
        renderer = render.Renderer()
        gdip = renderer.gdip
        try:
            for scale in (1.0, 1.5):
                renderer.layout(View(LISTENING), scale)  # fonts are created once per scale
            baseline = gdip.live
            for state, scale, reduced in itertools.product(STATES, (1.0, 1.5), (False, True)):
                view = View(state, "vamos rever o pull request", 0.6, 0.4, reduced)
                lay = renderer.layout(view, scale)
                frame = cli.FrameBuffer(lay)
                renderer.draw(frame.address, frame.width * 4, lay, view)
                pixels = list(frame.pixels)
                self.assertEqual(pixels[0] >> 24, 0)  # the corner is transparent
                cx, cy = (int(v) for v in lay.orb)
                self.assertGreater(pixels[cy * frame.width + cx] >> 24, 200)  # the orb core is opaque
                self.assertEqual(gdip.live, baseline, state)
        finally:
            renderer.close()
        self.assertEqual(gdip.live, 0)
        self.assertEqual(cli.own_windows(), 0)

    def test_png_is_valid(self) -> None:
        from quill.indicator.gdiplus import png_bytes

        data = png_bytes(2, 1, bytes([255, 0, 0, 255, 0, 0, 255, 128]))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data)
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))


# ---------------------------------------------------------------- command line


class CliTest(unittest.TestCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                code = cli.main(list(args))
            except SystemExit as exc:
                code = int(exc.code)
        return code, err.getvalue()

    def test_demo_refuses_without_the_flag(self) -> None:
        with mock.patch.object(cli, "run_demo") as demo, \
                mock.patch.object(window, "OverlayWin32") as layer, mock.patch.object(window, "Indicator") as made:
            code, err = self.run_main("--demo")
        self.assertEqual(code, 2)
        self.assertIn("--allow-desktop-window", err)
        demo.assert_not_called()
        layer.assert_not_called()
        made.assert_not_called()

    def test_demo_runs_only_with_the_flag(self) -> None:
        with mock.patch.object(cli, "run_demo", return_value=0) as demo:
            code, _ = self.run_main("--demo", "--allow-desktop-window", "--seconds", "10")
        self.assertEqual(code, 0)
        demo.assert_called_once_with("pointer", 10.0)

    def test_needs_one_mode(self) -> None:
        self.assertEqual(self.run_main()[0], 2)
        self.assertEqual(self.run_main("--demo", "--render-frames", "x")[0], 2)

    def test_frame_set_covers_the_required_evidence(self) -> None:
        stems = [stem for stem, _, _ in cli.frame_set()]
        for state in STATES:
            self.assertIn(f"state-{state}-100", stems)
            self.assertIn(f"state-{state}-150", stems)
        sequence = [view for stem, view, _ in cli.frame_set() if stem.startswith("listening-seq-")]
        self.assertEqual(len(sequence), 12)
        self.assertGreater(len({view.level for view in sequence}), 6)
        self.assertIn("long-text-100", stems)
        self.assertTrue(any(view.reduced_motion for _, view, _ in cli.frame_set()))


if __name__ == "__main__":
    unittest.main()
