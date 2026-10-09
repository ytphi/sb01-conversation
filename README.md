# SB01 — Unitree G1 Conversation — Engineering Track (IST 5930)

A voice-driven conversation system for the **Unitree G1 humanoid robot** (nicknamed *sb01*), developed at **California State University, San Bernardino (CSUSB)**. The robot listens via its onboard ASR, reasons with Claude (Anthropic), and speaks back using Edge TTS — all in real time. It also uses **face recognition** to identify known people at startup and load their personal memory profile.

---

## Engineering Track Focus — IST 5930

This branch is for the **Engineering** track. Your focus: **Unitree SDK and hardware integration** — getting the G1's sensors and actuators talking to real software over DDS, and wiring in LLM-based behavior. This repo's existing conversation loop is your reference implementation, not the finish line.

Areas to dig into:
- **Unitree SDK2** (Python/C++) — DDS pub/sub patterns, joint control, low-level state (`unitree_hg` IDLs)
- **Cameras** — onboard camera capture and processing (face recognition is already wired up in `teleop/face_id.py`)
- **Speakers / audio** — TTS playback and ASR ingestion via `AudioClient` (see `scripts/sb01_conversation.py`)
- **LLM integration** — extend the Claude + edge-tts loop with new tools, behaviors, or lower-latency pipelines

**Deliverable:** a working extension or new feature on the G1 (new sensor integration, new LLM-driven behavior, improved audio/video pipeline, etc.), demonstrated live or on video.

---

## Demo

Click the image below to watch the video on YouTube.

[![SB01 Demo](https://img.youtube.com/vi/R4tsqcWj8_U/0.jpg)](https://youtu.be/R4tsqcWj8_U?si=sJ4VN0mgyzGZyRys)

---

## Quick Start

### Step 1 — Get an Anthropic API key

Go to [console.anthropic.com](https://console.anthropic.com), create a free account, and generate an API key.

---

### Step 2 — Prepare your computer

| Your OS | What to do |
|---|---|
| **Ubuntu / Debian** | You're ready. Go to Step 3. |
| **Mac** | Install [Homebrew](https://brew.sh) if you don't have it. Go to Step 3. |
| **Windows** | Install WSL2 first (see below), then follow the Ubuntu steps. |

**Windows → WSL2 setup (one time):**
Open PowerShell as Administrator and run:
```powershell
wsl --install
```
Reboot when prompted. This gives you Ubuntu inside Windows. Open the **Ubuntu** app and continue from there.

---

### Step 3 — Clone and run setup

```bash
git clone https://github.com/ytphi/sb01-conversation.git
cd sb01-conversation
bash setup.sh
```

`setup.sh` will automatically:
- Install system dependencies (`cmake`, `ffmpeg`, etc.)
- Clone and install the Unitree Python SDK
- Install all Python packages (`anthropic`, `edge-tts`, `face_recognition`, etc.)
- Create your `.env` file

---

### Step 4 — Add your API key

Open the `.env` file that was created:
```bash
nano .env
```
Replace the placeholder with your real key:
```
ANTHROPIC_API_KEY=sk-ant-...your-key-here...
```
Save and close (`Ctrl+X`, then `Y`).

---

### Step 5 — (Optional) Enroll your face

So the robot can recognize and greet you by name:
```bash
python3 scripts/enroll_face.py YourName
```
Look at the camera and press **Space** to capture. Your photo is saved to `memory/faces/`.

---

### Step 6 — Connect and run

Plug an Ethernet cable from your laptop into the G1. Then:

```bash
# Ubuntu / WSL2
python3 scripts/sb01_conversation.py enp2s0

# Mac
python3 scripts/sb01_conversation.py en0
```

The robot will greet you and start listening. Press `Ctrl+C` to stop — it will save a summary of the conversation automatically.

### Speech recognition — Whisper on the PC

The robot's microphones are streamed to the PC, where Silero VAD finds the end of each
turn (500 ms of silence) and faster-whisper transcribes it: the same code and the same
`experimental/speech-framework/config.yaml` (`stt:` section) as `run_robot.py`.
Needs `pip install faster-whisper silero-vad pyyaml` (in `experimental/speech-framework/requirements.txt`).
The first start downloads the Whisper model.

| Setting | Effect |
|---|---|
| `SB01_STT=g1-mic` *(default, from `stt.source`)* | robot mics → Whisper on the PC |
| `SB01_STT=pc-mic` | a microphone on the PC → Whisper |
| `SB01_STT=g1-asr` | the robot's onboard ASR, as before (gives emotion tags; Whisper does not) |
| `SB01_WHISPER_MODEL=large-v3-turbo` | another Whisper model (default `small`) |
| `SB01_WHISPER_LANGUAGE=en` | fix the language instead of detecting it per utterance |

If Whisper cannot start, the program says why and uses the onboard ASR.

**Languages.** The robot converses in any of Chatterbox Multilingual's 23 languages (`teleop/languages.py`):
Arabic, Chinese, Danish, Dutch, English, Finnish, French, German, Greek, Hebrew, Hindi, Italian, Japanese,
Korean, Malay, Norwegian, Polish, Portuguese, Russian, Spanish, Swahili, Swedish, Turkish.
With Whisper listening, Whisper's own language detection picks the reply language, but only when it is at
least 70 % sure (`SB01_LANGUAGE_CONFIDENCE=0.7`) and the phrase is clear (a greeting, or 3+ words / 4+
characters); otherwise the robot stays in the language it last spoke. The reply is spoken in that
language by Chatterbox (or the matching Edge voice), unless its writing system shows another one.
Reply timings also come from that config: partial-result wait `stt.g1_asr.debounce_s` (0.8 s),
after a reply at most its length plus `robot.playback_grace_s` (0.5 s), then `robot.echo_tail_s` (0.3 s).

### Optional — the Chatterbox voice

By default the robot speaks with Microsoft Edge TTS. To use the Chatterbox voice from
`experimental/speech-framework` instead:

```bash
# once, in the Python environment the conversation program runs in (needs git; tested with Python 3.10, Python 3.11 is not yet validated)
pip install -r experimental/speech-framework/requirements.txt

SB01_VOICE=chatterbox python3 scripts/sb01_conversation.py enp2s0
```

That requirements file installs Chatterbox at one exact commit. Do not replace it with `pip install chatterbox-tts`:
the 0.1.7 release on PyPI lacks options the speech framework uses. The first start downloads the models
(several GB) to the Hugging Face cache in your home folder, not into this project.

- **Voices** are set in `experimental/speech-framework/config.yaml` (engine and optional voice clip per language).
- **Arm gestures work with either voice.** With Chatterbox, teaching gestures are placed by estimate: it does
  not report when each word is spoken, and the program says so.
- **Fallback.** If Chatterbox cannot load or cannot say a reply, the program says why and uses Edge TTS.
- **Where it runs.** Chatterbox runs on the processor by default, because its models need about 5.7 GB of
  graphics memory and the gesture server uses the same card. On a processor it is slow: the robot is silent
  while a reply is synthesized. On the development laptop with nothing else running that took 4 to 6 seconds
  for one English sentence, 14 to 17 seconds for four, and 15 to 19 seconds for one Spanish sentence (the
  multilingual engine is slower); two to three times longer when other programs were using the processor.
  That is too slow for live conversation.
  `SB01_CHATTERBOX_DEVICE=cuda` puts it on the graphics card only if at least 7 GB of its memory is free;
  otherwise it stays on the processor and says so. Sharing a 6 GB card with the gesture server is not supported.
- **Faster English on a small card (optional).** `SB01_CHATTERBOX_DEVICE=cuda SB01_CHATTERBOX_GPU_ENGINES=nano`
  puts only the English engine on the graphics card and leaves the multilingual engine on the processor.
  It is used only if 4 GB of graphics memory is free (the 2.5 GB the engine uses plus 1.5 GB kept for the
  gesture server and anything else); otherwise English runs on the processor too and the program says why.
  On the development laptop's 6 GB card, beside the gesture server, the robot then started speaking 1.6 to
  4.7 seconds after a reply was ready to be spoken (typically 2.3), with 3.6 of 6 GB in use. Spanish and Chinese stay on
  the processor and as slow as above. Putting both engines on a card needs clearly more than 8 GB, or the
  gesture server on another machine. Tested on that one laptop with a stand-in robot only.
- **Volume is normally left unchanged.** Chatterbox audio is played at the amplitude the model generates, in
  English, Spanish and Chinese. No loudness matching to Edge TTS is performed, nothing turns a reply up, and
  Edge TTS audio is not touched. The one exception is actual digital clipping: if a reply would go above
  full scale (16-bit audio cannot hold that), that reply alone is turned down by the smallest amount that
  fits, the same for the robot audio and the gesture copy, and the program prints that it did so and by how
  much. In earlier offline runs an occasional Spanish reply went over full scale, by up to 0.33 dB; in the
  run made after this was added, no natural reply needed it.
  Chatterbox's volume sounded acceptable in the team's G1 testing of the voice; the G1's speaker has not
  been retested with this integration.
- **Monitoring:** monitoring is not included in this branch. Monitoring integration is a separate future step.
  No monitoring behavior was physically validated by this work.
- **Status:** offline tests only. This has not been run on the physical robot.

---

## How It Works

```
G1 ASR (DDS) ──▶ sb01_conversation.py ──▶ Claude API ──▶ Edge TTS ──▶ G1 speaker
                          │
                 face_id + memory_manager
                 (who is this? what do I know?)
```

1. **ASR callback** — receives speech transcripts from the G1 over DDS (`rt/audio_msg`)
2. **Emotion detection** — reads the `<|HAPPY|>` / `<|SAD|>` tag embedded in the ASR message and sets the chest LED color
3. **Claude query** — builds a system prompt with the person's memory + web context, sends the full conversation history
4. **TTS** — converts Claude's reply to PCM audio via Edge TTS and streams it to the G1 speaker
5. **Session save** — on shutdown (`Ctrl+C`), asks Claude to summarize the conversation and extract new facts about the user

---

## Features

- **Voice conversation** — listens over DDS, replies with natural speech
- **Emotion-aware LEDs** — chest LED color reflects detected speaker emotion (happy → yellow, sad → purple, angry → red)
- **Face recognition at startup** — identifies known people and loads their memory profile
- **Persistent memory** — per-person facts and session summaries are saved across sessions
- **Weather lookups** — automatically queries `wttr.in` when a weather question is detected
- **Multilingual TTS** — auto-detects English vs. Spanish and picks the right voice

---

## Project Structure

```
setup.sh                   ← run this first
.env.example               ← copy to .env and add your API key

scripts/
├── sb01_conversation.py   ← main conversation loop
└── enroll_face.py         ← register a face for recognition

teleop/
├── face_id.py             ← camera-based face recognition
└── memory_manager.py      ← per-person profiles & session summaries

memory/
├── faces/                 ← put face photos here (name.jpg)
├── profiles/              ← auto-generated, gitignored
└── sessions/              ← auto-generated, gitignored
```

---

## Hardware & Network

| Interface | Role |
|---|---|
| `enp2s0` (Ethernet, `192.168.123.222`) | DDS — ASR in, TTS / LED out |
| `wlp0s20f3` (WiFi) | Claude API + weather / web fetches |

The G1 uses [Unitree SDK2](https://github.com/unitreerobotics/unitree_sdk2_python) for DDS communication. `setup.sh` installs this for you.

---

## Customizing the Persona

Edit `BASE_SYSTEM_PROMPT` near the top of `scripts/sb01_conversation.py` to change the robot's name, personality, or instructions.

---

## Troubleshooting

**`face_recognition` install fails on Apple Silicon (M1/M2/M3)**
```bash
conda install -c conda-forge dlib
pip install face_recognition
```

**`No module named 'unitree_sdk2py'`**
Re-run `bash setup.sh` — the SDK install may have been skipped.

**Robot not responding to speech**
- Check that `enp2s0` is the right interface: run `ip link` (Linux) or `ifconfig` (Mac)
- Make sure the Ethernet cable is connected and the G1 is powered on

**`ANTHROPIC_API_KEY` error at startup**
Make sure `.env` exists and contains your key, then re-export it:
```bash
export ANTHROPIC_API_KEY=$(grep ANTHROPIC_API_KEY .env | cut -d= -f2)
```

---

## Notes for Students

- **Never commit your `.env` file.** It contains your private API key. The `.gitignore` already excludes it.
- The `memory/faces/` folder is also gitignored — face photos are personal data, don't share them.
- If you don't have a G1, you can still read and modify the conversation logic — the DDS subscriber simply won't receive messages without the robot.

---

## Course Context

Developed as part of **IST 5930 (Fall 2026)** and the robotics program at **California State University, San Bernardino (CSUSB)** under **Prof. Yutong Liu**. This branch supports the **Engineering** track — Unitree SDK, hardware integration, and LLM integration. Demonstrates real-time LLM integration on an embedded humanoid platform.
