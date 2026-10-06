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

---

### 2026-10-05 — Service monitoring, security telemetry, operator-controlled reports (Goal 5)

**Status: tested in simulation only. NOT yet run on the physical G1.**
When a real-robot session has been checked, update `VALIDATION` in `teleop/monitor.py`.

**Files:** `teleop/monitor.py` (collector + passive network hooks), `teleop/monitor_reports.py`
(Team 1 / Security packages, save flow), `scripts/monitor_export.py` (CLI), small hooks in
`scripts/sb01_conversation.py`, outcome metadata in `teleop/face_id.py` (match decision unchanged).
Also fixed: `.env` loading (setup.sh creates `.env`; the script only read `env`).
Precedence, highest first: `env` file > shell environment > `.env` file. `env` overrides the shell as
it always did; `.env` only fills in variables that are not already set, so an unedited `.env`
(placeholder key) can never replace a key that is exported in the shell.

**Where data lives:** a private temp folder outside the repo (`/tmp/sb01-monitor-<uid>/<session>/`).
At clean shutdown: `Save monitoring report? [y/N]` → Team 1 or Security → zenity Save As
(terminal fallback) → ZIP written only where chosen → temp data deleted. After a crash the temp data is
kept and offered at the next start, then deleted. Nothing is uploaded or sent.

**Two audiences:**
- `--audience team1`: summary, turn timings, errors by component, service status (no hosts/IPs/URLs).
- `--audience security`: README, indicators, network contacts (DNS/connect/HTTP, expected or not),
  errors, face-recognition outcomes, robot mode changes, gesture events, process + file access, timeline.

**Never recorded:** API keys, tokens, cookies, auth headers, URL query values, speech-derived URL paths,
audio, images, face encodings, transcripts, replies, names, emotions, home paths.

**Gesture behavior unchanged:** gesture call sequence identical to the original script in simulation
(normal run and TTS-failure crash). Robot mode / gesture faults are read from the gesture client's
existing attributes only.

**Failure safety:** monitoring never raises into the conversation (log writing runs on a background
thread; a full disk stops recording, not the robot). If no safe temp folder exists, monitoring is disabled.
Errors are labelled by step (tts / gesture / robot_audio / claude / memory); SIGTERM is "signal", not "crashed".

**Known limits:** the gesture client's tracking / write / stream faults are printed but not stored, so
monitoring cannot see them (gesture_client.py is not modified; its `_state`/`_fault` are read in one
documented function). `play_state` during the greeting is recorded as an unconfirmed observation only.

**Real-G1 checklist (pending):** `robot_audio` check shows `SetVolume reply code 0` on `eno0` (the SDK
source, `unitree_sdk2py/g1/audio/g1_audio_client.py`, returns the RPC code as an int; N/A would mean the
installed SDK differs); note whether `play_state` is seen;
robot mode events appear (`mode_pr/mode_machine`) with gestures enabled; Claude, Edge TTS, CSUSB and
wttr.in contacts show as `expected`; gesture motion looks the same with monitoring on; both ZIPs open.

---

### 2026-10-06 — Transcript report and robot-command records (updates the 2026-10-05 entry)

**Status: tested in simulation only. NOT yet run on the physical G1.**

**Three reports now**, chosen at the shutdown prompt or with `scripts/monitor_export.py --audience`:
- `team1`: unchanged (timings, errors by component, service status).
- `security`: as before, plus `robot_commands.csv` (SetVolume, every LED change, one PlayStream record
  per spoken reply, each with the robot's reply code) and the robot FSM id, read once at startup when
  gestures are enabled. Arm commands stay summarized per reply in `gesture_events.csv`.
- `transcript` (new): `transcript.md` + `transcript.csv` — what the person said and what Yotie said,
  with millisecond timestamps and speaker labels. Speech is in this report only.

**This replaces the earlier rule that transcripts and replies are never recorded.** Still never recorded:
API keys, tokens, passwords, cookies, authorization headers, URL query values, audio, images, face
encodings. Enrolled names are written as `(user)` in speech (`[person]` elsewhere). A secret said aloud
in the form "my password is X" (also Spanish and Chinese) loses the word after it.

**Saving:** nothing is uploaded. The security and transcript ZIPs show a warning first and are not
encrypted by this code; save them to an encrypted drive by typing its path at the prompt or with
`--output <folder on the drive>`.

**Tests:** `python3 -m unittest tests.test_monitor` (offline, stand-in robot).

**Real-G1 checklist additions:** `robot_commands.csv` shows reply code 0 for LedControl and PlayStream;
`robot_state.csv` shows `fsm_id` 802; the transcript matches what was said.

---

## Pending / Next Steps

- [ ] Implement Claude streaming response + per-sentence TTS (reduce latency)
- [ ] Test ElevenLabs streaming TTS as alternative to edge-tts
- [ ] Add arm gesture during speech (wave hello on greeting)
- [ ] Test sb01 with multiple people talking
