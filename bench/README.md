# Quill engine benchmark

Reproducible speech-to-text benchmark on the user's real Portuguese recordings.
Python 3.12. Everything runs on the standard library (`tomllib`, `wave`,
`json`, `urllib`, `unittest`) except the local faster-whisper engines, whose
pinned packages live in `bench/requirements.txt` and are installed only into
the ignored `.venv` after the Sponsor approves the commands.

## Setup

1. Copy `bench/bench.example.toml` to `local/bench.toml` (ignored by Git).
2. Fill in the absolute paths of the recordings folder, the recording script
   and the reference project's `config.toml`. They stay on this machine.
   The optional `[engines.gemini]` and `[engines.deepgram]` tables change the
   Gemini model id and the Deepgram language or keyterm use.
3. Run `py -3.12 -m bench.run --preflight` and follow what it lists.

The recordings are read in place and never copied into this repository.

## Commands

```
py -3.12 -m unittest discover -s bench/tests -t .   # offline unit tests
py -3.12 -m bench.run --preflight                   # what is still missing
py -3.12 -m bench.run --dry-run                     # dataset counts only
.venv\Scripts\python -m bench.run                   # full benchmark
.venv\Scripts\python -m bench.run --engines groq-whisper-large-v3
py -3.12 -m bench.privacy_guard                     # scan publishable files
py -3.12 -m bench.report --check docs/research/ENGINES.md
```

Tests use invented phrases only. They never play sound, open the microphone
or read the real recordings. They run offline with fake engines, a local fake
HTTP server that stands in for the provider hosts, and a fake Ollama.

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
- **Intent preserved**: share of takes judged as preserving intent; see
  [Intent preserved](#intent-preserved).
- **Latency p50/p95**: nearest-rank percentiles, the ceil(p/100 * n)-th
  smallest value.

## Engines

All engines implement `bench.engines.base.Engine`: `transcribe(wav, hints)`
turns one 16 kHz mono PCM16 WAV file into text.

| id | engine | vocabulary hints |
|---|---|---|
| `faster-whisper-large-v3` | local, CUDA float16, beam 5, language `pt` | `initial_prompt` and `hotwords` |
| `faster-whisper-large-v3-turbo` | same | same |
| `groq-whisper-large-v3` | Groq `/openai/v1/audio/transcriptions`, language `pt` | `prompt` (kept short: Groq limits it to 224 tokens) |
| `groq-whisper-large-v3-turbo` | same | same |
| `gemini-<model>` | Gemini `generateContent` with inline WAV; model configurable, default `gemini-2.5-flash`. Dedicated `*-transcribe` models use the Interactions API (`language_codes` `pt-PT`, `store: false`) | vocabulary line in the instruction; `custom_vocabulary` (first 100 terms) for `*-transcribe` |
| `deepgram-nova-3` | Deepgram `/v1/listen`, `nova-3`, language `pt-PT` | `keyterm` (up to 100) |

- faster-whisper is imported only when a local engine loads. Models are read
  from the ignored `models/` folder with `local_files_only`; the benchmark
  never downloads a model. On Windows the CUDA libraries shipped in the torch
  and nvidia wheels are registered before the import.
- Cloud engines use `urllib` only. Deepgram's Nova-3 Portuguese codes (`pt`,
  `pt-BR`, `pt-PT`) and keyterm support were checked in its documentation on
  2026-09-28.
- The hints vocabulary is built at runtime: the project names resolved for the
  dataset first, then the generic terms in `bench/terms_en.txt`. It is never
  written to committed files.

## Variants

Each engine runs four variants: `raw`, `hints`, `raw+cleanup` and
`hints+cleanup`.

- When an engine cannot run (missing key, package or model, failed load) all
  its variants are SKIPPED with the reason.
- An engine listed in the optional `[engines.skip]` table of `local/bench.toml`
  (engine id = reason, for example a recorded decision not to run it) is not
  built and all its variants are SKIPPED with `skipped: <reason>`.
- When an engine does not support hints (for example Deepgram with
  `keyterm = false`), `hints` and `hints+cleanup` are SKIPPED with the
  documented reason. Nothing is faked.
- Any engine error during a variant SKIPS that whole variant: partial results
  are never mixed with complete ones.

## Cleanup

`bench/cleanup.py` sends each engine output to local Ollama at
`http://127.0.0.1:11434` with `qwen3:8b`, thinking disabled, temperature 0 and
one fixed prompt (punctuation, capitalization, remove fillers and accidental
repetitions, keep English terms and names, never translate or add). In
`hints+cleanup` the prompt also lists the hints vocabulary.

Ollama is shared with other projects. The client only calls `/api/version`,
`/api/tags`, `/api/ps` and `/api/chat`; it never pulls, deletes or unloads a
model and never sends `keep_alive`. Before and after the cleanup phase and the
intent phase the run records free VRAM (`nvidia-smi`) and the loaded models
(`/api/ps`). `qwen3:14b`, or any other model using 4 GiB or more of VRAM, is
reported as VRAM contention. When `qwen3:8b` is not installed or cannot load,
the cleanup variants are SKIPPED with the reason.

## Intent preserved

A phrase counts as preserved only when both hold:

1. **Slot rule**: every project name and every term from `bench/terms_en.txt`
   that occurs in the reference occurs at least as often in the hypothesis,
   after normalization.
2. **Local judge**: `qwen3:8b` (thinking off, temperature 0, JSON output)
   answers `yes` to the fixed prompt `JUDGE_SYSTEM` in `bench/intent.py`:
   someone acting on the hypothesis would do the same action with the same
   meaning (same command, target, names, terms, negation and numbers),
   ignoring punctuation, fillers and small wording differences.

The judge only runs when the slot rule holds, and it runs locally, so texts
never leave the PC for judging. If the judge fails on any phrase, no phrase of
that variant is counted and the summary notes why. For every variant a
human-checkable table (`id`, `reference`, `hypothesis`, `verdict`, `reason`
and an empty `human` column) is written to
`bench/results/runs/<run>/intent/`, never elsewhere.

## Latency

The takes are short commands, so latency uses composites: real takes
concatenated with 0.3 s of low-level noise between them, 10 composites with
durations spread across 5-15 s, built deterministically (fixed seed). No
synthetic speech is used.

- Latency runs from the moment the whole composite is in memory (simulated key
  release) to the final text: the engine call including the network for cloud
  engines, plus the cleanup call for cleanup variants.
- Every composite runs 2 times per engine and variant (20 samples); the
  summary reports p50 and p95 over them.
- Model load and the first (warm-up) call are measured and reported apart.
  Cloud engines skip the warm-up call so no extra audio is sent.
- Rate pacing happens before the clock starts. A sample whose HTTP call needed
  a retry is measured again, so waits never count as latency.

## Cache and rate limits

Cloud answers (per take and per latency sample) are cached as JSON under
`bench/results/cache/`, keyed by a hash of the audio hash, engine id and
parameters, so a rerun never resends audio. The cache holds only text and
timings. Requests are paced for the free tiers (Groq one request per 3.2 s,
Gemini one per 6.5 s). HTTP 429 and 5xx are retried with `Retry-After` or
exponential backoff; a wait over 120 s or a fifth failure SKIPS that
engine/variant.

## Keys and network

- Keys are read from the ignored `.env` only (names in `.env.example`), never
  from the process environment.
- Key values never appear in logs, exceptions, cache files or results: reprs
  list names only and every error message is redacted. Tests assert this on
  the error paths.
- Only `api.groq.com`, `generativelanguage.googleapis.com` and
  `api.deepgram.com` are contacted, over HTTPS on port 443, with verified TLS,
  no proxy and no redirects. Gemini gets its key in a header, never in the URL.

## Preflight

`py -3.12 -m bench.run --preflight` lists every missing item: the `.venv`
virtual environment, the pinned packages in `bench/requirements.txt`, the two
local models in `models/`, Ollama and `qwen3:8b`, and each key in `.env`.
Every missing item comes with the exact command and a one-line reason, or for
keys numbered steps for the Sponsor. It installs nothing, prints no key values
and exits 0.

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
