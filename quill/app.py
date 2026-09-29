"""The Quill dictation app: triggers, capture, streaming recognition, text pipeline, typing.

Usage (with the .venv interpreter, from the repository folder):
    .venv\\Scripts\\python -m quill                     run Quill (Ctrl+C or --stop ends it)
    .venv\\Scripts\\python -m quill --stop              end the running Quill
    .venv\\Scripts\\python -m quill --check             readiness report; touches no hook, window or microphone
    .venv\\Scripts\\python -m quill --install-startup   start Quill with Windows (HKCU Run entry)
    .venv\\Scripts\\python -m quill --remove-startup    stop starting Quill with Windows

``QuillApp`` wires the parts (``Parts``; ``real_parts`` builds the Windows
ones, tests pass fakes): the low-level hooks and the trigger machine
(``quill.hooks``), click-to-focus (``quill.focus``), MME capture
(``quill.audio``), the streaming transcriber with the configured Whisper
model and the personal vocabulary as hints (``quill.streaming``), the
indicator (``quill.indicator``), the text pipeline (``TextPipeline``:
cleanup, vocabulary matching, learned corrections, the target window's
profile) and the injector (``quill.inject``). Sessions are run by
``quill.session.SessionManager``. Command mode (``quill.command``) rewrites
the selection with the local Ollama model when the command trigger is bound.
Learning from corrections is wired too:
the correction key and manual-edit detection (``quill.corrections``,
``quill.edits``) see the key events of the hooks in memory only.

Only one Quill runs at a time (a named mutex). The model loads once, at
start, with the indicator in the ``loading`` state; a press while it loads
shows that state and records nothing. ``stop`` releases everything and
``start`` may be called again.

Logs go to ``local/logs/quill.log`` (ignored by Git): events, reason codes
and timings, never spoken or typed text.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from quill import startup
from quill.cleanup import Cleanup
from quill.command import COMMAND_TIMEOUT_S, CommandMode, CommandRewriter
from quill.config import LOCAL_CONFIG, REPO_ROOT, Config, ConfigError, load_config
from quill.corrections import CorrectionKey, CorrectionStore, Dictation, Learner, new_dictation_id
from quill.edits import EditTracker, KeyTranslator, ManualEdits
from quill.focus import ClickToFocus
from quill.hooks import TriggerHooks, monotonic_ms, real_hooks
from quill.indicator.render import LOADING
from quill.inject import Injector, Target
from quill.profiles import CLAUDE_CODE, Profiles, StyleError, WindowInfo, apply_profile, load_style_samples, style_prompt, window_info
from quill.session import CLEANUP_FALLBACK, Processed, SessionManager
from quill.streaming import StreamingTranscriber, options_for
from quill.triggers import KEY, InputEvent
from quill.vocabulary import Matcher, Vocabulary, VocabularyError, hint_list, load_generic_terms, load_vocabulary
from quill.whisper import MODELS, Decode, model_present

log = logging.getLogger("quill.app")

LOG_DIR = REPO_ROOT / "local" / "logs"
LOG_FILE = LOG_DIR / "quill.log"
VENV_DIR = REPO_ROOT / ".venv"
VENV_PACKAGES = ("faster_whisper", "ctranslate2", "numpy")
INSTANCE_NAME = "Local\\Quill.Dictation.Instance"
STOP_EVENT_NAME = "Local\\Quill.Dictation.Stop"
LOAD_WAIT_S = 0.1
WARMUP_S = 1.0

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ALREADY_RUNNING = 3


class AlreadyRunning(RuntimeError):
    """Another Quill holds the single-instance mutex."""


# ---------------------------------------------------------------- single instance


class InstanceLock:
    """The named mutex that keeps Quill single-instance, and the named event ``--stop`` sets.

    The objects live in the session namespace (``Local\\``), so they concern
    the signed-in user only. ``api`` defaults to kernel32 through ctypes.
    """

    def __init__(self, api: object | None = None, name: str = INSTANCE_NAME, stop_name: str = STOP_EVENT_NAME) -> None:
        self.api = api
        self.name = name
        self.stop_name = stop_name
        self._mutex = 0
        self._event = 0

    def _kernel(self) -> object:
        if self.api is None:
            self.api = Kernel32()
        return self.api

    @property
    def held(self) -> bool:
        return bool(self._mutex)

    def acquire(self) -> bool:
        """True when this process is now the only Quill; False when another one runs."""
        if self._mutex:
            return True
        api = self._kernel()
        handle, existed = api.create_mutex(self.name)
        if existed:
            api.close(handle)
            return False
        try:
            self._event = api.create_event(self.stop_name)
        except BaseException:
            api.close(handle)
            raise
        self._mutex = handle
        return True

    def wait_stop(self, timeout_s: float) -> bool:
        """Whether ``--stop`` asked this instance to end (waits up to ``timeout_s``)."""
        if not self._event:
            time.sleep(timeout_s)
            return False
        return self._kernel().wait(self._event, timeout_s)

    def release(self) -> None:
        api = self.api
        for attribute in ("_event", "_mutex"):
            handle = getattr(self, attribute)
            setattr(self, attribute, 0)
            if handle and api is not None:
                api.close(handle)

    def signal_stop(self) -> bool:
        """From another process: ask the running Quill to end; False when none runs."""
        return self._kernel().set_event(self.stop_name)


class Kernel32:
    """The kernel32 calls of ``InstanceLock`` (ctypes)."""

    ERROR_ALREADY_EXISTS = 183
    WAIT_OBJECT_0 = 0
    EVENT_MODIFY_STATE = 0x0002
    SYNCHRONIZE = 0x00100000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        from quill.win32 import bind

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        bind(kernel32, "CreateMutexW", wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        bind(kernel32, "CreateEventW", wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        bind(kernel32, "OpenEventW", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        bind(kernel32, "SetEvent", wintypes.BOOL, wintypes.HANDLE)
        bind(kernel32, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
        bind(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
        self._ctypes = ctypes
        self._k = kernel32

    def create_mutex(self, name: str) -> tuple[int, bool]:
        handle = self._k.CreateMutexW(None, False, name)
        existed = self._ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS
        if not handle:
            raise OSError(f"CreateMutexW failed (error {self._ctypes.get_last_error()})")
        return handle, existed

    def create_event(self, name: str) -> int:
        handle = self._k.CreateEventW(None, True, False, name)
        if not handle:
            raise OSError(f"CreateEventW failed (error {self._ctypes.get_last_error()})")
        return handle

    def set_event(self, name: str) -> bool:
        handle = self._k.OpenEventW(self.EVENT_MODIFY_STATE | self.SYNCHRONIZE, False, name)
        if not handle:
            return False
        try:
            return bool(self._k.SetEvent(handle))
        finally:
            self._k.CloseHandle(handle)

    def wait(self, handle: int, timeout_s: float) -> bool:
        return self._k.WaitForSingleObject(handle, max(0, int(timeout_s * 1000))) == self.WAIT_OBJECT_0

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)


# ---------------------------------------------------------------- engine


class WarmModel:
    """The Whisper model, loaded once and warmed with one short decode of quiet noise,
    so the first dictation does not pay the GPU's first-call cost."""

    def __init__(self, model: object, warmup_s: float = WARMUP_S) -> None:
        self.model = model
        self.warmup_s = warmup_s

    def load(self) -> None:
        self.model.load()
        samples = int(self.warmup_s * 16_000)
        # Quiet deterministic noise (a pseudo-random walk of +-8), never real audio.
        pcm = bytearray()
        value = 0
        for index in range(samples):
            value = ((value * 1103515245 + 12345) & 0x7FFFFFFF)
            pcm += int((value >> 16) % 17 - 8).to_bytes(2, "little", signed=True)
        try:
            self.model.transcribe(bytes(pcm), Decode(beam_size=1, without_timestamps=True, max_new_tokens=8))
        except Exception as exc:  # noqa: BLE001 - a failed warm-up only makes the first dictation slower
            log.warning("model warm-up failed (%s)", type(exc).__name__)

    def transcribe(self, pcm: bytes, options: Decode) -> object:
        return self.model.transcribe(pcm, options)

    def close(self) -> None:
        close = getattr(self.model, "close", None)
        if close is not None:
            close()


def mme_capture_factory(microphone: str, api_factory: Callable[[], object] | None = None) -> Callable:
    """Capture sessions on the configured MME microphone, looked up at every press (it may be replugged)."""
    from quill.audio import Capture, input_devices, select_device

    state: dict[str, object] = {}

    def factory(on_data: Callable[[bytes], None]) -> object:
        api = state.get("api")
        if api is None:
            if api_factory is None:
                from quill.audio import WinMM

                api = WinMM()
            else:
                api = api_factory()
            state["api"] = api
        index = select_device(input_devices(api), microphone)
        return Capture(api, index, on_data=on_data)

    return factory


# ---------------------------------------------------------------- text pipeline


class TextPipeline:
    """Raw recognized text -> the text typed: cleanup, vocabulary, learned corrections, profile.

    The same stages, in the same order, as ``bench.pipeline`` measures. The
    target window's profile is chosen once per session (``describe``); it
    also tells the send trigger whether the target is Claude Code.
    """

    def __init__(self, config: Config, *, vocabulary: Vocabulary, generic_terms: Sequence[str],
                 describe: Callable[[Target], WindowInfo | None], learner: Learner | None = None,
                 client: object | None = None) -> None:
        self.config = config
        self.keep = tuple(hint_list(vocabulary, (), generic_terms))
        self.matcher = Matcher(vocabulary, (), generic_terms)
        self.profiles = Profiles(config.profiles)
        self.describe = describe
        self.learner = learner
        self.client = client
        if config.cleanup_mode == "llm" and client is None:
            raise ValueError("the llm cleanup needs an Ollama client")

    def _style(self, profile: str) -> str:
        try:
            samples = load_style_samples(self.config.style_dir, profile)
        except StyleError as exc:
            log.warning("%s", exc)
            samples = ()
        return style_prompt(profile, samples)

    def __call__(self, raw: str, target: Target) -> Processed:
        try:
            info = self.describe(target)
        except Exception as exc:  # noqa: BLE001 - an unreadable window gets the default profile
            log.warning("target window not described (%s)", type(exc).__name__)
            info = None
        profile = self.profiles.select(info)
        notice = None
        if self.config.cleanup_mode == "llm":
            cleanup = Cleanup("llm", client=self.client, model=self.config.ollama_model, keep=self.keep,
                              style=self._style(profile))
        else:
            cleanup = Cleanup("rules", keep=self.keep)
        cleaned = cleanup(raw)
        if cleaned.fallback:
            log.warning("llm cleanup fell back to the rules (%s)", cleaned.fallback.split(":")[0])
            if cleaned.fallback.startswith("llm failed"):
                notice = CLEANUP_FALLBACK
        text = self.matcher.apply(cleaned.text)
        if self.learner is not None:
            text = self.learner.apply(text)
        text = apply_profile(text, profile, self.keep)
        return Processed(text, profile, profile == CLAUDE_CODE, notice)


# ---------------------------------------------------------------- parts and app


@dataclass
class Parts:
    """Everything the app talks to; ``real_parts`` builds the Windows ones, tests pass fakes."""

    api: object  # quill.win32.User32 or a fake: input, windows, clipboard, key state
    model: object  # load / transcribe / close
    capture_factory: Callable[[Callable[[bytes], None]], object]
    indicator: object
    instance: InstanceLock
    hooks_factory: Callable[[], object] = real_hooks
    layout: object | None = None  # keyboard layout for manual-edit detection; None disables it
    client: object | None = None  # local Ollama client (llm cleanup)
    command_client: object | None = None  # local Ollama client of command mode; None disables it
    vocabulary: Vocabulary = field(default_factory=Vocabulary)
    generic_terms: Sequence[str] = ()
    clock: Callable[[], float] = time.perf_counter
    clock_ms: Callable[[], float] = monotonic_ms
    wall_clock: Callable[[], float] = time.time
    focus_options: dict = field(default_factory=dict)
    inject_options: dict = field(default_factory=dict)
    command_options: dict = field(default_factory=dict)


class QuillApp:
    def __init__(self, config: Config, parts: Parts) -> None:
        self.config = config
        self.parts = parts
        api = parts.api
        self.injector = Injector(api, **parts.inject_options)
        self.focus = ClickToFocus(api, config.click_to_focus, **parts.focus_options)
        hints = hint_list(parts.vocabulary, (), parts.generic_terms)
        self.transcriber = StreamingTranscriber(parts.model, options_for(config.engine_model), hints)
        self.learner = Learner(CorrectionStore(config.corrections_path), clock=parts.wall_clock)
        self.pipeline = TextPipeline(config, vocabulary=parts.vocabulary, generic_terms=parts.generic_terms,
                                     describe=lambda target: window_info(api, target.hwnd), learner=self.learner,
                                     client=parts.client)
        self.command: CommandMode | None = None
        if config.trigger("command").enabled and parts.command_client is not None:
            rewriter = CommandRewriter(parts.command_client, config.ollama_model)
            self.command = CommandMode(api, self.injector, rewriter, lambda target: window_info(api, target.hwnd),
                                       **parts.command_options)
        self.correction_key = CorrectionKey(self.learner, self._read_selection, api.foreground_window)
        self.edits: ManualEdits | None = None
        if parts.layout is not None:
            ignore = frozenset(item.vk for trigger in config.triggers for item in trigger.inputs if item.kind == KEY)
            if config.correction_key is not None:
                ignore |= {config.correction_key.vk}
            self.edits = ManualEdits(self.learner, KeyTranslator(parts.layout, ignore),
                                     EditTracker(config.edit_window_s), api.foreground_window)
        self.sessions = SessionManager(
            transcriber=self.transcriber, capture_factory=parts.capture_factory, focus=self.focus,
            injector=self.injector, indicator=parts.indicator, pipeline=self.pipeline, command=self.command,
            on_session_start=self._session_started, on_typed=self._typed, housekeeping=self._housekeeping,
            clock=parts.clock,
        )
        self.hooks: TriggerHooks | None = None
        self._loader: threading.Thread | None = None
        self._stopping = threading.Event()
        self._correction_down = False
        self._correction_thread: threading.Thread | None = None
        self.running = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self.running:
            raise RuntimeError("Quill is already running")
        if not self.parts.instance.acquire():
            raise AlreadyRunning("another Quill is already running")
        self.running = True
        self._stopping.clear()
        try:
            self.parts.indicator.start()
            self.parts.indicator.show(LOADING)
            self.sessions.start()
            self.transcriber.start()
            started = self.parts.clock()
            self._loader = threading.Thread(target=self._wait_loaded, args=(started,), name="quill-loader",
                                            daemon=True)
            self._loader.start()
            self.hooks = TriggerHooks.from_config(self.config, self.sessions.handle, factory=self.parts.hooks_factory,
                                                  key_state=self.parts.api.key_down, clock_ms=self.parts.clock_ms,
                                                  on_event=self._on_event)
            self.hooks.start()
        except BaseException:
            self.stop()
            raise
        log.info("Quill started (engine %s, cleanup %s, indicator %s, command mode %s)", self.config.engine_model,
                 self.config.cleanup_mode, self.config.indicator_position, "on" if self.command else "off")

    def _wait_loaded(self, started: float) -> None:
        while not self.transcriber.ready.wait(LOAD_WAIT_S):
            if self._stopping.is_set():
                return
        if self._stopping.is_set():
            return
        error = self.transcriber.load_error
        if error:
            log.error("speech model not loaded: %s", error)
        else:
            log.info("speech model ready in %.1f s", self.parts.clock() - started)
        self.sessions.loaded(error=bool(error))

    def stop(self) -> None:
        """Remove the hooks first, then release the engine, the sessions, the indicator and the lock."""
        self._stopping.set()
        steps = (
            ("hooks", self._stop_hooks),
            ("engine", self.transcriber.stop),
            ("sessions", self.sessions.stop),
            ("loader", self._join_loader),
            ("corrections", self._join_correction),
            ("edits", self._stop_edits),
            ("indicator", self.parts.indicator.stop),
        )
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - every part is released even when one fails
                log.error("stopping %s failed (%s)", name, type(exc).__name__)
        self.parts.instance.release()
        if self.running:
            log.info("Quill stopped")
        self.running = False

    def _stop_hooks(self) -> None:
        hooks, self.hooks = self.hooks, None
        if hooks is not None:
            hooks.stop()

    def _join_loader(self) -> None:
        loader, self._loader = self._loader, None
        if loader is not None:
            loader.join(5.0)

    def _join_correction(self) -> None:
        thread = self._correction_thread
        if thread is not None:
            thread.join(5.0)

    def _stop_edits(self) -> None:
        if self.edits is not None:
            self.edits.stop()

    def run(self, poll_s: float = 0.5) -> int:
        """Start, then wait for ``--stop`` or Ctrl+C."""
        try:
            self.start()
        except AlreadyRunning:
            log.warning("another Quill is already running")
            return EXIT_ALREADY_RUNNING
        try:
            while not self.parts.instance.wait_stop(poll_s):
                pass
            log.info("stop requested")
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop()
        return EXIT_OK

    # ------------------------------------------------------------ learning (hook worker and session threads)

    def _session_started(self) -> None:
        if self.edits is not None:
            self.edits.stop()

    def _typed(self, target: Target, text: str) -> None:
        dictation = Dictation(new_dictation_id(), text, target.hwnd, self.parts.wall_clock())
        self.correction_key.remember(dictation)
        if self.edits is not None:
            self.edits.typed(dictation)

    def _housekeeping(self) -> None:
        if self.edits is not None:
            self.edits.poll()

    def _on_event(self, event: InputEvent) -> None:
        key = self.config.correction_key
        if key is not None and event.kind == KEY and event.vk == key.vk:
            if not event.down:
                self._correction_down = False
            elif not event.is_injected and not self._correction_down:
                self._correction_down = True
                self._press_correction_key()
            return
        if self.edits is not None:
            self.edits.on_input(event)

    def _press_correction_key(self) -> None:
        thread = self._correction_thread
        if thread is not None and thread.is_alive():
            log.info("correction key: previous press still running")
            return
        self._correction_thread = threading.Thread(target=self._correct, name="quill-correction", daemon=True)
        self._correction_thread.start()

    def _correct(self) -> None:
        try:
            log.info("correction key: %s", self.correction_key.press())
        except Exception as exc:  # noqa: BLE001
            log.error("correction key failed (%s)", type(exc).__name__)

    def _read_selection(self) -> str | None:
        from quill.clipboard import copy_events, copy_selection
        from quill.win32 import KeyEvent

        api = self.parts.api

        def send_copy() -> bool:
            events = [KeyEvent(vk, 0, flags) for vk, flags in copy_events()]
            return api.send_input(events) == len(events)

        result = copy_selection(api, send_copy)
        log.info("correction key: selection %s", result.reason)
        return result.text


def load_personal(config: Config) -> tuple[Vocabulary, list[str]]:
    return load_vocabulary(config.vocabulary_path), load_generic_terms()


def real_parts(config: Config) -> Parts:
    """The Windows parts. Creating them installs nothing; ``QuillApp.start`` does."""
    from quill.edits import Win32Layout
    from quill.indicator.window import Indicator
    from quill.ollama import OllamaClient
    from quill.whisper import Whisper
    from quill.win32 import User32

    vocabulary, terms = load_personal(config)
    return Parts(
        api=User32(),
        model=WarmModel(Whisper(config.engine_model)),
        capture_factory=mme_capture_factory(config.microphone),
        indicator=Indicator(config.indicator_position),
        instance=InstanceLock(),
        layout=Win32Layout(),
        client=OllamaClient(config.ollama_url) if config.cleanup_mode == "llm" else None,
        command_client=(OllamaClient(config.ollama_url, timeout_s=COMMAND_TIMEOUT_S)
                        if config.trigger("command").enabled else None),
        vocabulary=vocabulary,
        generic_terms=terms,
    )


# ---------------------------------------------------------------- readiness check


@dataclass(frozen=True)
class CheckLine:
    name: str
    ok: bool
    detail: str
    required: bool = True


def venv_site_packages(venv: Path = VENV_DIR) -> Path:
    return venv / "Lib" / "site-packages"


def check_readiness(config: Config, *, models_dir: Path | None = None, venv: Path = VENV_DIR,
                    devices: Callable[[], list[str]] | None = None, client: object | None = None,
                    registry: object | None = None, now: float | None = None) -> list[CheckLine]:
    """Readiness of every part, without hooks, windows or the microphone being opened.

    ``devices`` lists the MME input names (names only; no device is opened).
    Lines name parts and states only: never the microphone name, a path
    outside the repository or any personal value.
    """
    from quill.audio import AudioError, select_device
    from quill.whisper import MODELS_DIR

    lines: list[CheckLine] = []
    folder = models_dir or MODELS_DIR
    present = model_present(config.engine_model, folder)
    lines.append(CheckLine("model", present, f"{config.engine_model}: " + (
        "files present" if present else f"files missing in models/{MODELS[config.engine_model]}")))

    site = venv_site_packages(venv)
    missing = [name for name in VENV_PACKAGES if not (site / name).is_dir()]
    lines.append(CheckLine("venv", not missing, "packages present" if not missing
                           else f"missing in .venv: {', '.join(missing)}"))
    pythonw = startup.venv_pythonw(venv)
    lines.append(CheckLine("pythonw", pythonw.is_file(), ".venv pythonw present" if pythonw.is_file()
                           else ".venv\\Scripts\\pythonw.exe missing (needed to start with Windows)", required=False))
    in_venv = Path(sys.prefix).resolve() == venv.resolve()
    lines.append(CheckLine("interpreter", in_venv, "running with the .venv interpreter" if in_venv
                           else "not the .venv interpreter: run Quill with .venv\\Scripts\\python", required=False))

    if devices is None:
        from quill.audio import WinMM, input_devices

        def devices() -> list[str]:
            return input_devices(WinMM())
    try:
        select_device(devices(), config.microphone)
        lines.append(CheckLine("microphone", True, "configured microphone found (not opened)"))
    except AudioError as exc:
        lines.append(CheckLine("microphone", False, str(exc)))
    except OSError as exc:
        lines.append(CheckLine("microphone", False, f"MME inputs not listed ({type(exc).__name__})"))

    try:
        vocabulary = load_vocabulary(config.vocabulary_path)
        counts = vocabulary.counts()
        lines.append(CheckLine("vocabulary", True, f"{counts['names']} names, {counts['terms']} terms"))
    except VocabularyError as exc:
        lines.append(CheckLine("vocabulary", False, str(exc)))

    needed = config.cleanup_mode == "llm"
    if client is None:
        from quill.ollama import OllamaClient

        client = OllamaClient(config.ollama_url, timeout_s=3.0)
    try:
        installed = client.installed()
        found = config.ollama_model in installed
        detail = f"{config.ollama_model} " + ("installed" if found else "not installed")
    except OSError as exc:
        found, detail = False, f"Ollama not reachable ({type(exc).__name__})"
    uses = [use for use, on in (("used by the llm cleanup", needed),
                                 ("used by command mode", config.trigger("command").enabled)) if on]
    usage = " and ".join(uses) if uses else "not used (rules cleanup, no command trigger)"
    lines.append(CheckLine("ollama", found, f"{detail}; {usage}", required=needed))

    try:
        state = startup.status(registry)
        lines.append(CheckLine("startup", True, startup.STATUS_TEXT[state], required=False))
    except OSError as exc:
        lines.append(CheckLine("startup", False, f"Run entry not readable ({type(exc).__name__})", required=False))

    from quill.review import reminder

    corrections = CorrectionStore(config.corrections_path).load()
    note = reminder(corrections, time.time() if now is None else now)
    counts = corrections.counts()
    lines.append(CheckLine("corrections", True, f"{counts['active']} active, {counts['pending']} pending"
                           + ("; review due: py -3.12 -m quill.review" if note else ""), required=False))
    return lines


def print_check(lines: Sequence[CheckLine], out: Callable[[str], None] = print) -> int:
    for line in lines:
        mark = "OK  " if line.ok else ("FAIL" if line.required else "note")
        out(f"{mark} {line.name}: {line.detail}")
    ready = all(line.ok for line in lines if line.required)
    out("Quill is ready." if ready else "Quill is not ready: fix the FAIL lines.")
    return EXIT_OK if ready else EXIT_FAILED


# ---------------------------------------------------------------- command line


def setup_logging(path: Path = LOG_FILE, console: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3,
                                                                            encoding="utf-8")]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None, *, parts_factory: Callable[[Config], Parts] = real_parts,
         registry: object | None = None, out: Callable[[str], None] = print,
         readiness: Callable[..., list[CheckLine]] = check_readiness, kernel: object | None = None,
         logging_setup: Callable[[], None] = setup_logging) -> int:
    """The command line. Tests pass fakes for the parts, the registry, the
    readiness probes, the kernel objects and the logging setup."""
    parser = argparse.ArgumentParser(prog="python -m quill", description="Quill: push-to-talk dictation.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="report readiness without hooks, windows or microphone")
    group.add_argument("--install-startup", action="store_true", help="start Quill with Windows (HKCU Run entry)")
    group.add_argument("--remove-startup", action="store_true", help="remove the HKCU Run entry")
    group.add_argument("--stop", action="store_true", help="end the running Quill")
    parser.add_argument("--config", type=Path, default=LOCAL_CONFIG, help="settings file (default local/quill.toml)")
    args = parser.parse_args(argv)

    if args.install_startup:
        return startup.install_command(registry, out=out)
    if args.remove_startup:
        return startup.remove_command(registry, out=out)
    if args.stop:
        stopped = InstanceLock(kernel).signal_stop()
        out("Quill was asked to stop." if stopped else "Quill is not running.")
        return EXIT_OK if stopped else EXIT_FAILED
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        out(str(exc))
        return EXIT_USAGE
    if args.check:
        return print_check(readiness(config, registry=registry), out)

    logging_setup()
    try:
        parts = parts_factory(config)
    except (VocabularyError, OSError, ValueError) as exc:
        log.error("Quill cannot start: %s", exc)
        return EXIT_FAILED
    code = QuillApp(config, parts).run()
    if code == EXIT_ALREADY_RUNNING:
        out("Quill is already running.")
    return code
