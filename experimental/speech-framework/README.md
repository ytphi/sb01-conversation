# Speech framework (experimental)

A basic **INPUT → RESPONSE → INPUT** voice loop for sb01, separate from the
course reference script (`scripts/sb01_conversation.py`).

```
speech input ──▶ LLM provider ──▶ sentence ──▶ tags ──▶ voice (TTS) ──▶ playback queue
  G1 mic / PC mic   gemini (default)   splitter     <tone> [laugh]  chatterbox          G1 speaker
  → faster-whisper  deepseek           (streamed)   {gesture}       chatterbox-nano     PC speakers
  G1 onboard ASR    claude                                          teto (UTAU)
  keyboard                                                          voice-refs/<name>/
```

The reply is streamed: the first sentence (or first clause, at a comma after 6 words)
is synthesized and playing while the LLM is still writing the rest.

Not included yet (on purpose): persistent memory, face recognition, other sensors.
The only "memory" is the last few turns of the current session (`max_history_turns`).

## Layout

| Path | What |
|---|---|
| `speech-framework/conversation.py` | The loop + `SpeechInput` / `SpeechOutput` interfaces, streamed replies |
| `speech-framework/speech_pipeline.py` | Sentences → TTS → player as overlapping stages |
| `speech-framework/reply_tags.py` | The reply tag grammar (`<tone>`, `[laugh]`, `{gesture}`, `<pause>`) and sentence splitter |
| `speech-framework/voices.py` | Voice registry: config voices + `voice-refs/<name>/` folders |
| `speech-framework/speech_synth.py` | Speaks a phrase in the selected voice (engine + asset per language) |
| `speech-framework/timing_log.py` | Per-turn latency log (`logs/turns.jsonl`, timings only) |
| `speech-framework/compare_voices.py` | Renders the same lines in every voice for side-by-side listening |
| `speech-framework/speech_recog.py` | Builds PC-side speech input (audio source + VAD + STT engine) |
| `speech-framework/lang_detect.py` | en / zh / ja / es detection (script + keywords, no deps) |
| `speech-framework/config.yaml` | Provider, models, per-language TTS settings |
| `speech-framework/run_robot.py` | Entry point on the robot machine |
| `utilities/llm/` | `LLMProvider` interface + Gemini / DeepSeek / Claude |
| `utilities/tts/chatterbox-nano/` | Chatterbox Nano engine (English) |
| `utilities/tts/chatterbox-ml3/` | Chatterbox Multilingual v3 engine |
| `utilities/tts/utau/` | UTAU voicebank engine: oto.ini, G2P (CMUdict / pykakasi), tone planner, WORLD renderer |
| `utilities/tts/G1-builtin/` | Streaming PCM playback queue + LEDs through `AudioClient` |
| `utilities/stt/stt_common.py` | `STTEngine` interface, PC mic source, Silero VAD end-of-turn loop |
| `utilities/stt/faster-whisper/` | faster-whisper engine (Whisper on the PC) |
| `utilities/stt/G1-builtin/` | G1 onboard ASR subscriber (`rt/audio_msg`) + G1 mic multicast receiver |
| `utilities/persona-defs/` | System prompts, and the `<tone>` guide added for UTAU voices |
| `utilities/voice-refs/` | One folder per custom voice (see its README) |
| `../non-robot-testmode/run_local.py` | Same loop with keyboard + PC audio |

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r experimental/speech-framework/requirements.txt
cp experimental/speech-framework/.env.example experimental/speech-framework/.env   # add your key(s)
```

Keys are read from `experimental/speech-framework/.env`, `experimental/.env`, or the repo-root `.env`
(`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`).

## Run on the robot

```bash
python3 experimental/speech-framework/run_robot.py eno0
python3 experimental/speech-framework/run_robot.py eno0 --provider claude
python3 experimental/speech-framework/run_robot.py eno0 --stt g1-asr      # old onboard-ASR path
python3 experimental/speech-framework/run_robot.py eno0 --voice teto      # UTAU Kasane Teto
python3 experimental/speech-framework/run_robot.py --list-voices
python3 experimental/speech-framework/run_robot.py eno0 --no-stream       # old whole-reply behaviour
```

On the Alienware PC the robot port is `enp2s0`, and the venv is `.venv`.
Each turn appends one line to `speech-framework/logs/turns.jsonl` with seconds from the
end of your speech to: turn detected, transcript ready, first LLM token, first audio
ready, first audio played, playback end (timings only, never text).

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

`stt.vad.min_silence_ms` is 300 ms (was 500). With `g1-mic`, if no robot audio arrives
within `stt.fallback_after_s` (5 s) the runner switches to the onboard ASR by itself.

Each turn prints `[timing]` lines so you can see where the latency is.

## Languages and voices

The persona tells the LLM to answer in the language the user spoke. Each sentence
is then classified (kana → ja, Han → zh, Spanish words/accents → es, else en) and
routed by the selected voice (`tts.voice` or `--voice`). A voice lists an engine,
asset and settings per language; languages it lacks go to its `fallback` voice.

| Voice | en | ja | zh / es |
|---|---|---|---|
| `chatterbox` (default) | Multilingual | Multilingual | Multilingual |
| `chatterbox-nano` | Nano | (chatterbox) | (chatterbox) |
| `teto` | UTAU English CVVC | UTAU Japanese CV | (chatterbox) |
| `testmp` (folder) | Nano clone of `1007.MP3` | Multilingual clone | Multilingual clone |

Add a friend's voice by dropping a clip in `utilities/voice-refs/<name>/`
(see `utilities/voice-refs/README.md`). `compare_voices.py` renders the same lines
in every voice into `non-robot-testmode/out/voices/`.

### Reply tags and tone

One tag grammar is shared by every consumer and stripped before speech:
`<tone fall|rise|question|flat pitch=+2 speed=1.1 range=1.3 emph=word>` sets the
tone of the text after it, `<pause 300>` adds silence, `[laugh]` is a sound tag
(spoken only by engines that support it, i.e. Nano), `{point}` is a gesture cue
(collected with its position for Track G, not used yet).

For voices with `tone_tags: true` (Teto) the LLM is told about `<tone>` via
`utilities/persona-defs/tone_guide.txt` and plans each sentence's tone; the UTAU
engine follows it fully (pitch curve, speed, emphasis), with rule-based
declination, stress accents and a falling or rising end when no tag is given.
Chatterbox Multilingual maps tone onto `exaggeration` / `cfg_weight`; Nano only
honours pauses.

### UTAU engine

`utilities/tts/utau/` renders a voicebank without a neural model: text →
syllables (CMUdict + letter rules for English, pykakasi for Japanese) → bank
aliases placed by their oto.ini timings → WORLD analysis of each sample
(cached in `~/.cache/sb01-utau`) re-pitched along the planned curve and
synthesized per phrase. A sentence renders in about 0.05 s on the Alienware CPU
once the cache is warm. Whisper read-back on the Oct 7 test lines: 4.5% word
error for English, 2% kana error for Japanese.

Note: the G1's onboard ASR may not recognise every language well; Whisper
(`g1-mic` / `pc-mic`) auto-detects the language per utterance unless
`stt.whisper.language` is set. The TTS side handles all four regardless.

## Adding a provider

Subclass `LLMProvider` in `utilities/llm/`, implement `chat(system, messages)`,
and register it in `_REGISTRY` in `llm_providers.py`.
