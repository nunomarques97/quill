"""Text cleanup of a dictated utterance, before it is typed.

The default mode is deterministic rules (``clean_text``), applied in passes
until nothing changes, so cleaning twice gives the same text:

1. Fillers. ``hum``-like hesitations and ``é pá`` (also written ``epá``)
   are always removed. ``pronto`` is removed only at a clause boundary (text
   start, after punctuation or after a connective such as ``então``) and not
   before ``para``/``a`` ("pronto para"); after a verb ("está pronto") it
   is content. As the last word of a sentence it is also a filler ("…no
   menu pronto. Depois"), unless a state verb earlier in that sentence
   makes it an adjective ("tenho o relatório pronto", "está tudo pronto")
   or it follows ``de`` ("de pronto"). ``tipo`` is removed unless it is a noun: after a determiner or
   preposition ("o tipo", "do tipo", "por tipo", "sem tipo"), before ``de``
   ("tipo de dados") or as the last word of a sentence right after another
   word ("agrupa as tarefas tipo.").
   A comma that belonged to a removed filler goes with it; a sentence end
   moves to the previous word.
2. Immediate repetitions of 1 to 4 words ("põe a põe a"): the first copy
   is removed. Copies with a number are kept ("12 12"), and so are single
   emphatic words ("não, não", "muito muito") and valid doublets ("para
   para", "se se").
3. Punctuation and spacing: stray punctuation joins the previous word,
   repeated marks collapse, and the text ends with a sentence mark.
4. Capitalization: the first word and every word after ``.``, ``?`` or
   ``!`` start with a capital letter. Nothing is ever lowercased, and words
   in ``keep`` (names, terms) or with digits, capitals or inner symbols
   are never changed.

Rules are generic Portuguese dictation rules, never keyed to a script.

The optional ``llm`` mode asks a local Ollama model (``qwen3:8b``) to clean
the text and then applies the rules to its reply. ``style`` (the active-window
profile's instruction and the user's style samples, ``quill.profiles``) is
added to its prompt; the rules mode never reads it. Any failure (Ollama down,
timeout, an empty or implausible reply) falls back to the rules, so a
dictation is never lost. The client is injected: any object with
``chat(model, system, user, max_tokens=...)`` returning an object with a
``content`` string, such as the loopback-only Ollama client.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Hesitation sounds. Never "um"/"uma" (articles) or "é" (a verb).
HESITATIONS = frozenset({"hum", "humm", "hummm", "hmm", "hm", "hã", "hãã", "ãh", "ahn", "uhm", "ehm"})
# "é pá" written as one word.
EPA_WORDS = frozenset({"epá", "épá", "ehpá", "é-pá"})
EPA_FIRST = frozenset({"é", "eh"})
# Words after which "pronto" still starts a clause ("então pronto", "mas pronto").
PRONTO_CONNECTIVES = frozenset({"então", "portanto", "olha", "bem", "ok", "pois", "mas", "enfim"})
# "pronto para sair", "pronto a usar": an adjective, not a filler.
PRONTO_COMPLEMENTS = frozenset({"para", "pra", "p'ra", "a"})
# Verbs that make a sentence-final "pronto" an adjective ("fica pronto",
# "tenho o relatório pronto"); clitic forms match on the verb ("deixa-o").
PRONTO_STATE_VERBS = frozenset({
    "estar", "está", "estás", "estou", "estamos", "estão", "estava", "estavas", "estávamos", "estavam",
    "esteve", "estive", "estiveram", "esteja", "estejam", "estará", "estarão", "estaria", "tá", "tou",
    "ficar", "fica", "ficas", "fico", "ficamos", "ficam", "ficou", "fiquei", "ficaram", "ficava", "fique",
    "fiquem", "ficará", "ficaria",
    "deixar", "deixa", "deixas", "deixo", "deixamos", "deixam", "deixou", "deixei", "deixe", "deixem", "deixá",
    "ter", "tenho", "tens", "tem", "temos", "têm", "tinha", "tive", "teve", "tenha", "terá", "teria",
    "pôr", "põe", "ponho", "pôs", "pus", "ponha", "meter", "mete", "meto", "meteu",
    "ser", "é", "são", "era", "foi", "seja", "será", "parece", "parecia", "considero", "dou", "dá", "deu",
})
# Words before a noun "tipo" ("o tipo", "do tipo", "que tipo", "qualquer tipo").
TIPO_DETERMINERS = frozenset({
    "o", "um", "do", "no", "ao", "pelo", "dum", "num", "este", "esse", "aquele", "deste", "desse",
    "daquele", "neste", "nesse", "naquele", "que", "qual", "qualquer", "cada", "outro", "mesmo",
    "novo", "certo", "algum", "nenhum", "meu", "teu", "seu", "nosso", "vosso", "todo", "tal",
    "próprio", "primeiro", "último", "único", "bom", "mau",
})
# Plain prepositions before a noun "tipo" ("por tipo", "sem tipo", "com tipo").
TIPO_PREPOSITIONS = frozenset({
    "a", "por", "com", "sem", "para", "pra", "p'ra", "em", "de", "sobre", "entre", "até", "contra",
    "após", "desde", "perante",
})
# "tipo de dados": a noun followed by a preposition.
TIPO_COMPLEMENTS = frozenset({"de", "da", "do", "das", "dos", "d'"})
# Single words whose repetition is emphasis, not a stumble.
EMPHATIC = frozenset({
    "não", "sim", "muito", "muita", "muitos", "muitas", "tão", "bem", "pois", "já", "ah", "ha", "olá",
    "adeus", "bye", "obrigado", "obrigada", "xau", "tchau", "nunca", "sempre", "mais",
    # Valid doublets: "ele para para ver", "se se fizer isso".
    "para", "se",
})
NUMBER_WORDS = frozenset({
    "zero", "dois", "duas", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez", "onze",
    "doze", "vinte", "trinta", "cem", "cento", "mil", "milhão", "primeiro", "segundo", "terceiro",
})
MAX_REPEAT = 4
OPENERS = "\"'([{«“‘¿¡"
CLOSERS = "\"')]}»”’"
MARKS = ".,;:!?…"
SENTENCE_END = ".!?"
MAX_PASSES = 20


@dataclass
class Token:
    lead: str
    core: str
    trail: str

    @property
    def key(self) -> str:
        return unicodedata.normalize("NFC", self.core).casefold()

    @property
    def marks(self) -> str:
        return "".join(char for char in self.trail if char in MARKS)

    def ends_sentence(self) -> bool:
        marks = self.marks
        return bool(marks) and marks[-1] in SENTENCE_END and not marks.endswith("..")

    def text(self) -> str:
        return self.lead + self.core + self.trail


def tokenize(text: str) -> list[Token]:
    """Words with their leading and trailing punctuation; pure punctuation joins the previous word."""
    tokens: list[Token] = []
    for raw in unicodedata.normalize("NFC", text).split():
        start, end = 0, len(raw)
        while start < end and raw[start] in OPENERS:
            start += 1
        while end > start and (raw[end - 1] in MARKS or raw[end - 1] in CLOSERS):
            end -= 1
        core = raw[start:end]
        if not core:
            if tokens:
                tokens[-1].trail += raw
            continue
        tokens.append(Token(raw[:start], core, raw[end:]))
    return tokens


def _set_marks(token: Token, marks: str) -> None:
    """Replace the punctuation marks of ``token``'s trail, keeping closing brackets."""
    closers = "".join(char for char in token.trail if char not in MARKS)
    token.trail = marks + closers


def _merge_end(previous: Token, removed: Token) -> None:
    """A removed filler's sentence end moves to the previous word."""
    if removed.ends_sentence() and not previous.ends_sentence():
        _set_marks(previous, removed.marks[-1])


def _boundary(tokens: list[Token], index: int) -> bool:
    if index == 0:
        return True
    previous = tokens[index - 1]
    # Not after a colon: "Estado: pronto" is content.
    return any(char in previous.marks for char in ",;.!?…") or previous.key in PRONTO_CONNECTIVES


def _sentence_final_marker(tokens: list[Token], index: int) -> bool:
    """A "pronto" that closes its sentence with no state verb or "de" before it."""
    if index + 1 < len(tokens) and not tokens[index].ends_sentence():
        return False
    previous = tokens[index - 1]
    if ":" in previous.marks or previous.key == "de":
        return False
    for token in reversed(tokens[:index]):
        if token.ends_sentence():
            break
        if token.key.split("-")[0] in PRONTO_STATE_VERBS:
            return False
    return True


def _is_filler(tokens: list[Token], index: int, keep: frozenset[str]) -> int:
    """How many tokens starting at ``index`` form a filler (0 when none)."""
    token = tokens[index]
    key = token.key
    if key in keep:
        return 0
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    if key in HESITATIONS or key in EPA_WORDS:
        return 1
    if key in EPA_FIRST and not token.marks and following is not None and following.key == "pá":
        return 2
    if key == "pronto":
        if following is not None and not token.marks and following.key in PRONTO_COMPLEMENTS:
            return 0
        if _boundary(tokens, index):
            return 1
        return 1 if _sentence_final_marker(tokens, index) else 0
    if key == "tipo":
        previous = tokens[index - 1] if index else None
        if previous is not None and not previous.marks:
            if previous.key in TIPO_DETERMINERS or previous.key in TIPO_PREPOSITIONS:
                return 0
            # "agrupa as tarefas tipo.": a sentence-final noun, not a filler.
            if following is None or token.ends_sentence():
                return 0
        if following is not None and not token.marks and following.key in TIPO_COMPLEMENTS:
            return 0
        return 1
    return 0


def remove_fillers(tokens: list[Token], keep: frozenset[str]) -> list[Token]:
    out: list[Token] = []
    index = 0
    while index < len(tokens):
        size = _is_filler(tokens, index, keep)
        if not size:
            out.append(tokens[index])
            index += 1
            continue
        last = tokens[index + size - 1]
        if out:
            _merge_end(out[-1], last)
        index += size
    if not out:
        # A lone "Pronto." is an answer, not a filler.
        out = [token for token in tokens if token.key == "pronto"][:1]
    return out


def _is_number(key: str) -> bool:
    return any(char.isdigit() for char in key) or key in NUMBER_WORDS


def _copies_match(tokens: list[Token], start: int, size: int) -> bool:
    first = tokens[start : start + size]
    second = tokens[start + size : start + 2 * size]
    if len(second) < size or [t.key for t in first] != [t.key for t in second]:
        return False
    if any(_is_number(t.key) for t in first):
        return False
    if size == 1 and first[0].key in EMPHATIC:
        return False
    # No punctuation inside the copies; between them at most a comma, and only
    # for phrases ("não, não" is an answer, "e se, e se" a stumble).
    if any(t.marks for t in first[:-1] + second[:-1]):
        return False
    between = first[-1].marks
    return between == "" or (between == "," and size > 1)


def remove_repetitions(tokens: list[Token]) -> list[Token]:
    for size in range(MAX_REPEAT, 0, -1):
        index = 0
        while index + 2 * size <= len(tokens):
            if _copies_match(tokens, index, size):
                tokens[index + size].lead = tokens[index].lead + tokens[index + size].lead
                del tokens[index : index + size]
            else:
                index += 1
    return tokens


def _tidy_marks(marks: str) -> str:
    if not marks:
        return ""
    if "…" in marks or "..." in marks:
        return "..."
    ends = [char for char in marks if char in SENTENCE_END]
    if ends:
        # "?!" stays; ",." or ". ," become one end mark.
        kept = "".join(dict.fromkeys(ends))
        return kept if kept in ("?!", "!?") else ends[0]
    return marks[-1]


def tidy_punctuation(tokens: list[Token], final_mark: bool = True) -> list[Token]:
    for token in tokens:
        closers = "".join(char for char in token.trail if char not in MARKS)
        token.trail = _tidy_marks(token.marks) + closers
    if tokens and final_mark:
        last = tokens[-1]
        marks = last.marks
        if not marks or marks[-1] in ",;:":
            _set_marks(last, ".")
    return tokens


def _can_capitalize(token: Token, keep: frozenset[str]) -> bool:
    core = token.core
    if token.key in keep or not core[0].isalpha() or not core[0].islower():
        return False
    return not any(char.isdigit() or char.isupper() or char in "._/\\@#" for char in core[1:])


def capitalize(tokens: list[Token], keep: frozenset[str]) -> list[Token]:
    start = True
    for token in tokens:
        if start and _can_capitalize(token, keep):
            token.core = token.core[0].upper() + token.core[1:]
        start = token.ends_sentence()
    return tokens


def _keep_set(keep: Iterable[str]) -> frozenset[str]:
    return frozenset(unicodedata.normalize("NFC", word).casefold() for term in keep for word in term.split())


def clean_text(text: str, keep: Iterable[str] = (), final_mark: bool = True) -> str:
    """The rule-based cleanup of ``text``; idempotent. ``keep`` lists words never touched."""
    protected = _keep_set(keep)
    current = " ".join(text.split())
    for _ in range(MAX_PASSES):
        tokens = tokenize(current)
        tokens = remove_fillers(tokens, protected)
        tokens = remove_repetitions(tokens)
        tokens = tidy_punctuation(tokens, final_mark)
        tokens = capitalize(tokens, protected)
        cleaned = " ".join(token.text() for token in tokens)
        if cleaned == current:
            break
        current = cleaned
    return current


# ---------------------------------------------------------------- local LLM

LLM_MODEL = "qwen3:8b"
LLM_SYSTEM = (
    "You clean up dictated text. The text is European Portuguese and may contain English "
    "technical terms and names. Add punctuation and capitalization. Remove filler words "
    "(such as hum, hã, é pá, tipo or pronto when they are only fillers) and accidental "
    "repetitions. Keep every English term, name and number exactly as written. Do not "
    "translate, paraphrase, summarize, answer or add anything. Reply with the cleaned text only."
)
KEEP_LINE = "\nSpell these exactly like this: "
# Reply cap and plausibility bounds, relative to the input's word count.
TOKENS_PER_WORD = 4
MIN_TOKENS = 64
MIN_WORD_RATIO = 0.5
MAX_WORD_RATIO = 1.3
PREAMBLES = ("aqui está", "aqui tens", "texto limpo", "here is", "here's", "cleaned text", "claro", "sure")


@dataclass(frozen=True)
class CleanupResult:
    text: str
    mode: str  # "rules" or "llm"
    fallback: str | None = None  # why the LLM was not used, when it was asked for
    llm_s: float | None = None


def plausible(source: str, reply: str) -> str | None:
    """None when ``reply`` can replace ``source``; otherwise the reason."""
    words, answer = len(source.split()), len(reply.split())
    if not reply.strip():
        return "empty reply"
    lowered = reply.strip().casefold()
    if lowered.startswith(PREAMBLES) or "\n\n" in reply.strip():
        return "reply has a preamble or explanation"
    if answer < MIN_WORD_RATIO * words or answer > MAX_WORD_RATIO * words + 2:
        return "reply length out of bounds"
    return None


class Cleanup:
    """Rules by default; ``mode="llm"`` asks the local model and falls back to rules."""

    def __init__(
        self,
        mode: str = "rules",
        client: object | None = None,
        model: str = LLM_MODEL,
        keep: Iterable[str] = (),
        clock: Callable[[], float] = time.perf_counter,
        style: str = "",
    ) -> None:
        if mode not in ("rules", "llm"):
            raise ValueError("cleanup mode must be rules or llm")
        if mode == "llm" and client is None:
            raise ValueError("the llm cleanup needs an Ollama client")
        self.mode = mode
        self.client = client
        self.model = model
        self.keep = tuple(keep)
        self._clock = clock
        # The active-window profile's instruction and style samples
        # (``quill.profiles.style_prompt``); used only by the llm mode.
        self.style = style.strip()

    def system_prompt(self) -> str:
        prompt = LLM_SYSTEM + (KEEP_LINE + ", ".join(self.keep) + "." if self.keep else "")
        return prompt + ("\n" + self.style if self.style else "")

    def __call__(self, text: str) -> CleanupResult:
        if self.mode == "rules" or not text.strip():
            return CleanupResult(clean_text(text, self.keep), "rules")
        started = self._clock()
        try:
            reply = self.client.chat(
                self.model, self.system_prompt(), text, max_tokens=max(MIN_TOKENS, TOKENS_PER_WORD * len(text.split()))
            ).content
        except Exception as exc:  # Ollama down, timeout, HTTP error: never lose the dictation
            return CleanupResult(clean_text(text, self.keep), "rules", f"llm failed: {type(exc).__name__}")
        elapsed = self._clock() - started
        if not isinstance(reply, str):
            return CleanupResult(clean_text(text, self.keep), "rules", "llm returned no text", elapsed)
        problem = plausible(text, reply)
        if problem:
            return CleanupResult(clean_text(text, self.keep), "rules", f"llm {problem}", elapsed)
        return CleanupResult(clean_text(reply, self.keep), "llm", None, elapsed)
