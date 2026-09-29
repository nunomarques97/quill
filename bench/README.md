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

# Phase 2
py -3.12 -m bench.record --list-devices             # MME inputs; microphone not opened
py -3.12 -m bench.record                            # record the dictation script
py -3.12 -m bench.record --redo dt-05,dt-07         # record takes again
py -3.12 -m bench.pipeline --set all --dry-run      # counts of both sets, no GPU
.venv\Scripts\python -m bench.pipeline --set all --stage raw --summary docs/research/phase2-summary.json
.venv\Scripts\python -m bench.pipeline --set all --stage streamed --summary docs/research/phase2-summary.json
.venv\Scripts\python -m bench.pipeline --set all --stage cleanup --summary docs/research/phase2-summary.json
py -3.12 -m bench.pipeline --compare-cleanup bench/results/pipeline/<run>   # rules vs qwen3:8b, no GPU
.venv\Scripts\python -m bench.pipeline --set all --stage vocabulary --summary docs/research/phase2-summary.json
py -3.12 -m quill.vocabulary --check                # validate local/vocabulary.toml; counts only
.venv\Scripts\python -m bench.pipeline --set all --stage corrections --summary docs/research/phase2-summary.json
py -3.12 -m quill.review --list                     # learned corrections (local/corrections.json)
.venv\Scripts\python -m bench.pipeline --set all --stage profiles --summary docs/research/phase2-summary.json
py -3.12 -m quill.profiles --check                  # style samples per profile (local/style/); counts only
py -3.12 -m bench.intent --run bench/results/pipeline/<run> --reviews bench/results/intent-review/<file>.json [--rejudge]
.venv\Scripts\python -m bench.streaming --set all --mode deterministic   # streamed text quality
.venv\Scripts\python -m bench.streaming --set all --mode realtime        # release-to-final latency
py -3.12 -m bench.record --set rewrite              # record the spoken rewrite instructions
py -3.12 -m bench.rewrite --dry-run                 # rewrite script and recording counts, no GPU
.venv\Scripts\python -m bench.rewrite                # command mode: instruction WER, correctness, latency
py -3.12 -m bench.pipeline --summary docs/research/phase2-summary.json --add-command-mode bench/results/rewrite/<run>/summary.json --add-selftests local/selftest
py -3.12 -m bench.pipeline --summary docs/research/phase2-summary.json --require complete --require overall
py -3.12 -m bench.pipeline --summary docs/research/phase2-summary.json --write-doc docs/research/FASE2.md
py -3.12 -m bench.pipeline --summary docs/research/phase2-summary.json --check-doc docs/research/FASE2.md
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

## Dictation set (Phase 2)

Phase 2 adds a second real-voice set. The 44 takes above form the `commands`
set; the `dictation` set holds longer utterances (about 5-30 s) in the styles
of Claude Code prompts, VS Code, WhatsApp and email.

- The script `bench/dictation/guiao-ditado-pt.md` is committed invented text:
  the same table as the commands script plus an `estilo` column
  (`claude-code`, `vscode`, `whatsapp`, `email`), ids `dt-NN`, and
  `<projeto-N>` placeholders instead of real project names.
- Markup in `frase`: `{hum}`, `{pronto}`, `{tipo}` and `{é pá}` are fillers;
  `[words]` is a self-repetition and must be followed by the same first word
  (`[corre os] corre os testes`). The loader derives the verbatim reference
  (everything spoken), the clean reference (fillers and repetitions dropped),
  the filler/repetition spans and the content words. Markup errors name the
  take id only.
- Recordings live in `local/recordings/dictation/` (ignored; any configured
  folder must be under `local/`) with a `manifesto.json` of the same shape as
  the commands manifest (`ficheiro`, `duracao_s`, `origem`, `projetos`).
- Script rows that are not recorded yet are *pending*, not invalid. The set is
  complete with at least `min_takes` (30) valid takes. Invalid and discarded
  takes follow the commands rules.
- Placeholders are resolved from each take's manifest entry; the recorder
  shows them from `[dictation.projects]` in `local/bench.toml`, or else from
  the commands manifest. Names must exist in the reference `config.toml`.

The loader, metrics and results for the commands set are unchanged.

### Recorder

`py -3.12 -m bench.record` shows each pending phrase (placeholders resolved,
fillers marked with `…`), then Enter starts and Enter stops the take; `s`
skips it, `q` quits and `r` records the take just saved again. Running it
again resumes at the first take not recorded; `--redo dt-NN,...` repeats
chosen takes.

- Capture is 16 kHz mono PCM16 through MME (`waveIn` via `ctypes`, no extra
  package). DirectSound is never used; WASAPI is not implemented.
- The device is chosen by the configured name (`[recorder].device`, else the
  reference config's `[microfone].nome`). MME truncates names to 31
  characters, so a device name of 10+ characters that is a prefix of the
  configured name also matches. No match, or more than one, is an error that
  points to `--list-devices`.
- A take is rejected and asked again when it has 0.5 s or more of exact
  zeros, when the captured audio falls behind or runs ahead of the wall clock
  by more than 0.25 s + 10 %, when it is near-silent, or when it is shorter
  than one second.
- A repeated take keeps the old file as `dt-NN.invalida-K.wav`; the manifest
  records `substituiu`. WAV files and the manifest are written only under
  `local/`.
- `--list-devices` enumerates MME inputs without opening the microphone.

Tests use a fake `waveIn` layer; they never open the microphone or play sound.

### Pipeline evaluation

`bench.pipeline` is the shared Phase 2 oracle. `--set commands|dictation|all`
selects the sets. `--dry-run` prints counts only (no GPU): a dictation set
with nothing recorded reports 0 recorded and exits 0; only a structural error
(missing or malformed script, bad settings) exits non-zero.

`--stage raw` loads faster-whisper large-v3 (float16, CUDA) once, warms it up,
and transcribes every valid take with the vocabulary hints (`initial_prompt`
and `hotwords`). `raw` stays the Phase 2 baseline. `--stage streamed` runs `raw` and then
replays every take through the product's streaming transcription
(`quill.streaming`, see [Streaming replay](#streaming-replay)) on the
deterministic schedule, with the product's engine model and its tuning
(`large-v3-turbo` by default, loaded once next to large-v3); its text is the
source of every later stage. The streaming model and options used are
recorded under `engine.streaming`. `--stage cleanup` runs both and then
applies the product's deterministic cleanup rules (`quill.cleanup.clean_text`,
with the hints vocabulary as protected words) to the streamed text.
`--stage vocabulary` runs all three and then applies the product's
post-recognition matcher (`quill.vocabulary.Matcher`) to the cleaned text; see
[Personal vocabulary](#personal-vocabulary); this and every later stage use
the personal vocabulary. `--stage corrections` runs all four and then
simulates learning from corrections on the vocabulary text; see
[Learning from corrections](#learning-from-corrections). `--stage profiles`
runs all five and then applies the active-window profile rules to the
corrected text; see [Window profiles](#window-profiles). It is the final text
of the application. Per set and stage the summary holds:

- `wer_verbatim` and `wer_clean`: corpus WER against the verbatim and the
  clean reference (equal for the commands set, which has no markup);
- `term_error_rate`, `name_error_rate` and `intent_preserved` (slot rule plus
  the local judge, as above);
- dictation only: `filler_removal_rate` (share of marked filler/repetition
  spans whose words are all absent, from a word alignment that prefers
  deleting marked words) and `content_deleted` (content words missing);
- dictation `cleanup` stage only: `content_deleted_by_cleanup`, the content
  words the previous stage had right (aligned with the identical word) that
  the cleaned text no longer has, deleted or changed. Recognition omissions
  stay visible in `content_deleted` and are never charged to the cleanup;
- `vocabulary` stage only: `vocabulary_changes`, the spans the matcher
  rewrote;
- `corrections` stage only: `recurrences`, `recurrences_fixed`,
  `recurrences_fixed_rate`, `recurrences_not_yet_active`, `new_errors`,
  `corrections_applied`, `takes_with_corrections` and the final
  `learned_active`/`learned_pending`/`learned_conflicts` counts (see below);
- `profiles` stage only: `profile_changes` (takes whose text the rules
  changed) and `profile_takes` (takes per profile);
- `p50_s`/`p95_s` of the transcription time (moved to the ignored
  `timings.json` of the run, never into the committed summary).

Per-take text goes only to `bench/results/pipeline/<run>/<set>/<stage>.json`.
`--summary PATH` (default `bench/results/pipeline/summary.json`) is
aggregate-only and refused when a phrase or name would appear in it.

`--require TARGET` (repeatable) reads the summary and exits 1, printing
measured-vs-target aggregates, when a target is unmet or a set is missing or
incomplete: `complete` (both sets measured, n equals the valid takes),
`latency` (release-to-final p95 <= 0.5 s for utterances up to 15 s, the
Sponsor target; see [Streaming replay](#streaming-replay)), `cleanup`
(dictation removal >= 95 % in the final stage and 0
`content_deleted_by_cleanup`), `vocabulary` (name
and term error <= 10 % on the dictation set; the commands set is printed as an
`info` indicator and never fails, Sponsor decision 2026-09-29), `corrections` (100 % of learned recurrences
fixed, 0 new errors, on both sets; a set with no learned recurrence has nothing
left unfixed and prints so), `overall` (final-text WER <= 10 %, intent >= 95 %) and `desktop` (the Sponsor's
manual self-tests: 0 characters lost, extra or changed while typing, the clipboard unchanged, no trigger
error and the indicator never in the foreground).
`--add-command-mode RUN_SUMMARY` (a `bench.rewrite` summary) and `--add-selftests DIR` (the newest
`typing`, `triggers` and `indicator` results in `local/selftest/`) merge whitelisted counts into
`--summary`, without timings. They are one-off manual steps, never a check; a later `--stage` run writes a
fresh summary, so repeat them after it.
`--write-doc DOC` inserts the Portuguese table between
`<!-- pipeline:summary:start -->` and `<!-- pipeline:summary:end -->`;
`--check-doc DOC` exits 1 when that block differs from the summary.

`--compare-cleanup RUN` needs no GPU: it reads the `streamed.json` texts that
an earlier run saved under `bench/results/pipeline/RUN/` and cleans them with
the rules and with the local `qwen3:8b` cleanup (`quill.cleanup.Cleanup`,
mode `llm`, which falls back to the rules on any Ollama failure or
implausible reply). It prints, per set and mode, WER, intent, removal,
`content_deleted_by_cleanup`, the LLM's p50/p95 time per take and the
fallbacks; the aggregates, timings and per-take texts go only to
`bench/results/cleanup/<time>/`. When Ollama or `qwen3:8b` is not ready only
the rules are measured and the reason is printed.

### Learning from corrections

The product learns word and phrase replacements from the user's corrections
(`quill/corrections.py`; the correction key and the manual-edit detection of
`quill/edits.py` feed it, and `python -m quill.review` reviews what was
learned). The `corrections` stage measures that rule on the real takes with
an online simulation, in the fixed take order of each set:

1. the take's `vocabulary` text gets the replacements active so far
   (`Corrections.apply`: whole words, case-preserving, never across
   punctuation);
2. the simulated user corrects the typed text to the clean reference, and the
   product's `derive` and `Corrections.learn` decide what is kept: at most
   three words a side, no pure insertion or deletion, no rewrite (more than
   five changed runs or fewer than half of the words unchanged), no swap
   between function words only (`dos` -> `do` depends on the sentence), and
   case or number differences that the normalizer ignores are not errors;
3. a replacement seen in two distinct takes becomes active; one source with
   two targets, or a pair and its reverse, is a conflict and never applied.

Each take is its own dictation, so a take never counts twice. Per set:
`recurrences` counts the word errors of a take whose replacement was already
active, `recurrences_fixed` those the applied text no longer has
(`recurrences_fixed_rate`, target 100 %), `recurrences_not_yet_active` the
errors seen before but not active yet (the second sighting, which activates
them, or a conflict), and `new_errors` the reference words the take had right
(one minimum-cost word alignment) that the applied replacements made wrong
(target 0). The errors themselves are listed with the same alignment without
the rewrite guard, so every error of a take is checked. Only counts go to
the summary; the corrected texts go to `corrections.json` of the run.

### Window profiles

The product picks a profile from the foreground window (`quill/profiles.py`:
process image name, window class and title against the `[profiles.*]`
matchers of the config, first match wins, `default` otherwise; an unreadable
process never matches a process list). The profile rules change only
punctuation and sentence-start capitals, never a word: `claude-code` and
`vscode` (technical) always end with a sentence mark and a trailing ellipsis
becomes a period; `whatsapp` (informal) drops a single final period; `email`
(full sentences) adds the comma after an opening greeting and ends every text
with a mark; `default` keeps the cleanup's punctuation.

The `profiles` stage gives each dictation take the profile of its script
`estilo` column and each commands take (no `estilo`) the `default` profile.
The WER, term, name, removal and deletion metrics ignore punctuation and case,
so they equal the `corrections` row; only the intent judge sees the change.
Style samples (`local/style/`) feed the local LLM cleanup only, which is off
by default, so the stage measures the rules alone.

### Personal vocabulary

The personal vocabulary is `local/vocabulary.toml` (ignored; format and
invented entries in [vocabulary.example.toml](../vocabulary.example.toml)):
project `names`, technical `terms` and, under `[variants]`, spoken or misheard
forms of an entry. `--vocabulary FILE` measures another file; a missing file
is an empty vocabulary. The summary records counts only (`vocabulary`: names,
terms, variants, hints kept and dropped, whether the takes were streamed
again), never an entry.

- **Hints.** The product passes Whisper, in this priority order: personal
  names, the resolved project names, `bench/terms_en.txt`, then personal
  terms; duplicates are dropped ignoring case, and the list is cut at
  `HINT_MAX_CHARS` (330 characters) from the end, so names are the last to
  go and personal terms the first (`quill.vocabulary.whisper_hints`; a longer
  list made the dictation set worse, see FASE2). Variants are never hints. When
  this list differs from the baseline hints (resolved names sorted, then the
  generic terms), the `vocabulary` stage streams and cleans every take again
  with it before matching (per-take text in `vocabulary-source.json`);
  otherwise it matches the `cleanup` texts. The earlier stages always keep
  the baseline hints, so their rows stay comparable across tasks.
- **Matcher.** After cleanup, a span of up to as many words as an entry
  (joined only by spaces or hyphens, never with a number) is rewritten with
  the entry's spelling when its folded form (no accents, case or separators;
  `y`->`i`, `k`->`c`, `ph`->`f`, doubled letters collapsed) equals the entry
  or a declared variant, or is within 1 edit (entries of 7+ folded
  characters) or 2 edits (12+), with the same first letter and not merely
  the entry plus a suffix (plurals stay). Shorter entries change only on an
  exact folded match or a variant; two entries equally close leave the span
  alone. It uses the vocabulary list only, never the reference text.

### Command mode (rewrite set)

The `rewrite` set measures command mode: with text selected, the Sponsor
holds the command trigger and says what to do with it.

- The script `bench/dictation/guiao-reescrita-pt.md` is committed invented
  text with the columns `id` (`rw-NN`), `caso`, `frase` (the spoken
  instruction, with the dictation markup), `seleção` (the invented selected
  text), `língua` (`pt` or `en`, the language of the rewrite) and
  `preservar` (terms the rewrite must keep, `;`-separated, or `—`).
- `py -3.12 -m bench.record --set rewrite` records it under
  `local/recordings/rewrite/` (the optional `[rewrite]` table in
  `local/bench.toml` changes the folder, script or minimum; any folder must
  be under `local/`). The recorder shows the selection for context, marked
  not to be read, and then the instruction to say.
- `bench.rewrite` replays each take through the product's streaming engine,
  sends the transcribed instruction and the selection to
  `quill.command.CommandRewriter` (the product's Ollama model, prompt and
  validation) and measures instruction WER, rewrite correctness
  (deterministic checks: accepted by the product, target language, preserved
  terms, shorter for `encurtar`, hyphen items for `lista`, changed text
  otherwise; plus the local judge unless `--no-judge`) and p50/p95 latency of
  the instruction, the rewrite and both.
- Per-take text and the human-checkable table (with an empty Sponsor column)
  go only to `bench/results/rewrite/<run>/`. The summary holds aggregates
  only, is written under `bench/results/` by default and is refused when a
  spoken or script text would leak into it.

### Streaming replay

`bench.streaming` feeds each real take, read in place, to `quill.streaming`
in 50 ms chunks (one MME buffer) and releases right after the last chunk.

- `--mode deterministic` waits until the worker is idle after every chunk,
  so every partial runs at the same audio time and the final text is
  reproducible run to run. It measures the WER of the streamed text against
  the clean reference and prints it next to the committed raw baseline.
- `--mode realtime` feeds at the pace of the recording with the model warm
  (one warm-up take first); stale partials are dropped as in the app. It
  reports release-to-final p50/p95/max for takes up to 15 s per set and for
  both sets, and a GPU snapshot before and after (utilization, free memory,
  models loaded in the shared Ollama, read-only).
- `--model` picks the local model (default `large-v3-turbo`, the product's
  engine; `large-v3` is the precise mode) and with it the product's tuning
  for that model (`quill.streaming.options_for`). With `large-v3-turbo`,
  partials every 0.5 s are only shown and the release transcribes the whole
  utterance with beam 5 (`commit` off). With `large-v3`, text is committed
  at pauses and the release transcribes only the rest.
- Every `StreamOptions` field has a flag that replaces one field of that
  tuning (`--step-s`, `--partial-beam`, `--final-beam`, `--commit-margin-s`,
  `--context-chars`, `--tail-pad-s`, `--pause-commit`, `--agreement`,
  `--commit`, ...). `--agreement true` also commits words two partials
  agree on (shorter tails, worse text).

Per-take text and every timing go only to
`bench/results/streaming/<run>/<mode>.json`; the console prints aggregates.
Timings are never written to committed files.

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

The product cleanup is `quill/cleanup.py` (deterministic rules by default;
see the `--compare-cleanup` option above). This section is the Phase 1 LLM
cleanup used by `bench.run`.

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

A phrase counts as preserved only when all hold:

1. **Slot rule**: every project name and every term from `bench/terms_en.txt`
   that occurs in the reference occurs at least as often in the hypothesis,
   after normalization.
2. **Cut-off rule** (Phase 2): the hypothesis is not cut off, i.e. the
   reference's last word has a counterpart in every minimum-edit word
   alignment (a different last word is left to the judge).
3. **Local judge**: `qwen3:8b` (thinking off, temperature 0, JSON output)
   answers `yes` to the fixed prompt `JUDGE_SYSTEM` in `bench/intent.py`:
   someone acting on the hypothesis would do the same action with the same
   meaning (same command, target, names, terms, negation and numbers),
   ignoring punctuation, fillers and small wording differences. Since the
   Phase 2 review the prompt also says that a request turned into a
   statement, a changed tense and a missing or replaced informative word are
   not the same meaning.

The judge only runs when the slot and cut-off rules hold, and it runs locally, so texts
never leave the PC for judging. If the judge fails on any phrase, no phrase of
that variant is counted and the summary notes why. For every variant a
human-checkable table (`id`, `reference`, `hypothesis`, `verdict`, `reason`
and an empty `human` column) is written to
`bench/results/runs/<run>/intent/`, never elsewhere.

**Judge review.** `python -m bench.intent --run bench/results/pipeline/<run>
--reviews FILE` joins a run's per-take outputs with a manual assessment (an
ignored JSON file under `bench/results/`, `{"sets": {set: {id: {"preserved",
"reason", "hypothesis"}}}}`; a review given for a different hypothesis is
ignored as stale) and writes, per set, a European Portuguese table to
`bench/results/intent-review/` with the reference, the hypothesis, the judge
verdict and reason, the manual verdict and reason and an empty Sponsor
column. It prints only the agreement aggregates. `--rejudge` judges the
saved outputs again with the current rules and judge (needs Ollama).

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

- Keys are read from the ignored `.env` only (names in `env.example`), never
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
  after normalization (also across line breaks). Dictation phrases count in
  their verbatim and clean forms. Only the committed dictation script may
  contain dictation phrases; it is still checked against the commands script
  and every other rule;
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
