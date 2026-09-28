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
- `quill/win32.py`: the ctypes layer. It has no call that moves the keyboard focus.

Tests: `py -3.12 -m unittest discover -s quill/tests -t .`. They use a fake Win32 layer and never create windows, install hooks, send input, open the microphone or touch the real clipboard; the real layer is disabled while they run.

### Manual typing self-test

```
py -3.12 -m quill.selftest.typing --allow-desktop-input [--targets edit,edge-textarea,edge-contenteditable,console,vscode]
```

It opens its own windows (a Win32 edit box, Edge app windows with a temporary profile, a console, VS Code with a temporary profile), takes the focus and types an invented corpus into each, then compares what arrived and checks that the clipboard is unchanged. Run it only when you choose, and do not touch the keyboard or mouse until it ends (about a minute). It writes an aggregate result, with counts of lost, extra and changed characters and never the typed text, to `local/selftest/typing-<UTC time>.json`. Without `--allow-desktop-input` it exits with code 2 and opens nothing. It is never part of automated tests or checks.

## Privacy

Audio, transcripts, personal vocabulary, learned corrections and API keys stay on the machine and are never committed. Audio only leaves the PC for the engine the user has explicitly chosen.

## License

Not yet chosen.
