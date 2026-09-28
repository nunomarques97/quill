"""Keys come only from .env and never appear in messages, reprs or outputs."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from bench.engines import create_engines
from bench.engines.base import EngineError, Transport
from bench.envfile import KEY_NAMES, EnvFileError, Keys, load_keys, parse_env, redact
from bench.tests.fakes import FAKE_KEY, FakeServer, json_reply, tone_wav


class ParseTest(unittest.TestCase):
    def test_comments_quotes_and_export(self) -> None:
        text = '# comment\n\nGROQ_API_KEY="abc12345"\nexport GEMINI_API_KEY=\'def67890\'\nDEEPGRAM_API_KEY=\n'
        self.assertEqual(
            parse_env(text), {"GROQ_API_KEY": "abc12345", "GEMINI_API_KEY": "def67890", "DEEPGRAM_API_KEY": ""}
        )

    def test_bad_line_error_has_no_value(self) -> None:
        with self.assertRaises(EnvFileError) as caught:
            parse_env("GROQ_API_KEY=ok\n" + FAKE_KEY + "\n")
        self.assertIn("line 2", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.env = Path(self.folder.name) / ".env"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_missing_file_means_no_keys(self) -> None:
        self.assertEqual(load_keys(self.env).missing(), list(KEY_NAMES))

    def test_only_known_names_from_the_file(self) -> None:
        self.env.write_text("GROQ_API_KEY=" + FAKE_KEY + "\nOTHER=x\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "value-from-the-process-environment"}):
            keys = load_keys(self.env)
        self.assertEqual(keys.present(), ["GROQ_API_KEY"])
        self.assertIsNone(keys.get("GEMINI_API_KEY"))
        self.assertNotIn("OTHER", keys.values)

    def test_repr_lists_names_only(self) -> None:
        keys = Keys({"GROQ_API_KEY": FAKE_KEY})
        self.assertEqual(repr(keys), "Keys(present=['GROQ_API_KEY'])")
        self.assertNotIn(FAKE_KEY, str(keys))
        engine = create_engines(keys, send=lambda r, t: None, only={"groq-whisper-large-v3"})[0].engine
        self.assertNotIn(FAKE_KEY, repr(engine))


class RedactionTest(unittest.TestCase):
    def test_redact_longest_first_and_skips_short_values(self) -> None:
        self.assertEqual(redact(f"a {FAKE_KEY} b {FAKE_KEY[:12]}", [FAKE_KEY[:12], FAKE_KEY]), "a [redacted] b [redacted]")
        self.assertEqual(redact("pt pt", ["pt"]), "pt pt")

    def test_error_paths_never_show_the_key(self) -> None:
        cases = [
            json_reply({"error": {"message": f"Invalid API Key {FAKE_KEY}"}}, 401),
            json_reply({"error": f"bad {FAKE_KEY}"}, 403),
            json_reply({"err_msg": f"invalid credentials {FAKE_KEY}"}, 400),
        ]
        for reply in cases:
            with self.subTest(status=reply[0]), FakeServer(lambda request, r=reply: r) as server:
                slots = create_engines(
                    Keys({name: FAKE_KEY for name in KEY_NAMES}), send=server.send, sleep=lambda s: None
                )
                for slot in slots[2:]:
                    with self.assertRaises(EngineError) as caught:
                        slot.engine.transcribe(tone_wav(1), None)
                    message = str(caught.exception)
                    self.assertNotIn(FAKE_KEY, message)
                    self.assertNotIn(FAKE_KEY, repr(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    context = caught.exception.__context__
                    self.assertTrue(context is None or FAKE_KEY not in str(context))

    def test_key_not_in_url_or_logs_of_a_failing_run(self) -> None:
        def fail(request, timeout):
            raise OSError(f"proxy said no to {request.full_url} {FAKE_KEY}")

        transport = Transport([FAKE_KEY], send=fail, sleep=lambda s: None, max_attempts=1)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                transport.request("POST", "https://api.groq.com/openai/v1/audio/transcriptions", {}, b"")
            except EngineError as exc:
                print(exc)
        self.assertNotIn(FAKE_KEY, out.getvalue() + err.getvalue())
        self.assertIn("network error contacting api.groq.com", out.getvalue())


if __name__ == "__main__":
    unittest.main()
