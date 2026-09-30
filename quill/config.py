"""Quill settings: the ignored local/quill.toml read over the committed example.

``load_config`` starts from ``quill.example.toml`` and merges
``local/quill.toml`` over it table by table (a profile is replaced whole, with all
its alternatives).
An input the local file binds to a trigger is taken off the example's
triggers first, so a local binding always wins over an example one (an older
local file that binds mouse 5 to ``send_claude`` keeps loading).
The merged result is validated strictly: unknown field names, unsupported key
or button names, an input bound twice within the same file, a non-loopback Ollama address or a
personal-data path outside ``local/``, a voice trigger with a mouse button or
more than one key, or a shortcut folder that is not an absolute path raise
``ConfigError``. Error messages
name the field only, never its value, because values can be personal (for
example the microphone name).

Usage: py -3.12 -m quill.config --check [PATH]
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from quill.notify import DEFAULT_FILTER, FILTERS
from quill.speech import DEFAULT_RATE, DEFAULT_VOLUME, RATE_RANGE, VOLUME_RANGE
from quill.whisper import DEFAULT_MODEL, PRECISE_MODEL

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "quill.example.toml"
LOCAL_DIR = REPO_ROOT / "local"
LOCAL_CONFIG = LOCAL_DIR / "quill.toml"

# send_polished (always rewritten by the local model) and send_raw (never
# rewritten) press Enter in Claude Code; send_claude is the older send action
# (rewritten only when long, like a dictation), kept for existing local files.
# voice runs a spoken command (quill.voice): it never clicks, types or presses Enter.
ACTIONS = ("dictation", "command", "send_claude", "send_polished", "send_raw", "voice")
VOICE_ACTION = "voice"
PROFILE_NAMES = ("claude-code", "vscode", "whatsapp", "email")
INDICATOR_POSITIONS = ("pointer", "bottom-center")
CLEANUP_MODES = ("rules", "llm")
ENGINE_MODELS = (DEFAULT_MODEL, PRECISE_MODEL)
MIN_HOLD_RANGE = (50, 2000)
EDIT_WINDOW_RANGE = (5, 600)
# Automatic rewrite of long dictations: what counts as long, and how long the model may take.
REWRITE_AUDIO_RANGE = (5.0, 120.0)
REWRITE_WORDS_RANGE = (10, 1000)
REWRITE_TIMEOUT_RANGE = (0.5, 30.0)
UNDO_WINDOW_RANGE = (5, 600)

# Virtual-key codes of the inputs a trigger may use.
BUTTONS = {"middle": 0x04, "xbutton1": 0x05, "xbutton2": 0x06}
KEYS = {
    **{f"f{number}": 0x6F + number for number in range(1, 25)},
    "pause": 0x13,
    "insert": 0x2D,
    "apps": 0x5D,
    "scroll_lock": 0x91,
    "right_shift": 0xA1,
    "right_ctrl": 0xA3,
    "right_alt": 0xA5,
}

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
MAX_TEXT = 200
# [voice_commands] shortcut_dirs: how many folders, and how long each path may be.
MAX_SHORTCUT_DIRS = 20
MAX_PATH_TEXT = 260

# Allowed fields: a dict is a table, None a value.
SCHEMA: dict[str, object] = {
    "triggers": {action: {"buttons": None, "keys": None} for action in ACTIONS},
    "input": {"min_hold_ms": None, "click_to_focus": None},
    "audio": {"microphone": None},
    "engine": {"model": None},
    "indicator": {"position": None},
    "ollama": {"url": None, "model": None},
    "cleanup": {"mode": None},
    "corrections": {"key": None, "edit_window_s": None},
    "autorewrite": {"enabled": None, "min_audio_s": None, "min_words": None, "timeout_s": None, "undo_key": None,
                    "undo_window_s": None},
    "claude_alert": {"enabled": None, "sound": None, "filter": None, "speak_project": None, "speech_volume": None,
                     "speech_rate": None},
    "voice_commands": {"shortcut_dirs": None, "model": None},
    "paths": {"vocabulary": None, "corrections": None, "style": None},
    "profiles": {name: {"processes": None, "classes": None, "titles": None} for name in PROFILE_NAMES},
}


class ConfigError(ValueError):
    """The configuration is invalid; the message names the field, never its value."""


@dataclass(frozen=True)
class Input:
    """One physical input bound to a trigger: a mouse button or a key."""

    kind: str  # "button" or "key"
    name: str
    vk: int


@dataclass(frozen=True)
class Trigger:
    action: str
    inputs: tuple[Input, ...]

    @property
    def enabled(self) -> bool:
        return bool(self.inputs)


@dataclass(frozen=True)
class ProfileMatcher:
    """Matches the foreground window: every non-empty field must match."""

    name: str
    processes: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutoRewrite:
    """``[autorewrite]``: a dictation longer than ``min_audio_s`` seconds of audio or
    ``min_words`` words is checked by the local model, which may take ``timeout_s``.
    ``undo_key`` (None when disabled) puts the original text back for
    ``undo_window_s`` seconds after an automatic rewrite."""

    enabled: bool = False
    min_audio_s: float = 15.0
    min_words: int = 40
    timeout_s: float = 4.0
    undo_key: Input | None = None
    undo_window_s: int = 30


@dataclass(frozen=True)
class ClaudeAlert:
    """``[claude_alert]``: the alert when Claude Code finishes a reply or asks for a
    permission (``quill.notify``). ``sound`` plays a short sound with it;
    ``filter`` chooses which Claude Code sessions ring (``quill.notify.FILTERS``).
    ``speak_project`` says the project names after the sound with a Windows
    voice (``quill.speech``) at ``speech_volume`` (0-100) and ``speech_rate``
    (1.0 is normal speed); without ``sound`` nothing is spoken either."""

    enabled: bool = True
    sound: bool = True
    filter: str = DEFAULT_FILTER
    speak_project: bool = True
    speech_volume: int = DEFAULT_VOLUME
    speech_rate: float = DEFAULT_RATE


@dataclass(frozen=True)
class VoiceSettings:
    """``[voice_commands]``: the folders whose Windows shortcuts (.lnk) the voice
    command "abre VS Code no <projeto>" may open (``quill.shortcuts``), and the
    Whisper model that decodes the voice trigger's holds (``model``; the
    dictation keeps ``[engine] model``)."""

    shortcut_dirs: tuple[Path, ...] = ()
    model: str = PRECISE_MODEL


@dataclass(frozen=True)
class Config:
    triggers: tuple[Trigger, ...]
    min_hold_ms: int
    click_to_focus: bool
    microphone: str
    engine_model: str
    indicator_position: str
    ollama_url: str
    ollama_model: str
    cleanup_mode: str
    vocabulary_path: Path
    corrections_path: Path
    style_dir: Path
    profiles: tuple[ProfileMatcher, ...]
    # Learning from corrections: the correction key (None when disabled) and
    # how long manual edits of a typed text are followed.
    correction_key: Input | None = None
    edit_window_s: int = 30
    autorewrite: AutoRewrite = AutoRewrite()
    claude_alert: ClaudeAlert = ClaudeAlert()
    voice: VoiceSettings = VoiceSettings()

    def trigger(self, action: str) -> Trigger:
        for trigger in self.triggers:
            if trigger.action == action:
                return trigger
        raise KeyError(action)

    def action_for(self, kind: str, vk: int) -> str | None:
        """The action bound to an input, or None."""
        for trigger in self.triggers:
            if any(item.kind == kind and item.vk == vk for item in trigger.inputs):
                return trigger.action
        return None


# ---------------------------------------------------------------- reading


def _label(path: Path) -> str:
    """How a file is named in messages: repository-relative, or its name only."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def read_toml(path: Path) -> dict[str, object]:
    label = _label(path)
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"quill config: {label} not found") from None
    except tomllib.TOMLDecodeError as exc:
        # The decoder reports a position only, never the offending text.
        raise ConfigError(f"quill config: {label} is not valid TOML: {exc}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"quill config: {label} cannot be read ({type(exc).__name__})") from None


def merge(base: dict[str, object], override: dict[str, object], path: str = "") -> dict[str, object]:
    """Tables merge key by key; values and profiles are replaced whole."""
    merged = dict(base)
    for key, value in override.items():
        here = f"{path}.{key}" if path else key
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict) and path != "profiles":
            merged[key] = merge(old, value, here)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------- validation


def _check_fields(data: object, schema: dict[str, object], path: str) -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"quill config: {path} must be a table")
    for key, value in data.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"quill config: unknown field {here}")
        sub = schema[key]
        if path == "profiles" and isinstance(value, list):
            # [[profiles.<name>]]: alternative matchers for one profile.
            if not value:
                raise ConfigError(f"quill config: {here} needs processes, classes or titles")
            for index, entry in enumerate(value):
                _check_fields(entry, sub, f"{here}[{index}]")
        elif isinstance(sub, dict):
            _check_fields(value, sub, here)
        elif isinstance(value, dict):
            raise ConfigError(f"quill config: {here} must be a value, not a table")


def _get(data: dict[str, object], dotted: str) -> object:
    node: object = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"quill config: missing field {dotted}")
        node = node[part]
    return node


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"quill config: {field} must be a string")
    if not value.strip() and not allow_empty:
        raise ConfigError(f"quill config: {field} must not be empty")
    if len(value) > MAX_TEXT or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise ConfigError(f"quill config: {field} must be at most {MAX_TEXT} printable characters")
    return value


def _choice(value: object, field: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(f"quill config: {field} must be one of: {', '.join(choices)}")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"quill config: {field} must be a list of strings")
    return tuple(_text(item, f"{field}[{index}]") for index, item in enumerate(value))


def _triggers(data: dict[str, object]) -> tuple[Trigger, ...]:
    owners: dict[tuple[str, int], str] = {}
    triggers: list[Trigger] = []
    for action in ACTIONS:
        table = data.get("triggers", {}).get(action, {})
        inputs: list[Input] = []
        for field_name, kind, names in (("buttons", "button", BUTTONS), ("keys", "key", KEYS)):
            field = f"triggers.{action}.{field_name}"
            values = table.get(field_name, [])
            if not isinstance(values, list):
                raise ConfigError(f"quill config: {field} must be a list of names")
            if action == VOICE_ACTION and kind == "button" and values:
                raise ConfigError(f"quill config: {field} must be empty: the voice trigger takes a key, "
                                  "never a mouse button")
            if action == VOICE_ACTION and kind == "key" and len(values) > 1:
                raise ConfigError(f"quill config: {field} takes at most one key")
            for index, name in enumerate(values):
                here = f"{field}[{index}]"
                if not isinstance(name, str) or name not in names:
                    raise ConfigError(f"quill config: {here} is not a supported {kind} name")
                owner = owners.get((kind, names[name]))
                if owner is not None:
                    raise ConfigError(f"quill config: {here} is already bound in {owner}")
                owners[(kind, names[name])] = here
                inputs.append(Input(kind, name, names[name]))
        triggers.append(Trigger(action, tuple(inputs)))
    if not triggers[0].enabled:
        raise ConfigError("quill config: triggers.dictation needs at least one button or key")
    return tuple(triggers)


def _correction_key(value: object, triggers: tuple[Trigger, ...], field: str = "corrections.key") -> Input | None:
    if value == "":
        return None
    if not isinstance(value, str) or value not in KEYS:
        raise ConfigError(f"quill config: {field} is not a supported key name")
    if any(item.kind == "key" and item.vk == KEYS[value] for trigger in triggers for item in trigger.inputs):
        raise ConfigError(f"quill config: {field} is already bound to a trigger")
    return Input("key", value, KEYS[value])


def _undo_key(value: object, triggers: tuple[Trigger, ...], correction: Input | None) -> Input | None:
    field = "autorewrite.undo_key"
    key = _correction_key(value, triggers, field)
    if key is not None and correction is not None and key.vk == correction.vk:
        raise ConfigError(f"quill config: {field} is already the correction key")
    return key


def _edit_window(value: object) -> int:
    low, high = EDIT_WINDOW_RANGE
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"quill config: corrections.edit_window_s must be an integer from {low} to {high}")
    return value


def _number(value: object, field: str, bounds: tuple[float, float]) -> float:
    low, high = bounds
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"quill config: {field} must be a number from {low:g} to {high:g}")
    return float(value)


def _integer(value: object, field: str, bounds: tuple[int, int]) -> int:
    low, high = bounds
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"quill config: {field} must be an integer from {low} to {high}")
    return value


def _autorewrite(data: dict[str, object], triggers: tuple[Trigger, ...], correction: Input | None) -> AutoRewrite:
    words = _integer(_get(data, "autorewrite.min_words"), "autorewrite.min_words", REWRITE_WORDS_RANGE)
    return AutoRewrite(
        enabled=_bool(_get(data, "autorewrite.enabled"), "autorewrite.enabled"),
        min_audio_s=_number(_get(data, "autorewrite.min_audio_s"), "autorewrite.min_audio_s", REWRITE_AUDIO_RANGE),
        min_words=words,
        timeout_s=_number(_get(data, "autorewrite.timeout_s"), "autorewrite.timeout_s", REWRITE_TIMEOUT_RANGE),
        undo_key=_undo_key(_get(data, "autorewrite.undo_key"), triggers, correction),
        undo_window_s=_integer(_get(data, "autorewrite.undo_window_s"), "autorewrite.undo_window_s",
                               UNDO_WINDOW_RANGE),
    )


def _claude_alert(data: dict[str, object]) -> ClaudeAlert:
    return ClaudeAlert(
        enabled=_bool(_get(data, "claude_alert.enabled"), "claude_alert.enabled"),
        sound=_bool(_get(data, "claude_alert.sound"), "claude_alert.sound"),
        filter=_choice(_get(data, "claude_alert.filter"), "claude_alert.filter", FILTERS),
        speak_project=_bool(_get(data, "claude_alert.speak_project"), "claude_alert.speak_project"),
        speech_volume=_integer(_get(data, "claude_alert.speech_volume"), "claude_alert.speech_volume", VOLUME_RANGE),
        speech_rate=_number(_get(data, "claude_alert.speech_rate"), "claude_alert.speech_rate", RATE_RANGE),
    )


def _voice(data: dict[str, object]) -> VoiceSettings:
    field = "voice_commands.shortcut_dirs"
    value = _get(data, field)
    if not isinstance(value, list):
        raise ConfigError(f"quill config: {field} must be a list of folder paths")
    if len(value) > MAX_SHORTCUT_DIRS:
        raise ConfigError(f"quill config: {field} takes at most {MAX_SHORTCUT_DIRS} folders")
    folders: list[Path] = []
    for index, item in enumerate(value):
        here = f"{field}[{index}]"
        if (not isinstance(item, str) or not item.strip() or len(item) > MAX_PATH_TEXT
                or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in item)):
            raise ConfigError(f"quill config: {here} must be a path of at most {MAX_PATH_TEXT} printable characters")
        path = Path(item)
        if not path.is_absolute():
            raise ConfigError(f"quill config: {here} must be an absolute folder path")
        folders.append(path)
    model = _choice(_get(data, "voice_commands.model"), "voice_commands.model", ENGINE_MODELS)
    return VoiceSettings(tuple(folders), model)


def _min_hold(value: object) -> int:
    low, high = MIN_HOLD_RANGE
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"quill config: input.min_hold_ms must be an integer from {low} to {high}")
    return value


def _ollama_url(value: object) -> str:
    field = "ollama.url"
    if not isinstance(value, str):
        raise ConfigError(f"quill config: {field} must be a string")
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        raise ConfigError(f"quill config: {field} is not a valid URL") from None
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if (parts.scheme != "http" or not loopback or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ConfigError(f"quill config: {field} must be a local http://127.0.0.1:<port> address")
    return value.rstrip("/")


def _local_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"quill config: {field} must be a path string")
    relative = Path(value)
    if relative.is_absolute() or relative.drive or relative.root:
        raise ConfigError(f"quill config: {field} must be a relative path inside local/")
    resolved = (REPO_ROOT / relative).resolve()
    local = LOCAL_DIR.resolve()
    if resolved == local or not resolved.is_relative_to(local):
        raise ConfigError(f"quill config: {field} must be a relative path inside local/")
    return resolved


def _profiles(data: dict[str, object]) -> tuple[ProfileMatcher, ...]:
    profiles: list[ProfileMatcher] = []
    for name, tables in data.get("profiles", {}).items():
        alternatives = tables if isinstance(tables, list) else [tables]
        for index, table in enumerate(alternatives):
            field = f"profiles.{name}[{index}]" if isinstance(tables, list) else f"profiles.{name}"
            fields = {key: _string_list(table.get(key, []), f"{field}.{key}")
                      for key in ("processes", "classes", "titles")}
            if not any(fields.values()):
                raise ConfigError(f"quill config: {field} needs processes, classes or titles")
            profiles.append(ProfileMatcher(name, **fields))
    return tuple(profiles)


def validate(data: dict[str, object]) -> Config:
    _check_fields(data, SCHEMA, "")
    triggers = _triggers(data)
    correction = _correction_key(_get(data, "corrections.key"), triggers)
    return Config(
        triggers=triggers,
        min_hold_ms=_min_hold(_get(data, "input.min_hold_ms")),
        click_to_focus=_bool(_get(data, "input.click_to_focus"), "input.click_to_focus"),
        microphone=_text(_get(data, "audio.microphone"), "audio.microphone"),
        engine_model=_choice(_get(data, "engine.model"), "engine.model", ENGINE_MODELS),
        indicator_position=_choice(_get(data, "indicator.position"), "indicator.position", INDICATOR_POSITIONS),
        ollama_url=_ollama_url(_get(data, "ollama.url")),
        ollama_model=_model(_get(data, "ollama.model")),
        cleanup_mode=_choice(_get(data, "cleanup.mode"), "cleanup.mode", CLEANUP_MODES),
        vocabulary_path=_local_path(_get(data, "paths.vocabulary"), "paths.vocabulary"),
        corrections_path=_local_path(_get(data, "paths.corrections"), "paths.corrections"),
        style_dir=_local_path(_get(data, "paths.style"), "paths.style"),
        profiles=_profiles(data),
        correction_key=correction,
        edit_window_s=_edit_window(_get(data, "corrections.edit_window_s")),
        autorewrite=_autorewrite(data, triggers, correction),
        claude_alert=_claude_alert(data),
        voice=_voice(data),
    )


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"quill config: {field} must be true or false")
    return value


def _model(value: object) -> str:
    if not isinstance(value, str) or not MODEL_NAME.match(value):
        raise ConfigError("quill config: ollama.model must be an Ollama model name")
    return value


def load_config(local: Path | None = LOCAL_CONFIG, example: Path = EXAMPLE_CONFIG) -> Config:
    """The example merged with ``local`` (skipped when None or missing), validated."""
    data = read_toml(example)
    _check_fields(data, SCHEMA, "")
    if local is not None and local.exists() and local.resolve() != example.resolve():
        override = read_toml(local)
        _check_fields(override, SCHEMA, "")
        data = merge(_free_example_inputs(data, override), override)
        if "key" not in override.get("corrections", {}):
            data = _free_default_key(data, "corrections", "key")
        if "undo_key" not in override.get("autorewrite", {}):
            data = _free_default_key(data, "autorewrite", "undo_key", also=("corrections.key",))
    return validate(data)


def _free_example_inputs(example: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """The example's trigger lists without the inputs the local file binds to a trigger.

    The example's voice trigger also gives its key up to the local correction
    or undo key: an older local file with ``corrections.key = "f9"`` keeps
    loading (other example triggers keep refusing those keys, as before).

    Only lists the local file leaves to the example lose an input: two
    triggers of the local file bound to one input still raise ``ConfigError``.
    Names that are not strings or not supported are left for ``validate``.
    """
    local = override.get("triggers", {})
    taken: set[tuple[str, int]] = set()
    for table in local.values():
        for field_name, kind, names in (("buttons", "button", BUTTONS), ("keys", "key", KEYS)):
            values = table.get(field_name, []) if isinstance(table, dict) else []
            if isinstance(values, list):
                taken |= {(kind, names[name]) for name in values if isinstance(name, str) and name in names}
    keys: set[tuple[str, int]] = set()  # the local correction and undo keys
    for table, field in (("corrections", "key"), ("autorewrite", "undo_key")):
        section = override.get(table)
        name = section.get(field) if isinstance(section, dict) else None
        if isinstance(name, str) and name in KEYS:
            keys.add(("key", KEYS[name]))
    if not taken and not keys:
        return example
    triggers: dict[str, object] = {}
    for action, table in example.get("triggers", {}).items():
        freed = dict(table)
        gone = taken | keys if action == VOICE_ACTION else taken
        for field_name, kind, names in (("buttons", "button", BUTTONS), ("keys", "key", KEYS)):
            values = table.get(field_name)
            if field_name in local.get(action, {}) or not isinstance(values, list):
                continue
            freed[field_name] = [name for name in values
                                 if not (isinstance(name, str) and name in names and (kind, names[name]) in gone)]
        triggers[action] = freed
    return {**example, "triggers": triggers}


def _free_default_key(data: dict[str, object], table: str, name: str, also: tuple[str, ...] = ()) -> dict[str, object]:
    """An example key gives way to a local trigger (or another ``also`` key) bound to the same key."""
    key = _get(data, f"{table}.{name}")
    taken = {(item.kind, item.vk) for trigger in _triggers(data) for item in trigger.inputs}
    taken |= {("key", KEYS.get(other)) for other in (_get(data, field) for field in also) if isinstance(other, str)}
    if isinstance(key, str) and ("key", KEYS.get(key)) in taken:
        return merge(data, {table: {name: ""}})
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m quill.config", description=__doc__.splitlines()[0])
    parser.add_argument("--check", nargs="?", const=LOCAL_CONFIG, type=Path, metavar="PATH",
                        help="validate PATH read over the example (default: local/quill.toml)")
    args = parser.parse_args(argv)
    if args.check is None:
        parser.print_usage(sys.stderr)
        return 2
    if not args.check.is_file():
        print(f"quill config: {_label(args.check)} not found", file=sys.stderr)
        return 2
    try:
        config = load_config(args.check)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    enabled = [trigger.action for trigger in config.triggers if trigger.enabled]
    print(f"quill config: OK ({_label(args.check)}; triggers: {', '.join(enabled)}; "
          f"{len(config.profiles)} profile matchers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
