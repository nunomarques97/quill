"""GPU memory and Ollama snapshots around each Ollama phase.

Reads free VRAM with ``nvidia-smi`` and the loaded models with Ollama
/api/ps. Only reads: nothing is unloaded to make room. A snapshot showing
qwen3:14b, or any other large model besides the cleanup model, is reported
as VRAM contention.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

from bench.cleanup import CLEANUP_MODEL, OllamaClient, OllamaError

CONTENTION_MODELS = ("qwen3:14b",)
LARGE_MODEL_MIB = 4096
QUERY = ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"]


def query_vram(run: Callable = subprocess.run) -> dict:
    """First GPU's memory in MiB, or {"error": reason}."""
    try:
        completed = run(QUERY, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return {"error": "nvidia-smi not found"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"nvidia-smi failed: {type(exc).__name__}"}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {"error": f"nvidia-smi exited with {completed.returncode}"}
    fields = [part.strip() for part in completed.stdout.strip().splitlines()[0].split(",")]
    try:
        name = fields[0]
        total, used, free = (int(float(value)) for value in fields[1:4])
    except (IndexError, ValueError):
        return {"error": "unexpected nvidia-smi output"}
    return {"gpu": name, "total_mib": total, "used_mib": used, "free_mib": free}


def ollama_models(client: OllamaClient) -> dict:
    try:
        return {"loaded": client.loaded()}
    except OllamaError as exc:
        return {"error": str(exc)}


def contention(loaded: list[dict], own_model: str = CLEANUP_MODEL) -> list[str]:
    """Messages for models that compete with the benchmark for VRAM."""
    messages = []
    for model in loaded:
        name = model.get("name", "")
        if name == own_model:
            continue
        vram = model.get("vram_mib") or 0
        if name in CONTENTION_MODELS or vram >= LARGE_MODEL_MIB:
            messages.append(f"VRAM contention: {name} is loaded by another client ({vram} MiB in VRAM)")
    return messages


def snapshot(label: str, client: OllamaClient, run: Callable = subprocess.run) -> dict:
    """VRAM and Ollama state at one point of the run, with contention messages."""
    models = ollama_models(client)
    return {
        "label": label,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "vram": query_vram(run),
        "ollama": models,
        "contention": contention(models.get("loaded", [])),
    }
