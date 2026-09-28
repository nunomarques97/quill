"""Preflight: list everything the benchmark still needs, with the exact fix.

Checks the ignored virtual environment and its pinned packages
(bench/requirements.txt), the two local faster-whisper models in models/,
Ollama with qwen3:8b, and the API keys in .env. Every missing item comes with
the exact command and a one-line reason or, for keys, numbered steps for the
Sponsor. Nothing is installed, downloaded or created here, and key values are
never printed: only whether each key name has a value.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bench.cleanup import CLEANUP_MODEL, OllamaClient, OllamaError
from bench.engines import local_whisper
from bench.envfile import ENV_FILE, KEY_NAMES, EnvFileError, load_keys
from bench.settings import REPO_ROOT

REQUIREMENTS = REPO_ROOT / "bench" / "requirements.txt"
VENV_DIR = REPO_ROOT / ".venv"
VENV_PYTHON = r".venv\Scripts\python"
VENV_HF = r".venv\Scripts\hf"
MODEL_REPOS = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
# Prints the installed version of each distribution name given as argument.
VERSION_PROBE = (
    "import json, sys\n"
    "from importlib import metadata\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        out[name] = metadata.version(name)\n"
    "    except metadata.PackageNotFoundError:\n"
    "        out[name] = None\n"
    "print(json.dumps(out))\n"
)
PASTE_STEPS = [
    "In the Quill folder, if there is no file named .env, copy .env.example and name the copy .env.",
    "Open .env in a text editor, paste the key right after {name}= on the same line "
    "(no spaces, no quotes) and save the file.",
    "Run py -3.12 -m bench.run --preflight again: the line for {name} must show [ok].",
]
KEY_STEPS = {
    "GROQ_API_KEY": [
        "Open https://console.groq.com in the browser and sign up for a free account (no card is needed).",
        "Open https://console.groq.com/keys, click Create API Key, name it quill-bench and copy the key.",
    ],
    "GEMINI_API_KEY": [
        "Open https://aistudio.google.com/apikey in the browser and sign in with your Google account.",
        "Click Create API key, accept the terms and copy the key. Do not turn on billing: "
        "the benchmark uses only the free tier.",
    ],
    "DEEPGRAM_API_KEY": [
        "Open https://console.deepgram.com/signup in the browser and create a free account "
        "(it comes with free welcome credit, no card is needed).",
        "In the Deepgram console open API Keys, click Create a New API Key, name it quill-bench, "
        "keep the default permissions and copy the key.",
    ],
}


@dataclass(frozen=True)
class Item:
    name: str
    ok: bool
    detail: str = ""
    command: str | None = None
    reason: str | None = None
    steps: tuple[str, ...] = field(default=())


def parse_requirements(path: Path = REQUIREMENTS) -> list[tuple[str, str]]:
    """Pinned ``name==version`` lines; options and comments are ignored."""
    pins = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, sep, version = line.partition("==")
        if not sep:
            raise ValueError("bench/requirements.txt must pin every package with ==")
        pins.append((name.strip(), version.strip()))
    return pins


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def probe_versions(python: Path, names: list[str], run: Callable = subprocess.run) -> dict[str, str | None]:
    """Installed versions inside the virtual environment (None when absent)."""
    try:
        completed = run([str(python), "-c", VERSION_PROBE, *names], capture_output=True, text=True, timeout=60)
        data = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {name: None for name in names}
    return {name: data.get(name) if isinstance(data, dict) else None for name in names}


def check_packages(venv_dir: Path, requirements: Path, run: Callable = subprocess.run) -> list[Item]:
    pins = parse_requirements(requirements)
    install = f"{VENV_PYTHON} -m pip install -r bench\\requirements.txt"
    python = venv_python(venv_dir)
    items: list[Item] = []
    if not python.is_file():
        items.append(
            Item(
                "virtual environment .venv",
                False,
                "missing",
                "py -3.12 -m venv .venv",
                "Isolated Python 3.12 environment for the local engines, so nothing is installed globally.",
            )
        )
        items.append(
            Item(
                "Python packages",
                False,
                ", ".join(f"{n}=={v}" for n, v in pins) + " not installed",
                install,
                "faster-whisper with the CUDA 12.8 libraries for the RTX 5060 Ti, pinned in bench/requirements.txt.",
            )
        )
        return items
    items.append(Item("virtual environment .venv", True, "present"))
    installed = probe_versions(python, [name for name, _ in pins], run)
    wrong = [f"{n}=={v} ({'missing' if not installed.get(n) else 'found ' + installed[n]})" for n, v in pins if installed.get(n) != v]
    if wrong:
        items.append(
            Item(
                "Python packages",
                False,
                "; ".join(wrong),
                install,
                "Installs the pinned faster-whisper stack for the local engines into .venv.",
            )
        )
    else:
        items.append(Item("Python packages", True, ", ".join(f"{n}=={v}" for n, v in pins)))
    return items


def check_models(models_dir: Path) -> list[Item]:
    items = []
    for model, folder in local_whisper.MODELS.items():
        name = f"local model {folder}"
        if local_whisper.model_present(model, models_dir):
            items.append(Item(name, True, f"models/{folder}"))
        else:
            items.append(
                Item(
                    name,
                    False,
                    f"models/{folder} missing or incomplete",
                    f"{VENV_HF} download {MODEL_REPOS[model]} --local-dir models\\{folder}",
                    f"CTranslate2 Whisper {model} weights (about "
                    f"{'3.1' if model == 'large-v3' else '1.6'} GB) into the ignored models/ folder; "
                    "the benchmark never downloads models itself.",
                )
            )
    return items


def check_ollama(client: OllamaClient) -> list[Item]:
    try:
        version = client.version()
        installed = client.installed()
    except OllamaError:
        return [
            Item(
                "Ollama",
                False,
                "not reachable at 127.0.0.1:11434",
                "ollama serve",
                "Cleanup and the intent judge run on the local Ollama server; start it and keep it running.",
            )
        ]
    items = [Item("Ollama", True, f"version {version}")]
    if CLEANUP_MODEL in installed:
        items.append(Item(f"Ollama model {CLEANUP_MODEL}", True, "installed"))
    else:
        items.append(
            Item(
                f"Ollama model {CLEANUP_MODEL}",
                False,
                "not installed",
                f"ollama pull {CLEANUP_MODEL}",
                "Local model for text cleanup and the intent judge (about 5.2 GB); other models are left untouched.",
            )
        )
    return items


def check_keys(env_path: Path) -> list[Item]:
    try:
        keys = load_keys(env_path)
    except EnvFileError as exc:
        return [
            Item(
                ".env file",
                False,
                str(exc),
                steps=(
                    "Open .env in a text editor.",
                    "Make every line either empty, a comment starting with #, or NAME=value.",
                    "Save the file and run py -3.12 -m bench.run --preflight again.",
                ),
            )
        ]
    items = []
    for name in KEY_NAMES:
        if keys.get(name):
            items.append(Item(name, True, "set in .env (value not shown)"))
        else:
            steps = tuple(KEY_STEPS[name]) + tuple(step.format(name=name) for step in PASTE_STEPS)
            items.append(Item(name, False, "missing from .env", steps=steps))
    return items


def collect(
    *,
    venv_dir: Path = VENV_DIR,
    requirements: Path = REQUIREMENTS,
    models_dir: Path = local_whisper.MODELS_DIR,
    env_path: Path = ENV_FILE,
    client: OllamaClient | None = None,
    run: Callable = subprocess.run,
) -> list[Item]:
    return [
        *check_packages(venv_dir, requirements, run),
        *check_models(models_dir),
        *check_ollama(client or OllamaClient(timeout_s=10.0)),
        *check_keys(env_path),
    ]


def render(items: list[Item]) -> str:
    lines = ["Quill benchmark preflight (run every command from the repository folder)", ""]
    for item in items:
        lines.append(f"[{'ok' if item.ok else 'missing'}] {item.name}: {item.detail}")
        if item.ok:
            continue
        if item.command:
            lines.append(f"    command: {item.command}")
        if item.reason:
            lines.append(f"    reason:  {item.reason}")
        for number, step in enumerate(item.steps, start=1):
            lines.append(f"    {number}. {step}")
    missing = [item for item in items if not item.ok]
    lines.append("")
    if missing:
        lines.append(f"{len(missing)} item(s) missing. Nothing was installed or changed.")
    else:
        lines.append("Everything is in place.")
    lines.append(f"Run the benchmark with: {VENV_PYTHON} -m bench.run")
    return "\n".join(lines)


def main() -> int:
    print(render(collect()))
    return 0
