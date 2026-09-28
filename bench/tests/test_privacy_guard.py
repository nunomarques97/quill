"""Privacy guard tests with invented phrases, names and keys.

Sensitive-looking fixtures are assembled at runtime so that this file itself
never matches the guard's patterns.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from bench import privacy_guard as guard

PROJECTS = ("zeta-board", "omegapp")
PERSON = "Ana Exemplar"
SCRIPT = ("liga o painel do <projeto-1> com calma", "liga o painel do zeta-board com calma", "sim envia")

SEP = "\\"
HOME_WIN = "C:" + SEP + "Users" + SEP + "someone" + SEP + "work"
HOME_WIN_FWD = "d:/" + "users/someone"
HOME_JSON = "C:" + SEP * 2 + "Users" + SEP * 2 + "someone"
HOME_UNIX = "/" + "home/someone/src"
GROQ_LIKE = "gsk_" + "a1B2" * 12
GOOGLE_LIKE = "AI" + "za" + "Sy" + "x9" * 17
ASSIGNED = "DEEPGRAM_API_" + "KEY=" + "0f3a" * 10
PRIVATE_BLOCK = "-----BEGIN " + "PRIVATE KEY-----"


def rules():
    return guard.build_rules(PROJECTS, PERSON, SCRIPT)


def categories(text):
    return [(f.line, f.category) for f in guard.scan_text("f.md", text, rules())]


class NameTest(unittest.TestCase):
    def test_project_name_variants(self):
        for text in ("zeta-board", "Zeta_Board", "ZETA BOARD", "zetaboard", "o zeta.board.", "OmegApp"):
            self.assertEqual(categories(text), [(1, "project-name")], text)

    def test_project_name_inside_other_words_is_ignored(self):
        self.assertEqual(categories("zetaboards e omegappx"), [])

    def test_personal_name_whole_and_parts(self):
        self.assertEqual(categories("autor: Ana Exemplar"), [(1, "personal-name")])
        self.assertEqual(categories("por EXEMPLAR"), [(1, "personal-name")])
        # Parts shorter than 4 letters are too generic to match alone.
        self.assertEqual(categories("ana"), [])


class ScriptTextTest(unittest.TestCase):
    def test_four_word_ngram(self):
        self.assertEqual(categories("antes: O painel de Zeta.\n"), [])
        found = categories("nota\n  Liga o PAINEL do agora")
        self.assertEqual(found, [(2, "script-text")])

    def test_ngram_across_lines_reports_first_line(self):
        self.assertEqual(categories("x\nliga o\npainel do\n"), [(2, "script-text")])

    def test_three_words_are_allowed(self):
        self.assertEqual(categories("liga o painel"), [])

    def test_placeholder_form_is_also_caught(self):
        self.assertIn((1, "script-text"), categories("do <projeto-1> com calma"))


class DictationScriptTest(unittest.TestCase):
    """Dictation phrases may appear only in the committed dictation script."""

    DICTATION = (
        "hum abre o painel do <projeto-2> e corre os testes",
        "abre o painel do <projeto-2> e corre os testes",
        "hum abre o painel do omegapp e corre os testes",
        "abre o painel do omegapp e corre os testes",
    )
    EXEMPT = "bench/dictation/guiao.md"

    def rules(self):
        return guard.build_rules(PROJECTS, PERSON, SCRIPT, self.DICTATION, [self.EXEMPT])

    def found(self, path, text):
        return [(f.line, f.category) for f in guard.scan_text(path, text, self.rules())]

    def test_exempt_file_may_hold_raw_dictation_phrases(self):
        self.assertEqual(self.found(self.EXEMPT, "| dt-01 | {hum} abre o painel do <projeto-2> e corre os testes |"), [])

    def test_other_files_are_flagged_raw_and_resolved(self):
        for text in ("abre o painel do <projeto-2>", "Abre o PAINEL do Omegapp", "e corre os testes"):
            self.assertIn((1, "script-text"), self.found("docs/notes.md", text), text)
        self.assertIn((1, "script-text"), self.found("bench/dictation/other.md", "e corre os testes"))

    def test_exempt_file_is_still_checked_for_everything_else(self):
        # The commands script, real names and paths stay forbidden there.
        found = self.found(self.EXEMPT, "liga o painel do <projeto-1>\no omegapp\n" + HOME_UNIX)
        self.assertEqual(found, [(1, "script-text"), (2, "project-name"), (3, "home-path")])

    def test_resolved_phrase_in_exempt_file_is_flagged_by_name(self):
        self.assertIn((1, "project-name"), self.found(self.EXEMPT, "abre o painel do omegapp e corre os testes"))

    def test_committed_script_passes_its_own_exemption(self):
        from bench.dataset import parse_script, strip_markup
        from bench.settings import DICTATION_SCRIPT, REPO_ROOT

        text = DICTATION_SCRIPT.read_text(encoding="utf-8")
        phrases = [form for row in parse_script(text, "dt", markup=True) for form in strip_markup(row.text)]
        path = DICTATION_SCRIPT.relative_to(REPO_ROOT).as_posix()
        rules = guard.build_rules((), None, (), phrases, [path])
        self.assertEqual(guard.scan_text(path, text, rules), [])
        self.assertTrue(guard.scan_text("README.md", phrases[0], rules))


class PatternTest(unittest.TestCase):
    def test_home_paths(self):
        for path in (HOME_WIN, HOME_WIN_FWD, HOME_JSON, HOME_UNIX):
            self.assertEqual(categories(f'root = "{path}"'), [(1, "home-path")], path)

    def test_non_home_paths_allowed(self):
        self.assertEqual(categories("see /path/to/reference-project and C:/Program Files/x"), [])
        self.assertEqual(categories("https://example.com/Users/list"), [])

    def test_api_keys(self):
        for key in (GROQ_LIKE, GOOGLE_LIKE, ASSIGNED, PRIVATE_BLOCK):
            self.assertEqual(categories(key), [(1, "api-key")], key[:4])

    def test_key_names_without_values_allowed(self):
        self.assertEqual(categories("GROQ_API_KEY=\nDEEPGRAM_API_KEY = load_from_environment_file(path)"), [])

    def test_output_never_shows_matched_text(self):
        text = "\n".join((GROQ_LIKE, HOME_WIN, "zeta-board", PERSON, SCRIPT[1]))
        lines = [str(f) for f in guard.scan_text("notes.md", text, rules())]
        self.assertEqual(
            lines,
            [
                "notes.md:1: api-key",
                "notes.md:2: home-path",
                "notes.md:3: project-name",
                "notes.md:4: personal-name",
                "notes.md:5: project-name",
                "notes.md:5: script-text",
            ],
        )
        joined = "\n".join(lines)
        for secret_part in (GROQ_LIKE, "someone", "zeta", "Ana", "painel"):
            self.assertNotIn(secret_part, joined)


class AudioTest(unittest.TestCase):
    def test_audio_by_extension_and_signature(self):
        self.assertTrue(guard.is_audio("a/take.WAV", b""))
        self.assertTrue(guard.is_audio("a/take.flac", b""))
        self.assertTrue(guard.is_audio("a/data.bin", b"RIFF\x00\x00\x00\x00WAVEfmt "))
        self.assertTrue(guard.is_audio("a/data.bin", b"ID3\x04"))
        self.assertTrue(guard.is_audio("a/data.bin", b"OggS\x00"))
        self.assertFalse(guard.is_audio("a/readme.md", b"# title"))


@unittest.skipUnless(shutil.which("git"), "git is required")
class RepositoryScanTest(unittest.TestCase):
    def test_tracked_and_untracked_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitignore").write_text("private/\n", encoding="utf-8")
            (root / "private").mkdir()
            (root / "private" / "notes.md").write_text("zeta-board\n", encoding="utf-8")
            (root / "tracked.md").write_text("ok\nomegapp\n", encoding="utf-8")
            (root / "new.txt").write_text(HOME_UNIX + "\n", encoding="utf-8")
            (root / "clip.bin").write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
            (root / "blob.dat").write_bytes(b"\x00\x01zeta-board")
            subprocess.run(["git", "add", "tracked.md"], cwd=root, check=True)

            files = guard.list_files(root)
            self.assertIn("tracked.md", files)
            self.assertIn("new.txt", files)
            self.assertNotIn("private/notes.md", files)

            found = [str(f) for f in guard.scan(root, files, rules())]
            self.assertEqual(
                found,
                ["clip.bin:0: audio-file", "new.txt:1: home-path", "tracked.md:2: project-name"],
            )


if __name__ == "__main__":
    unittest.main()
