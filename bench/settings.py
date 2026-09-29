"""Machine-specific benchmark settings, read from the ignored local/bench.toml.

``load_settings`` returns the settings of the commands set (the 44 takes read
in place from the reference project) with the dictation set attached as
``settings.dictation`` and the command-mode set of spoken rewrite
instructions as ``settings.rewrite``. Both live in this repository: their
scripts are committed and their recordings stay under the ignored ``local/``
folder.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "local" / "bench.toml"
RESULTS_DIR = REPO_ROOT / "bench" / "results"
LOCAL_DIR = REPO_ROOT / "local"
DEFAULT_MANIFEST_NAME = "manifesto.json"
DEFAULT_EXPECTED_TAKES = 44

DICTATION_SCRIPT = REPO_ROOT / "bench" / "dictation" / "guiao-ditado-pt.md"
DICTATION_RECORDINGS = LOCAL_DIR / "recordings" / "dictation"
DICTATION_PREFIX = "dt"
DICTATION_EXPECTED_TAKES = 36
DICTATION_MIN_TAKES = 30

REWRITE_SCRIPT = REPO_ROOT / "bench" / "dictation" / "guiao-reescrita-pt.md"
REWRITE_RECORDINGS = LOCAL_DIR / "recordings" / "rewrite"
REWRITE_PREFIX = "rw"
REWRITE_EXPECTED_TAKES = 20
REWRITE_MIN_TAKES = 16

ID_PREFIX = re.compile(r"^[a-z]{2,8}$")
PLACEHOLDER_KEY = re.compile(r"^<projeto-\d+>$")


class SettingsError(Exception):
    """Raised when local/bench.toml is missing or incomplete."""


@dataclass(frozen=True)
class Settings:
    recordings_dir: Path
    recording_script: Path
    reference_config: Path
    manifest: Path
    expected_takes: int = DEFAULT_EXPECTED_TAKES
    # Dataset shape: the commands set uses pt-NN ids and plain phrases.
    name: str = "commands"
    id_prefix: str = "pt"
    min_takes: int | None = None
    markup: bool = False
    # When true, script rows not recorded yet are pending instead of invalid.
    allow_pending: bool = False
    # Placeholder -> project name shown while recording (dictation set only).
    projects: tuple[tuple[str, str], ...] | None = None
    dictation: Settings | None = None
    # Spoken rewrite instructions for command mode (commands set only).
    rewrite: Settings | None = None
    # MME input device name for bench.record; None falls back to the reference config.
    recorder_device: str | None = None

    @property
    def minimum_takes(self) -> int:
        return self.min_takes if self.min_takes is not None else self.expected_takes

    def for_set(self, name: str) -> Settings:
        if name == self.name:
            return self
        for extra in (self.dictation, self.rewrite):
            if extra is not None and name == extra.name:
                return extra
        raise SettingsError(f"unknown dataset set: {name}")


def _positive(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SettingsError(f"benchmark settings: {key} must be a positive integer")
    return value


def _repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root.resolve() or root.resolve() in resolved.parents


def _script_set(data: dict, section: str, reference_config: Path, *, recordings: Path, script: Path, prefix: str,
                expected: int, minimum: int, projects: bool) -> Settings:
    """A set recorded with bench.record from a committed script: [dictation] or [rewrite]."""
    table = data.get(section, {})
    if not isinstance(table, dict):
        raise SettingsError(f"benchmark settings: [{section}] must be a table")

    def text(key: str) -> str | None:
        value = table.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SettingsError(f"benchmark settings: [{section}].{key} must be a non-empty string")
        return value

    recordings_dir = _repo_path(text("recordings_dir")) if text("recordings_dir") else recordings
    if not _inside(recordings_dir, LOCAL_DIR):
        raise SettingsError(f"benchmark settings: [{section}].recordings_dir must be under the ignored local/ folder")
    script = _repo_path(text("recording_script")) if text("recording_script") else script
    prefix = text("id_prefix") or prefix
    if not ID_PREFIX.match(prefix):
        raise SettingsError(f"benchmark settings: [{section}].id_prefix must be 2-8 lowercase letters")
    expected = _positive(table.get("expected_takes", expected), f"[{section}].expected_takes")
    minimum = _positive(table.get("min_takes", minimum), f"[{section}].min_takes")
    if minimum > expected:
        raise SettingsError(f"benchmark settings: [{section}].min_takes cannot exceed expected_takes")
    mapping: tuple[tuple[str, str], ...] | None = None
    table_projects = table.get("projects") if projects else None
    if table_projects is not None:
        if not isinstance(table_projects, dict):
            raise SettingsError(f"benchmark settings: [{section}.projects] must be a table")
        for key, value in table_projects.items():
            if not PLACEHOLDER_KEY.match(key) or not isinstance(value, str) or not value.strip():
                raise SettingsError(f"benchmark settings: [{section}.projects] maps \"<projeto-N>\" to a project name")
        mapping = tuple(sorted((key, value.strip()) for key, value in table_projects.items()))
    return Settings(
        recordings_dir=recordings_dir,
        recording_script=script,
        reference_config=reference_config,
        manifest=recordings_dir / DEFAULT_MANIFEST_NAME,
        expected_takes=expected,
        name=section,
        id_prefix=prefix,
        min_takes=minimum,
        markup=True,
        allow_pending=True,
        projects=mapping,
    )


def _dictation(data: dict, reference_config: Path) -> Settings:
    return _script_set(data, "dictation", reference_config, recordings=DICTATION_RECORDINGS, script=DICTATION_SCRIPT,
                       prefix=DICTATION_PREFIX, expected=DICTATION_EXPECTED_TAKES, minimum=DICTATION_MIN_TAKES,
                       projects=True)


def _rewrite(data: dict, reference_config: Path) -> Settings:
    return _script_set(data, "rewrite", reference_config, recordings=REWRITE_RECORDINGS, script=REWRITE_SCRIPT,
                       prefix=REWRITE_PREFIX, expected=REWRITE_EXPECTED_TAKES, minimum=REWRITE_MIN_TAKES,
                       projects=False)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings; error messages name the missing key, never its value."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG
    if not config_path.is_file():
        raise SettingsError(
            "benchmark settings not found: create local/bench.toml from bench/bench.example.toml"
        )
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"invalid TOML in benchmark settings: {exc}") from None

    paths = data.get("paths")
    if not isinstance(paths, dict):
        raise SettingsError("benchmark settings need a [paths] table")

    def required(key: str) -> Path:
        value = paths.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SettingsError(f"benchmark settings: [paths].{key} is required")
        return Path(value)

    recordings_dir = required("recordings_dir")
    manifest_value = paths.get("manifest")
    if manifest_value is not None and not isinstance(manifest_value, str):
        raise SettingsError("benchmark settings: [paths].manifest must be a string")
    manifest = Path(manifest_value) if manifest_value else recordings_dir / DEFAULT_MANIFEST_NAME

    dataset = data.get("dataset", {})
    expected = dataset.get("expected_takes", DEFAULT_EXPECTED_TAKES) if isinstance(dataset, dict) else None
    if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
        raise SettingsError("benchmark settings: [dataset].expected_takes must be a positive integer")

    recorder = data.get("recorder", {})
    if not isinstance(recorder, dict):
        raise SettingsError("benchmark settings: [recorder] must be a table")
    device = recorder.get("device")
    if device is not None and (not isinstance(device, str) or not device.strip()):
        raise SettingsError("benchmark settings: [recorder].device must be a non-empty string")

    reference_config = required("reference_config")
    return Settings(
        recordings_dir=recordings_dir,
        recording_script=required("recording_script"),
        reference_config=reference_config,
        manifest=manifest,
        expected_takes=expected,
        dictation=_dictation(data, reference_config),
        rewrite=_rewrite(data, reference_config),
        recorder_device=device.strip() if isinstance(device, str) else None,
    )
