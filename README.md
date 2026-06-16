# SB01 — Unitree G1 Conversation 

A voice-driven conversation system for the **Unitree G1 humanoid robot** (nicknamed *sb01*), developed at **California State University, San Bernardino (CSUSB)**. The robot listens via its onboard ASR, reasons with Claude (Anthropic), and speaks back using Edge TTS — all in real time. It also uses **face recognition** to identify known people at startup and load their personal memory profile.

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
python3 scripts/sb01_conversation.py eno0

# Mac
python3 scripts/sb01_conversation.py en0
```

The robot will greet you and start listening. Press `Ctrl+C` to stop — it will save a summary of the conversation automatically.

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
| `eno0` (Ethernet, `192.168.123.222`) | DDS — ASR in, TTS / LED out |
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
- Check that `eno0` is the right interface: run `ip link` (Linux) or `ifconfig` (Mac)
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

Developed as part of the robotics program at **California State University, San Bernardino (CSUSB)** under **Prof. Yutong Liu**. Demonstrates real-time LLM integration on an embedded humanoid platform.
