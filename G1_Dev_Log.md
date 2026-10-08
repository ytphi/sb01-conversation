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

### 2026-10-07 — Chatterbox voice in the main conversation program (off by default)

**Status: offline tests only. NOT run on the physical G1.**

`SB01_VOICE=chatterbox` makes `scripts/sb01_conversation.py` speak with the Chatterbox voice from
`experimental/speech-framework`, using that framework's own engines, `config.yaml` and voice clips
through `teleop/voice_chatterbox.py`. Unset or `edge` = Edge TTS, unchanged.

- **Loaded once**, at startup. Nothing is reloaded per sentence.
- **Gestures are unchanged.** The gesture server receives Chatterbox's audio exactly as it received
  Edge TTS audio, so speech gestures follow the real sound. A reply's audio is made completely before
  the gesture is requested or anything is played, and the gesture starts with the first audio chunk.
- **Fallback.** If Chatterbox cannot load (package or model missing, wrong voice setting, not enough
  memory) the program says why, lets go of whatever was loaded, and uses Edge TTS. If it fails on one
  reply, that whole reply is spoken with Edge TTS; nothing of Chatterbox's is played for it. After three
  failures in a row its models are released and it is not tried again that session.
- **Teaching gestures are placed by estimate.** Chatterbox reports no word times. With
  `SB01_GESTURE_CUES=1`, sentences are synthesized one by one so each sentence's start and length are
  measured; a word's time inside its sentence is estimated from its position in the text. The program
  prints that this is an estimate. Edge TTS still gives real word times.
- **Joined sentences.** Each sentence keeps at most 0.08 s of leading and 0.18 s of trailing silence, is
  faded over 5 ms at both ends, and is followed by a 0.12 s pause, so a join cannot click or leave a long
  gap. A reply spoken in one piece is passed through unaltered.

**Where it runs (changed after review).** The processor is the default. `auto` means cpu here; it never
selects the graphics card. `SB01_CHATTERBOX_DEVICE=cuda` (or `tts.device: cuda` in the framework's config)
is honored only if `SB01_CHATTERBOX_GPU_GB` (default 7) of graphics memory is free at startup; otherwise
Chatterbox runs on the processor and says so.

Measured on the development laptop (RTX 3050, 6 GB):

| Setup | Graphics memory | Time to synthesize one reply |
|---|---|---|
| Chatterbox alone on the card | about 5.7 of 6 GB | 1.4 to 2.4 s |
| Gesture server alone on the card | about 1.3 GB | (not applicable) |
| Both on the card | full (6.0 of 6 GB) | 5 to 12 s, in two test sessions |
| Chatterbox on the processor, gesture server on the card | about 1.3 GB | see "Speed on the processor" below |

Sharing a 6 GB card ran without crashing in those two sessions, but with the memory full and the voice
several times slower. That is not evidence it is reliable, so it is not supported and is no longer what
`auto` does. For Chatterbox on a graphics card, use one with clearly more than 8 GB or run the gesture
server on another machine.

**Speed on the processor** (i5-13420H, 6 threads; time to synthesize, during which the robot is silent):

| What | Nothing else running | Other programs using the processor |
|---|---|---|
| Loading at startup | 29 to 41 s | 48 to 114 s |
| One English sentence (about 3 s of speech, nano engine) | 4 to 6 s | 6 to 17 s |
| Four English sentences (about 12 s of speech) | 14 s in one piece, 17 s sentence by sentence | 23 to 39 s |
| One Spanish sentence (about 2.5 s of speech, multilingual engine) | 15 to 19 s | 14 to 15 s measured |

The right-hand column is from the first measurements, taken while other test jobs were running; the
"about 40 s for the first reply" seen then was not reproduced on an idle machine (first reply 5.6 to 10.5 s, later
ones 4.3 to 6.1 s). Claude's own reply time comes on top of these. This is too slow for live conversation, most of
all in Spanish and Chinese. No safe software change was found that makes it faster:
- the models are loaded once at startup (preload works) and the same model objects serve every reply;
- settings are read once (0.3 s);
- everything this project adds around the model (checks, trimming, conversion) takes about 2 ms per reply;
- the model already produces 24 kHz audio, so the gesture audio is not resampled;
- the processor thread count PyTorch picks (6) was the fastest of 3, 6 and 12;
- synthesizing sentence by sentence costs about 20% more, and is only done when teaching gestures are on,
  where it is needed to time them;
- a warm-up synthesis at startup was tried and dropped: it saved about 1 s on the first reply and added
  12 s to startup.
Inside the models the time is half speech tokens (T3) and half tokens-to-sound (S3Gen); the watermark costs
nothing measurable. Switching gradients off or flushing tiny numbers gave the same audio sample for sample
and no reliable gain. On the processor there is nothing left to take out.

**English engine on the card (added, opt-in).** Measured alone on the card at their peak (weights plus a
long reply), the nano engine needs 2.3 GB and the multilingual engine 3.6 GB. Nano therefore fits beside
the gesture server (1.3 GB) on a 6 GB card with room to spare, which both engines together do not.
`SB01_CHATTERBOX_DEVICE=cuda` with `SB01_CHATTERBOX_GPU_ENGINES=nano` puts only that engine on the card,
if 2.5 GB is free (4.5 GB for `multilingual`); every other engine is made for the processor. Without
`SB01_CHATTERBOX_GPU_ENGINES` nothing changes: the processor is still the default and cuda for both
engines still needs 7 GB. The audio comes from the same model with the same settings.

Tested on the laptop (RTX 3050 6 GB) with the real models, the real gesture server on the same card and a
stand-in robot, teaching gestures on:
- 25 English replies in a row (3 to 7 s long): all in the Chatterbox voice, a gesture every time, arms
  released every time, no fallback, no error in the gesture server.
- Time from "reply ready to be spoken" to the first audio: 1.7 to 4.5 s, typically 2.6 s (on the processor:
  4 to 17 s for the same kind of reply). A four-sentence reply was synthesized in 3.7 s instead of 17 s.
- Graphics memory for the whole card: 3.2 GB before the first reply, 3.4 GB at most, flat over the last
  replies, back to 1.3 GB when the voice program ended.
- Audio checks, 10 of 10: 16 kHz one channel 16-bit, 4.9 dB below Edge TTS, pauses between sentences
  0.37 to 0.38 s, no clicks, a recognizer heard all 47 words once.
- Spanish and Chinese still use the multilingual engine on the processor (15 s for a 2 s sentence).
That is one machine and one sitting, 25 replies: enough to say it worked there, not that it is proven for a
long session. The multilingual engine on the card beside the gesture server was not tested; by the
measurements it would leave under 1 GB free. Both engines on a card still need clearly more than 8 GB, or
the gesture server on another machine.

Limits of what was checked:
- The free-memory check could only be exercised here as "a 6 GB card is refused". Under Windows/WSL the
  driver reported about 5 GB free whatever other programs held, so it could not be shown to notice another
  program's use. On the lab computer (Linux) the figure should be accurate; that is untested.
- A real out-of-memory error could not be produced on the laptop: with the card deliberately filled and the
  check lowered, the driver let Chatterbox spill into ordinary memory instead of failing. The handling of that
  error (reason printed, models released, Edge TTS speaks) is covered by tests with a stand-in engine only.
- Known limitation, pointing gesture (not caused by Chatterbox; gesture code unchanged since `f2016e7`).
  The pointing gesture does not always reach its full pose. In simulation, 16 placements across a
  four-sentence reply (both arms, Edge TTS audio and Chatterbox audio) ended 0.08 to 0.32 rad from the pose
  (median 0.18; the arm starts 0.90 rad away), and where it fell shortest it also arrived about 0.4 s after
  the planned time. The worst case was the same with both voices (0.32 rad, on the word "diagram" in the
  second sentence). By design the cue only asks for 85% of the pose (within 0.09 to 0.20 rad); the rest of
  the shortfall is the collision filter holding the shoulder yaw at its safety margin. The command is not
  interrupted: with a stand-in robot every frame the server sent was commanded by the client, with no fault.
  The motion is repeatable (identical on a second run) and stays within the 0.05 rad/frame speed limit.
  One earlier combined run with Chatterbox ended 0.57 rad short; that was not reproduced. Nothing was changed:
  reaching farther would mean loosening the collision margins. Short replies point more fully than long ones.

**Installation (changed after review).** `experimental/speech-framework/requirements.txt` now pins
Chatterbox to commit `5de7a54aa4e5e2baadb0182dde554908b48b85c2` of its GitHub repository. The framework
calls `from_pretrained(nano=...)` and `from_pretrained(t3_model=...)`, which the 0.1.7 release on PyPI does
not have. Installing from that file was tested in a new environment (Python 3.10, torch 2.6.0).

**Monitoring.** Session monitoring is not included in this branch, and this work neither adds nor changes
it. Combining the Chatterbox voice with monitoring is a separate future step. No monitoring behavior was
validated by this work, on the robot or otherwise.

**Housekeeping.** `.claude/settings.local.json` (one machine's Claude Code permissions, with `/home/aloha/`
paths; no credentials) is no longer tracked and is in `.gitignore`. Pulling this change removes that file
from a checkout that still has it unmodified; keep a copy first if it is wanted. The speech framework's
install was tested with Python 3.10; Python 3.11 is not yet validated.

**Tests:** `python3 -m unittest discover` runs everything (`tests/__init__.py` was added so that works);
`python3 -m unittest tests.test_voice` runs the Chatterbox tests with a stand-in engine. Also checked with
the real Chatterbox models, the real gesture server and a stand-in robot.

---

## Pending / Next Steps

- [ ] Implement Claude streaming response + per-sentence TTS (reduce latency)
- [ ] Test ElevenLabs streaming TTS as alternative to edge-tts
- [ ] Add arm gesture during speech (wave hello on greeting)
- [ ] Test sb01 with multiple people talking
