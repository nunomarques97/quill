"""The project context pack: temporary Git repositories with invented content only.

These tests run the installed ``git`` in temporary folders (``git init`` and
``git add``, never a commit, a network call or the user's repositories).
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quill import config, context_pack
from quill.context_pack import (BUILT, CACHED, FAILED, MAX_SUMMARY_CHARS, MAX_TERMS, NO_FOLDER, NO_GIT,
                                NOT_WORK_TREE, TIMEOUT, ContextPacks, Git, GitError, PackTimeout, cache_name,
                                denied, instructions, parse_markdown, read_text, summary_of)

GIT = shutil.which("git")
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Invented words that must never reach a pack, a cache file or a log.
CANARIES = {
    "ignored": "zorblaxignored",
    "env": "zorblaxenv",
    "env_md": "zorblaxenvmd",
    "token": "zorblaxtoken",
    "secret": "zorblaxsecret",
    "credentials": "zorblaxcredentials",
    "wav": "zorblaxwav",
    "recordings": "zorblaxrecordings",
    "local": "zorblaxlocal",
    "junction": "zorblaxjunction",
    "symlink": "zorblaxsymlink",
    "hardlink": "zorblaxhardlink",
    "key": "zorblaxkey",
}

CLAUDE_MD = """# Invented Wallet Desk

Invented Wallet Desk tracks an invented crypto wallet and its trades for one trader.
It is a sample project made up for tests.

## Rules

- Never commit a wallet seed.
- Keep the ledger local.

```bash
run --wallet sample
```

## Details

This second section is not part of the summary.
"""

README_MD = """# Wallet Desk

A made-up readme. The wallet balance shows the ledger, the wallet history and the rebalance plan.
Use `LedgerSync` and `wallet_balance` to follow the wallet; see [the guide](docs/guide.md) or https://example.invalid/x.

## Rebalance

The ledger rebalance runs every hour. The ledger keeps every trade.
"""

GUIDE_MD = """# Guide

## Stop loss

The stop loss closes a trade. Configure `stop_loss_pct` in the settings.
"""

CODE_PY = """class LedgerSync:
    pass


class _Hidden:
    pass


def helper():
    return None
"""


def canary_text(word: str) -> str:
    """A Markdown body that would surely give ``word`` as a term (heading, inline code, repeated)."""
    return f"# {word}\n\n{word} {word} {word} `{word}` {word}.\n\n## {word}\n"


@unittest.skipUnless(GIT, "git is not installed")
class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.repo = self.base / "project"
        self.cache = self.base / "cache"
        self.repo.mkdir()
        self.git("init", "-q")
        self.time = [1_000_000.0]

    def git(self, *args: str) -> None:
        subprocess.run([GIT, "-C", str(self.repo), *args], check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=context_pack.git_environment(),
                       creationflags=FLAGS)

    def write(self, relative: str, text: str | bytes, folder: Path | None = None) -> Path:
        path = (folder or self.repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, "utf-8")
        return path

    def standard(self) -> None:
        self.write("CLAUDE.md", CLAUDE_MD)
        self.write("README.md", README_MD)
        self.write("docs/guide.md", GUIDE_MD)
        self.write("src/ledger_sync.py", CODE_PY)
        self.git("add", ".")

    def packs(self, **kwargs: object) -> ContextPacks:
        kwargs.setdefault("now", lambda: self.time[0])
        kwargs.setdefault("timeout_s", 20.0)
        return ContextPacks(self.cache, **kwargs)

    def cache_files(self) -> list[Path]:
        return sorted(self.cache.iterdir()) if self.cache.exists() else []


class DenyListTest(unittest.TestCase):
    def test_credentials_media_and_private_folders_are_denied_whatever_git_says(self) -> None:
        for path in (".env", ".ENV", ".env.local", "config/.env.production", ".env.md", "keys/server.key",
                     "cert.PEM", "store.jks", "app.keystore", "id.pfx", "id.p12", "google-services.json",
                     "App/Credentials.json", "docs/api-token.md", "docs/Secrets/plan.md", "my_secret.py",
                     "recordings/take.md", "docs/Recordings/notes.md", "local/notes.md", "sub/local/x.md",
                     ".git/description", "voice.wav", "clips/demo.MP4", "a.m4a", "notes.local.md", "CLAUDE.local.md",
                     "captures/x.md", "transcripts/a.md", "id_rsa", ".npmrc", "readme.md:stream", "../outside.md",
                     "/abs.md", "\\abs.md", "C:/abs.md", "docs//x.md", "./x.md", "x.md.", "x.md ", "", "a\0b.md"):
            self.assertTrue(denied(path), path)

    def test_ordinary_files_are_allowed(self) -> None:
        for path in ("README.md", "CLAUDE.md", "docs/guide.md", "src/wallet.py", "docs/local-setup.md",
                     "src/tokenizer_like.py".replace("token", "tok"), "app/secretary.py".replace("secret", "secr")):
            self.assertFalse(denied(path), path)

    def test_only_markdown_and_source_files_are_candidates(self) -> None:
        listed = ["b.md", "a.py", "a.py", "image.png", "notes.txt", ".env", "docs/c.MD", "data.json"]
        self.assertEqual(context_pack.candidates(listed), ["a.py", "b.md", "docs/c.MD"])

    def test_git_runs_without_a_shell_and_without_git_variables(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_DIR": "x", "git_index_file": "y", "GIT_CONFIG_PARAMETERS": "z",
                                          "OTHER": "kept"}):
            env = context_pack.git_environment()
        self.assertFalse([key for key in env if key.upper().startswith("GIT_")])
        self.assertEqual(env["OTHER"], "kept")
        command = Git("C:\\invented\\git.exe").command("C:\\invented\\folder")
        self.assertEqual(command[:6], ["C:\\invented\\git.exe", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                       "-C", "C:\\invented\\folder"])
        self.assertIn("--exclude-standard", command)
        self.assertEqual(command[command.index("--") + 1:], list(context_pack.PATHSPECS))
        self.assertIsNone(Git("git").executable)  # only an absolute path is ever run


class PrivacyTest(RepoCase):
    """Canary words in files that may never be read never reach the pack, the cache or the logs."""

    def plant(self) -> Path:
        self.standard()
        outside = self.base / "outside"
        outside.mkdir()
        self.write(".gitignore", "docs/private.md\n")
        self.write("docs/private.md", canary_text(CANARIES["ignored"]))
        self.write(".env", f"KEY={CANARIES['env']}\n")
        self.write(".env.md", canary_text(CANARIES["env_md"]))
        self.write("docs/api-token.md", canary_text(CANARIES["token"]))
        self.write("docs/client-secret.md", canary_text(CANARIES["secret"]))
        self.write("credentials.json", json.dumps({"key": CANARIES["credentials"]}))
        self.write("server.key", CANARIES["key"])
        self.write("recordings/take.wav", b"RIFF" + CANARIES["wav"].encode() + b"WAVE")
        self.write("recordings/notes.md", canary_text(CANARIES["recordings"]))
        self.write("local/notes.md", canary_text(CANARIES["local"]))
        self.git("add", "-f", ".env", ".env.md", "docs/api-token.md", "docs/client-secret.md", "credentials.json",
                 "server.key", "recordings/take.wav", "recordings/notes.md", "local/notes.md")
        self.write("junction/leak.md", canary_text(CANARIES["junction"]), outside)
        self.write("symlink.md", canary_text(CANARIES["symlink"]), outside)
        self.write("hardlink.md", canary_text(CANARIES["hardlink"]), outside)
        self.links = []
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(outside / "junction"), str(self.repo / "docs" / "linked"))
            self.links.append("docs/linked/leak.md")
        try:
            os.symlink(outside / "symlink.md", self.repo / "docs" / "symlinked.md")
            self.links.append("docs/symlinked.md")
        except (OSError, NotImplementedError):
            pass  # no symlink privilege here: the junction and the hard link still cover links
        os.link(outside / "hardlink.md", self.repo / "docs" / "hardlinked.md")
        self.links.append("docs/hardlinked.md")
        return outside

    def assert_clean(self, text: str, where: str) -> None:
        for name, word in CANARIES.items():
            self.assertNotIn(word, text.casefold(), f"{name} canary in {where}")

    def test_no_canary_reaches_the_pack_the_cache_or_the_logs(self) -> None:
        self.plant()
        with self.assertLogs("quill.context_pack", logging.DEBUG) as logs:
            pack, reason = self.packs().lookup(self.repo)
        self.assertEqual(reason, BUILT)
        self.assertIsNotNone(pack)
        # The pack is built from the allowed files ...
        self.assertIn("wallet", [term.casefold() for term in pack.terms])
        self.assertIn("LedgerSync", pack.terms)
        self.assertEqual(pack.files, 4)
        # ... and from nothing else.
        self.assert_clean(pack.summary + " " + " ".join(pack.terms), "pack")
        [cache] = self.cache_files()
        self.assert_clean(cache.read_text("utf-8"), "cache")
        record = json.loads(cache.read_text("utf-8"))
        self.assertEqual(sorted(entry[0] for entry in record["sources"]),
                         ["CLAUDE.md", "README.md", "docs/guide.md", "src/ledger_sync.py"])
        logged = "\n".join(logs.output)
        self.assert_clean(logged, "logs")
        for private in ("wallet", "ledger", str(self.base), "project"):
            self.assertNotIn(private.casefold(), logged.casefold())

    def test_links_and_denied_names_are_refused_even_when_asked_directly(self) -> None:
        self.plant()
        root = os.path.realpath(self.repo)
        for relative in self.links:
            self.assertIsNone(read_text(root, relative, 4096), relative)
        for relative in (".env", ".env.md", "docs/api-token.md", "recordings/notes.md", "local/notes.md",
                         "recordings/take.wav", "../outside/symlink.md"):
            self.assertIsNone(read_text(root, relative, 4096), relative)
        self.assertIsNotNone(read_text(root, "docs/guide.md", 4096))

    def test_a_linked_project_folder_reads_its_real_files_only(self) -> None:
        self.standard()
        if sys.platform != "win32":
            self.skipTest("junctions are a Windows feature")
        import _winapi

        alias = self.base / "alias"
        _winapi.CreateJunction(str(self.repo), str(alias))
        pack, reason = self.packs().lookup(alias)
        self.assertEqual((reason, pack.files), (BUILT, 4))

    def test_binary_and_invalid_text_files_are_skipped(self) -> None:
        self.write("README.md", README_MD)
        self.write("docs/binary.md", b"# Zorblaxbinary\n\x00\x01")
        self.write("docs/latin.md", "# Zorblaxlatin olá".encode("latin-1"))
        self.git("add", ".")
        pack, _ = self.packs().lookup(self.repo)
        self.assertEqual(pack.files, 1)
        self.assertNotIn("zorblax", " ".join(pack.terms).casefold())


class LimitsTest(RepoCase):
    def test_file_count_and_size_caps(self) -> None:
        self.write("README.md", "# Title\n\n" + "Filler words here. " * 20000)
        for index in range(context_pack.MAX_READ_FILES + 20):
            self.write(f"src/module{index:03d}.py", f"class Invented{index:03d}Thing:\n    pass\n")
        self.git("add", ".")
        pack, _ = self.packs().lookup(self.repo)
        self.assertEqual(pack.files, context_pack.MAX_READ_FILES)
        self.assertLessEqual(len(pack.summary), MAX_SUMMARY_CHARS)
        self.assertLessEqual(len(pack.terms), MAX_TERMS)
        # A long file is read up to the cap only (a cut multi-byte character is dropped, not an error).
        long = self.write("docs/long.md", "é" * (context_pack.MAX_FILE_BYTES))
        text, source = read_text(os.path.realpath(self.repo), "docs/long.md", 1001)
        self.assertEqual((len(text), source.size), (500, long.stat().st_size))


class SummaryTest(unittest.TestCase):
    def test_first_section_prose_only(self) -> None:
        summary = summary_of(parse_markdown(CLAUDE_MD))
        self.assertEqual(summary, "Invented Wallet Desk tracks an invented crypto wallet and its trades for one "
                                  "trader. It is a sample project made up for tests.")

    def test_markup_is_plain_text_and_the_length_is_bounded(self) -> None:
        text = ("---\ntitle: x\n---\n# T\n\n![badge](https://example.invalid/b.svg)\n\nA **bold** [link](x.md) "
                "![icon](i.png)and `code` <b>tag</b> at https://example.invalid/page.\n\n| a | b |\n> quote\n"
                "    indented code\n")
        self.assertEqual(summary_of(parse_markdown(text)), "A bold link and code tag at.")
        long = "# T\n\n" + " ".join(f"Sentence number {index} is here." for index in range(200))
        cut = summary_of(parse_markdown(long))
        self.assertLessEqual(len(cut), MAX_SUMMARY_CHARS)
        self.assertTrue(cut.endswith("."))
        self.assertEqual(summary_of(parse_markdown("# Only\n\n- a rule\n- another\n```\ncode\n```\n")), "")

    def test_setext_headings_and_rules(self) -> None:
        markdown = parse_markdown("Title\n=====\n\nFirst part.\n\n***\n\nLater part.\n")
        self.assertEqual(markdown.headings, ["Title"])
        self.assertEqual(summary_of(markdown), "First part.")

    def test_instruction_and_metadata_paragraphs(self) -> None:
        for text in ("Read AGENTS.md and its map before any work.", "Always run the tests first.",
                     "Nunca faças commit sem testes.", "Lê o guia antes de mexer no código.",
                     "New tasks use the queue. The conversation agent prepares the goal.",
                     "See CLAUDE.md for the rules.", "Sponsor: Someone · Developer: Another one",
                     "Owner: A Person | Team: B Group", "Status: pre-release, private repository.",
                     "Read first: docs/brief.md (what and why).", "Commands:"):
            self.assertTrue(instructions(text), text)
        for text in ("Invented Wallet Desk tracks an invented crypto wallet.", "A local tool for invented files.",
                     "Wallet Desk: a ledger for one trader.", "Aplicação de faturação para pequenas lojas.",
                     "Open-source (MIT), local-first ledger. It keeps every trade.", ""):
            self.assertFalse(instructions(text), text)

    def test_instructions_are_left_out_of_the_summary(self) -> None:
        text = ("# Invented\n\nOwner: Someone · Developer: Another one\n\nInvented Desk keeps an invented ledger."
                "\n\nStatus: draft.\n\nRead first: docs/brief.md.\n\n## Later\n\nNot the summary.\n")
        self.assertEqual(summary_of(parse_markdown(text)), "Invented Desk keeps an invented ledger.")
        # A first section of instructions only gives no summary (never a later section's paragraph).
        opening = ("# Rules\n\nRead AGENTS.md before work. Follow DESIGN.md.\n\n## Desk\n\n"
                   "Invented Desk keeps a ledger.\n")
        self.assertEqual(summary_of(parse_markdown(opening)), "")


class TermsTest(RepoCase):
    def test_sources_of_terms_and_function_words(self) -> None:
        self.standard()
        pack, _ = self.packs().lookup(self.repo)
        terms = [term.casefold() for term in pack.terms]
        for expected in ("wallet", "ledger", "rebalance", "stop", "loss", "ledgersync", "wallet_balance",
                         "stop_loss_pct", "ledger_sync", "guide"):
            self.assertIn(expected, terms)
        for excluded in ("the", "and", "every", "para", "_hidden", "helper", "run"):
            self.assertNotIn(excluded, terms)
        self.assertEqual(len(terms), len(set(terms)))

    def test_summary_falls_back_to_the_readme(self) -> None:
        self.write("CLAUDE.md", "# Rules\n\n- Only rules here.\n")
        self.write("README.md", README_MD)
        self.git("add", ".")
        pack, _ = self.packs().lookup(self.repo)
        self.assertTrue(pack.summary.startswith("A made-up readme."))
        self.assertNotIn("http", pack.summary)

    def test_a_claude_md_opening_with_agent_instructions_gives_the_readme_summary(self) -> None:
        self.write("CLAUDE.md", "# Invented\n\nRead AGENTS.md and its map before work. All work follows "
                                "DESIGN.md.\n\n## Process\n\nNew tasks use the queue. The conversation agent "
                                "prepares the goal.\n")
        self.write("README.md", README_MD)
        self.git("add", ".")
        pack, _ = self.packs().lookup(self.repo)
        self.assertTrue(pack.summary.startswith("A made-up readme."))
        self.assertNotIn("AGENTS", pack.summary)

    def test_case_and_accents_fold_and_the_order_is_deterministic(self) -> None:
        self.write("README.md", "# Configuração\n\nconfiguracao Configuração carteira Carteira carteira também "
                                "também também para para para\n")
        self.git("add", ".")
        first, _ = self.packs().lookup(self.repo, rebuild=True)
        second, _ = self.packs().lookup(self.repo, rebuild=True)
        self.assertEqual(first.terms, second.terms)
        self.assertEqual(first.terms, ("Configuração", "carteira"))

    def test_the_term_list_is_bounded(self) -> None:
        self.write("README.md", "".join(f"## Heading{index:04d}\n" for index in range(MAX_TERMS * 2)))
        self.git("add", ".")
        pack, _ = self.packs().lookup(self.repo)
        self.assertEqual(len(pack.terms), MAX_TERMS)
        self.assertEqual(pack.terms[0], "Heading0000")


class CacheTest(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.standard()

    def test_a_second_lookup_uses_the_cache(self) -> None:
        packs = self.packs()
        built, reason = packs.lookup(self.repo)
        self.assertEqual(reason, BUILT)
        [cache] = self.cache_files()
        self.assertEqual(cache.name, cache_name(str(self.repo)))
        self.assertEqual(cache_name(str(self.repo).upper() + "\\"), cache.name)
        cached, reason = packs.lookup(self.repo)
        self.assertEqual((reason, cached.terms, cached.summary), (CACHED, built.terms, built.summary))
        self.assertEqual(packs.get(self.repo), cached)

    def assert_rebuilt(self, packs: ContextPacks) -> None:
        self.assertEqual(packs.lookup(self.repo)[1], BUILT)
        self.assertEqual(packs.lookup(self.repo)[1], CACHED)

    def test_a_changed_source_file_rebuilds(self) -> None:
        packs = self.packs()
        packs.lookup(self.repo)
        guide = self.repo / "docs" / "guide.md"
        guide.write_text(GUIDE_MD + "\n## Take profit\n", "utf-8")
        self.assert_rebuilt(packs)
        status = guide.stat()
        os.utime(guide, ns=(status.st_atime_ns, status.st_mtime_ns + 5_000_000_000))  # same size, newer
        self.assert_rebuilt(packs)
        self.assertIn("profit", [term.casefold() for term in packs.get(self.repo).terms])

    def test_a_changed_file_list_rebuilds(self) -> None:
        packs = self.packs()
        packs.lookup(self.repo)
        self.write("src/order_book.py", "class OrderBook:\n    pass\n")  # untracked, not ignored: listed
        self.assert_rebuilt(packs)
        self.assertIn("OrderBook", packs.get(self.repo).terms)
        self.write(".gitignore", "src/order_book.py\n")  # now ignored: the list changes again
        self.assert_rebuilt(packs)
        self.assertNotIn("OrderBook", packs.get(self.repo).terms)

    def test_an_old_or_future_cache_rebuilds(self) -> None:
        packs = self.packs(max_age_s=3600.0)
        packs.lookup(self.repo)
        self.time[0] += 3599
        self.assertEqual(packs.lookup(self.repo)[1], CACHED)
        self.time[0] += 2
        self.assert_rebuilt(packs)
        self.time[0] -= 3600  # a clock set back: the cache looks written in the future
        self.assert_rebuilt(packs)

    def test_a_corrupt_cache_rebuilds(self) -> None:
        packs = self.packs()
        packs.lookup(self.repo)
        [cache] = self.cache_files()
        good = json.loads(cache.read_text("utf-8"))
        for broken in (b"not json", b"\xff\xfe", b"[]", b"{}", json.dumps({**good, "version": 99}).encode(),
                       json.dumps({**good, "terms": "wallet"}).encode(),
                       json.dumps({**good, "terms": ["x" * 41]}).encode(),
                       json.dumps({**good, "summary": "x" * (MAX_SUMMARY_CHARS + 1)}).encode(),
                       json.dumps({**good, "sources": [[".env", 1, 1]] * good["files"]}).encode(),
                       json.dumps({**good, "files": True}).encode(),
                       b" " * (context_pack.MAX_CACHE_BYTES + 1)):
            cache.write_bytes(broken)
            self.assertEqual(packs.lookup(self.repo)[1], BUILT, broken[:40])
            self.assertEqual(json.loads(cache.read_text("utf-8"))["version"], context_pack.VERSION)

    def test_writes_are_atomic_and_a_failed_write_keeps_the_pack(self) -> None:
        self.packs().lookup(self.repo)
        self.assertEqual([path.suffix for path in self.cache_files()], [".json"])
        self.cache.joinpath(cache_name(str(self.repo))).unlink()
        self.cache.rmdir()
        self.cache.write_text("a file where the cache folder should be", "utf-8")
        with self.assertLogs("quill.context_pack", logging.WARNING) as logs:
            pack, reason = self.packs().lookup(self.repo)
        self.assertEqual((reason, pack.files), (BUILT, 4))
        self.assertIn(context_pack.WRITE_FAILED, "\n".join(logs.output))
        with mock.patch.object(context_pack.os, "replace", side_effect=OSError("disk")):
            self.cache.unlink()
            self.packs().lookup(self.repo)
        self.assertEqual(self.cache_files(), [])  # the temporary file is removed

    def test_rebuild_ignores_the_cache(self) -> None:
        packs = self.packs()
        packs.lookup(self.repo)
        self.assertEqual(packs.lookup(self.repo, rebuild=True)[1], BUILT)


class FailureTest(RepoCase):
    def test_a_folder_outside_a_git_work_tree_gives_no_pack(self) -> None:
        plain = self.base / "plain"
        self.write("README.md", README_MD, plain)
        other = self.base / "other"
        other.mkdir()
        subprocess.run([GIT, "init", "-q", str(other)], check=True, stdout=subprocess.DEVNULL,
                       env=context_pack.git_environment(), creationflags=FLAGS)
        # A GIT_DIR of the environment is never used for the folder.
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(plain)}):
            self.assertEqual(self.packs().lookup(plain), (None, NOT_WORK_TREE))
        self.assertEqual(self.cache_files(), [])

    def test_no_git_gives_no_pack(self) -> None:
        self.standard()
        self.assertEqual(self.packs(git=Git("")).lookup(self.repo), (None, NO_GIT))
        missing = Git(str(self.base / "absent" / "git.exe"))
        self.assertEqual(self.packs(git=missing).lookup(self.repo), (None, NO_GIT))

    def test_bad_folders_are_never_probed(self) -> None:
        packs = self.packs()
        for folder in (None, 3, "", "relative\\folder", "\\\\invalid.example\\share", "//invalid.example/share",
                       "\\\\?\\C:\\x", str(self.base / "absent"), str(self.repo / "a\0b")):
            self.assertEqual(packs.lookup(folder), (None, NO_FOLDER), folder)

    def test_a_slow_build_gives_no_pack_and_writes_nothing(self) -> None:
        self.standard()
        ticks = iter(range(0, 10_000, 1))
        pack, reason = self.packs(timeout_s=3.0, clock=lambda: float(next(ticks))).lookup(self.repo)
        self.assertEqual((pack, reason), (None, TIMEOUT))
        self.assertEqual(self.cache_files(), [])

    def test_a_slow_git_gives_no_pack(self) -> None:
        self.standard()

        class SlowGit:
            def list_files(self, folder: str, timeout_s: float) -> list[str]:
                raise PackTimeout()

        self.assertEqual(self.packs(git=SlowGit()).lookup(self.repo), (None, TIMEOUT))

    def test_a_git_past_its_timeout_is_killed(self) -> None:
        calls: list[object] = []

        class Hanging:
            returncode = None

            def __init__(self, args: list[str], **kwargs: object) -> None:
                calls.append((args, kwargs))

            def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
                if timeout is not None:
                    raise subprocess.TimeoutExpired("git", timeout)
                return b"", b""

            def kill(self) -> None:
                calls.append("kill")

        with mock.patch.object(context_pack.subprocess, "Popen", Hanging), self.assertRaises(PackTimeout):
            Git(GIT).list_files(str(self.repo), 0.25)
        (_, kwargs), killed = calls
        self.assertEqual(killed, "kill")
        self.assertIs(kwargs["shell"], False)
        self.assertFalse([key for key in kwargs["env"] if key.upper().startswith("GIT_")])

    def test_any_error_gives_no_pack_and_never_raises(self) -> None:
        self.standard()

        class BrokenGit:
            def list_files(self, folder: str, timeout_s: float) -> list[str]:
                raise RuntimeError(f"broken at {folder}")

        with self.assertLogs("quill.context_pack", logging.WARNING) as logs:
            self.assertIsNone(self.packs(git=BrokenGit()).get(self.repo))
        self.assertNotIn(str(self.base), "\n".join(logs.output))
        self.assertIn(FAILED, "\n".join(logs.output))

    def test_git_error_codes(self) -> None:
        self.assertEqual(GitError(NO_GIT).reason, NO_GIT)


class CliTest(RepoCase):
    def run_cli(self, *args: str, folders: tuple = ()) -> tuple[int, str, str]:
        settings = config.load_config(None)
        project = config.ProjectContext(folders, cache_dir=self.cache, max_age_h=24, build_timeout_s=20.0)
        settings = config.Config(**{**settings.__dict__, "project_context": project})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(config, "load_config", return_value=settings), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = context_pack.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_check_prints_counts_only(self) -> None:
        self.standard()
        plain = self.base / "plain-folder"
        plain.mkdir()
        folders = (("invented-wallet", self.repo), ("invented-plain", plain),
                   ("invented-absent", self.base / "absent"))
        code, out, err = self.run_cli("--check", folders=folders)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "context pack: 3 configured projects")
        self.assertRegex(lines[1], r"^project 1: built, 4 files read, \d+ summary characters, \d+ terms, in \d+ ms$")
        self.assertRegex(lines[2], rf"^project 2: no pack \({NOT_WORK_TREE}\) in \d+ ms$")
        self.assertEqual(lines[3], "project 3: no folder")
        self.assertRegex(self.run_cli("--check", folders=folders)[1].splitlines()[1], r"^project 1: cached, ")
        self.assertRegex(self.run_cli("--check", "--rebuild", folders=folders)[1].splitlines()[1],
                         r"^project 1: built, ")
        for private in ("invented", "wallet", "ledger", str(self.base)):
            self.assertNotIn(private.casefold(), out.casefold())

    def test_usage(self) -> None:
        self.assertEqual(self.run_cli()[0], 2)


if __name__ == "__main__":
    unittest.main()
