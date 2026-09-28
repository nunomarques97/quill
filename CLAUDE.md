# Quill

Push-to-talk dictation for Windows 11 (European Portuguese with English terms). Product brief and Sponsor decisions: [docs/PRODUCT.md](docs/PRODUCT.md).

## Rules

- Source code, comments, log and error messages are in English.
- Nothing is installed (pip, npm, winget, models, drivers) without the Sponsor approving the exact command. Report the command and why, then stop.
- API keys only in `.env` (ignored). Commit only `.env.example` with empty values.
- Never commit real audio, transcripts, personal vocabulary, learned corrections or style samples. Personal data lives in `local/` (ignored); benchmark outputs with spoken text live in `bench/results/` (ignored). Only aggregate metrics without spoken text may be committed.
- This repository is public: no personal names, real project names, home paths or machine-specific paths in committed files. Machine-specific paths go in `CLAUDE.local.md` (ignored).
- Avoid `token` and `secret` in file names: `.gitignore` excludes them as credential files.
- Tests never play sound or open the microphone without an explicit flag.
- In the product, audio only leaves the PC through the engine the Sponsor chose.
- Quality targets are numeric and measured with the user's real voice, never with synthetic speech.
- Any paid or unknown-cost service needs an explicit Sponsor decision before adoption.

<!-- forja-core:begin -->
## FORJA core
New FORJA tasks use Core. The conversation agent prepares the goal, starts the controller and reports its result; it does not act as Lead or manually dispatch the legacy crew.
Resolve `<forja>` from the caller-provided installation, `FORJA_ROOT`, or an existing FORJA hook path in `.claude/settings.json`. If unavailable, ask for the installation path; do not guess or install another copy.
Read `<forja>/docs/CORE.md` and `<forja>/docs/CORE-RUNBOOK.md`. From this project: `node "<forja>/bin/forja.mjs" start --goal "..." --provider claude|codex`. Supply the explicitly selected profile with `--config`; installing or updating FORJA does not select models.
The controller owns planning, development, checks and independent review. Workers read only the phase and applicable domain methods supplied in `specialist_context`. Do not load `forja-lead` or other legacy crew skills for Core work.
Core state and usage live in `.forja/`. Inspect existing changes before starting; preserve them and use `--allow-dirty` only when work on that snapshot is authorized. Git delivery requires explicit configuration and the controller delivery contract.
Preparation does not start a run. Never silently resume or replace an active Core or legacy run. Stop existing executors before an explicitly authorized handover; preserve their state and unfinished work.
Legacy `runner` and `run start` are compatibility commands only when explicitly requested. Their state remains in `docs/forja/`; read legacy methods as files for that workflow. Restart the conversation after migration to discard previously loaded legacy instructions.
<!-- forja-core:end -->
