"""bench.run --preflight lists every missing item with its fix, offline."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bench import preflight
from bench.cleanup import OllamaClient
from bench.envfile import KEY_NAMES
from bench.run import main as run_main
from bench.tests.fakes import FAKE_KEY, FakeOllama

MISSING_OLLAMA = "http://127.0.0.1:9"


def versions(found: dict):
    def run(args, **kwargs):
        names = args[3:]
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({n: found.get(n) for n in names}), stderr="")

    return run


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.venv = self.root / ".venv"
        self.models = self.root / "models"
        self.env = self.root / ".env"
        self.pins = dict(preflight.parse_requirements())

    def tearDown(self) -> None:
        self.folder.cleanup()

    def collect(self, client: OllamaClient, run=None) -> list[preflight.Item]:
        return preflight.collect(
            venv_dir=self.venv,
            models_dir=self.models,
            env_path=self.env,
            client=client,
            run=run or versions({}),
        )

    def make_venv(self) -> None:
        python = preflight.venv_python(self.venv)
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")

    def make_model(self, folder: str) -> None:
        (self.models / folder).mkdir(parents=True)
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (self.models / folder / name).write_bytes(b"x")

    def test_requirements_are_pinned(self) -> None:
        self.assertEqual(
            set(self.pins), {"faster-whisper", "ctranslate2", "numpy", "torch", "huggingface_hub"}
        )
        self.assertEqual(self.pins["torch"], "2.9.1+cu128")
        text = preflight.REQUIREMENTS.read_text(encoding="utf-8")
        self.assertIn("--extra-index-url https://download.pytorch.org/whl/cu128", text)

    def test_everything_missing(self) -> None:
        with FakeOllama(installed=["qwen3:14b"]) as ollama:
            items = self.collect(OllamaClient(ollama.base_url))
        missing = {item.name: item for item in items if not item.ok}
        self.assertEqual(missing["virtual environment .venv"].command, "py -3.12 -m venv .venv")
        self.assertEqual(
            missing["Python packages"].command, r".venv\Scripts\python -m pip install -r bench\requirements.txt"
        )
        self.assertEqual(
            missing["local model faster-whisper-large-v3-turbo"].command,
            r".venv\Scripts\hf download mobiuslabsgmbh/faster-whisper-large-v3-turbo "
            r"--local-dir models\faster-whisper-large-v3-turbo",
        )
        self.assertEqual(missing["Ollama model qwen3:8b"].command, "ollama pull qwen3:8b")
        for name in KEY_NAMES:
            steps = missing[name].steps
            self.assertGreaterEqual(len(steps), 4)
            self.assertTrue(any(f"{name}=" in step for step in steps))
            self.assertTrue(steps[0].startswith("Open https://"))
        for item in missing.values():
            self.assertTrue(item.steps or (item.command and item.reason), item.name)

    def test_complete_setup_is_all_ok(self) -> None:
        self.make_venv()
        self.make_model("faster-whisper-large-v3")
        self.make_model("faster-whisper-large-v3-turbo")
        self.env.write_text("".join(f"{name}={FAKE_KEY}\n" for name in KEY_NAMES), encoding="utf-8")
        with FakeOllama() as ollama:
            items = self.collect(OllamaClient(ollama.base_url), versions(self.pins))
        self.assertEqual([item.name for item in items if not item.ok], [])
        report = preflight.render(items)
        self.assertIn("Everything is in place.", report)
        self.assertNotIn(FAKE_KEY, report)

    def test_wrong_package_versions_are_listed(self) -> None:
        self.make_venv()
        found = dict(self.pins, torch="2.9.1+cpu")
        del found["numpy"]
        with FakeOllama() as ollama:
            items = self.collect(OllamaClient(ollama.base_url), versions(found))
        packages = next(item for item in items if item.name == "Python packages")
        self.assertFalse(packages.ok)
        self.assertIn("torch==2.9.1+cu128 (found 2.9.1+cpu)", packages.detail)
        self.assertIn("numpy==2.4.6 (missing)", packages.detail)

    def test_ollama_unreachable_and_key_values_never_printed(self) -> None:
        self.env.write_text("GROQ_API_KEY=" + FAKE_KEY + "\n", encoding="utf-8")
        items = self.collect(OllamaClient(MISSING_OLLAMA, timeout_s=2))
        ollama = next(item for item in items if item.name == "Ollama")
        self.assertEqual((ollama.ok, ollama.command), (False, "ollama serve"))
        report = preflight.render(items)
        self.assertIn("[ok] GROQ_API_KEY: set in .env (value not shown)", report)
        self.assertIn("[missing] GEMINI_API_KEY", report)
        self.assertNotIn(FAKE_KEY, report)
        self.assertIn("Nothing was installed or changed.", report)

    def test_unreadable_env_line_gets_steps(self) -> None:
        self.env.write_text(FAKE_KEY + "\n", encoding="utf-8")
        items = preflight.check_keys(self.env)
        self.assertEqual(items[0].name, ".env file")
        self.assertTrue(items[0].steps)
        self.assertNotIn(FAKE_KEY, preflight.render(items))

    def test_run_flag_prints_and_exits_zero(self) -> None:
        items = [preflight.Item("GROQ_API_KEY", False, "missing from .env", steps=("step one",))]
        out = io.StringIO()
        with mock.patch.object(preflight, "collect", return_value=items), redirect_stdout(out):
            code = run_main(["--preflight"])
        self.assertEqual(code, 0)
        self.assertIn("1. step one", out.getvalue())


if __name__ == "__main__":
    unittest.main()
