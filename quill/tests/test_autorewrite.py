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


if __name__ == "__main__":
    unittest.main()
