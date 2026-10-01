# Non-robot test mode

Run the experimental speech framework on Ubuntu or Windows with no G1 attached:
keyboard in, PC speakers out. Uses the same config and code as the robot runner.

```bash
pip install -r experimental/speech-framework/requirements.txt
```

| Command | What it tests |
|---|---|
| `python non-robot-testmode/run_local.py --text-only` | LLM provider only (no torch/Chatterbox needed) |
| `python non-robot-testmode/run_local.py` | Full loop: LLM + TTS + playback |
| `python non-robot-testmode/run_local.py --provider deepseek` | Another provider |
| `python non-robot-testmode/run_local.py --save-wav non-robot-testmode/out --no-play` | Write replies to .wav |
| `python non-robot-testmode/run_local.py --say "Hola, ¿cómo estás?"` | TTS only, auto-detected language |
| `python non-robot-testmode/run_local.py --say "你好" --lang zh` | TTS only, forced language |

Type `quit` or press Ctrl-C to exit.

Without an NVIDIA GPU, Chatterbox runs on CPU: Nano is fine, Multilingual is slow.
