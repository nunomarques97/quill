"""quill.cleanup tests with invented phrases and a fake LLM client; no Ollama is called."""

import unittest
from dataclasses import dataclass

from quill.cleanup import Cleanup, clean_text, plausible


class FillerTest(unittest.TestCase):
    def test_hesitations_are_always_removed(self):
        self.assertEqual(clean_text("hum abre o painel hã do jardim"), "Abre o painel do jardim.")
        self.assertEqual(clean_text("Hmm, liga a rega."), "Liga a rega.")

    def test_e_pa_in_both_spellings(self):
        self.assertEqual(clean_text("é pá isto não liga"), "Isto não liga.")
        self.assertEqual(clean_text("epá, isto não liga"), "Isto não liga.")
        self.assertEqual(clean_text("a porta, é pá, ficou aberta"), "A porta, ficou aberta.")

    def test_e_as_a_verb_is_kept(self):
        self.assertEqual(clean_text("o bolo é para a festa"), "O bolo é para a festa.")

    def test_pronto_as_a_filler(self):
        self.assertEqual(clean_text("pronto, amanhã levo o bolo"), "Amanhã levo o bolo.")
        self.assertEqual(clean_text("então pronto vamos embora"), "Então vamos embora.")
        self.assertEqual(clean_text("fechei a janela. pronto, já está"), "Fechei a janela. Já está.")

    def test_pronto_closing_a_sentence(self):
        self.assertEqual(clean_text("leva as caixas para a garagem pronto. depois liga-me"), "Leva as caixas para a garagem. Depois liga-me.")
        self.assertEqual(clean_text("arruma a sala pronto"), "Arruma a sala.")
        self.assertEqual(clean_text("compra pão pronto? e leite"), "Compra pão? E leite.")

    def test_pronto_as_content(self):
        self.assertEqual(clean_text("o relatório está pronto"), "O relatório está pronto.")
        self.assertEqual(clean_text("pronto para sair às dez"), "Pronto para sair às dez.")
        self.assertEqual(clean_text("fica pronto a usar amanhã"), "Fica pronto a usar amanhã.")
        self.assertEqual(clean_text("Estado: pronto"), "Estado: pronto.")
        self.assertEqual(clean_text("pronto."), "Pronto.")  # a one-word answer
        # A state verb earlier in the same sentence makes it an adjective.
        self.assertEqual(clean_text("tenho o jantar pronto. vem cedo"), "Tenho o jantar pronto. Vem cedo.")
        self.assertEqual(clean_text("está tudo pronto"), "Está tudo pronto.")
        self.assertEqual(clean_text("deixa-o limpo e pronto"), "Deixa-o limpo e pronto.")
        self.assertEqual(clean_text("respondeu de pronto"), "Respondeu de pronto.")
        # The verb must be in the same sentence.
        self.assertEqual(clean_text("está frio. fecha a porta pronto."), "Está frio. Fecha a porta.")

    def test_tipo_as_a_filler(self):
        self.assertEqual(clean_text("tipo, amanhã não posso"), "Amanhã não posso.")
        self.assertEqual(clean_text("era tipo enorme"), "Era enorme.")

    def test_tipo_as_a_noun(self):
        self.assertEqual(clean_text("que tipo de papel compro"), "Que tipo de papel compro.")
        self.assertEqual(clean_text("o tipo da loja ligou"), "O tipo da loja ligou.")
        self.assertEqual(clean_text("muda o tipo do campo"), "Muda o tipo do campo.")

    def test_tipo_after_a_preposition_is_a_noun(self):
        self.assertEqual(clean_text("agrupa os erros por tipo"), "Agrupa os erros por tipo.")
        self.assertEqual(clean_text("filtra por tipo e data"), "Filtra por tipo e data.")
        self.assertEqual(clean_text("uma variável sem tipo"), "Uma variável sem tipo.")
        self.assertEqual(clean_text("ordena com tipo e tamanho"), "Ordena com tipo e tamanho.")

    def test_tipo_ending_a_sentence_is_a_noun(self):
        self.assertEqual(clean_text("separa as caixas tipo. depois arruma"), "Separa as caixas tipo. Depois arruma.")
        self.assertEqual(clean_text("indica o tamanho e tipo"), "Indica o tamanho e tipo.")

    def test_tipo_filler_between_words_or_commas(self):
        self.assertEqual(clean_text("e tipo uma cópia do mapa"), "E uma cópia do mapa.")
        self.assertEqual(clean_text("a mesa, tipo, azul"), "A mesa, azul.")

    def test_um_and_uma_are_articles(self):
        self.assertEqual(clean_text("compra um pão e uma maçã"), "Compra um pão e uma maçã.")


class RepetitionTest(unittest.TestCase):
    def test_immediate_repetitions_of_one_to_four_words(self):
        self.assertEqual(clean_text("o o gato"), "O gato.")
        self.assertEqual(clean_text("abre a abre a gaveta"), "Abre a gaveta.")
        self.assertEqual(clean_text("põe a caixa em põe a caixa em cima"), "Põe a caixa em cima.")
        self.assertEqual(clean_text("eu acho que era eu acho que era verde"), "Eu acho que era verde.")

    def test_repetition_across_a_comma_for_phrases_only(self):
        self.assertEqual(clean_text("e se, e se chover"), "E se chover.")
        self.assertEqual(clean_text("não, não quero"), "Não, não quero.")

    def test_emphasis_and_numbers_are_kept(self):
        self.assertEqual(clean_text("muito muito bom"), "Muito muito bom.")
        self.assertEqual(clean_text("a senha é 12 12"), "A senha é 12 12.")
        self.assertEqual(clean_text("são dois dois lugares"), "São dois dois lugares.")

    def test_valid_doublets_are_kept(self):
        self.assertEqual(clean_text("ele para para ver o rio"), "Ele para para ver o rio.")
        self.assertEqual(clean_text("se se fizer isso, avisa"), "Se se fizer isso, avisa.")

    def test_sentence_boundary_is_not_a_repetition(self):
        self.assertEqual(clean_text("Fecha. Fecha a porta."), "Fecha. Fecha a porta.")


class PunctuationTest(unittest.TestCase):
    def test_spacing_and_repeated_marks(self):
        self.assertEqual(clean_text("olá ,  tudo bem ??"), "Olá, tudo bem?")
        self.assertEqual(clean_text("chega,."), "Chega.")
        self.assertEqual(clean_text("sério?!"), "Sério?!")

    def test_final_mark_and_capitals_after_sentence_ends(self):
        self.assertEqual(clean_text("liga a luz. depois fecha a porta"), "Liga a luz. Depois fecha a porta.")
        self.assertEqual(clean_text("já está? sim"), "Já está? Sim.")
        self.assertEqual(clean_text("abre a lista,"), "Abre a lista.")
        self.assertEqual(clean_text("liga a luz", final_mark=False), "Liga a luz")

    def test_nothing_is_lowercased(self):
        self.assertEqual(clean_text("Liga A Luz"), "Liga A Luz.")

    def test_empty_and_filler_only(self):
        self.assertEqual(clean_text(""), "")
        self.assertEqual(clean_text("  hum  hã "), "")


class PreservedTest(unittest.TestCase):
    def test_english_terms_names_and_numbers(self):
        text = "faz o deploy do zeta-board para a branch main às 14:30 com 3 workers"
        self.assertEqual(clean_text(text), "Faz o deploy do zeta-board para a branch main às 14:30 com 3 workers.")

    def test_keep_words_are_never_capitalized_or_removed(self):
        self.assertEqual(clean_text("npm install corre. zeta-board abre", keep=["npm", "zeta-board"]),
                         "npm install corre. zeta-board abre.")
        self.assertEqual(clean_text("abre o Tipo agora", keep=["Tipo"]), "Abre o Tipo agora.")
        self.assertEqual(clean_text("tipo abre", keep=["tipo"]), "tipo abre.")

    def test_mixed_case_and_symbols_are_untouched(self):
        self.assertEqual(clean_text("iPhone novo. config.toml mudou. @ana viu"),
                         "iPhone novo. config.toml mudou. @ana viu.")


class IdempotenceTest(unittest.TestCase):
    PHRASES = [
        "hum abre a abre a gaveta, tipo, já",
        "então pronto, o o relatório está pronto para review.",
        "é pá não, não sei . depois vejo",
        "e se, e se fores amanhã ?? hã",
        "Liga o VS Code. pronto. commit e push",
        "tipo tipo de dados",
        "agrupa por tipo, tipo, e ordena por tipo",
        "ele para para para ver",
        "",
    ]

    def test_cleaning_twice_changes_nothing(self):
        for phrase in self.PHRASES:
            once = clean_text(phrase)
            self.assertEqual(clean_text(once), once, phrase)


@dataclass
class Reply:
    content: object


class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def chat(self, model, system, user, max_tokens=None):
        self.calls.append((model, system, user, max_tokens))
        if self.error:
            raise self.error
        return Reply(self.reply)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.25
        return self.now


class LlmTest(unittest.TestCase):
    def test_rules_mode_never_calls_a_client(self):
        client = FakeClient("x")
        result = Cleanup("rules", client=client)("hum liga a luz")
        self.assertEqual((result.text, result.mode, result.fallback), ("Liga a luz.", "rules", None))
        self.assertEqual(client.calls, [])

    def test_llm_reply_is_used_and_tidied_by_the_rules(self):
        client = FakeClient("liga a luz da sala")
        cleanup = Cleanup("llm", client=client, keep=["zeta-board"], clock=FakeClock())
        result = cleanup("hum liga a luz da sala")
        self.assertEqual((result.text, result.mode, result.fallback, result.llm_s), ("Liga a luz da sala.", "llm", None, 0.25))
        model, system, user, max_tokens = client.calls[0]
        self.assertEqual((model, user), ("qwen3:8b", "hum liga a luz da sala"))
        self.assertIn("zeta-board", system)
        self.assertGreaterEqual(max_tokens, 64)

    def test_falls_back_to_rules_when_ollama_fails(self):
        for error in (ConnectionError("down"), TimeoutError(), RuntimeError("HTTP 500")):
            result = Cleanup("llm", client=FakeClient(error=error))("hum liga a luz")
            self.assertEqual((result.text, result.mode), ("Liga a luz.", "rules"))
            self.assertTrue(result.fallback.startswith("llm failed: "))

    def test_falls_back_on_implausible_replies(self):
        cases = {
            "": "llm empty reply",
            "Aqui está o texto limpo: liga a luz": "llm reply has a preamble or explanation",
            "liga": "llm reply length out of bounds",
            "liga a luz da sala e depois fecha a porta e as janelas todas": "llm reply length out of bounds",
        }
        for reply, reason in cases.items():
            result = Cleanup("llm", client=FakeClient(reply))("hum liga a luz da sala")
            self.assertEqual((result.text, result.mode, result.fallback), ("Liga a luz da sala.", "rules", reason), reply)
        result = Cleanup("llm", client=FakeClient(None))("liga a luz")
        self.assertEqual((result.mode, result.fallback), ("rules", "llm returned no text"))

    def test_empty_text_skips_the_llm(self):
        client = FakeClient("x")
        self.assertEqual(Cleanup("llm", client=client)("  ").text, "")
        self.assertEqual(client.calls, [])

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):
            Cleanup("smart")
        with self.assertRaises(ValueError):
            Cleanup("llm")

    def test_plausible(self):
        self.assertIsNone(plausible("liga a luz", "Liga a luz."))
        self.assertEqual(plausible("a b", "a\n\nb"), "reply has a preamble or explanation")


if __name__ == "__main__":
    unittest.main()
