# Quill

Push-to-talk dictation for Windows 11, in the spirit of Wispr Flow.

Put the cursor in any window, hold a key, speak European Portuguese (with English terms mixed in) and clean, punctuated text is typed where the cursor is, without filler words or repetitions. Quill adapts to its user over time: personal vocabulary, learned corrections, writing style and the active application.

## Status

Phase 2: the dictation app and command mode run locally (see "Running Quill" below); the Sponsor-run acceptance measurements are still open. Product brief: [docs/PRODUCT.md](docs/PRODUCT.md); Phase 2 results: [docs/research/FASE2.md](docs/research/FASE2.md).

The benchmark harness lives in [bench/](bench/README.md). Its results, the engine comparison and the pending engine/cost decision (in European Portuguese) are in [docs/research/ENGINES.md](docs/research/ENGINES.md); the aggregate numbers are in [docs/research/engines-summary.json](docs/research/engines-summary.json).

## Running Quill

Quill runs locally: Whisper through faster-whisper on the GPU, with the personal vocabulary as hints, and an optional local Ollama model for the cleanup. Audio never leaves the PC. It needs the ignored `.venv` (with `faster-whisper`) and the model files in the ignored `models/` folder; see [bench/README.md](bench/README.md) for how they were set up. Run every command from the repository folder with the `.venv` interpreter.

1. Put your own settings in `local/quill.toml` (ignored by Git), starting from [quill.example.toml](quill.example.toml): at least the `[audio] microphone` name (a prefix of the MME name; `py -3.12 -m bench.record --list-devices` lists them without opening any). Check the file with `py -3.12 -m quill.config --check local/quill.toml`.
2. Check readiness. It lists the model files, the `.venv` packages, whether the configured microphone exists (by name; it is not opened), the vocabulary, the Ollama model, the start-with-Windows entry and the learned corrections. It installs no hook, opens no window and does not open the microphone:

   ```
   .venv\Scripts\python -m quill --check
   ```

3. Start Quill. The model loads once (a few seconds; the indicator shows the loading state, and a press meanwhile records nothing):

   ```
   .venv\Scripts\python -m quill
   ```

   Ctrl+C in that console, or `.venv\Scripts\python -m quill --stop` from another one, ends it. Only one Quill runs at a time: a second start exits with code 3.

4. Optionally start Quill with Windows. These manual commands add or remove one value, `Quill`, under the current user's `Software\Microsoft\Windows\CurrentVersion\Run` key. The entry runs `.venv\Scripts\pythonw.exe` (no console window) with this folder's `quill\__main__.py`, both resolved when the command runs; run it again after moving the folder. Nothing else writes the registry.

   ```
   .venv\Scripts\python -m quill --install-startup
   .venv\Scripts\python -m quill --remove-startup
   ```

### Using it

- **Dictation** (default: hold mouse button 4, or F13, or Right Ctrl): the microphone starts at the press, so no word is lost. After `min_hold_ms` Quill clicks once at the pointer to focus the text field under it (not for Right Ctrl, which types where the focus already is). Speak; the words appear live in the indicator. On release the rest is transcribed, cleaned (fillers, repetitions, punctuation), corrected with the personal vocabulary and the learned corrections, shaped by the active window's profile and typed into the window captured at the press. It never presses Enter.
- **Send to Claude Code** (default: mouse button 5 or F15): the same, then one Enter, only when the text was typed completely and the target matches the `claude-code` profile. In any other window the text is typed without Enter and the indicator says so. By default that profile is Windows Terminal with "Claude Code" in the title, or VS Code with the `[Claude Code]` marker in the title. VS Code shows the marker only with this user setting, and only while the Claude Code view in the sidebar has the focus (set `"claudeCode.preferredLocation": "sidebar"` to open it there):

  ```json
  "window.title": "${dirty}${activeEditorShort}${separator}${rootName}${separator}${profileName}${separator}${appName} [${focusedView}]"
  ```

  A Claude Code editor tab or Claude Code in the integrated terminal cannot be told apart from a file or a shell, so there the text is typed without Enter.
- **Command** (default: F14): select text, hold the key, speak an instruction (for example "põe isto mais formal", "traduz para inglês") and release. The selection is copied (the clipboard is put back), rewritten by the local Ollama model and typed over the selection. The trigger never clicks, so the selection is kept; terminals are refused, and on any failure the selection stays as it was and the indicator says why. Any free key can be added, for example F8 as well as F14 (F14 suits a Stream Deck style button):

  ```toml
  [triggers.command]
  keys = ["f14", "f8"]
  ```

  A key bound to a trigger loses its normal action in every program while Quill runs (F8 is, for example, "next problem" in VS Code), and an input can belong to one trigger only.
- A tap shorter than `min_hold_ms`, or another key pressed while a keyboard trigger is held (for example Right Ctrl+C), cancels: nothing is clicked or typed. While Quill runs, the bound mouse buttons and F keys do not do their normal action.
- A new press while the previous text is still being finalized starts recording at once; texts are typed strictly in order, each one once, into its own target. When a target closed, the foreground window changed, the microphone or the engine failed, or Ollama is down (the cleanup falls back to the rules), the indicator shows the error in European Portuguese and the next dictation is unaffected.
- **Correcting**: select the corrected text of the last dictation and press the correction key (default F16), or just edit it by hand right after it was typed. A replacement seen in two dictations is applied automatically from then on. Review what was learned with `py -3.12 -m quill.review` (a reminder appears weekly in `--check`).
- **Indicator**: a non-activating, click-through overlay beside the pointer (or at the bottom center, `[indicator] position`), with the states *A carregar*, *A ouvir* with the live words and the voice level, *A transcrever*, *Enviado para o Claude Code*, *Modo comando* and *Erro* with its message. It never takes the focus.

Logs go to `local/logs/quill.log` (ignored, rotated at 1 MB): events, reason codes and timings per session, including the release-to-typed time, never spoken or typed text.

Sponsor-facing usage steps in European Portuguese: [docs/USAR.md](docs/USAR.md).

## The `quill` package

The dictation app lives in [quill/](quill/) (Python 3.12, standard library and ctypes Win32 only; the speech engine's packages are imported later, inside the `.venv`). `quill/app.py` wires the parts below (plus streaming recognition, cleanup, vocabulary, corrections, profiles and the indicator) and `quill/session.py` runs each push-to-talk hold as an ordered session; `quill/startup.py` manages the start-with-Windows entry. Among the parts:

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

It installs the real hooks with the triggers from `local/quill.toml` (or the example) and prints every decision (action, trigger input name, signal and reason, for example `dictation xbutton1 cancel short_hold`) while you press, hold, tap and combine the triggers. While it runs the bound buttons and keys do not do their normal action. With `--click-to-focus` a confirmed dictation or send-to-Claude hold also clicks at the pointer, as the app does; nothing is typed. It ends after `--seconds` or with Ctrl+C and writes counts per action, reason and trigger input, plus per trigger input the press durations in buckets and how late the hook callbacks ran (never other keys, positions or window names), to `local/selftest/triggers-<UTC time>.json`. Without `--allow-desktop-input` it installs nothing: it validates the settings, lists the bound trigger inputs and exits with code 0 (2 when the settings are invalid). It is never part of automated tests or checks.

## Privacy

Audio, transcripts, personal vocabulary, learned corrections and API keys stay on the machine and are never committed. Audio only leaves the PC for the engine the user has explicitly chosen.

## License

Not yet chosen.
