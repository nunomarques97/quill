# Quill

Push-to-talk dictation for Windows 11, in the spirit of Wispr Flow.

Put the cursor in any window, hold a key, speak European Portuguese (with English terms mixed in) and clean, punctuated text is typed where the cursor is, without filler words or repetitions. Quill adapts to its user over time: personal vocabulary, learned corrections, writing style and the active application.

## Status

Early stage. The first milestone is an engine benchmark on real recordings of the user's voice, to choose the speech-to-text engine and the monthly cost. See [docs/PRODUCT.md](docs/PRODUCT.md).

The benchmark harness lives in [bench/](bench/README.md). Its results, the engine comparison and the pending engine/cost decision (in European Portuguese) are in [docs/research/ENGINES.md](docs/research/ENGINES.md); the aggregate numbers are in [docs/research/engines-summary.json](docs/research/engines-summary.json).

## The `quill` package

The dictation app is being built in [quill/](quill/) (Python 3.12, standard library and ctypes Win32 only; the speech engine's packages are imported later, inside the `.venv`). So far:

- `quill/config.py`: settings. The committed [quill.example.toml](quill.example.toml) holds invented defaults; your own values go in `local/quill.toml` (ignored by Git), which is read over the example table by table. It sets the push-to-talk triggers (dictation, command, send to Claude Code; mouse buttons `xbutton1`, `xbutton2`, `middle` and keys `f1` to `f24`, `right_ctrl` and a few others), `min_hold_ms`, click-to-focus, the microphone name, the indicator position, the active-window profiles, the local Ollama model and the cleanup mode. Unknown field names, an input bound to two triggers, a non-loopback Ollama address and personal-data paths outside `local/` are rejected; errors name the field, never its value. Check a file with:

  ```
  py -3.12 -m quill.config --check local/quill.toml
  ```

- `quill/inject.py`: types text into the window captured when the trigger went down, as Unicode `SendInput` events tagged with Quill's marker. It never uses or writes the clipboard (a change made by another program while typing is reported), drops control characters, never produces a bare Enter from text (a line break becomes a space or Shift+Enter), waits for Ctrl/Alt/Windows to be released and checks before every burst that the target still exists, belongs to the same process, responds and is in the foreground. Elevated targets are refused. A plain Enter is only sent by the separate `press_enter(target)` call, used by the send-to-Claude-Code trigger.
- `quill/clipboard.py`: clipboard snapshots (all formats) with compare and restore.
- `quill/triggers.py`: the push-to-talk state machine (no Win32). A hold of a bound input emits `start` at once, `confirm` after `min_hold_ms`, then `stop` on release; a shorter hold, another key or button pressed while a keyboard trigger is held (a combo) or the hooks stopping emit `cancel`. Injected events (flagged by Windows or carrying Quill's marker), key auto-repeat, a release without a press and a second trigger while one is held are ignored. A missed release is recovered (live key state for inputs that pass through, a second press, or a 120 s maximum hold), so it never stays stuck. Bound mouse buttons and bound non-modifier keys such as F13 to F24 are swallowed while Quill runs (Back/Forward never fire); Right Ctrl, Right Shift and Right Alt pass through so shortcuts keep working.
- `quill/hooks.py`: the `WH_KEYBOARD_LL`/`WH_MOUSE_LL` callbacks only classify the event, queue it and return the suppression decision; a worker thread runs the state machine and the application's handler. Decisions and logs name the trigger input only, never other keys.
- `quill/focus.py`: click-to-focus. When a dictation or send-to-Claude hold is confirmed, it sends one primary-button click, marked as Quill's, at the current pointer position (it never moves the pointer), waits for the window under the pointer to take the foreground and captures it as the target. The command trigger never clicks, so the selection is kept. No click is sent when `click_to_focus` is off, over Quill's own windows, or while a modifier key or another mouse button is down. A trigger on `right_ctrl`, `right_shift` or `right_alt` passes through to Windows, so it never clicks (a click would become a shortcut such as Ctrl+click): it types into the window that already has the focus.
- `quill/win32.py`: the ctypes layer. It has no call that moves the keyboard focus.

Tests: `py -3.12 -m unittest discover -s quill/tests -t .`. They use a fake Win32 layer and a fake hook installer and never create windows, install hooks, send input, open the microphone or touch the real clipboard; the real layer and the real hook installer are disabled while they run.

### Manual typing self-test

```
py -3.12 -m quill.selftest.typing --allow-desktop-input [--targets edit,edge-textarea,edge-contenteditable,console,vscode]
```

It opens its own windows (a Win32 edit box, Edge app windows with a temporary profile, a console, VS Code with a temporary profile), takes the focus and types an invented corpus into each, then compares what arrived and checks that the clipboard is unchanged. Run it only when you choose, and do not touch the keyboard or mouse until it ends (about a minute). It writes an aggregate result, with counts of lost, extra and changed characters and never the typed text, to `local/selftest/typing-<UTC time>.json`. Without `--allow-desktop-input` it exits with code 2 and opens nothing. It is never part of automated tests or checks.

### Manual trigger self-test

```
py -3.12 -m quill.selftest.triggers --allow-desktop-input [--seconds 120] [--click-to-focus]
```

It installs the real hooks with the triggers from `local/quill.toml` (or the example) and prints every decision (action, trigger input name, signal and reason, for example `dictation xbutton1 cancel short_hold`) while you press, hold, tap and combine the triggers. While it runs the bound buttons and keys do not do their normal action. With `--click-to-focus` a confirmed dictation or send-to-Claude hold also clicks at the pointer, as the app does; nothing is typed. It ends after `--seconds` or with Ctrl+C and writes counts per action, reason and trigger input (never other keys, positions or window names) to `local/selftest/triggers-<UTC time>.json`. Without `--allow-desktop-input` it exits with code 2 and installs nothing. It is never part of automated tests or checks.

## Privacy

Audio, transcripts, personal vocabulary, learned corrections and API keys stay on the machine and are never committed. Audio only leaves the PC for the engine the user has explicitly chosen.

## License

Not yet chosen.
