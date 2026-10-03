"""Automatic rewrite of long dictations: guard, prompt, context and the rewriter, with invented text only.

No Ollama is reached: the client is a fake, and the real client's HTTP opener
is replaced.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unittest
import unittest.mock
from types import SimpleNamespace

from quill import autorewrite as A
from quill import ollama as O
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

    def test_forced_rewrite_ignores_enabled_and_the_thresholds_but_keeps_the_guard(self) -> None:
        # The send_polished trigger: a short dictation with [autorewrite] off still asks the model.
        short = "Abre o ficheiro e corre os testes."
        for settings in (ON, Settings()):
            client = FakeClient(short)
            result = self.run_with(client, short.replace("ficheiro", "fixeiro"), audio_s=1.0, settings=settings,
                                   force=True)
            self.assertEqual(len(client.calls), 1)
            # Outside context mode a fix to a word that is no term stays, as before.
            self.assertEqual((result.reason, result.called, result.text, result.kept), (A.REWRITTEN, True, short, 0))
            self.assertNotIn("never with any other word", client.calls[0].system)
        refused = self.run_with(FakeClient("Abre o ficheiro."), short, audio_s=1.0, settings=Settings(), force=True)
        self.assertEqual((refused.reason, refused.text), (A.REFUSED, short))  # a dropped word: the original
        failed = self.run_with(FakeClient(error=OSError("down")), short, audio_s=1.0, settings=Settings(), force=True)
        self.assertEqual((failed.reason, failed.text), (A.FAILED, short))

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


# Context mode (send_polished in Claude Code): an invented project and a misheard domain word.
PACK = SimpleNamespace(summary="Plataforma de trading com carteiras, ordens de compra e um painel de risco.",
                       terms=("wallet", "ordem de compra", "painel de risco", "ledger"))
HEARD = "Abre o módulo da uólete e mostra o saldo de cada conta antes da ordem de compra, sem mexer na API."
FIXED = HEARD.replace("uólete", "wallet")
ENRICHED = ("Pedido: Abre o módulo da wallet e mostra o saldo de cada conta antes da ordem de compra.\n"
            "Contexto: plataforma de trading com carteiras.\n"
            "Restrições: sem mexer na API.")
CONTEXT_ON = Settings(enabled=True, min_audio_s=15.0, min_words=40, timeout_s=4.0, enrich_timeout_s=12.0)


class Replies(FakeClient):
    """One reply per call, in order; an Exception reply is raised; ``takes`` is per call or for every call."""

    def __init__(self, *replies: object, clock: Clock | None = None, takes: float | tuple[float, ...] = 1.0) -> None:
        super().__init__(clock=clock)
        self.replies = list(replies)
        self.durations = list(takes) if isinstance(takes, tuple) else [takes] * len(replies)

    def chat(self, model, system, user, max_tokens=None, history=(), timeout_s=None):
        self.reply = self.replies.pop(0)
        self.takes = self.durations.pop(0)
        self.error = self.reply if isinstance(self.reply, Exception) else None
        return super().chat(model, system, user, max_tokens, history, timeout_s)


class SoundKeyTest(unittest.TestCase):
    def test_sound_key_folds_letters_that_sound_alike(self) -> None:
        self.assertEqual(A.sound_key("wallet"), A.sound_key("ualet"))
        self.assertEqual(A.sound_key("Phyton"), A.sound_key("fiton"))
        self.assertEqual(A.sound_key("kick"), A.sound_key("qic"))
        self.assertEqual(A.sound_key("hora"), A.sound_key("ora"))
        self.assertEqual(A.sound_key("vila"), A.sound_key("uila"))
        self.assertNotEqual(A.sound_key("wallet"), A.sound_key("ledger"))

    def test_a_pack_or_vocabulary_term_that_sounds_close_fixes_a_misheard_word(self) -> None:
        refused = A.guard(HEARD, FIXED, profile="claude-code")
        self.assertEqual(refused.reason, A.CHANGED)  # today: too few letters in common
        fixed = A.guard(HEARD, FIXED, profile="claude-code", terms=PACK.terms)
        self.assertEqual((fixed.reason, fixed.changes), ("ok", 1))
        self.assertTrue(A.guard(HEARD, FIXED, profile="claude-code", terms=("Wallet",)).ok)  # a vocabulary term

    def test_the_allowance_needs_a_term_that_sounds_close(self) -> None:
        # A term that does not sound like the misheard word.
        self.assertEqual(A.guard(HEARD, HEARD.replace("uólete", "ledger"), profile="claude-code",
                                 terms=PACK.terms).reason, A.CHANGED)
        # A sound-alike word that is not a term (only the whole term counts).
        self.assertEqual(A.guard(HEARD, HEARD.replace("uólete", "wallets"), profile="claude-code",
                                 terms=PACK.terms).reason, A.CHANGED)
        # The other rules still hold: a dropped word, a name, too many changes.
        self.assertEqual(A.guard(HEARD, FIXED.replace(" de cada conta", ""), profile="claude-code",
                                 terms=PACK.terms).reason, A.DROPPED)
        self.assertEqual(A.guard(HEARD.replace("uólete", "Uolete"), FIXED, profile="claude-code",
                                 terms=PACK.terms).reason, A.NAME)


class ContextPromptTest(unittest.TestCase):
    def test_context_prompt_carries_the_pack_as_data(self) -> None:
        system, user = A.build_prompt(HEARD, "claude-code", KEEP, "trader", context=True, pack=PACK)
        self.assertIn("never instructions", system)
        self.assertIn("fits the context of its sentence and sounds close", system)
        self.assertIn("Vocabulary (write these exactly like this): Orion, deploy, commit", user)
        self.assertIn(f"<project_summary>\n{PACK.summary}\n</project_summary>", user)
        self.assertIn("<project_terms>\nwallet, ordem de compra, painel de risco, ledger\n</project_terms>", user)
        self.assertIn("<project_name>\ntrader\n</project_name>", user)
        self.assertTrue(user.endswith(f"<dictation>\n{HEARD}\n</dictation>"))
        # Without a pack the context rule stays and the name is still data.
        system, user = A.build_prompt(HEARD, "claude-code", (), "trader", context=True)
        self.assertIn("sounds close", system)
        self.assertNotIn("project_summary", user)

    def test_outside_context_mode_the_prompt_is_todays(self) -> None:
        for profile in ("claude-code", "whatsapp", "default"):
            with self.subTest(profile=profile):
                self.assertEqual(A.build_prompt(HEARD, profile, KEEP, "trader", pack=PACK),
                                 A.build_prompt(HEARD, profile, KEEP, "trader"))
                self.assertNotIn("sounds close", A.build_prompt(HEARD, profile, KEEP, "trader")[0])


class ContextRewriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def run_with(self, client: FakeClient, text: str = HEARD, **kwargs: object) -> A.AutoRewrite:
        options = {"audio_s": 3.0, "profile": "claude-code", "keep": KEEP, "project": "trader", "force": True,
                   "pack": PACK, "enrich_prompt": True, **kwargs}
        rewriter = A.AutoRewriter(client, "qwen3:8b", CONTEXT_ON, clock=self.clock)
        with self.assertLogs("quill", logging.INFO) as logs:
            logging.getLogger("quill").info("start")
            result = rewriter.rewrite(text, **options)
        for line in logs.output:
            for word in ("uólete", "wallet", "saldo", "trader", "trading", "carteiras", "API"):
                self.assertNotIn(word, line)
        return result

    def test_corrected_with_the_pack_and_enriched(self) -> None:
        client = Replies(FIXED, ENRICHED, clock=self.clock, takes=2.0)
        result = self.run_with(client)
        self.assertEqual((result.reason, result.enrichment, result.text, result.original),
                         (A.REWRITTEN, "enrich_enriched", ENRICHED, HEARD))
        self.assertEqual((result.corrected, result.enriched, result.rewritten), (True, True, True))
        self.assertEqual((result.seconds, result.enrich_seconds), (2.0, 2.0))
        correct, enrich = client.calls
        self.assertEqual((correct.timeout_s, enrich.timeout_s), (4.0, 12.0))
        self.assertIn("<project_terms>", correct.user)
        self.assertIn(f"<dictation>\n{FIXED}\n</dictation>", enrich.user)  # the corrected text
        self.assertIn("Critérios de aceitação", enrich.system)

    def test_without_the_pack_the_misheard_word_is_refused_and_nothing_is_enriched(self) -> None:
        client = Replies(FIXED, ENRICHED)
        result = self.run_with(client, pack=None)
        self.assertEqual((result.reason, result.detail, result.text, result.enrichment),
                         (A.REFUSED, A.CHANGED, HEARD, ""))
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(result.rewritten)

    def test_unchanged_correction_is_still_enriched(self) -> None:
        result = self.run_with(Replies(FIXED, ENRICHED), text=FIXED)
        self.assertEqual((result.reason, result.enrichment, result.text, result.original),
                         (A.UNCHANGED, "enrich_enriched", ENRICHED, FIXED))
        self.assertEqual((result.corrected, result.enriched, result.rewritten), (False, True, True))

    def test_refused_failed_or_slow_enrichment_types_the_corrected_text(self) -> None:
        invented = ENRICHED + "\nCritérios de aceitação: cobertura total em exporter.py."
        for reply, reason, takes in ((invented, "enrich_refused", 1.0), (OSError("down"), "enrich_ollama_failed", 1.0),
                                     (TimeoutError("slow"), "enrich_timeout", 1.0), (ENRICHED, "enrich_timeout", 12.5)):
            with self.subTest(reason=reason, takes=takes):
                self.clock.now = 100.0
                # The correction alone is well within timeout_s (4 s); the enrichment has its own 12 s.
                client = Replies(FIXED, reply, clock=self.clock, takes=(3.5, takes))
                result = self.run_with(client)
                self.assertEqual((result.reason, result.enrichment, result.text, result.original),
                                 (A.REWRITTEN, reason, FIXED, HEARD))
                self.assertEqual((result.corrected, result.enriched), (True, False))
                self.assertTrue(result.enrich_message.endswith("foi o texto corrigido"))

    def test_a_broken_enricher_never_loses_the_corrected_text(self) -> None:
        rewriter = A.AutoRewriter(Replies(FIXED), "m", CONTEXT_ON, clock=self.clock)
        rewriter.enricher = SimpleNamespace(enrich=lambda *args, **kwargs: 1 / 0)
        result = rewriter.rewrite(HEARD, audio_s=3.0, profile="claude-code", force=True, pack=PACK,
                                  enrich_prompt=True)
        self.assertEqual((result.text, result.enrichment, result.enrich_detail),
                         (FIXED, "enrich_ollama_failed", "ZeroDivisionError"))

    def test_a_failed_correction_is_not_enriched(self) -> None:
        for reply, reason in ((OSError("down"), A.FAILED), (TimeoutError("slow"), A.TIMEOUT)):
            client = Replies(reply, ENRICHED)
            result = self.run_with(client)
            self.assertEqual((result.reason, result.text, result.enrichment), (reason, HEARD, ""))
            self.assertEqual(len(client.calls), 1)

    def test_outside_send_polished_in_claude_code_everything_is_todays(self) -> None:
        cases = ({"profile": "vscode"}, {"profile": "default"}, {"force": False, "audio_s": 20.0})
        for case in cases:
            with self.subTest(**case):
                client = Replies(FIXED, ENRICHED)
                result = self.run_with(client, **case)
                self.assertEqual(len(client.calls), 1)  # no enrichment
                self.assertEqual(result.enrichment, "")
                self.assertEqual((result.reason, result.detail), (A.REFUSED, A.CHANGED))  # no sound-key allowance
                profile = case.get("profile", "claude-code")
                self.assertEqual((client.calls[0].system, client.calls[0].user),
                                 A.build_prompt(HEARD, profile, KEEP, "trader"))
        # Without enrich_prompt, send_polished in Claude Code corrects with the pack and stops.
        client = Replies(FIXED, ENRICHED)
        result = self.run_with(client, enrich_prompt=False)
        self.assertEqual((result.reason, result.text, result.enrichment, len(client.calls)),
                         (A.REWRITTEN, FIXED, "", 1))

    def test_context_overrides_the_profile(self) -> None:
        # Claude Code in a terminal: the vscode layout profile (one paragraph), still in context mode.
        client = Replies(FIXED, ENRICHED)
        result = self.run_with(client, profile="vscode", context=True)
        self.assertEqual((result.reason, result.enrichment, result.text), (A.REWRITTEN, "enrich_enriched", ENRICHED))
        self.assertEqual((client.calls[0].system, client.calls[0].user),
                         A.build_prompt(HEARD, "vscode", KEEP, "trader", context=True, pack=PACK))
        # context=False keeps today's rewrite even in the claude-code profile.
        client = Replies(FIXED, ENRICHED)
        result = self.run_with(client, context=False)
        self.assertEqual((result.reason, result.enrichment, len(client.calls)), (A.REFUSED, "", 1))
        # Without force, context changes nothing.
        client = Replies(FIXED, ENRICHED)
        result = self.run_with(client, context=True, force=False, audio_s=20.0)
        self.assertEqual((result.enrichment, len(client.calls)), ("", 1))

    def test_on_enrich_runs_once_just_before_the_enrichment_is_asked(self) -> None:
        seen = []
        client = Replies(FIXED, ENRICHED)
        result = self.run_with(client, on_enrich=lambda: seen.append(len(client.calls)))
        self.assertEqual((seen, result.enrichment), ([1], "enrich_enriched"))  # after the correction call
        # A dictation too short to enrich never announces it; a failing hook never stops the enrichment.
        seen.clear()
        result = self.run_with(Replies("Sim, continua."), text="Sim, continua.", on_enrich=lambda: seen.append(1))
        self.assertEqual((seen, result.enrichment), ([], "enrich_short"))
        client = Replies(FIXED, ENRICHED)
        result = self.run_with(client, on_enrich=lambda: 1 / 0)
        self.assertEqual((result.enrichment, result.text), ("enrich_enriched", ENRICHED))
        # A refused correction is not enriched: the hook is not called.
        result = self.run_with(Replies(FIXED), pack=None, on_enrich=lambda: seen.append(1))
        self.assertEqual((seen, result.reason), ([], A.REFUSED))


class TermsOnlyTest(unittest.TestCase):
    """send_polished in Claude Code: a fix may only bring a word that was not dictated when it is a term."""

    SOURCE = ("Gera o gráfico mensal da uólete no json e faz o de ploi quando o servidor responde, depois arquiva "
              "a cópia.")
    REPLY = ("Gera o gráfico mensal da wallet no JSON e faz o deploy quando o servidor responder, depois arquiva "
             "a cópia.")

    def test_a_fix_to_a_word_that_is_no_term_is_typed_as_dictated(self) -> None:
        verdict = A.guard(self.SOURCE, self.REPLY, profile="claude-code", terms=("wallet",),
                          replacements=("wallet", "deploy", "JSON"))
        # Case fixes and term fixes stay; the other verb form is undone.
        self.assertEqual((verdict.reason, verdict.changes, verdict.kept), ("ok", 2, 1))
        self.assertEqual(verdict.text, self.REPLY.replace("responder", "responde"))
        # Without the restriction (the automatic rewrite), all three fixes count: too many for 20 words.
        today = A.guard(self.SOURCE, self.REPLY, profile="claude-code", terms=("wallet",))
        self.assertEqual((today.reason, today.changes, today.kept), (A.TOO_MANY, 3, 0))

    def test_only_whole_terms_accents_and_case_count(self) -> None:
        source = "Abre o painel de rísco e mostra a ordem de conpra."
        reply = "Abre o painel de risco e mostra a ordem de compra."
        # An accent-only fix needs no term; "compra" alone is only a word of a term, so it is undone.
        verdict = A.guard(source, reply, replacements=("ordem de compra",))
        self.assertEqual((verdict.text, verdict.kept), (reply.replace("compra", "conpra"), 1))
        # The whole term as one fix is accepted.
        verdict = A.guard("Abre o painel de risco e mostra a ordemde conpra.", reply,
                          replacements=("ordem de compra",))
        self.assertEqual((verdict.text, verdict.kept), (reply, 0))
        # An empty list of terms undoes every fix that brings a new word.
        verdict = A.guard(source, reply, replacements=())
        self.assertEqual((verdict.text, verdict.kept), (reply.replace("compra", "conpra"), 1))

    def test_undoing_keeps_the_dictated_words_and_the_other_rules(self) -> None:
        source = "Guarda a lista de compras e eu reveijo, antes da reunião."
        verdict = A.guard(source, "Guarda a lista de compras e eu revejo, antes da reunião.", replacements=())
        self.assertEqual((verdict.text, verdict.kept, verdict.changes), (source, 1, 0))
        # Refusals are unchanged: a dropped word, an added word, a changed number.
        for reply, reason in (("Guarda a lista e eu revejo, antes da reunião.", A.DROPPED),
                              ("Guarda a lista de compras e eu revejo, antes da reunião, por favor.", A.EXPLANATION),
                              ("Guarda a lista de compras e eu revejo, antes da reunião 2.", A.NUMBER)):
            with self.subTest(reason=reason):
                self.assertEqual(A.guard(source, reply, replacements=()).reason, reason)

    def test_the_rewriter_passes_the_terms_of_its_mode(self) -> None:
        heard = "Corre o de ploi da uólete no módulo do trader e mostra o fexeiro de registo."
        reply = "Corre o deploy da wallet no módulo do trader e mostra o ficheiro de registo."
        clock = Clock()
        # Context mode: vocabulary, pack terms and the project name.
        rewriter = A.AutoRewriter(Replies(reply), "m", CONTEXT_ON, clock=clock)
        with self.assertLogs("quill.autorewrite", logging.INFO) as logs:
            result = rewriter.rewrite(heard, audio_s=3.0, profile="claude-code", keep=KEEP, project="trader",
                                      force=True, pack=PACK)
        self.assertEqual((result.reason, result.kept), (A.REWRITTEN, 1))
        self.assertEqual(result.text, reply.replace("ficheiro", "fexeiro"))
        self.assertIn("1 fixes kept as dictated", logs.output[-1])
        for word in ("fexeiro", "ficheiro", "wallet", "trader"):
            self.assertNotIn(word, logs.output[-1])
        # send_polished in any other window keeps today's rewrite: no sound-key allowance, no terms-only rule.
        rewriter = A.AutoRewriter(Replies(reply), "m", CONTEXT_ON, clock=clock)
        result = rewriter.rewrite(heard, audio_s=3.0, profile="default", keep=KEEP, project="trader", force=True)
        self.assertEqual((result.reason, result.detail), (A.REFUSED, A.CHANGED))
        rewriter = A.AutoRewriter(Replies(reply.replace("wallet", "uólete")), "m", CONTEXT_ON, clock=clock)
        result = rewriter.rewrite(heard, audio_s=3.0, profile="default", keep=KEEP, force=True)
        self.assertEqual((result.reason, result.text, result.kept), (A.REWRITTEN, reply.replace("wallet", "uólete"), 0))
        # The same in the claude-code profile with context=False (Claude Code without context mode).
        rewriter = A.AutoRewriter(Replies(reply.replace("wallet", "uólete")), "m", CONTEXT_ON, clock=clock)
        result = rewriter.rewrite(heard, audio_s=3.0, profile="claude-code", keep=KEEP, force=True, context=False)
        self.assertEqual((result.reason, result.kept), (A.REWRITTEN, 0))
        self.assertIn("ficheiro", result.text)
        # The automatic rewrite of a long dictation keeps today's rules.
        rewriter = A.AutoRewriter(Replies(reply.replace("wallet", "uólete")), "m", CONTEXT_ON, clock=clock)
        result = rewriter.rewrite(heard, audio_s=20.0, profile="default", keep=KEEP)
        self.assertEqual((result.text, result.kept), (reply.replace("wallet", "uólete"), 0))

    def test_the_prompt_says_the_rule_only_in_context_mode(self) -> None:
        for profile in ("default", "claude-code"):
            self.assertNotIn("only with a term", A.build_prompt(HEARD, profile, KEEP, "trader")[0])
        context = A.build_prompt(HEARD, "claude-code", KEEP, "trader", context=True, pack=PACK)
        self.assertIn("only with a term of the vocabulary or of the project", context[0])
        self.assertIn("<project_terms>", context[1])


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


class _Opener:
    """The real client's HTTP opener, replaced: records (method, path, payload, timeout), answers ``body``."""

    def __init__(self, body: bytes = b'{"message": {"content": "ok"}}') -> None:
        self.body = body
        self.sent: list[tuple[str, str, dict | None, float]] = []

    def open(self, request, timeout):
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        path = request.full_url.split("11434", 1)[1]
        self.sent.append((request.get_method(), path, payload, timeout))
        body = self.body

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        return Response()


def _client(keep_alive=None, body: bytes = b'{"message": {"content": "ok"}}') -> tuple[OllamaClient, _Opener]:
    client = OllamaClient("http://127.0.0.1:11434", timeout_s=4.0, keep_alive=keep_alive)
    client._opener = _Opener(body)
    return client, client._opener


class KeepAliveTest(unittest.TestCase):
    """keep_alive: only a positive duration, only on Quill's own chat and warm-up; never an unload."""

    def test_only_positive_durations_are_accepted(self) -> None:
        self.assertEqual([O.keep_alive_seconds(v) for v in ("90s", "30m", "2h", "1m")], [90, 1800, 7200, 60])
        for value in ("0", "0s", "0m", "00m", "-1", "-5m", "-1m", "1.5h", "30", "30M", " 30m", "30m ", "", "5d",
                      "1e3s", 0, -1, 30, 1.5, True):
            with self.subTest(value=value):
                self.assertIsNone(O.keep_alive_seconds(value))
                with self.assertRaises(ValueError):
                    OllamaClient("http://127.0.0.1:11434", keep_alive=value)

    def test_chat_and_warm_up_carry_the_clients_duration(self) -> None:
        client, opener = _client("30m")
        client.chat("m", "system", "user")
        client.warm("m", timeout_s=60.0)
        (chat_method, chat_path, chat, _), (warm_method, warm_path, warm, timeout) = opener.sent
        self.assertEqual((chat_method, chat_path, chat["keep_alive"]), ("POST", "/api/chat", "30m"))
        # The warm-up is a chat with no message: it loads the model and generates nothing.
        self.assertEqual((warm_method, warm_path, timeout), ("POST", "/api/chat", 60.0))
        self.assertEqual(warm, {"model": "m", "messages": [], "stream": False, "keep_alive": "30m"})

    def test_without_a_duration_nothing_is_sent(self) -> None:
        client, opener = _client()
        client.chat("m", "system", "user")
        client.warm("m")
        self.assertEqual(len(opener.sent), 2)
        self.assertTrue(all("keep_alive" not in payload for _, _, payload, _ in opener.sent))

    def test_an_unload_value_is_refused_before_any_request(self) -> None:
        client, opener = _client()
        for value in (0, "0", "0s", -1, "-1", "-5m", None, ""):
            with self.subTest(value=value):
                with self.assertRaises(OllamaError):
                    client._call("POST", "/api/chat", {"model": "m", "messages": [], "keep_alive": value})
        with self.assertRaises(OllamaError):  # never on another call either
            client._call("GET", "/api/tags", {"keep_alive": "30m"})
        client.keep_alive = "0"  # even when set by hand after the constructor's check
        with self.assertRaises(OllamaError):
            client.warm("m")
        with self.assertRaises(OllamaError):
            client.chat("m", "system", "user")
        self.assertEqual(opener.sent, [])

    def test_no_call_can_pull_delete_or_unload_a_model(self) -> None:
        self.assertEqual(O.ALLOWED_CALLS, {("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/chat")})
        client, opener = _client()
        for method, path in (("POST", "/api/pull"), ("DELETE", "/api/delete"), ("POST", "/api/generate"),
                             ("POST", "/api/create"), ("POST", "/api/copy"), ("POST", "/api/push")):
            with self.subTest(path=path):
                with self.assertRaises(OllamaError):
                    client._call(method, path, {"model": "m"})
        self.assertEqual(opener.sent, [])

    def test_loaded_lists_the_models_in_memory_read_only(self) -> None:
        client, opener = _client(body=b'{"models": [{"name": "qwen3:8b"}, {"name": 3}, "x"]}')
        self.assertEqual(client.loaded(timeout_s=0.5), ["qwen3:8b"])
        self.assertEqual(opener.sent, [("GET", "/api/ps", None, 0.5)])

    def test_a_warm_up_error_raises_without_the_servers_text(self) -> None:
        client, _ = _client(body=b'{"error": "model not found"}')
        with self.assertRaises(OllamaError) as raised:
            client.warm("m")
        self.assertNotIn("model not found", str(raised.exception))


class WarmClient:
    """A fake Ollama for the warmer: ``memory`` is what /api/ps lists; ``gate`` holds a warm-up."""

    def __init__(self, memory=(), error: Exception | None = None, gate: threading.Event | None = None,
                 unreadable: bool = False) -> None:
        self.memory = list(memory)
        self.error = error
        self.gate = gate
        self.unreadable = unreadable
        self.warms: list[tuple[str, str, float | None]] = []  # (model, thread name, timeout_s)
        self.checks = 0
        self.entered = threading.Event()

    def loaded(self, timeout_s=None):
        self.checks += 1
        if self.unreadable:
            raise OllamaError("Ollama unreachable: URLError")
        return list(self.memory)

    def warm(self, model, timeout_s=None):
        self.warms.append((model, threading.current_thread().name, timeout_s))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5.0)
        if self.error is not None:
            raise self.error
        self.memory = [model]


def _wait(condition, what: str) -> None:
    deadline = time.monotonic() + 5.0
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


class ModelWarmerTest(unittest.TestCase):
    def warmer(self, client: WarmClient, **kwargs) -> O.ModelWarmer:
        if client.gate is not None:
            self.addCleanup(client.gate.set)
        return O.ModelWarmer(client, "qwen3:8b", **kwargs)

    def test_a_warm_up_runs_on_its_own_thread_and_returns_at_once(self) -> None:
        client = WarmClient(gate=threading.Event())
        warmer = self.warmer(client)
        with self.assertLogs("quill.ollama", logging.INFO) as logs:
            self.assertTrue(warmer.warm("mouse 5 hold"))  # returns while the load is still held
            self.assertTrue(client.entered.wait(5.0))
            self.assertTrue(warmer.busy)
            self.assertFalse(warmer.warm("mouse 5 hold"))  # at most one in flight
            self.assertFalse(warmer.warm("start", unless_other=True))
            client.gate.set()
            _wait(lambda: not warmer.busy, "the warm-up to end")
        self.assertEqual(client.warms, [("qwen3:8b", "quill-model-warm-up", O.WARM_TIMEOUT_S)])
        self.assertNotEqual(client.warms[0][1], threading.current_thread().name)
        self.assertIn("model warm-up (mouse 5 hold) done", "\n".join(logs.output))
        self.assertTrue(warmer.warm("again"))  # a new one once the last has ended
        _wait(lambda: not warmer.busy, "the second warm-up")
        self.assertEqual(len(client.warms), 2)

    def test_a_failure_is_only_logged(self) -> None:
        for error in (OllamaError("Ollama unreachable: TimeoutError"), ValueError("dictated words")):
            with self.subTest(error=type(error).__name__):
                client = WarmClient(error=error)
                warmer = self.warmer(client)
                with self.assertLogs("quill.ollama", logging.WARNING) as logs:
                    self.assertTrue(warmer.warm("start"))
                    _wait(lambda: not warmer.busy, "the failed warm-up")
                self.assertIn(type(error).__name__, logs.output[0])
                self.assertNotIn("dictated", logs.output[0])

    def test_no_thread_is_only_logged(self) -> None:
        def broken(job):
            raise RuntimeError("no thread")

        warmer = self.warmer(WarmClient(), spawn=broken)
        with self.assertLogs("quill.ollama", logging.WARNING):
            self.assertFalse(warmer.warm("start"))
        self.assertFalse(warmer.busy)

    def test_at_start_another_projects_model_is_left_alone(self) -> None:
        for memory, warmed, message in ((["other:14b"], 0, "skipped: Ollama holds another model"),
                                        (["qwen3:8b"], 0, "already in memory"),
                                        ([], 1, "done")):
            with self.subTest(memory=memory):
                client = WarmClient(memory)
                warmer = self.warmer(client)
                with self.assertLogs("quill.ollama", logging.INFO) as logs:
                    warmer.warm("start", unless_other=True)
                    _wait(lambda: not warmer.busy, "the start warm-up")
                self.assertEqual(len(client.warms), warmed)
                self.assertIn(message, "\n".join(logs.output))
                self.assertNotIn("other:14b", "\n".join(logs.output))

    def test_before_a_call_a_model_in_memory_costs_one_check(self) -> None:
        client = WarmClient(["qwen3:8b"])
        warmer = self.warmer(client)
        self.assertEqual(warmer.before_call(8.0), 0.0)
        self.assertEqual((client.checks, client.warms), (1, []))

    def test_before_a_call_a_cold_model_is_loaded_and_waited_for(self) -> None:
        client = WarmClient([])
        warmer = self.warmer(client)
        waited = warmer.before_call(5.0)
        self.assertEqual([warm[0] for warm in client.warms], ["qwen3:8b"])
        self.assertEqual(client.memory, ["qwen3:8b"])
        self.assertFalse(warmer.busy)
        self.assertLess(waited, 5.0)

    def test_the_wait_before_a_call_is_bounded(self) -> None:
        client = WarmClient([], gate=threading.Event())
        warmer = self.warmer(client)
        warmer.warm("mouse 5 hold")
        self.assertTrue(client.entered.wait(5.0))
        started = time.monotonic()
        waited = warmer.before_call(0.05)
        self.assertGreaterEqual(waited, 0.04)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(warmer.busy)  # the load goes on, for the call that follows
        self.assertEqual(client.checks, 0)  # a warm-up in flight: nothing to ask
        self.assertEqual(len(client.warms), 1)  # and no second one

    def test_a_slow_check_counts_in_the_wait(self) -> None:
        clock = Clock()
        client = WarmClient([], gate=threading.Event())
        checks = client.loaded

        def slow_check(timeout_s=None):
            clock.now += 3.0  # /api/ps took the whole wait
            return checks(timeout_s)

        client.loaded = slow_check
        warmer = self.warmer(client, clock=clock)
        started = time.monotonic()
        self.assertEqual(warmer.before_call(3.0), 3.0)
        self.assertLess(time.monotonic() - started, 2.0)  # no further wait: the call goes ahead
        self.assertTrue(client.entered.wait(5.0))  # the load started, for the next call

    def test_an_unknown_state_lets_the_call_go_ahead(self) -> None:
        client = WarmClient(unreadable=True)
        warmer = self.warmer(client)
        self.assertEqual(warmer.before_call(8.0), 0.0)
        self.assertEqual(client.warms, [])


class FakeWarmer:
    """The rewriter's warmer: ``waits`` seconds on the clock before a call (at most the wait given)."""

    def __init__(self, clock: Clock, waits: float = 0.0, error: Exception | None = None) -> None:
        self.clock = clock
        self.waits = waits
        self.error = error
        self.asked: list[float] = []
        self.warms: list[str] = []

    def before_call(self, wait_s: float) -> float:
        self.asked.append(wait_s)
        if self.error is not None:
            raise self.error
        waited = min(self.waits, wait_s)
        self.clock.now += waited
        return waited

    def warm(self, reason: str, **kwargs) -> bool:
        self.warms.append(reason)
        return True


class ColdModelTest(unittest.TestCase):
    """A correction that meets a model still loading: bounded by load_wait_s + timeout_s, never loses text."""

    SETTINGS = Settings(enabled=True, min_audio_s=15.0, min_words=40, timeout_s=4.0, load_wait_s=8.0)

    def setUp(self) -> None:
        self.clock = Clock()

    def rewrite(self, client: FakeClient, warmer: FakeWarmer, text: str = LONG) -> A.AutoRewrite:
        rewriter = A.AutoRewriter(client, "qwen3:8b", self.SETTINGS, clock=self.clock, warmer=warmer)
        with self.assertLogs("quill.autorewrite", logging.INFO) as logs:
            result = rewriter.rewrite(text, audio_s=18.0, keep=KEEP, force=True)
        self.logs = "\n".join(logs.output)
        self.assertNotIn("Orion", self.logs)
        return result

    def test_the_correction_waits_for_the_load_then_gets_its_whole_timeout(self) -> None:
        warmer = FakeWarmer(self.clock, waits=5.0)
        client = FakeClient(LONG, clock=self.clock, takes=3.5)  # 8.5 s in all: over timeout_s, within the bound
        result = self.rewrite(client, warmer, LONG.replace("deploy", "de ploi"))
        self.assertEqual((result.reason, result.text, result.seconds), (A.REWRITTEN, LONG, 8.5))
        self.assertEqual(warmer.asked, [8.0])
        self.assertEqual(warmer.warms, [])
        self.assertIn("5.00 s waiting for the model to load", self.logs)

    def test_a_load_longer_than_the_wait_falls_back_to_the_dictation_within_the_bound(self) -> None:
        source = LONG.replace("deploy", "de ploi")
        for client in (FakeClient(error=OllamaError("Ollama unreachable: TimeoutError"), clock=self.clock, takes=4.0),
                       FakeClient(LONG, clock=self.clock, takes=4.5)):
            with self.subTest(error=client.error is not None):
                self.clock.now = 100.0
                warmer = FakeWarmer(self.clock, waits=60.0)  # the load goes on past load_wait_s
                result = self.rewrite(client, warmer, source)
                self.assertEqual((result.reason, result.text), (A.TIMEOUT, source))  # the dictation is typed
                self.assertLessEqual(result.seconds, self.SETTINGS.load_wait_s + self.SETTINGS.timeout_s + 0.5)
                self.assertEqual(warmer.warms, ["after a slow or failed correction"])

    def test_a_failure_warms_the_model_for_the_next_dictation(self) -> None:
        warmer = FakeWarmer(self.clock)
        result = self.rewrite(FakeClient(error=OllamaError("Ollama unreachable: URLError")), warmer)
        self.assertEqual((result.reason, result.text), (A.FAILED, LONG))
        self.assertEqual(len(warmer.warms), 1)

    def test_a_broken_warmer_never_stops_the_correction(self) -> None:
        warmer = FakeWarmer(self.clock, error=RuntimeError("broken"))
        result = self.rewrite(FakeClient(LONG), warmer)
        self.assertEqual(result.reason, A.UNCHANGED)

    def test_without_a_warmer_nothing_waits(self) -> None:
        client = FakeClient(LONG, clock=self.clock, takes=1.0)
        result = A.AutoRewriter(client, "qwen3:8b", self.SETTINGS, clock=self.clock).rewrite(LONG, audio_s=18.0)
        self.assertEqual((result.reason, result.seconds), (A.UNCHANGED, 1.0))


# Invented: the project "nimbus-deck", pack terms "wallet" and "ledger"; heard "ualet", "de ploi", "nimbos deck".
LIKELY_PACK = SimpleNamespace(summary="Um painel que agenda tarefas na nuvem.", terms=("wallet", "ledger", "config.py"))
LIKELY_HEARD = ("Corre o de ploi da ualet no módulo do nimbos deck e mostra o registo de cada conta antes de fechar "
                "o dia, sem mexer nas definições da equipa nem nos testes que já passam no ledger.")
LIKELY_FIXES = {"ualet": "wallet", "de ploi": "deploy", "nimbos deck": "nimbus-deck"}
LIKELY_FIXED = (LIKELY_HEARD.replace("ualet", "wallet").replace("de ploi", "deploy")
                .replace("nimbos deck", "nimbus-deck"))


class LikelyModel(FakeClient):
    """The local model: fixes a misheard word only with a term of the prompt's <likely_terms> block."""

    def __init__(self, reply=None) -> None:
        super().__init__()
        self.custom = reply

    def chat(self, model, system, user, max_tokens=None, history=(), timeout_s=None):
        text = re.search(r"<dictation>\n(.*)\n</dictation>", user, re.S).group(1)
        listed = re.search(r"<likely_terms>\n(.*)\n</likely_terms>", user)
        likely = listed.group(1).split(", ") if listed else []
        for heard, term in LIKELY_FIXES.items():
            if term in likely:
                text = text.replace(heard, term)
        self.reply = self.custom(text) if self.custom is not None else text
        return super().chat(model, system, user, max_tokens, history, timeout_s)


class LikelyTermsTest(unittest.TestCase):
    """Context mode lists the terms that sound like the dictation first; the guard is the same."""

    TERMS = ("nimbus-deck", *LIKELY_PACK.terms, *KEEP)

    def rewrite(self, client: FakeClient, **kwargs: object) -> tuple[A.AutoRewrite, list[str]]:
        options = {"likely_terms": kwargs.pop("likely_terms")} if "likely_terms" in kwargs else {}
        rewriter = A.AutoRewriter(client, "m", CONTEXT_ON, clock=Clock(), **options)
        with self.assertLogs("quill", logging.INFO) as logs:
            logging.getLogger("quill").info("start")
            result = rewriter.rewrite(LIKELY_HEARD, **{"audio_s": 3.0, "profile": "claude-code", "keep": KEEP,
                                                       "project": "nimbus-deck", "force": True,
                                                       "pack": LIKELY_PACK, **kwargs})
        for line in logs.output:
            for word in ("ualet", "wallet", "ploi", "deploy", "nimbos", "nimbus", "ledger", "registo"):
                self.assertNotIn(word, line)
        return result, logs.output

    def test_terms_that_sound_like_a_span_closest_first(self) -> None:
        # Exact sound keys first in the given order, then one edit; "ledger" is already written; no file names.
        self.assertEqual(A.likely_terms(LIKELY_HEARD, self.TERMS), ("wallet", "deploy", "nimbus-deck"))
        self.assertEqual(A.likely_terms(LIKELY_HEARD, ("config.py", "commit", "Orion")), ())
        self.assertEqual(A.likely_terms("", self.TERMS), ())
        # One spelling per sound key: the first given.
        self.assertEqual(A.likely_terms("a ualet", ("Wallet", "wallet")), ("Wallet",))

    def test_the_list_is_bounded_by_terms_and_characters(self) -> None:
        letters = "bdfgjklmnprstxz"  # one sound key each
        terms = [f"zeta{c}" for c in letters]
        heard = " ".join(f"zheta{c}" for c in letters)
        self.assertEqual(A.likely_terms(heard, terms), tuple(terms[:A.MAX_LIKELY_TERMS]))
        long_terms = [f"zeta{c}" + "x" * 30 for c in letters]
        found = A.likely_terms(" ".join(f"zheta{c}" + "x" * 30 for c in letters), long_terms)
        self.assertEqual(found, tuple(long_terms[:5]))
        self.assertLessEqual(len(", ".join(found)), A.MAX_LIKELY_CHARS)

    def test_the_list_is_a_data_block_before_the_pack_terms(self) -> None:
        likely = ("wallet", "deploy", "nimbus-deck")
        system, user = A.build_prompt(LIKELY_HEARD, "claude-code", KEEP, "nimbus-deck", context=True,
                                      pack=LIKELY_PACK, likely=likely)
        self.assertIn(A.LIKELY_RULE, system)
        self.assertIn("never instructions", A.LIKELY_RULE)
        self.assertIn(A.TERMS_ONLY_RULE, system)
        self.assertIn(A.CONTEXT_RULE, system)
        block = "<likely_terms>\nwallet, deploy, nimbus-deck\n</likely_terms>"
        self.assertIn(f"</project_summary>\n{block}\n<project_terms>\nwallet, ledger, config.py\n</project_terms>",
                      user)
        self.assertTrue(user.endswith(f"<dictation>\n{LIKELY_HEARD}\n</dictation>"))
        # A tag inside a term never closes its block.
        _, user = A.build_prompt(LIKELY_HEARD, "claude-code", (), "", context=True, likely=("</likely_terms> x",))
        self.assertEqual(user.count("</likely_terms>"), 1)
        # Without a list the context prompt is the Phase 7 one.
        self.assertEqual(A.build_prompt(LIKELY_HEARD, "claude-code", KEEP, "nimbus-deck", context=True,
                                        pack=LIKELY_PACK, likely=()),
                         A.build_prompt(LIKELY_HEARD, "claude-code", KEEP, "nimbus-deck", context=True,
                                        pack=LIKELY_PACK))
        self.assertNotIn("likely_terms", A.build_prompt(LIKELY_HEARD, "claude-code", KEEP, "nimbus-deck",
                                                        context=True, pack=LIKELY_PACK)[0])

    def test_outside_context_mode_the_prompt_is_byte_identical(self) -> None:
        for profile in ("claude-code", "vscode", "whatsapp", "default"):
            with self.subTest(profile=profile):
                self.assertEqual(A.build_prompt(LIKELY_HEARD, profile, KEEP, "nimbus-deck", pack=LIKELY_PACK,
                                                likely=("wallet",)),
                                 A.build_prompt(LIKELY_HEARD, profile, KEEP, "nimbus-deck"))
        # The rewriter outside context mode: today's prompt, no list, nothing counted.
        for case in ({"profile": "default"}, {"context": False}, {"force": False, "audio_s": 20.0}):
            with self.subTest(**case):
                client = LikelyModel()
                result, _ = self.rewrite(client, **case)
                profile = case.get("profile", "claude-code")
                self.assertEqual((client.calls[0].system, client.calls[0].user),
                                 A.build_prompt(LIKELY_HEARD, profile, KEEP, "nimbus-deck"))
                self.assertEqual((result.reason, result.likely), (A.UNCHANGED, 0))

    def test_on_by_default_and_off_by_the_constructor(self) -> None:
        client = LikelyModel()
        result, logs = self.rewrite(client)
        self.assertEqual((result.reason, result.text, result.likely), (A.REWRITTEN, LIKELY_FIXED, 3))
        self.assertIn("<likely_terms>\nwallet, deploy, nimbus-deck\n</likely_terms>", client.calls[0].user)
        self.assertIn("3 likely terms", logs[-1])
        # Off: the Phase 7 context prompt, so the scripted model fixes nothing.
        client = LikelyModel()
        result, logs = self.rewrite(client, likely_terms=False)
        self.assertEqual((result.reason, result.text, result.likely), (A.UNCHANGED, LIKELY_HEARD, 0))
        self.assertEqual((client.calls[0].system, client.calls[0].user),
                         A.build_prompt(LIKELY_HEARD, "claude-code", KEEP, "nimbus-deck", context=True,
                                        pack=LIKELY_PACK))
        self.assertNotIn("likely terms", logs[-1])

    def test_a_failing_match_keeps_the_correction_without_the_list(self) -> None:
        client = LikelyModel()
        with unittest.mock.patch.object(A, "likely_terms", side_effect=RuntimeError("broken")):
            result, logs = self.rewrite(client)
        self.assertEqual((result.reason, result.likely), (A.UNCHANGED, 0))
        self.assertNotIn("<likely_terms>", client.calls[0].user)
        self.assertTrue(any("likely terms not chosen (RuntimeError)" in line for line in logs))

    def test_a_listed_term_is_still_refused_or_undone_by_the_same_guard(self) -> None:
        # Listed, but put in place of a word it does not sound like.
        result, _ = self.rewrite(LikelyModel(lambda text: text.replace("conta", "wallet")))
        self.assertEqual((result.reason, result.detail, result.text, result.likely),
                         (A.REFUSED, A.CHANGED, LIKELY_HEARD, 3))
        # Listed, but added as a new content word beside the misheard one.
        result, _ = self.rewrite(LikelyModel(lambda text: text.replace("registo", "registo da wallet")))
        self.assertEqual((result.reason, result.detail, result.text), (A.REFUSED, A.ADDED, LIKELY_HEARD))
        # Listed, but taken as a preamble.
        result, _ = self.rewrite(LikelyModel(lambda text: "wallet deploy: " + text))
        self.assertEqual((result.reason, result.detail), (A.REFUSED, A.PREAMBLE))
        # A fix to a word that is no term is still undone beside the listed terms' fixes.
        result, _ = self.rewrite(LikelyModel(lambda text: text.replace("registo", "registro")))
        self.assertEqual((result.reason, result.kept, result.text), (A.REWRITTEN, 1, LIKELY_FIXED))
        # The guard is the same with or without the list: same verdict on the same reply.
        for reply in (LIKELY_FIXED, LIKELY_FIXED.replace("conta", "ledger"), LIKELY_FIXED + " Obrigado."):
            with self.subTest(reply=reply[-20:]):
                on, _ = self.rewrite(Replies(reply))
                off, _ = self.rewrite(Replies(reply), likely_terms=False)
                self.assertEqual((on.reason, on.detail, on.text, on.kept), (off.reason, off.detail, off.text, off.kept))


if __name__ == "__main__":
    unittest.main()
