"""Automatic rewrite of long dictations: guard, prompt, context and the rewriter, with invented text only.

No Ollama is reached: the client is a fake, and the real client's HTTP opener
is replaced.
"""

from __future__ import annotations

import json
import logging
import unittest
from types import SimpleNamespace

from quill import autorewrite as A
from quill.config import AutoRewrite as Settings
from quill.ollama import OllamaClient, OllamaError

# 49 invented words: long by words.
LONG = ("Abre o ficheiro de definições do serviço e muda o limite de pedidos para 30 por minuto. "
        "Depois lança os testes do Orion no ramo principal, confirma se o deploy passa sem erros "
        "e escreve uma nota curta a explicar a mudança para a equipa rever amanhã de manhã.")
KEEP = ("Orion", "deploy", "commit")
ON = Settings(enabled=True, min_audio_s=15.0, min_words=40, timeout_s=4.0)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeClient:
    """``reply`` for every call; ``error`` raised by every call; ``takes`` seconds advance the clock."""

    def __init__(self, reply: object = LONG, error: Exception | None = None, clock: Clock | None = None,
                 takes: float = 1.0) -> None:
        self.reply = reply
        self.error = error
        self.clock = clock
        self.takes = takes
        self.calls: list[SimpleNamespace] = []

    def chat(self, model, system, user, max_tokens=None, history=(), timeout_s=None):
        self.calls.append(SimpleNamespace(model=model, system=system, user=user, max_tokens=max_tokens,
                                          timeout_s=timeout_s))
        if self.clock is not None:
            self.clock.now += self.takes
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.reply)


class GuardTest(unittest.TestCase):
    def ok(self, reply: str, profile: str = "default", source: str = LONG) -> A.Verdict:
        verdict = A.guard(source, reply, profile=profile, keep=KEEP)
        self.assertTrue(verdict.ok, verdict.reason)
        return verdict

    def refused(self, reply: str, reason: str, profile: str = "default", source: str = LONG) -> None:
        verdict = A.guard(source, reply, profile=profile, keep=KEEP)
        self.assertIsNone(verdict.text)
        self.assertEqual(verdict.reason, reason)

    def test_same_text_is_accepted_without_changes(self) -> None:
        verdict = self.ok(LONG)
        self.assertEqual((verdict.text, verdict.changes), (LONG, 0))

    def test_misheard_words_are_fixed(self) -> None:
        source = LONG.replace("deploy", "de ploi").replace("principal", "princípal")
        verdict = A.guard(source, LONG, keep=KEEP)
        self.assertEqual((verdict.text, verdict.changes), (LONG, 2))
        # Case and punctuation are free.
        self.assertEqual(self.ok(LONG.replace("Depois lança", "depois, lança")).changes, 0)

    def test_function_words_may_change_within_the_budget(self) -> None:
        self.assertEqual(self.ok(LONG.replace("testes do Orion", "testes no Orion")).changes, 1)
        self.assertEqual(self.ok(LONG.replace("a explicar a mudança", "a explicar mudança")).changes, 1)

    def test_empty_and_markup(self) -> None:
        self.refused("   ", A.EMPTY)
        self.refused("```\n" + LONG + "\n```", A.MARKUP)
        self.refused("<dictation>" + LONG + "</dictation>", A.MARKUP)
        self.refused("# Pedido\n" + LONG, A.MARKUP, profile="claude-code")
        self.refused(LONG.replace("Orion", "**Orion**"), A.MARKUP)

    def test_a_list_only_in_claude_code(self) -> None:
        items = LONG.replace(". Depois", ".\n- Depois")
        self.refused("- " + items, A.FORMAT, profile="whatsapp")
        verdict = self.ok("- " + items, profile="claude-code")
        self.assertEqual(verdict.text.split("\n")[1][:10], "- Depois l")
        self.assertEqual(verdict.text.count("\n"), 1)
        # Line breaks without a list are joined in the other profiles.
        self.assertEqual(self.ok(LONG.replace(" Depois", "\n\nDepois"), profile="email").text, LONG)

    def test_terms_and_numbers_stay(self) -> None:
        self.refused(LONG.replace("deploy", "deployment"), A.TERM)
        self.refused(LONG.replace("30", "trinta"), A.NUMBER)
        self.refused(LONG.replace("30", "40"), A.NUMBER)
        self.refused(LONG.replace("amanhã", "amanhã às 9"), A.NUMBER)

    def test_added_information_is_refused_by_position(self) -> None:
        self.refused("Aqui está o texto corrigido: " + LONG, A.PREAMBLE)
        self.refused(LONG + " Corrigi duas palavras.", A.EXPLANATION)
        self.refused(LONG.replace("nota curta", "nota muito curta"), A.ADDED)

    def test_dropped_and_changed_content(self) -> None:
        self.refused(LONG.replace(" de manhã", ""), A.DROPPED)
        self.refused(LONG.replace("confirma", "verifica"), A.CHANGED)  # a synonym, not a misheard word
        self.refused(LONG.replace("no ramo principal", "na branch main"), A.CHANGED)
        # A content word only replaces a content word, however alike the letters.
        self.refused(LONG.replace("para a equipa", "através da equipa"), A.ADDED)
        self.refused(LONG.replace("nota curta", "nota da"), A.DROPPED)

    def test_a_content_word_is_never_merged_into_a_neighbour(self) -> None:
        source = "Revê o relatório final amanhã"
        self.refused("Revê o relatório finalmente", A.DROPPED, source=source)
        self.refused(source, A.ADDED, source="Revê o relatório finalmente")
        self.refused(LONG.replace("confirma se", "confirmar"), A.DROPPED)  # "se" is not flexible either
        # Each content word of a fix keeps most of its letters, even with equal counts.
        self.refused("guarda o ficheiros fe da equipa", A.DROPPED, source="guarda o ficheiro fonte da equipa")
        # A near-identical split or merge is still a fix of a misheard word.
        self.assertEqual(A.guard(LONG.replace("deploy", "de ploi"), LONG, keep=KEEP).changes, 1)
        self.assertEqual(A.guard("abre o note book da equipa", "abre o notebook da equipa").changes, 1)
        self.assertEqual(A.guard("abre o notebook da equipa", "abre o note book da equipa").changes, 1)

    def test_polarity_words_are_content(self) -> None:
        source = "Lança a suíte com cache e publica ou guarda o relatório se passarem, mas nem abras o painel"
        for before, after, reason in (("com cache", "sem cache", A.ADDED), ("publica ou", "publica e", A.DROPPED),
                                      (" se passarem", " passarem", A.DROPPED), ("mas nem", "mas", A.DROPPED),
                                      (", mas", ",", A.DROPPED), ("e publica", "ou publica", A.ADDED)):
            with self.subTest(before=before):
                self.refused(source.replace(before, after), reason, source=source)

    def test_names_never_change(self) -> None:
        source = LONG.replace("Orion", "Oriana")
        self.refused(source.replace("Oriana", "Orion"), A.NAME, source=source)
        # A lowercase misheard word may become a name.
        self.ok(LONG, source=LONG.replace("Orion", "órion"))

    def test_too_many_changes(self) -> None:
        # 49 words allow 4 changes.
        reply = LONG.replace("Abre o", "Abra o").replace("muda o", "mude o").replace("lança os", "lance os")
        self.ok(reply.replace("confirma se", "confirme se"))
        self.refused(reply.replace("confirma se", "confirme se").replace("escreve", "escreva"), A.TOO_MANY)

    def test_length_bounds(self) -> None:
        self.refused("Vai lá com eles", A.LENGTH, source="Vai lá")

    def test_profile_rules_hold_on_the_result(self) -> None:
        self.assertFalse(self.ok(LONG, profile="whatsapp").text.endswith("."))
        email = "Bom dia equipa amanhã levo os documentos da reunião"
        self.assertEqual(A.guard(email, email + ".", profile="email").text,
                         "Bom dia, equipa amanhã levo os documentos da reunião.")
        items = self.ok("- abre o ficheiro\n- corre os testes", profile="claude-code",
                        source="Abre o ficheiro, corre os testes").text
        self.assertEqual(items, "- Abre o ficheiro.\n- Corre os testes.")


class ContextTest(unittest.TestCase):
    def test_project_hint_from_the_title(self) -> None:
        def window(title: str, process: str = "Code.exe") -> SimpleNamespace:
            return SimpleNamespace(title=title, process=process, window_class="x")

        self.assertEqual(A.project_hint(window("app.py - nimbus - Visual Studio Code [Claude Code]")), "nimbus")
        self.assertEqual(A.project_hint(window("nimbus - Visual Studio Code")), "nimbus")
        self.assertEqual(A.project_hint(window("README.md - Visual Studio Code")), "")
        self.assertEqual(A.project_hint(window("Claude Code - orion", "WindowsTerminal.exe"), ["Orion"]), "Orion")
        self.assertEqual(A.project_hint(window("Claude Code", "WindowsTerminal.exe"), ["Orion"]), "")
        self.assertEqual(A.project_hint(None, ["Orion"]), "")

    def test_prompt_carries_profile_vocabulary_and_project(self) -> None:
        system, user = A.build_prompt(LONG, "claude-code", KEEP, "nimbus")
        self.assertIn('starting with "- "', system)
        self.assertIn("Vocabulary (write these exactly like this): Orion, deploy, commit", user)
        self.assertIn("Active project: nimbus", user)
        self.assertTrue(user.endswith(f"<dictation>\n{LONG}\n</dictation>"))
        system, user = A.build_prompt(LONG, "whatsapp")
        self.assertIn("informal chat message", system)
        self.assertNotIn("Vocabulary", user)
        self.assertNotIn("project", user)
        many = [f"termo{index:03d}" for index in range(400)]
        _, user = A.build_prompt(LONG, "default", many)
        self.assertLessEqual(len(user.split("\n")[0]), A.MAX_VOCABULARY_CHARS + 50)

    def test_long_by_audio_or_words(self) -> None:
        self.assertTrue(A.is_long("curto", 15.5, ON))
        self.assertFalse(A.is_long("curto", 15.0, ON))
        self.assertTrue(A.is_long(LONG, 3.0, ON))
        self.assertFalse(A.is_long(" ".join(LONG.split()[:40]), None, ON))


class RewriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def run_with(self, client: FakeClient, text: str = LONG, audio_s: float | None = 18.0,
                 settings: Settings = ON, **kwargs: object) -> A.AutoRewrite:
        rewriter = A.AutoRewriter(client, "qwen3:8b", settings, clock=self.clock)
        with self.assertLogs("quill.autorewrite", logging.INFO) as logs:
            logging.getLogger("quill.autorewrite").info("start")
            result = rewriter.rewrite(text, audio_s=audio_s, keep=KEEP, **kwargs)
        for line in logs.output:
            for word in ("ficheiro", "Orion", "nimbus", "deploy", "de ploi"):
                self.assertNotIn(word, line)
        return result

    def test_short_or_disabled_never_calls_the_model(self) -> None:
        client = FakeClient()
        short = self.run_with(client, "Abre o ficheiro e corre os testes.", audio_s=4.0)
        self.assertEqual((short.reason, short.text, short.called), (A.SHORT, "Abre o ficheiro e corre os testes.", False))
        off = self.run_with(client, settings=Settings())
        self.assertEqual((off.reason, off.text), (A.DISABLED, LONG))
        self.assertEqual(client.calls, [])
        self.assertFalse(A.AutoRewriter(client, "m", ON).wants("curto", 3.0))
        self.assertTrue(A.AutoRewriter(client, "m", ON).wants("curto", 16.0))
        self.assertFalse(A.AutoRewriter(client, "m", Settings()).wants(LONG, 30.0))

    def test_accepted_rewrite(self) -> None:
        client = FakeClient(LONG, clock=self.clock, takes=1.5)
        source = LONG.replace("deploy", "de ploi")
        result = self.run_with(client, source, profile="claude-code", project="nimbus")
        self.assertEqual((result.reason, result.text, result.original, result.seconds), (A.REWRITTEN, LONG, source, 1.5))
        self.assertTrue(result.rewritten)
        call = client.calls[0]
        self.assertEqual((call.model, call.timeout_s, call.max_tokens), ("qwen3:8b", 4.0, 3 * 50))
        self.assertIn("Active project: nimbus", call.user)
        self.assertIn("coding assistant", call.system)

    def test_unchanged_reply(self) -> None:
        result = self.run_with(FakeClient(LONG))
        self.assertEqual((result.reason, result.text, result.rewritten), (A.UNCHANGED, LONG, False))

    def test_guard_refusal_types_the_original(self) -> None:
        result = self.run_with(FakeClient("Aqui está: " + LONG))
        self.assertEqual((result.reason, result.detail, result.text), (A.REFUSED, A.PREAMBLE, LONG))
        self.assertEqual(result.message, "Reescrita recusada; ficou o texto original")

    def test_ollama_failure_and_timeouts(self) -> None:
        failed = self.run_with(FakeClient(error=OllamaError("Ollama unreachable: ConnectionRefusedError")))
        self.assertEqual((failed.reason, failed.text), (A.FAILED, LONG))
        self.assertEqual(failed.message, "Ollama indisponível; ficou o texto original")
        timed_out = self.run_with(FakeClient(error=OllamaError("Ollama unreachable: TimeoutError")))
        self.assertEqual((timed_out.reason, timed_out.text), (A.TIMEOUT, LONG))
        slow = self.run_with(FakeClient(LONG.replace("deploy", "deploy"), clock=self.clock, takes=4.5),
                             LONG.replace("deploy", "de ploi"))
        self.assertEqual((slow.reason, slow.text), (A.TIMEOUT, LONG.replace("deploy", "de ploi")))
        self.assertEqual(slow.message, "A reescrita demorou demais; ficou o texto original")
        broken = self.run_with(FakeClient(error=ValueError("bad")))
        self.assertEqual((broken.reason, broken.text), (A.FAILED, LONG))
        no_text = self.run_with(FakeClient(reply=None))
        self.assertEqual((no_text.reason, no_text.detail), (A.FAILED, "no_text"))

    def test_a_very_long_text_is_typed_as_it_is(self) -> None:
        client = FakeClient()
        text = "palavra " * 800
        result = self.run_with(client, text)
        self.assertEqual((result.reason, result.detail, result.text, result.called), (A.REFUSED, A.TOO_LONG, text, False))
        self.assertEqual(client.calls, [])


class ClientTimeoutTest(unittest.TestCase):
    def test_chat_passes_its_timeout_and_never_keep_alive(self) -> None:
        sent = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"message": {"content": "ok"}}'

        class Opener:
            def open(self, request, timeout):
                sent.append((json.loads(request.data.decode("utf-8")), timeout))
                return Response()

        client = OllamaClient("http://127.0.0.1:11434", timeout_s=10.0)
        client._opener = Opener()
        client.chat("m", "system", "user", timeout_s=2.5)
        client.chat("m", "system", "user")
        self.assertEqual([timeout for _, timeout in sent], [2.5, 10.0])
        self.assertNotIn("keep_alive", sent[0][0])


if __name__ == "__main__":
    unittest.main()
