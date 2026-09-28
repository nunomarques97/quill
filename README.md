# Quill

Push-to-talk dictation for Windows 11, in the spirit of Wispr Flow.

Put the cursor in any window, hold a key, speak European Portuguese (with English terms mixed in) and clean, punctuated text is typed where the cursor is, without filler words or repetitions. Quill adapts to its user over time: personal vocabulary, learned corrections, writing style and the active application.

## Status

Early stage. The first milestone is an engine benchmark on real recordings of the user's voice, to choose the speech-to-text engine and the monthly cost. See [docs/PRODUCT.md](docs/PRODUCT.md).

The benchmark harness lives in [bench/](bench/README.md). Its results, the engine comparison and the pending engine/cost decision (in European Portuguese) are in [docs/research/ENGINES.md](docs/research/ENGINES.md); the aggregate numbers are in [docs/research/engines-summary.json](docs/research/engines-summary.json).

## Privacy

Audio, transcripts, personal vocabulary, learned corrections and API keys stay on the machine and are never committed. Audio only leaves the PC for the engine the user has explicitly chosen.

## License

Not yet chosen.
