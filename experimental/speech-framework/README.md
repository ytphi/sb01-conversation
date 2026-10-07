# Speech framework (experimental)

A basic **INPUT → RESPONSE → INPUT** voice loop for sb01, separate from the
course reference script (`scripts/sb01_conversation.py`).

```
speech input ──▶ LLM provider ──▶ language ID ──▶ Chatterbox TTS ──▶ G1 speaker / PC speakers
  G1 mic / PC mic   gemini (default)   en zh ja es    nano (en)
  → faster-whisper
  G1 onboard ASR
  keyboard
                      deepseek                          multilingual v3 (zh ja es)
                      claude
```

Not included yet (on purpose): persistent memory, face recognition, other sensors.
The only "memory" is the last few turns of the current session (`max_history_turns`).

## Layout

| Path | What |
|---|---|
| `speech-framework/conversation.py` | The loop + `SpeechInput` / `SpeechOutput` interfaces |
| `speech-framework/speech_synth.py` | Picks engine + voice per language |
| `speech-framework/speech_recog.py` | Builds PC-side speech input (audio source + VAD + STT engine) |
| `speech-framework/lang_detect.py` | en / zh / ja / es detection (script + keywords, no deps) |
| `speech-framework/config.yaml` | Provider, models, per-language TTS settings |
| `speech-framework/run_robot.py` | Entry point on the robot machine |
| `utilities/llm/` | `LLMProvider` interface + Gemini / DeepSeek / Claude |
| `utilities/tts/chatterbox-nano/` | Chatterbox Nano engine (English) |
| `utilities/tts/chatterbox-ml3/` | Chatterbox Multilingual v3 engine |
| `utilities/tts/G1-builtin/` | PCM playback + LEDs through `AudioClient` |
| `utilities/stt/stt_common.py` | `STTEngine` interface, PC mic source, Silero VAD end-of-turn loop |
| `utilities/stt/faster-whisper/` | faster-whisper engine (Whisper on the PC) |
| `utilities/stt/G1-builtin/` | G1 onboard ASR subscriber (`rt/audio_msg`) + G1 mic multicast receiver |
| `utilities/persona-defs/` | System prompts |
| `utilities/voice-refs/` | Voice cloning clips (empty for now) |
| `../non-robot-testmode/run_local.py` | Same loop with keyboard + PC audio |

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r experimental/speech-framework/requirements.txt   # needs git: Chatterbox is pinned to one commit
cp experimental/speech-framework/.env.example experimental/speech-framework/.env   # add your key(s)
```

Keys are read from `experimental/speech-framework/.env`, `experimental/.env`, or the repo-root `.env`
(`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`).

## Run on the robot

```bash
python3 experimental/speech-framework/run_robot.py eno0
python3 experimental/speech-framework/run_robot.py eno0 --provider claude
python3 experimental/speech-framework/run_robot.py eno0 --stt g1-asr      # old onboard-ASR path
```

## Speech input

`stt.source` in `config.yaml` (or `--stt`) picks where speech recognition happens:

| Source | Mic | Recognition | End of turn |
|---|---|---|---|
| `g1-mic` (default) | G1 head mics, streamed over UDP multicast | faster-whisper on the PC | Silero VAD, `vad.min_silence_ms` (500 ms) |
| `pc-mic` | Mic plugged into the PC | faster-whisper on the PC | Silero VAD, as above |
| `g1-asr` | G1 head mics | G1's onboard ASR | Robot decides (~1-2 s, not tunable) + `g1_asr.debounce_s` |

The `g1-mic` multicast address (`239.168.123.161:5555`, 16 kHz mono int16) comes from
Unitree's G1 audio docs and hasn't been tested on sb01 yet. If nothing arrives within
5 s the runner prints a warning; fall back to `--stt g1-asr` or `--stt pc-mic`.

Whisper model size is the main speed/accuracy knob (`stt.whisper.model`): `small`
is fine on CPU; with an NVIDIA GPU try `distil-large-v3` (English) or `large-v3-turbo`.

Each turn prints `[timing]` lines (stt / llm / tts) so you can see where the latency is.

## Languages and voices

The persona tells the LLM to answer in the language the user spoke. The reply is
then classified (kana → ja, Han → zh, Spanish words/accents → es, else en) and
routed by `tts.languages` in `config.yaml`. Each language has its own engine,
voice clip and generation settings, so you can give each language a distinct voice
once reference clips exist. `voice: null` uses the model's built-in voice.

Note: the G1's onboard ASR may not recognise every language well; Whisper
(`g1-mic` / `pc-mic`) auto-detects the language per utterance unless
`stt.whisper.language` is set. The TTS side handles all four regardless.

## Adding a provider

Subclass `LLMProvider` in `utilities/llm/`, implement `chat(system, messages)`,
and register it in `_REGISTRY` in `llm_providers.py`.
