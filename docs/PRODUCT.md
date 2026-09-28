# Quill product brief

Accepted by the Sponsor on 2026-09-28. The Sponsor decides product, cost and priorities.

## Goal

A dictation app for Windows 11 that gets as close as possible to Wispr Flow for one user who speaks European Portuguese with English terms and names mixed in. The user places the cursor in any window (Claude Code, VS Code, a browser, WhatsApp, email), holds a key, speaks, and the text appears at the cursor: clean, punctuated, without filler words ("hum", "pronto", "tipo") or repetitions.

## Adaptation layers (to be validated by research)

1. **Personal vocabulary**: project names, technical terms and English words used inside Portuguese, passed to the engine as hints.
2. **Learning from corrections**: when the user fixes a dictated word by hand, or uses a correction key, the replacement is stored. Once repeated, it is applied automatically.
3. **Writing style**: examples of how the user writes, given to the model that cleans the text.
4. **Active window context**: technical prompt style in Claude Code and VS Code, informal in WhatsApp, complete sentences in email.
5. **Weekly review** of learned corrections, which the user approves or deletes.

Optional, decided by the research: a key that sends the text straight to Claude Code.

## Known context

- The user's earlier voice-control project already solved: push-to-talk key, reliable capture with a Razer BlackShark V2 Pro headset (DirectSound returns silence at 16 kHz; MME or WASAPI is required), phrase boosting with a correction lexicon, and text cleanup with local Ollama (qwen3:8b). It is a read-only reference.
- In that project's measurements, local engines (Whisper medium/turbo, Parakeet) had a 38–46% word error rate on the user's Portuguese voice.
- Machine: Windows 11, RTX 5060 Ti 16 GB, i5-14400F, 32 GB RAM. Ollama is shared with other projects, which sometimes load qwen3:14b.
- Budget scenarios to confirm with current official prices: 0 EUR/month (local Whisper large-v3 + Ollama cleanup, or free cloud tiers) versus about 6 USD/month (cloud transcription + local cleanup). Wispr Flow costs 12–15 USD/month.

## Sponsor decisions (2026-09-28)

- Name: Quill. GitHub repository: public.
- Paid-engine comparison: only accounts with free tiers or free credits (Groq free, Gemini free, Deepgram welcome credit). No money is spent; OpenAI is excluded from the benchmark unless the Sponsor decides otherwise.
- The benchmark may send the user's test recordings to all tested cloud services, including the Gemini free tier.
- No paid service is adopted without an explicit Sponsor decision on engine and cost.
- Engine (decided after the Phase 1 benchmark, see docs/research/ENGINES.md): local Whisper large-v3 with vocabulary hints, on the GPU. Budget: 0 EUR/month. Audio never leaves the PC; no cloud speech engine is used by the product. Local Ollama cleanup stays off the critical path until measured on real dictation.

## Non-negotiable rules

- Nothing is installed without the Sponsor approving the exact command.
- API keys only in an ignored `.env`, never in commits.
- Real audio, transcripts, personal vocabulary and learned corrections are never versioned.
- Tests never play sound without an explicit flag.
- In the product, audio only leaves the PC through the engine the Sponsor chose.
- Quality targets are numeric and measured with the user's real voice, never with synthetic speech.
