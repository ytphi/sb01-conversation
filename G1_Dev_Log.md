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
nothing measurable. Switching gradients off or flushing tiny numbers gave the same generated waveform sample
for sample and no reliable gain in speed. On the processor there is nothing left to take out.

**English engine on the card (added, opt-in).** Measured alone on the card at their peak (weights plus a
long reply), the nano engine needs 2.3 GB and the multilingual engine 3.6 GB. Nano therefore fits beside
the gesture server (1.3 GB) on a 6 GB card with room to spare, which both engines together do not.
`SB01_CHATTERBOX_DEVICE=cuda` with `SB01_CHATTERBOX_GPU_ENGINES=nano` puts only that engine on the card,
if 4 GB is free (5.5 GB for `multilingual`); every other engine is made for the processor. Without
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
The multilingual engine on the card beside the gesture server was not tested and is now refused on a
6 GB card (see below). Both engines on a card still need clearly more than 8 GB, or the gesture server on
another machine.

**Memory headroom (raised after review).** The requirement is what an engine uses plus 1.5 GB kept free:

| On the card | Uses at its peak (weights, a long reply, CUDA runtime) | Free memory required |
|---|---|---|
| nano (English) | 2.5 GB (the card went from 1.3 to 3.4 GB with the gesture server already on it) | 4.0 GB |
| multilingual | 4.0 GB | 5.5 GB |
| both | 6.0 GB | 7.0 GB |

The 1.5 GB is the gesture server (1.3 GB) in case it starts after the voice, or the same room for anything
else. On the 6 GB laptop card with the gesture server running, 4.6 GB is free: nano is allowed and 2.6 GB
is still free once it has loaded; multilingual and "both" are refused, so the multilingual engine never
shares that card. `SB01_CHATTERBOX_GPU_GB` can raise the requirement, and can lower it only as far as what
the engines use. Free memory is now the lower of two readings, PyTorch's and `nvidia-smi`'s: under
Windows/WSL PyTorch reported 5.0 GB free whatever other programs held, while `nvidia-smi` counted them.
Checked on the laptop with nothing loaded: empty card, nano allowed; another program holding 4.8 GB,
"only 1.2 GB of graphics memory is free and Chatterbox needs about 4.0 GB, so it runs on the processor";
gesture server on the card, nano allowed, multilingual and both refused; no setting, processor.
A real out-of-memory error still could not be produced on this laptop; that handling is covered by tests
with a stand-in engine.

**Which engine speaks which language.** English uses the nano engine; Spanish, Chinese and Japanese use
the multilingual engine (as `config.yaml` ships). Checked with the real models with nano on the card:
English on `cuda:0`, Spanish and Chinese on the processor with the language passed to the model. A language
with no setting of its own goes to the multilingual engine, not to the English one (the speech framework's
own rule). The language of a reply is decided as before, by the same rule that picks the Edge TTS voice.

**Volume: left unchanged (decided 2026-10-07).** Chatterbox audio is played at the amplitude the model
generates, in English, Spanish and Chinese. There is no gain, no peak limiter and no matching to Edge TTS.
An automatic adjustment that turned English replies up toward Edge TTS's level was added during this work
(commits `89842c9` and `fe9ee51`) and has been taken out again: Chatterbox's volume sounded acceptable in
the team's G1 testing of the voice, so its original amplitude is preserved (this integration itself has
not been run on the robot). Edge TTS audio was never touched and is not now.
What is still done to Chatterbox audio is only what playing it needs: conversion to 16 kHz 16-bit for the
robot and to 24 kHz for the gesture model, and, when sentences are synthesized one by one, trimming silence
and a 5 ms fade at each join. Synthesis with PyTorch's gradient bookkeeping off (the memory fix below)
preserves the generated waveform sample for sample.

Checked on real audio with no robot and no DDS (8 English, 6 Spanish and 6 Chinese replies, plus one
three-sentence reply in each language):
- Amplitude unchanged: in all 20 replies the robot audio and the gesture copy were byte for byte the
  model's output put through the conversion alone (gain exactly 1.0). In the joined replies the samples
  between the fades were the model's own, and the joined audio was those pieces and nothing else.
- Format: 16 kHz, one channel, 16-bit for the robot; 24 kHz copy for the gesture model; length unchanged
  for replies spoken in one piece.
- Peaks in the robot audio (this run and the repeat in the final review): English -2.8 to -15.1 dBFS;
  Spanish 0.00 to -5.4 dBFS; Chinese -0.03 to -3.5 dBFS. The multilingual engine's output runs close to
  full scale.
- Full-scale samples, over three offline runs of six Spanish replies each (18 replies): 3 replies reached
  full scale in the robot audio. Two had one sample each at full scale. In the third the model's own
  output went above full scale (peak +0.19 dBFS, 5 samples); 16-bit audio cannot hold that, so the
  conversion cut 3 samples in the robot audio (by at most 0.33 dB) and 5 in the gesture copy. Never two in
  a row. None were seen in English or Chinese. This is reported, not acted on: nothing limits or lowers
  the audio because of it.
- Clipping or distortion: the only clipping found is those few isolated samples in that one Spanish reply,
  and it comes from the model's output exceeding what 16-bit audio can hold, not from anything this
  integration does to the level. No audible distortion was established in offline testing; nobody listened
  on the G1's speaker as part of it.
- Speech recognition: English 118 words heard for 117 written, 96% match; Spanish 62 for 61, 93% match.
  Chinese: 95 characters heard for 95 written, 70% match character for character, against 72% for Edge
  TTS's Chinese voice on the same sentences with the same recognizer, which writes traditional characters
  where the text has simplified ones. So the Chinese score says the two voices are recognized alike, not
  that either is hard to understand; a Chinese speaker should still listen.
- Sentence joins and pauses: pauses of 0.37 to 0.38 s between joined sentences in all three languages, no
  click at a join; four-sentence English reply, 10 of 10 audio checks, 47 of 47 words heard once.
- For information only: on ten English sentences Chatterbox's speech level measured about 5.6 dB below
  Edge TTS's, and Spanish and Chinese about 1 dB above it. Nothing is done about either.

Physical speaker validation remains pending. Volume limiting may be reconsidered only if testing on the
physical G1 reveals an audible problem.

**Longer run.** 50 English replies in a row (3 to 7 s long, teaching gestures on) with the nano engine on the
card, the real gesture server on the same card, the real gesture client and a stand-in robot (no DDS):
- all 50 in the Chatterbox voice, each synthesized once and played once, exactly the audio that was
  synthesized; Edge TTS never used; fallback counter 0; no reply started before the one before it had ended;
- 50 gestures made, arms released after each, every step within 0.05 rad/frame, no error in the gesture
  server's log; a recognizer heard 13 of 13 words in each of the 10 replies sampled;
- first audio 1.6 to 4.7 s after the reply was ready, typically 2.3 s, and no slower at the end than at the
  start (0.52 s of waiting per second of speech in the first ten replies, 0.50 in the last ten);
- graphics memory, whole card: 3.36 GB after the first reply, 3.50 GB from the tenth, 3.57 GB at the
  fiftieth. It rose in two small steps and did not fall; 2.5 GB of the 6 GB stayed free throughout;
- ordinary memory: gesture server level at about 0.22 GB; voice program 5.84 GB after the first reply and
  5.96 GB after the fiftieth (see the next paragraph).
Run again after the volume adjustment was taken out (the figures above are from before): 50 of 50 again,
Edge TTS never used, 50 gestures with arms released, first audio 1.6 to 4.6 s (typically 2.4 s), graphics
memory 3.37 GB after the first reply and level at 3.44 GB from the tenth, voice program's ordinary memory
5.25 GB to 5.34 GB.
This is one laptop, one sitting, a stand-in robot. It says nothing about other hardware or about hours of use.

**Memory kept with every reply (found by the longer run, fixed).** In the first 50-reply run the voice
program's ordinary memory rose by about 280 MB. Followed over 250 replies with nothing else running, it rose
by 1.7 GB, about 7 MB per reply, until the machine's memory was nearly used up. The cause is in the
Chatterbox package: the English engine's watermarking step keeps about 5 MB with every sentence when
PyTorch's gradient bookkeeping is on, and the package leaves it on for that engine. The conversation program
now synthesizes with it off (`torch.inference_mode()`), which preserves the generated waveform sample for
sample. After the change, 250 replies added 0.22 GB, most of it in the first 50, and 1 MB over the last 25. Speed and
graphics memory did not change. The speech framework's own program (`run_robot.py`) calls the engine without
this and was not changed; it should be expected to grow the same way in long sessions.

Limits of what was checked:
- The free-memory check was shown on the laptop to notice another program's use of the card (see "Memory
  headroom" above: with another program holding 4.8 GB it reported 1.2 GB free and kept Chatterbox on the
  processor). That depends on the second reading, from `nvidia-smi`; PyTorch's own figure under Windows/WSL
  stayed at about 5 GB whatever other programs held. It reads the first card only, and it has not been run
  on the lab computer.
- A real out-of-memory error could not be produced on the laptop: with the card deliberately filled and the
  check lowered (possible at the time), the driver let Chatterbox spill into ordinary memory instead of failing. The handling of that
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
