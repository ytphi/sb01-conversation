# G1 Development Log

## SDK Documentation References
- Python SDK: `unitree_sdk2_python/README.md`
- Python G1 examples: `unitree_sdk2_python/example/g1/readme.md`
- C++ SDK: `../unitree_sdk2/README.md`
- G1 audio client: `unitree_sdk2_python/unitree_sdk2py/g1/audio/g1_audio_client.py`
- Audio example: `unitree_sdk2_python/example/g1/audio/g1_audio_client_example.py`

---

## Session Log

### 2026-06-08 — sb01 Conversation Loop

**Goal:** Build a voice-interactive LLM conversation loop using G1's built-in ASR + Claude API + TTS.

**Network setup confirmed:**
- `eno0` (192.168.123.222) — Ethernet to G1, handles all DDS
- `wlp0s20f3` — WiFi, routes Claude API calls automatically

**ASR findings:**
- Topic: `rt/audio_msg`, type: `std_msgs::String_` (JSON)
- `is_final: true` fires after silence (~1-2s pause needed)
- Partial results (`is_final: false`) come while speaking
- `play_state: 1/0` signals TTS start/finish
- Added 1.5s debounce on partials to handle cases where `is_final` never arrives

**TTS:**
- Built-in: `TtsMaker(text, speaker_id)` — fast but limited (0=Chinese, 1=English)
- Switched to **edge-tts** (en-US-JennyNeural) for better voice quality
- edge-tts pipeline: text → Microsoft API → MP3 → pydub → PCM 16000Hz → `PlayStream`
- Known issue: ~2-4s total delay (Claude API + edge-tts generation)
- Next: implement Claude streaming + sentence-by-sentence TTS to reduce latency

**Script:** `scripts/sb01_conversation.py`

**Dependencies installed:**
```bash
pip install edge-tts pydub anthropic
sudo apt install ffmpeg
```

**Run:**
```bash
python3 scripts/sb01_conversation.py eno0
```

### 2026-10-07 — Latency: fixed delays + PC-side STT (experimental/speech-framework)

**Hypothesis (not yet measured):** most of the delay from the user finishing speaking
to the robot replying comes from
1. fixed waits in our code: 1.5 s debounce on ASR partials, and up to ~3.4 s+ of
   ignoring the mic after playback when `play_state: 0` doesn't arrive for `PlayStream`
2. the G1's onboard ASR: waits ~1-2 s of silence before `is_final` (not tunable), and
   accuracy is mixed (probably SenseVoice-based, judging by the `<|HAPPY|>` tags; unconfirmed)

**Changes:**
- Fixed waits are now config values: debounce 0.8 s, post-playback wait = audio length
  + `playback_grace_s` (0.5 s) + `echo_tail_s` (0.3 s)
- New `stt.source`: `g1-mic` (default, robot mics → PC), `pc-mic`, or `g1-asr` (old path).
  PC-side = Silero VAD (500 ms silence ends the turn) + faster-whisper (`small`)
- `[timing]` log lines for stt / llm / tts
- `non-robot-testmode/run_local.py --mic` to test STT without the robot

**To verify on the robot:** G1 mic multicast address (239.168.123.161:5555 from Unitree docs),
whether `play_state: 0` arrives for `PlayStream`, Whisper speed on the Linux PC (GPU or CPU),
and the `[timing]` breakdown for a few turns.

---

## Pending / Next Steps

- [ ] Implement Claude streaming response + per-sentence TTS (reduce latency)
- [ ] Test ElevenLabs streaming TTS as alternative to edge-tts
- [ ] Add arm gesture during speech (wave hello on greeting)
- [ ] Test sb01 with multiple people talking
