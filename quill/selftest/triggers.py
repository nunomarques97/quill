"""Manual trigger self-test: install the real hooks and log every trigger decision.

Usage: py -3.12 -m quill.selftest.triggers --allow-desktop-input [--seconds 120] [--click-to-focus]
       [--config local/quill.toml]

Manual only: it installs the low-level keyboard and mouse hooks, so while it
runs the bound mouse buttons and keys are swallowed (Back/Forward do nothing)
exactly as in the app. It is never part of automated tests or task checks.
Without ``--allow-desktop-input`` it installs nothing: it only validates the
settings, prints the bound trigger inputs and how to run the real test, and
exits 0 (2 when the settings are invalid), so task checks can run it safely.

Press, hold, tap and combine the configured triggers while it runs; each
decision is printed with the action, the trigger input name and the reason
(for example ``dictation xbutton1 cancel short_hold``). Other keys are never
printed or recorded. With ``--click-to-focus`` a confirmed dictation or
send-to-Claude hold also clicks at the pointer, as the app does, and the
outcome is printed as a reason code; nothing is ever typed.

It ends after ``--seconds`` or with Ctrl+C and writes an aggregate result
(counts per action, reason and trigger input; no keys, positions or window
names) to ``local/selftest/triggers-<UTC time>.json``. Version 2 adds, per
trigger input, how long each physical press lasted (down to up, on the
events' own times, in buckets next to ``min_hold_ms``) and how late the hook
callbacks ran (event age: callback time minus the event's time field, p95,
maximum and buckets), so a short hold can be told apart from a late
callback. Exit codes: 0 no errors (or, without the flag, a passed dry
check), 1 a hook, machine or handler error, 2 usage or settings error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from quill.config import LOCAL_CONFIG, Config, ConfigError, load_config
from quill.focus import ClickToFocus
from quill.hooks import TriggerHooks, monotonic_ms, real_hooks
from quill.triggers import CONFIRM, KEY, Binding, InputEvent, Signal, bindings_from

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "local" / "selftest"
RESULT_VERSION = 2
SECONDS_RANGE = (5, 1800)

# (lower bound in ms, label): physical press durations and hook callback delays.
HOLD_BUCKETS = ((0, "under_100ms"), (100, "100_249ms"), (250, "250_499ms"), (500, "500_999ms"),
                (1000, "1000_2999ms"), (3000, "3000ms_plus"))
AGE_BUCKETS = ((0, "under_16ms"), (16, "16_49ms"), (50, "50_99ms"), (100, "100_249ms"), (250, "250ms_plus"))
UNKNOWN_AGE = "unknown"

REFUSAL = (
    "not running the desktop test: it installs low-level keyboard and mouse hooks, so the bound\n"
    "mouse buttons and keys stop doing their normal action while it runs.\n"
    "Pass --allow-desktop-input to run it."
)


def percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))]


def bucket(value_ms: float, buckets: tuple[tuple[int, str], ...]) -> str:
    """The label of the last bucket whose lower bound is at most ``value_ms``."""
    label = buckets[0][1]
    for low, name in buckets:
        if value_ms >= low:
            label = name
    return label


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _bucket_rows(counts: Counter[tuple[str, str]], buckets: tuple[tuple[int, str], ...]) -> list[dict[str, object]]:
    order = {name: index for index, (_, name) in enumerate(buckets)}
    keys = sorted(counts, key=lambda key: (key[0], order.get(key[1], len(order))))
    return [{"trigger": name, "bucket": label, "count": counts[(name, label)]} for name, label in keys]


class Tally:
    """Counts and prints trigger decisions; only trigger names, actions and reasons."""

    def __init__(self, clock_ms: Callable[[], float], focus: ClickToFocus | None = None,
                 out: Callable[[str], None] = print) -> None:
        self.clock_ms = clock_ms
        self.started_ms = clock_ms()
        self.focus = focus
        self.out = out
        self.signals: Counter[tuple[str, str, str]] = Counter()
        self.ignored: Counter[tuple[str, str]] = Counter()
        self.inputs: Counter[tuple[str, str]] = Counter()
        self.clicks: Counter[tuple[str, str]] = Counter()
        self.lag_ms: list[float] = []
        self.hold_ms: list[float] = []
        # Per trigger input: physical press durations and hook callback delays.
        self.pressed: dict[str, float] = {}
        self.holds: Counter[tuple[str, str]] = Counter()
        self.ages: dict[str, list[float]] = {}
        self.age_buckets: Counter[tuple[str, str]] = Counter()

    def _line(self, text: str) -> None:
        self.out(f"{(self.clock_ms() - self.started_ms) / 1000:8.2f} s  {text}")

    def on_signal(self, signal: Signal) -> None:
        self.signals[(signal.action, signal.kind, signal.reason)] += 1
        line = f"{signal.action:<11} {signal.trigger:<11} {signal.kind}"
        if signal.reason:
            line += f" {signal.reason}"
        if signal.kind not in ("start", "confirm"):
            line += f" (held {signal.held_ms / 1000:.2f} s)"
            self.hold_ms.append(signal.held_ms)
        if signal.kind == CONFIRM and self.focus is not None:
            result = self.focus.on_confirm(signal.action, signal.trigger)
            self.clicks[(signal.action, result.reason)] += 1
            line += f"; click-to-focus: {result.reason}"
        self._line(line)

    def on_ignore(self, reason: str, binding: Binding) -> None:
        self.ignored[(binding.name, reason)] += 1
        if reason != "repeat":  # auto-repeat would flood the console
            self._line(f"{binding.action:<11} {binding.name:<11} ignored {reason}")

    def on_input(self, event: InputEvent, binding: Binding, suppressed: bool) -> None:
        callback_ms = event.time_ms + (event.age_ms or 0.0)
        self.lag_ms.append(max(0.0, self.clock_ms() - callback_ms))
        state = "down" if event.down else "up"
        self.inputs[(binding.name, f"{state}_{'suppressed' if suppressed else 'passed'}")] += 1
        if event.injected:
            self.inputs[(binding.name, "injected")] += 1
        if event.is_injected:
            return  # the machine ignores them, so they never count as presses
        name = binding.name
        if event.age_ms is None:
            self.age_buckets[(name, UNKNOWN_AGE)] += 1
        else:
            self.ages.setdefault(name, []).append(event.age_ms)
            self.age_buckets[(name, bucket(event.age_ms, AGE_BUCKETS))] += 1
        if event.down:
            if not (binding.input.kind == KEY and name in self.pressed):  # a key's auto-repeat keeps the first down
                self.pressed[name] = event.time_ms
        else:
            down_ms = self.pressed.pop(name, None)
            if down_ms is not None:
                self.holds[(name, bucket(max(0.0, event.time_ms - down_ms), HOLD_BUCKETS))] += 1

    def per_trigger(self) -> list[dict[str, object]]:
        names = sorted({name for name, _ in self.holds} | {name for name, _ in self.age_buckets})
        rows = []
        for name in names:
            ages = self.ages.get(name, [])
            rows.append({
                "trigger": name,
                "presses": sum(count for (held, _), count in self.holds.items() if held == name),
                "event_age_ms_p95": _rounded(percentile(ages, 0.95)),
                "event_age_ms_max": _rounded(max(ages)) if ages else None,
                "event_age_unknown": self.age_buckets[(name, UNKNOWN_AGE)],
            })
        return rows


def aggregate(tally: Tally, *, seconds: float, click: bool, callback_errors: int, handler_errors: int,
              machine_errors: int, finished: datetime, min_hold_ms: int | None = None) -> dict[str, object]:
    """The result file: counts and durations only, never keys, positions or window names."""
    starts = sum(count for (_, kind, _), count in tally.signals.items() if kind == "start")
    ends = sum(count for (_, kind, _), count in tally.signals.items() if kind in ("stop", "cancel"))
    lag_p95 = percentile(tally.lag_ms, 0.95)
    ages = [age for values in tally.ages.values() for age in values]
    return {
        "selftest": "triggers",
        "version": RESULT_VERSION,
        "finished_utc": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": callback_errors == 0 and handler_errors == 0 and machine_errors == 0 and starts == ends,
        "duration_s": round(seconds, 1),
        "click_to_focus": click,
        "min_hold_ms": min_hold_ms,
        "totals": {
            "starts": starts,
            "ends": ends,
            "callback_errors": callback_errors,
            "handler_errors": handler_errors,
            "machine_errors": machine_errors,
            "worker_lag_ms_p95": None if lag_p95 is None else round(lag_p95, 1),
            "hold_s_max": round(max(tally.hold_ms) / 1000, 2) if tally.hold_ms else None,
            "event_age_ms_p95": _rounded(percentile(ages, 0.95)),
            "event_age_unknown": sum(count for (_, label), count in tally.age_buckets.items() if label == UNKNOWN_AGE),
        },
        "signals": [{"action": action, "signal": kind, "reason": reason, "count": count}
                    for (action, kind, reason), count in sorted(tally.signals.items())],
        "ignored": [{"trigger": name, "reason": reason, "count": count}
                    for (name, reason), count in sorted(tally.ignored.items())],
        "inputs": [{"trigger": name, "event": event, "count": count}
                   for (name, event), count in sorted(tally.inputs.items())],
        "clicks": [{"action": action, "reason": reason, "count": count}
                   for (action, reason), count in sorted(tally.clicks.items())],
        "per_trigger": tally.per_trigger(),
        "holds": _bucket_rows(tally.holds, HOLD_BUCKETS),
        "event_ages": _bucket_rows(tally.age_buckets, AGE_BUCKETS),
    }


def write_result(data: dict[str, object], folder: Path, finished: datetime) -> Path:
    """Write the aggregate under ``folder`` (inside the ignored local/ folder)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"triggers-{finished.strftime('%Y%m%dT%H%M%SZ')}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)
    return path


def _label(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def run(config: Config, seconds: float, click: bool, results_dir: Path = RESULTS_DIR, *,
        api: object | None = None, factory: Callable[[], object] = real_hooks,
        wait: Callable[[float], None] = time.sleep, clock_ms: Callable[[], float] = monotonic_ms,
        out: Callable[[str], None] = print) -> int:
    if api is None:
        from quill.win32 import User32

        api = User32()
    focus = ClickToFocus(api, config.click_to_focus) if click else None
    tally = Tally(clock_ms, focus, out)
    service = TriggerHooks.from_config(config, tally.on_signal, factory=factory, key_state=api.key_down,
                                       clock_ms=clock_ms, on_ignore=tally.on_ignore, on_input=tally.on_input)
    bound = ", ".join(f"{trigger.action}: {'/'.join(item.name for item in trigger.inputs)}"
                      for trigger in config.triggers if trigger.enabled)
    out(f"Quill trigger self-test for {seconds:.0f} s (Ctrl+C ends it early). Triggers: {bound}. "
        f"Minimum hold {config.min_hold_ms} ms; click-to-focus {'on' if click else 'off'}.")
    service.start()
    hook_thread, worker = service.hooks, service.worker
    begun = clock_ms()
    try:
        wait(seconds)
    except KeyboardInterrupt:
        out("Stopped early.")
    finally:
        service.stop()
        finished = datetime.now(timezone.utc)
        data = aggregate(tally, seconds=(clock_ms() - begun) / 1000, click=click,
                         callback_errors=hook_thread.callback_errors, handler_errors=worker.handler_errors,
                         machine_errors=worker.machine_errors, finished=finished, min_hold_ms=config.min_hold_ms)
        path = write_result(data, results_dir, finished)
        for row in data["per_trigger"]:
            held = ", ".join(f"{item['bucket']} {item['count']}" for item in data["holds"]
                             if item["trigger"] == row["trigger"])
            out(f"{row['trigger']}: {row['presses']} presses ({held or 'none completed'}); "
                f"event age p95 {row['event_age_ms_p95']} ms, max {row['event_age_ms_max']} ms, "
                f"unknown {row['event_age_unknown']}")
        out(f"Aggregate result (trigger names and counts only): {_label(path)}")
    return 0 if data["passed"] else 1


def dry_check(path: Path, out: Callable[[str], None] = print) -> int:
    """Without the flag: validate the settings and list the bound inputs; no hooks, no input, no file."""
    try:
        config = load_config(path)
        bindings = bindings_from(config)
    except (ConfigError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    bound = ", ".join(f"{trigger.action}: {'/'.join(item.name for item in trigger.inputs)}"
                      for trigger in config.triggers if trigger.enabled)
    out(f"Dry check passed (nothing installed): {len(bindings)} trigger inputs bound ({bound or 'none'}); "
        f"minimum hold {config.min_hold_ms} ms.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.selftest.triggers", description=__doc__.splitlines()[0])
    parser.add_argument("--allow-desktop-input", action="store_true",
                        help="required: allow the test to install keyboard and mouse hooks")
    parser.add_argument("--seconds", type=float, default=120.0, help="how long to listen (default 120)")
    parser.add_argument("--click-to-focus", action="store_true",
                        help="also click at the pointer on a confirmed dictation or send-to-Claude hold")
    parser.add_argument("--config", type=Path, default=LOCAL_CONFIG, help="settings file (default local/quill.toml)")
    args = parser.parse_args(argv)
    if not args.allow_desktop_input:
        print(REFUSAL, file=sys.stderr)
        return dry_check(args.config)
    if sys.platform != "win32":
        print("this self-test needs Windows", file=sys.stderr)
        return 2
    low, high = SECONDS_RANGE
    if not low <= args.seconds <= high:
        parser.error(f"--seconds must be from {low} to {high}")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    return run(config, args.seconds, args.click_to_focus)


if __name__ == "__main__":
    sys.exit(main())
