"""The term list and shape of a Claude Code reply, derived from its visible text.

Every message here is invented; the words stand for the kind of text Claude
Code writes, not for any real conversation.
"""

from __future__ import annotations

import unittest

from quill import reply_terms as R
from quill.autorewrite import sound_key

SECRET_WORD = "brumaflex"  # an invented word that must never reach a repr


def derive(text: str | None, **bounds: int) -> R.ReplyContext:
    return R.derive(text, **bounds)


class EmptyTest(unittest.TestCase):
    def test_no_text_gives_an_empty_context(self) -> None:
        for text in (None, "", "   ", "\n\t \n"):
            with self.subTest(text=text):
                context = derive(text)
                self.assertEqual(context, R.EMPTY)
                self.assertEqual(context.terms, ())
                self.assertEqual(context.options, ())
                self.assertFalse(context.asks)
                self.assertEqual(context.words, 0)
                self.assertFalse(context)

    def test_only_function_words_and_hesitations_give_no_terms(self) -> None:
        context = derive("Hum, e isso é para o que se vê.")
        self.assertEqual(context.terms, ())
        self.assertFalse(context.asks)
        self.assertGreater(context.words, 0)

    def test_negative_bounds_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            derive("Ficou pronto.", max_terms=-1)
        with self.assertRaises(ValueError):
            derive("Ficou pronto.", max_chars=-1)


class CodeTest(unittest.TestCase):
    def test_a_fenced_block_gives_identifiers_only(self) -> None:
        text = (
            "Mudei o leitor.\n\n"
            "```python\n"
            "def bramble_reader(folder):\n"
            "    value = GlimmerStore().load_tail(folder)\n"
            "    return value  # retorna sempre o valor\n"
            "```\n"
        )
        terms = derive(text).terms
        for name in ("bramble_reader", "GlimmerStore", "load_tail"):
            self.assertIn(name, terms)
        for word in ("def", "folder", "value", "return", "retorna", "sempre", "valor"):
            self.assertNotIn(word, terms)
        self.assertIn("leitor", terms)

    def test_a_tilde_fence_and_an_unclosed_fence_hold_code(self) -> None:
        terms = derive("Antes.\n\n~~~\nplain words here zorbic_flag\n~~~\n\n```\nmore plain stuff\n").terms
        self.assertIn("zorbic_flag", terms)
        for word in ("plain", "words", "here", "more", "stuff"):
            self.assertNotIn(word, terms)

    def test_inline_code_gives_identifiers_only(self) -> None:
        terms = derive("Corre `pytest quill` e depois `--dry-run` com `GlimmerStore`, `read_tail` e `quill.wobble_cache`.").terms
        for name in ("dry-run", "GlimmerStore", "read_tail", "wobble_cache"):
            self.assertIn(name, terms)
        for word in ("pytest", "quill"):
            self.assertNotIn(word, terms)
        self.assertIn("corre", terms)
        self.assertIn("depois", terms)

    def test_file_names_keep_their_base_name(self) -> None:
        terms = derive("Editei `src/zorblat/parser_core.py` e docs/notas.md, e também o README.md.").terms
        self.assertIn("parser_core.py", terms)
        self.assertIn("notas.md", terms)
        self.assertIn("README.md", terms)
        for part in ("src", "zorblat", "docs"):
            self.assertNotIn(part, terms)

    def test_identifiers_come_before_content_words(self) -> None:
        terms = derive("Atualizei a configuração do `wobble_cache` hoje.").terms
        self.assertEqual(terms[0], "wobble_cache")


class UrlTest(unittest.TestCase):
    def test_a_url_gives_only_its_file_base_name(self) -> None:
        terms = derive("Vê https://example.org/manual/zorbic-setup.html e também https://example.org/pagina/aqui.").terms
        self.assertIn("zorbic-setup.html", terms)
        for word in ("https", "example", "manual", "pagina", "aqui", "example.org"):
            self.assertNotIn(word, terms)

    def test_a_bare_host_and_a_link_target_give_nothing_else(self) -> None:
        terms = derive("Abre [o guia](https://example.org/docs/guia.md#topo) ou www.example.org agora.").terms
        self.assertIn("guia.md", terms)
        self.assertIn("guia", terms)  # the link text is prose
        self.assertIn("abre", terms)
        self.assertNotIn("docs", terms)
        self.assertNotIn("topo", terms)
        self.assertFalse(any("example" in term for term in terms))

    def test_a_windows_path_gives_only_its_file_base_name(self) -> None:
        terms = derive("O ficheiro C:\\Pasta\\Outra\\zorblat_notes.txt mudou.").terms
        self.assertIn("zorblat_notes.txt", terms)
        self.assertNotIn("Pasta", terms)
        self.assertNotIn("Outra", terms)


class MarkdownTest(unittest.TestCase):
    def test_markers_are_stripped(self) -> None:
        text = "## Resumo\n\n- **Glimmer** ficou _pronto_\n> citação antiga\n\n| coluna | valor |\n|---|---|\n<b>negrito</b>"
        terms = derive(text).terms
        for word in ("resumo", "glimmer", "pronto", "citação", "antiga", "coluna", "valor", "negrito"):
            self.assertIn(word, terms)
        self.assertFalse(any(mark in term for term in terms for mark in "*#>|<"))


class DropTest(unittest.TestCase):
    def test_digit_bearing_words_are_dropped(self) -> None:
        terms = derive("Usei utf8 com v2 e 3 tentativas em `base64_codec` e `Sha256Hasher` no ficheiro run2.py.").terms
        for term in terms:
            self.assertFalse(any(ch.isdigit() for ch in term), term)
        self.assertIn("tentativas", terms)

    def test_number_and_polarity_words_and_short_words_are_dropped(self) -> None:
        terms = derive("Faltam dois ficheiros, nunca três; nada mudou, mas o bug sim.").terms
        for word in ("dois", "três", "nunca", "nada", "bug", "sim", "mas"):
            self.assertNotIn(word, terms)
        self.assertIn("faltam", terms)
        self.assertIn("ficheiros", terms)
        self.assertIn("mudou", terms)

    def test_a_sentence_start_capital_is_dropped_but_a_name_keeps_it(self) -> None:
        terms = derive("Funciona agora. Configurei o Glimmer ontem.").terms
        self.assertIn("funciona", terms)
        self.assertIn("configurei", terms)
        self.assertIn("Glimmer", terms)


class OptionsTest(unittest.TestCase):
    def test_numbered_options(self) -> None:
        text = "Há duas formas:\n\n1. **Manter** a cache local\n2) Apagar o `pointer_file` antigo\n3. Reescrever tudo"
        context = derive(text)
        self.assertEqual([option.label for option in context.options], ["1", "2", "3"])
        self.assertEqual(context.options[0].head, ("manter", "cache", "local"))
        self.assertEqual(context.options[1].head, ("apagar", "pointer_file", "antigo"))
        self.assertEqual(context.options[2].head, ("reescrever", "tudo"))
        self.assertEqual(context.terms[:8], ("manter", "cache", "local", "apagar", "pointer_file", "antigo",
                                             "reescrever", "tudo"))
        self.assertTrue(context.asks)

    def test_lettered_options(self) -> None:
        for text in ("a) keep the zorbic cache\nb) drop the cache", "(A) keep it\n(B) drop it",
                     "A: keep the cache\nB: drop the cache", "- A. keep the cache\n- B. drop the cache"):
            with self.subTest(text=text):
                context = derive(text)
                self.assertEqual([option.label.casefold() for option in context.options], ["a", "b"])
                self.assertTrue(context.asks)
                self.assertEqual(context.terms[0], "keep")

    def test_named_options(self) -> None:
        context = derive("Opção A: manter o leitor\nOpção B — trocar o leitor")
        self.assertEqual([option.label for option in context.options], ["A", "B"])
        self.assertEqual(context.options[0].head, ("manter", "leitor"))
        self.assertEqual(context.options[1].head, ("trocar", "leitor"))
        english = derive("Option 1: keep\nOption 2: `GlimmerStore` rewrite\n### Option 3 - drop all")
        self.assertEqual([option.label for option in english.options], ["1", "2", "3"])
        self.assertEqual(english.options[1].head, ("GlimmerStore", "rewrite"))
        self.assertEqual(english.options[2].head, ("drop",))

    def test_the_last_list_is_the_offer(self) -> None:
        text = ("Fiz isto:\n\n1. corrigi o parser\n2. atualizei os testes\n\nE agora?\n\n"
                "1. publicar já\n2. esperar pela revisão")
        context = derive(text)
        self.assertEqual([option.head[0] for option in context.options], ["publicar", "esperar"])
        self.assertEqual(context.terms[:3], ("publicar", "esperar", "revisão"))

    def test_a_single_item_is_not_an_offer(self) -> None:
        context = derive("Nota:\n\n1. o parser mudou")
        self.assertEqual(context.options, ())
        self.assertFalse(context.asks)

    def test_options_are_bounded(self) -> None:
        text = "\n".join(f"{n}. escolha número {n}" for n in range(1, 15))
        self.assertEqual(len(derive(text).options), R.MAX_OPTIONS)


class QuestionTest(unittest.TestCase):
    def test_a_question_in_the_last_paragraph_asks(self) -> None:
        self.assertTrue(derive("Corrigi o parser.\n\nQueres que continue com os testes?").asks)
        self.assertTrue(derive("Done.\n\nShould I also update the docs?").asks)

    def test_no_question_does_not_ask(self) -> None:
        self.assertFalse(derive("Corrigi o parser e os testes passam.").asks)

    def test_a_question_only_in_an_earlier_paragraph_does_not_ask(self) -> None:
        self.assertFalse(derive("Porque falhava? Faltava um caso.\n\nJá está corrigido.").asks)

    def test_a_question_mark_in_a_url_or_code_does_not_ask(self) -> None:
        self.assertFalse(derive("Vê https://example.org/page?x=1 e `query?` hoje.").asks)

    def test_a_code_block_after_the_question_still_asks(self) -> None:
        self.assertTrue(derive("Aplico esta alteração?\n\n```\nzorbic_flag = True\n```").asks)


class OrderTest(unittest.TestCase):
    def test_the_last_paragraph_comes_first_within_each_kind(self) -> None:
        terms = derive("Antes alterei `alpha_mod`.\n\nDepois testei `beta_mod`.").terms
        self.assertEqual(terms[:2], ("beta_mod", "alpha_mod"))
        self.assertLess(terms.index("depois"), terms.index("antes"))
        self.assertLess(terms.index("depois"), terms.index("testei"))

    def test_sound_alike_terms_are_kept_once(self) -> None:
        terms = derive("O cache e a Cache ficaram.\n\nA Kache mudou.").terms
        keys = [sound_key(term) for term in terms]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual([term for term in terms if sound_key(term) == sound_key("cache")], ["Kache"])

    def test_the_same_input_gives_the_same_output(self) -> None:
        text = "Opção A: manter\nOpção B: trocar\n\n`GlimmerStore` em docs/guia.md. Continuo?"
        self.assertEqual(derive(text), derive(text))
        self.assertEqual(derive(text).terms, derive(text).terms)


class BoundsTest(unittest.TestCase):
    TEXT = "Revi alfabeto, batata, cenoura, damasco, espinafre, framboesa, groselha e hortelã."

    def test_max_terms(self) -> None:
        self.assertEqual(len(derive(self.TEXT).terms), 9)
        self.assertEqual(derive(self.TEXT, max_terms=3).terms, ("revi", "alfabeto", "batata"))
        self.assertEqual(derive(self.TEXT, max_terms=0).terms, ())

    def test_max_chars(self) -> None:
        terms = derive(self.TEXT, max_chars=24).terms
        self.assertLessEqual(len(R.SEPARATOR.join(terms)), 24)
        self.assertEqual(terms, ("revi", "alfabeto", "batata"))
        self.assertEqual(derive(self.TEXT, max_chars=3).terms, ())
        for limit in range(0, 120, 7):
            self.assertLessEqual(len(R.SEPARATOR.join(derive(self.TEXT, max_chars=limit).terms)), limit)

    def test_a_long_text_keeps_its_end(self) -> None:
        text = "começo " + "para " * (R.MAX_INPUT_CHARS // 5 + 10) +"\n\nfinalmente pronto?"
        context = derive(text)
        self.assertIn("finalmente", context.terms)
        self.assertNotIn("começo", context.terms)
        self.assertTrue(context.asks)

    def test_an_overlong_token_is_dropped(self) -> None:
        self.assertEqual(derive("a" * 80 + "bc").terms, ())


class PrivacyTest(unittest.TestCase):
    def test_repr_shows_counts_only(self) -> None:
        context = derive(f"Opção A: {SECRET_WORD}\nOpção B: `{SECRET_WORD}_mod`\n\nO {SECRET_WORD} serve?")
        self.assertIn(SECRET_WORD, context.terms)
        for shown in (repr(context), str(context), repr(context.options), str(context.options)):
            self.assertNotIn(SECRET_WORD, shown)
        self.assertEqual(repr(context), f"ReplyContext(terms={len(context.terms)}, options=2, asks=True, "
                                        f"words={context.words})")


class MixedTest(unittest.TestCase):
    def test_a_mixed_message(self) -> None:
        text = (
            "Corrigi o bug no `SessionGlimmer` e atualizei docs/USAR.md.\n\n"
            "```ts\nexport function loadZorbic(path: string) { return readTail(path) }\n```\n\n"
            "The tests pass now. Queres que faça o commit ou preferes rever primeiro?"
        )
        context = derive(text)
        self.assertTrue(context.asks)
        self.assertEqual(context.options, ())
        for term in ("SessionGlimmer", "USAR.md", "loadZorbic", "readTail", "tests", "pass", "commit", "rever",
                     "preferes", "corrigi"):
            self.assertIn(term, context.terms)
        for word in ("export", "function", "string", "return", "path"):
            self.assertNotIn(word, context.terms)
        identifiers = [context.terms.index(name) for name in ("loadZorbic", "SessionGlimmer", "USAR.md")]
        self.assertLess(max(identifiers), context.terms.index("tests"))


if __name__ == "__main__":
    unittest.main()
