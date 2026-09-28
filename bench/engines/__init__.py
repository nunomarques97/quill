"""Engine registry: every candidate engine behind the ``Engine`` interface.

``create_engines`` returns one entry per engine id. An engine that cannot be
built (for example a key missing from .env) comes back with the reason, so
all of its variants are reported as SKIPPED instead of silently disappearing.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bench.engines import deepgram, gemini, groq, local_whisper
from bench.engines.base import Engine, Sender, Transport
from bench.envfile import Keys

VARIANTS = ("raw", "hints", "raw+cleanup", "hints+cleanup")
BASE_VARIANTS = ("raw", "hints")


@dataclass(frozen=True)
class EngineOptions:
    gemini_model: str = gemini.DEFAULT_MODEL
    gemini_thinking_budget: int | None = 0
    deepgram_language: str = deepgram.DEFAULT_LANGUAGE
    deepgram_keyterm: bool = True


class OptionsError(Exception):
    """Invalid [engines] settings in local/bench.toml."""


def load_engine_options(path: Path | None) -> EngineOptions:
    """Read the optional [engines.gemini] and [engines.deepgram] tables."""
    if path is None or not Path(path).is_file():
        return EngineOptions()
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        raise OptionsError("invalid TOML in benchmark settings") from None
    engines = data.get("engines", {})
    if not isinstance(engines, dict):
        raise OptionsError("[engines] must be a table")
    gem = engines.get("gemini", {})
    dg = engines.get("deepgram", {})
    if not isinstance(gem, dict) or not isinstance(dg, dict):
        raise OptionsError("[engines.gemini] and [engines.deepgram] must be tables")
    model = gem.get("model", gemini.DEFAULT_MODEL)
    if not isinstance(model, str) or not gemini.MODEL_ID.match(model):
        raise OptionsError("[engines.gemini].model must be a model id such as gemini-2.5-flash")
    # thinking_budget = -1 omits thinkingConfig (for models that reject it).
    default_budget = 0 if model == gemini.DEFAULT_MODEL else -1
    budget = gem.get("thinking_budget", default_budget)
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < -1:
        raise OptionsError("[engines.gemini].thinking_budget must be an integer >= -1")
    language = dg.get("language", deepgram.DEFAULT_LANGUAGE)
    if not isinstance(language, str) or not language.replace("-", "").isalpha():
        raise OptionsError("[engines.deepgram].language must be a language code such as pt-PT")
    keyterm = dg.get("keyterm", True)
    if not isinstance(keyterm, bool):
        raise OptionsError("[engines.deepgram].keyterm must be true or false")
    return EngineOptions(
        gemini_model=model,
        gemini_thinking_budget=None if budget == -1 else budget,
        deepgram_language=language,
        deepgram_keyterm=keyterm,
    )


@dataclass(frozen=True)
class EngineSlot:
    """An engine id with its engine, or the reason it cannot run."""

    id: str
    engine: Engine | None
    unavailable: str | None = None


def engine_ids(options: EngineOptions | None = None) -> list[str]:
    options = options or EngineOptions()
    return [
        "faster-whisper-large-v3",
        "faster-whisper-large-v3-turbo",
        "groq-whisper-large-v3",
        "groq-whisper-large-v3-turbo",
        f"gemini-{options.gemini_model}",
        "deepgram-nova-3",
    ]


def create_engines(
    keys: Keys,
    options: EngineOptions | None = None,
    *,
    send: Sender | None = None,
    sleep: Callable[[float], None] | None = None,
    models_dir: Path = local_whisper.MODELS_DIR,
    only: set[str] | None = None,
) -> list[EngineSlot]:
    """Build every engine; ``send``/``sleep`` are injected by offline tests."""
    options = options or EngineOptions()
    secrets = keys.secret_values()

    def transport(min_interval_s: float) -> Transport:
        extra = {"sleep": sleep} if sleep is not None else {}
        return Transport(secrets, send=send, min_interval_s=min_interval_s, **extra)

    slots: list[EngineSlot] = []
    for model in local_whisper.MODELS:
        slots.append(EngineSlot(f"faster-whisper-{model}", local_whisper.LocalWhisperEngine(model, models_dir)))
    for model in groq.MODELS:
        key = keys.get("GROQ_API_KEY")
        engine_id = f"groq-{model}"
        if key:
            slots.append(EngineSlot(engine_id, groq.GroqEngine(model, key, transport(groq.MIN_INTERVAL_S))))
        else:
            slots.append(EngineSlot(engine_id, None, "GROQ_API_KEY missing from .env"))
    gemini_id = f"gemini-{options.gemini_model}"
    key = keys.get("GEMINI_API_KEY")
    if key:
        engine = gemini.GeminiEngine(
            key, transport(gemini.MIN_INTERVAL_S), options.gemini_model, options.gemini_thinking_budget
        )
        slots.append(EngineSlot(gemini_id, engine))
    else:
        slots.append(EngineSlot(gemini_id, None, "GEMINI_API_KEY missing from .env"))
    key = keys.get("DEEPGRAM_API_KEY")
    if key:
        engine = deepgram.DeepgramEngine(key, transport(0.0), options.deepgram_language, options.deepgram_keyterm)
        slots.append(EngineSlot(deepgram.DeepgramEngine.id, engine))
    else:
        slots.append(EngineSlot(deepgram.DeepgramEngine.id, None, "DEEPGRAM_API_KEY missing from .env"))
    if only is not None:
        slots = [slot for slot in slots if slot.id in only]
    return slots
