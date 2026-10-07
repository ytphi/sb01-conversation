# Voice references

Reference clips for Chatterbox zero-shot voice cloning. None are in use yet;
until then every language uses the model's built-in default voice.

- One clean clip per voice, ~10 s, single speaker, no music, `.wav`
- Suggested naming: `<lang>_<name>.wav` (e.g. `en_sb01.wav`, `ja_sb01.wav`)
- Point a language at a clip in `experimental/speech-framework/config.yaml`:

```yaml
tts:
  languages:
    ja: { engine: multilingual, voice: ../utilities/voice-refs/ja_sb01.wav }
```

Paths are relative to `experimental/speech-framework/`.
Clips of real people's voices need their consent; consider keeping them out of git.
