"""quill.whisper.Whisper.transcribe against a fake faster_whisper module: no GPU, no model, no audio device."""

from __future__ import annotations

import importlib
import importlib.machinery
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from quill import whisper

# The keyword arguments every decode passed before the decode knobs existed (Phase 2 baseline).
TODAY = {"temperature": 0.0, "vad_filter": False, "condition_on_previous_text": False}


class FakeWhisperModel:
    """Records each ``transcribe`` call and returns one invented segment."""

    def __init__(self, path: str, **kwargs: object) -> None:
        self.path = path
        self.kwargs = kwargs
        self.calls: list[dict] = []
        self.detected = "en"

    def transcribe(self, audio: object, **kwargs: object) -> tuple[object, object]:
        self.calls.append(kwargs)
        language = kwargs.get("language") or self.detected
        word = types.SimpleNamespace(word=" zorblax", start=0.0, end=0.4)
        segment = types.SimpleNamespace(text=" zorblax  alfa ", words=[word] if kwargs.get("word_timestamps") else None)
        return iter([segment]), types.SimpleNamespace(language=language, language_probability=0.9)


def fake_module() -> types.ModuleType:
    module = types.ModuleType("faster_whisper")
    module.__spec__ = importlib.machinery.ModuleSpec("faster_whisper", None)
    module.WhisperModel = FakeWhisperModel
    return module


class TranscribeTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        models = Path(folder.name)
        target = whisper.model_dir("large-v3-turbo", models)
        target.mkdir(parents=True)
        for name in whisper.MODEL_FILES:
            (target / name).write_text("{}", encoding="utf-8")
        # numpy is imported for real, before the patch: a C extension cannot be imported twice in one process.
        importlib.import_module("numpy")
        patcher = mock.patch.dict(sys.modules, {"faster_whisper": fake_module()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.model = whisper.Whisper("large-v3-turbo", models, device="cpu", language="pt")
        self.pcm = b"\x00\x00" * 1600

    def call(self, options: whisper.Decode = whisper.Decode()) -> tuple[whisper.Transcript, dict]:
        transcript = self.model.transcribe(self.pcm, options)
        return transcript, self.model._whisper.calls[-1]

    def test_defaults_pass_exactly_todays_kwargs(self) -> None:
        transcript, kwargs = self.call()
        self.assertEqual(kwargs, {"language": "pt", "beam_size": 5, **TODAY, "initial_prompt": None,
                                  "hotwords": None})
        self.assertEqual(transcript.text, "zorblax alfa")
        self.assertEqual(transcript.language, "pt")

    def test_defaults_keep_the_named_language_and_the_other_options(self) -> None:
        options = whisper.Decode(beam_size=1, initial_prompt="Vocabulário: zorblax.", hotwords="zorblax",
                                 word_timestamps=True, without_timestamps=True, language="en")
        transcript, kwargs = self.call(options)
        self.assertEqual(kwargs, {"language": "en", "beam_size": 1, **TODAY,
                                  "initial_prompt": "Vocabulário: zorblax.", "hotwords": "zorblax",
                                  "word_timestamps": True, "without_timestamps": True})
        self.assertEqual([word.text for word in transcript.words], ["zorblax"])
        self.assertEqual(transcript.language, "en")

    def test_detect_language_lets_the_model_choose(self) -> None:
        transcript, kwargs = self.call(whisper.Decode(language="pt", detect_language=True))
        self.assertIsNone(kwargs["language"])
        self.assertEqual(transcript.language, "en")
        self.assertEqual({key: kwargs[key] for key in TODAY}, TODAY)

    def test_temperature_fallback_uses_the_schedule(self) -> None:
        _, kwargs = self.call(whisper.Decode(temperature_fallback=True))
        self.assertEqual(kwargs["temperature"], list(whisper.FALLBACK_TEMPERATURES))
        self.assertEqual(whisper.FALLBACK_TEMPERATURES[0], 0.0)
        self.assertEqual((kwargs["vad_filter"], kwargs["condition_on_previous_text"]), (False, False))

    def test_condition_and_vad_filter_are_passed_through(self) -> None:
        _, kwargs = self.call(whisper.Decode(condition_on_previous_text=True, vad_filter=True))
        self.assertEqual((kwargs["condition_on_previous_text"], kwargs["vad_filter"], kwargs["temperature"]),
                         (True, True, 0.0))
        self.assertEqual(kwargs["language"], "pt")

    def test_missing_detected_language_is_none(self) -> None:
        self.call()
        model = self.model._whisper
        model.transcribe = lambda audio, **kwargs: (iter([]), types.SimpleNamespace())
        transcript = self.model.transcribe(self.pcm, whisper.Decode())
        self.assertEqual((transcript.text, transcript.language), ("", None))

    def test_decode_defaults_are_off(self) -> None:
        options = whisper.Decode()
        self.assertEqual((options.detect_language, options.temperature_fallback, options.condition_on_previous_text,
                          options.vad_filter), (False, False, False, False))

    def test_the_fake_module_is_loaded(self) -> None:
        self.call()
        self.assertIsInstance(self.model._whisper, FakeWhisperModel)
        self.assertEqual(self.model._whisper.kwargs["device"], "cpu")
        self.assertTrue(self.model._whisper.kwargs["local_files_only"])


class CleanupTest(unittest.TestCase):
    def test_fake_module_is_removed_after_each_test(self) -> None:
        before = sys.modules.get("faster_whisper")
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(TranscribeTest).run(result)
        self.assertTrue(result.wasSuccessful())
        self.assertIs(sys.modules.get("faster_whisper"), before)


if __name__ == "__main__":
    unittest.main()
