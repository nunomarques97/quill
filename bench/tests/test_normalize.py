import unittest

from bench.normalize import EQUIVALENCES, normalize, normalize_words


class NormalizeTest(unittest.TestCase):
    def test_nfc(self):
        decomposed = "cafe\u0301"
        self.assertEqual(normalize(decomposed), "caf\u00e9")

    def test_casefold_keeps_accents(self):
        self.assertEqual(normalize("AÇÃO Está"), "ação está")
        self.assertEqual(normalize("Straße"), "strasse")

    def test_punctuation_removed(self):
        self.assertEqual(normalize("Olá, mundo! Tudo bem?"), "olá mundo tudo bem")
        self.assertEqual(normalize("«citação» (nota) 3,5 \u2026"), "citação nota 35")

    def test_symbols_removed(self):
        self.assertEqual(normalize("preço: 10€ + 5%"), "preço 10 5")

    def test_hyphen_between_letters_becomes_space(self):
        self.assertEqual(normalize("lê-me isso"), "lê me isso")
        self.assertEqual(normalize("modo\u2014rápido"), "modo rápido")

    def test_hyphen_not_between_letters_is_removed(self):
        self.assertEqual(normalize("versão-2 e -x"), "versão2 e x")

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize("  um\t dois \n\n três  "), "um dois três")
        self.assertEqual(normalize_words(" \t "), [])

    def test_equivalences(self):
        self.assertEqual(normalize("abre o VS Code"), "abre o vscode")
        self.assertEqual(normalize("abre o vscode"), "abre o vscode")
        self.assertEqual(normalize("abre o VS-Code"), "abre o vscode")
        self.assertEqual(normalize("usa o Visual Studio Code"), "usa o vscode")
        self.assertEqual(normalize("vê o Read Me"), "vê o readme")
        self.assertEqual(normalize("envia um e-mail"), "envia um email")

    def test_equivalence_only_on_whole_words(self):
        self.assertEqual(normalize("avs code"), "avs code")

    def test_equivalence_canonical_forms_are_single_tokens(self):
        for canonical, variants in EQUIVALENCES.items():
            self.assertEqual(normalize_words(canonical), [canonical])
            for variant in variants:
                self.assertEqual(normalize_words(variant), [canonical])


if __name__ == "__main__":
    unittest.main()
