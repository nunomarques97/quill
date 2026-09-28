# Quill engine benchmark

Reproducible speech-to-text benchmark on the user's real Portuguese recordings.
Python 3.12, standard library only (`tomllib`, `wave`, `json`, `unittest`).

## Setup

1. Copy `bench/bench.example.toml` to `local/bench.toml` (ignored by Git).
2. Fill in the absolute paths of the recordings folder, the recording script
   and the reference project's `config.toml`. They stay on this machine.

The recordings are read in place and never copied into this repository.

## Commands

```
py -3.12 -m unittest discover -s bench/tests -t .   # offline unit tests
py -3.12 -m bench.run --dry-run                     # dataset counts only
py -3.12 -m bench.privacy_guard                     # scan publishable files
py -3.12 -m bench.report --check docs/research/ENGINES.md
```

Tests use invented phrases only. They never play sound, open the microphone
or read the real recordings.

## Dataset

- The recording script is a Markdown table with the columns `id`, `caso`,
  `frase`, `intenção`, `projeto` and `ativação`. Rows have ids `pt-NN`.
- The manifest (`manifesto.json` next to the recordings by default) has one
  entry per id under `gravacoes`, with `ficheiro`, `duracao_s`, `origem` and
  `projetos` (placeholder to project name, as shown on screen while
  recording).
- Valid takes are the `pt-NN.wav` files listed in both the script and the
  manifest. Discarded takes (`*.invalida-*`) are always excluded.
- A take is reported as invalid, never silently used, when it is not 16 kHz
  mono PCM16, contains a run of 0.5 s or more of exact digital zeros, differs
  from the manifest duration by more than 0.05 s, or is missing.
- `<projeto-N>` placeholders are resolved at runtime from each recording's
  manifest entry and validated against the project names in the reference
  `config.toml`. A missing or unknown name is an error. Resolved names and
  spoken text are never printed and are only written under `bench/results/`.

## Normalizer

One normalizer (`bench/normalize.py`) is applied to every reference and every
engine output, for every variant:

1. Unicode NFC.
2. Casefold. Accents are kept (`é` stays `é`).
3. A dash (Unicode category Pd) between two letters becomes a space
   (`lê-me` becomes `lê me`).
4. Every other punctuation or symbol character (Unicode categories P and S)
   is deleted (`README.` becomes `readme`, `3,5` becomes `35`).
5. Whitespace is collapsed to single spaces and trimmed.
6. A small committed equivalence list maps generic technical spellings to one
   canonical token, longest match first (`vs code`, `visual studio code` and
   `vscode` all become `vscode`). See `EQUIVALENCES` in `bench/normalize.py`.

Known limits: symbols such as `+` and `#` are deleted (`c++` becomes `c`), and
numbers written as digits do not match numbers written as words.

## Metrics

- **WER**: corpus word error rate, the sum of word-level Levenshtein edits
  (substitutions, deletions, insertions) over the sum of reference words,
  after normalization.
- **English/technical term error rate**: per-occurrence recall over the
  generic terms in `bench/terms_en.txt`. Each occurrence of a term in a
  normalized reference counts once; it is found when the normalized
  hypothesis contains that term at least as many times (non-overlapping word
  sequences). Error rate = 1 - found / expected.
- **Project-name error rate**: the same per-occurrence recall, using only the
  names resolved at runtime for each take. The names are never committed.
- **Intent preserved**: share of takes judged as preserving intent. The method
  is defined together with the engine adapters.
- **Latency p50/p95**: nearest-rank percentiles, the ceil(p/100 * n)-th
  smallest value.

## Summary and report

`bench.metrics.build_summary` and `write_summary` produce the aggregate-only
summary JSON: per engine/variant `n`, WER, term and name error rates,
intent-preserved share, p50/p95 and the SKIPPED reason, plus dataset counts,
invalid reasons and recording origin. `write_summary` refuses to write when a
spoken phrase or a project name would appear in the file.

`bench.report` renders a Markdown comparison block between
`<!-- bench:summary:start lang=pt -->` and `<!-- bench:summary:end -->`.
`--write DOC` inserts or replaces the block; `--check DOC` exits 1 when the
block differs from the summary, when it is missing, or when an engine/variant
has `n` different from 44 without an explicit SKIPPED reason.

## Privacy guard

`bench.privacy_guard` scans tracked files and untracked files that Git does
not ignore. It fails on:

- `project-name`: the project names resolved at runtime (any case, with
  spaces, dashes, dots or underscores between their parts);
- `personal-name`: the Git `user.name`, whole or any part of 4+ letters;
- `script-text`: any 4-word sequence of a script phrase, raw or resolved,
  after normalization (also across line breaks);
- `home-path`: absolute paths inside a Windows `Users` folder or a Unix home;
- `api-key`: key-shaped strings (known provider prefixes, private key blocks,
  or a long letter-and-digit value assigned to an API key name);
- `audio-file`: audio by extension or file signature (reported as line 0).

Output is `file:line: category` only. The matched text is never printed.
Exit codes: 0 clean, 1 findings, 2 configuration error.

## Privacy rules

- Outputs with spoken text go to `bench/results/` (ignored).
- Only aggregate metrics without spoken text may be committed.
- File names never contain `token`, `secret` or `transcript`: `.gitignore`
  excludes those patterns.
