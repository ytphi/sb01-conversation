# SB01 — Unitree G1 Conversation Loop

A voice-driven conversation system for the **Unitree G1 humanoid robot** (nicknamed *sb01*), built at **California State University, San Bernardino (CSUSB)**. The robot listens via its onboard ASR, reasons with Claude (Anthropic), and speaks back using Edge TTS — all in real time.

---

## Features

- **Voice conversation** — listens over DDS (`rt/audio_msg`), replies over TTS with automatic language detection (English / Spanish)
- **Emotion-aware LEDs** — chest LED color reflects the detected speaker emotion (happy → yellow, sad → purple, angry → red)
- **Face recognition at startup** — identifies known people and loads their memory profile so the robot remembers them across sessions
- **Persistent memory** — per-person facts and session summaries are saved to disk and injected into the system prompt on the next visit
- **Weather lookups** — automatically calls `wttr.in` when a weather question is detected
- **Web context preloading** — fetches the CSUSB homepage and SDK docs at startup so the robot can answer campus questions

---

## Hardware & Network

| Interface | Role |
|---|---|
| `eno0` (Ethernet, `192.168.123.222`) | DDS — ASR messages in, TTS / LED commands out |
| `wlp0s20f3` (WiFi) | Claude API + weather / web fetches |

The G1 runs [Unitree SDK2](https://github.com/unitreerobotics/unitree_sdk2_python) for DDS communication.

---

## Project Structure

```
scripts/
└── sb01_conversation.py   # main conversation loop

teleop/
├── face_id.py             # camera-based face recognition at startup
└── memory_manager.py      # per-person profiles & session summaries

memory/
├── faces/                 # enroll face photos here  (one .jpg per person, filename = name)
├── profiles/              # auto-generated JSON profiles  (gitignored)
└── sessions/              # auto-generated session summaries  (gitignored)
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/ytphi/sb01-conversation.git
cd sb01-conversation
pip install anthropic edge-tts pydub opencv-python face_recognition
```

You also need the Unitree Python SDK (not included here — install separately):

```bash
pip install cyclonedds==0.10.2
# then follow https://github.com/unitreerobotics/unitree_sdk2_python
```

### 2. Set your API key

```bash
cp .env.example .env
# edit .env and paste your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...
```

> Get a free API key at [console.anthropic.com](https://console.anthropic.com)

### 3. Enroll faces (optional)

Drop a `.jpg` photo of each person into `memory/faces/`, named after them:

```
memory/faces/alice.jpg
memory/faces/bob.jpg
```

The robot will recognize them at startup and greet them by name.

### 4. Run

```bash
python3 scripts/sb01_conversation.py
# or specify the network interface explicitly:
python3 scripts/sb01_conversation.py eno0
```

The robot will:
1. Scan the camera for a known face
2. Say a greeting
3. Listen and respond continuously until `Ctrl-C`
4. Save a session summary on exit

---

## How It Works

```
G1 ASR (DDS) → sb01_conversation.py → Claude API → Edge TTS → G1 speaker
                        ↑
              face_id + memory_manager
              (who is this? what do I know?)
```

1. **ASR callback** — receives partial and final transcripts from `rt/audio_msg`
2. **Emotion detection** — parses the `<|HAPPY|>` / `<|SAD|>` tag embedded in the ASR message
3. **Claude query** — builds a system prompt with the person's memory + web context, sends the conversation history
4. **TTS** — converts Claude's reply to PCM audio via Edge TTS and streams it to the G1's speaker
5. **Session save** — on shutdown, asks Claude to summarize the conversation and extract new facts

---

## Customizing the Persona

Edit `BASE_SYSTEM_PROMPT` in `sb01_conversation.py` to change the robot's name, personality, or instructions.

---

## Dependencies

| Package | Purpose |
|---|---|
| `anthropic` | Claude API client |
| `edge-tts` | free Microsoft TTS (no key needed) |
| `pydub` | MP3 → PCM conversion |
| `opencv-python` | camera capture for face recognition |
| `face_recognition` | face encoding and matching |
| `cyclonedds==0.10.2` | DDS transport (required by Unitree SDK) |

---

## Notes for Students

- **Never commit your `.env` file or API key.** The `.gitignore` already excludes `.env`.
- The `memory/faces/` folder is also gitignored — face photos are personal data.
- If you don't have a Unitree G1, you can still study the conversation logic; the DDS subscriber will simply not receive any messages.
- Chinese TTS (`VOICE_ZH`) is commented out in the code but easy to re-enable.

---

## Course Context

This project was developed as part of the robotics program at **CSUSB** under the supervision of **Prof. Yutong Liu**. It demonstrates real-time LLM integration on an embedded humanoid platform.
