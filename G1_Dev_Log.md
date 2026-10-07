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
- `enp2s0` (192.168.123.222) — Ethernet to G1, handles all DDS
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
python3 scripts/sb01_conversation.py enp2s0
```

---

### 2026-10-06 — Fixed poses: "thinking" (off by default)

**Status: tested in simulation only. NOT yet run on the physical G1.**

All talking motion so far is generated from the reply's audio by the RoboGesture model; nothing is stored.
This adds fixed poses that are played without the model, starting with one:

- `thinking`: right hand raised to just under the chin, left forearm across the body with the hand in
  front of the right elbow. Measured on the full mesh model (which has BrainCo finger hands, not sb01's
  rigid ones): every gap between the arms, head and torso is 7 cm or more.
  Angles are in `POSES` in `scripts/gesture_server.py` (add a pose there as one line).
- While it stays in the pose the shoulders sway slightly (`POSE_SWAY`: the two shoulder pitch joints rock
  0.06 rad in opposite directions every 2.5 s, with a little roll and yaw). The G1 cannot lift or roll its
  shoulders and the waist is left alone for balance, so this is the closest to "shifting the shoulders".
- Enable with `SB01_GESTURE_POSES=thinking` (needs `SB01_GESTURE_URL`). Unset = behavior unchanged.
- The pose starts when a phrase is heard, beside the Claude request. When the reply arrives the arm turns
  round and goes back while the speech is synthesized, then the speech gesture follows as before.
  With a fast reply the arm only gets part of the way up (the full move takes 1.7 s).

**How it stays safe:** the sidecar plans only the way there (`POST /pose`), through the same collision
filter, at no more than 0.045 rad/frame. The client validates it like any block, and the way back is the
same frames in reverse, so the arms end exactly where they were taken. From a sway the arms first step
straight back onto that path (at most 0.06 rad, in steps under the speed limit). Same FSM gate, stale-state,
tracking and write checks as speech gestures. The collision model has no separate head (the body is one box that
also covers the head except its top 3 cm), so the pose keeps its distance from the head by design.

**Tests:** `python3 -m unittest tests.test_gesture_pose` (11 tests, stand-in robot and sidecar).
Also checked against the real sidecar and in the simulated conversation: reply time unchanged.

**Real-G1 checklist:** first run tethered with the remote in hand; watch the right hand's distance from
the head and chest; confirm the arm returns to its starting pose before the reply's gesture begins.

---

### 2026-10-06 — Teaching gestures on the word (off by default)

**Status: simulation only. NOT run on the physical G1. No waist motion: arms only.**

RoboGesture's motion follows the sound of the speech. This adds gestures with a meaning, chosen by Claude:
`yes` (right hand reaches forward and bobs gently twice), `point` / `point_right`, `one_hand` / `other_hand`,
`small`, `big`, `ask`. Poses are in `CUES` in `scripts/gesture_server.py`.

- Claude puts a mark before a word ("look at the [point] diagram"); `teleop/gesture_cues.py` removes the
  marks from what is spoken and, from the word times Edge TTS reports, gives each cue a time.
- The sidecar blends the pose into the generated motion so it arrives on that word, stays about 0.7 s and
  melts back (85 % pose, 15 % speech rhythm). Ways in are eased and take 1.4-1.9 s; a cue too early to be
  reached gently arrives late instead of fast. Every frame still passes the collision filter and the
  client's checks.
- Enable with `SB01_GESTURE_CUES=1` (needs `SB01_GESTURE_URL`); `SB01_BOARD_SIDE=left|right` for pointing.
  Unset = behavior unchanged.

**Measured on the demo sentences (English, Spanish, Chinese), in the motion after the collision filter:**
on its word each gesture is within 0.13-0.38 rad of its pose (1.0 rad away before it starts). Gestures aim to
be there 0.2 s before the word; pointing is a long reach the filter slows, so it sets off 0.6 s early.
Motion with cues is no sharper than the model alone.

**Tests:** `python3 -m unittest tests.test_gesture_cues` (15 tests). The blending itself runs only in the
sidecar's environment and was checked there with the comparison video, not by a repo test.

**Not yet done:** tried with the real Claude (no API key on the development laptop), or on the robot.

---

## Pending / Next Steps

- [ ] Implement Claude streaming response + per-sentence TTS (reduce latency)
- [ ] Test ElevenLabs streaming TTS as alternative to edge-tts
- [ ] Add arm gesture during speech (wave hello on greeting)
- [ ] Test sb01 with multiple people talking
