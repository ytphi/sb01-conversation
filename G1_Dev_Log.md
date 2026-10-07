# G1 Development Log

## SDK Documentation References
- Python SDK: `unitree_sdk2_python/README.md`
- Python G1 examples: `unitree_sdk2_python/example/g1/readme.md`
- C++ SDK: `../unitree_sdk2/README.md`
- G1 audio client: `unitree_sdk2_python/unitree_sdk2py/g1/audio/g1_audio_client.py`
- Audio example: `unitree_sdk2_python/example/g1/audio/g1_audio_client_example.py`

---

## Session Log

### 2026-10-07 — Google Drive lecture slides + schedule in Yotie's knowledge base

**Source:** Drive export `IST5930_Fall2026-*.zip` (360 MB). Only documents extracted → `memory/course_files/` (gitignored, 160 MB): Week 1–7 lecture decks (.pptx, incl. speaker notes), network-security deck, Lab 2 team docs, Lab 3 handout, course schedule (.xlsx), course doc, Oct Tech decks, extra readings. Skipped: 3D models, videos, blank template, duplicate readings. **Removed `IST5930_Fall2026 Lab Sheets.xlsx`** (student progress + attendance) — never index student records. Re-run for future exports: `scripts/import_course_zip.py <zip>`.

**`teleop/knowledge_base.py`:**
- Reads .pptx (slide text, tables, speaker notes; loc "slide N"), .docx (by heading), .xlsx (one unit per row as "header: value"), PDFs, recursively under `memory/course_files/`. Class-only (`kind: note`) like the Obsidian notes
- `COURSE_FILE_EXCLUDE` skips rosters/sign-ups/attendance/grades by filename
- `_preprocess`: underscores → spaces; "team 3" → "team3" too
- Numbered terms (week3/lab4/team2) pin the search: only chunks containing them, ranked by how many they contain; 1 shared term is enough; ≤2 snippets per file, k+1 results. Fixes "what did we learn in week 3?" (was filtered by the 2-shared-word rule)
- Duplicate snippets (overlapping windows of the same slide/page) removed
- Index: 40 docs, 6026 chunks (75 with speaker notes), ~26s first build, <1s cached
- New deps (installed in unitree/env, added to requirements.txt): python-pptx, python-docx, openpyxl

**Known miss:** "What reading is *assigned* for week 5?" doesn't hit the schedule's "Reading & *Assignment*" column — no stemming (TF-IDF synonym limit). Claude answered from the lab page and said to check Canvas.

---

### 2026-10-07 — IST5930 course notes in Yotie's knowledge base

**Goal:** Yotie answers lesson/lab questions (due dates, what's this week, lab goals), not just Week 1 + readings.

**`teleop/knowledge_base.py`:**
- Indexes markdown course notes in place from Obsidian (`NOTE_SOURCES`): `Work/Teaching/IST5930/*.md`, `15-Week Lab Practice Guide.md`, `Embodied AI Readings.md`. Excluded (`NOTE_EXCLUDE`): `2024Fall IST5930.md` (other course), logistics to-do. Edits in Obsidian → re-indexed on next start (fingerprint); missing paths skipped on other machines
- `_clean_markdown()`: drops "Check before posting" callouts, frontmatter, wikilink/embed syntax
- Notes chunked per heading section; each chunk prefixed "note title — section:"; sources shown as `(note › section)`
- `_preprocess()`: "week 7"/"Lab 4" → "week7"/"lab4" in index + query (the tokenizer was dropping the digit, so all labs looked alike)
- `retrieve(include_notes=False)` skips notes — demo mode uses this because notes name students
- Index: 9 PDFs + 12 notes → 2821 chunks (224 from notes)

**`scripts/sb01_conversation.py`:** each turn gets `[Today is …, IST 5930 week N.]` (`COURSE_START = 2026-08-24`); "this/next/last week" adds `weekN` to the search query; prompt: weekly pages beat the original plan, never guess dates/rooms; demo prompt: never name students.

**Tested (class mode):** reflection paper due date, this week's topic, Lab 4 summary all answered from the pages; "final exam room" → "check Canvas", no guess. Demo mode declined to name students.

**Note:** due dates come from the Obsidian notes — if a date changes on Canvas, update the note too. The Week 7 TF-IDF lecture note's §2 ("what gets indexed: PDFs", "top 9") is now out of date.

---

### 2026-10-07 — Faster replies (streaming + Sonnet 5.5)

**Measured time until the robot starts speaking** (median of 4 questions, fake speaker, real Claude + edge-tts): **4.1s → 2.0s** in demo mode, 2.2s in classroom mode.

**Changes (`scripts/sb01_conversation.py`):**
- Live turns use `LIVE_MODEL = claude-sonnet-5-5`, effort `low` (benchmark first-text: 0.5s vs 1.2s for Opus 4.8 + web search). Session summary stays on Opus (`SUMMARY_MODEL`)
- Claude reply streamed; `pop_sentences()` splits it into sentences (pieces < 25 chars merged, so "U.S." doesn't split); `_speak_sentences()` synthesizes sentence N+1 with edge-tts while sentence N plays. `speaking` stays set for the whole reply (ASR ignored while thinking, too)
- Web search off in demo mode; classroom mode keeps it (`web_search_20260209`)
- System prompt cached (`cache_control`); server-side refusal fallback (`fallbacks="default"`); refusal or API error → spoken apology
- Prompts ask for sentences under ~20 words (faster first sentence)
- Demo prompt now says face recognition uses the laptop camera, not the head cameras

**Known:** a 0.4s gap between sentences (existing post-play pause). Not yet tested on the robot speaker.

---

### 2026-10-07 — Demo mode + arm gestures while talking

**Goal:** Prep sb01 for a visitor demo (Navy officer, Fri 10/09): move while talking, and stop steering guests back to course topics.

**Gestures (`GestureController` in `scripts/sb01_conversation.py`):**
- Uses the G1 built-in arm actions (`G1ArmActionClient`, service `arm`), not custom joint control
- Whitelist `GESTURES`: high wave, face wave, clap (self-releasing); shake hand 4s, high five 3s, heart 2s (then `release arm`). Hug/kiss/x-ray/hands up left out on purpose
- Claude puts `[GESTURE: name]` in its reply; `split_gesture()` strips it before TTS; gesture runs on a thread alongside speech; busy → skipped
- Fixed: high wave on greeting, face wave on Ctrl-C goodbye, `release arm` at shutdown
- Failures (nonzero code / client init error) are logged, never crash the loop

**Demo mode (`--demo`):** `DEMO_SYSTEM_PROMPT` = visitor persona; answers anything; describes lab work honestly (teleop + text-to-motion as ongoing); candid about data flow (text → Claude API, voice → Microsoft TTS, ASR locality under study); defense uses framed as research directions only. Drops the "no course material matched" hint. Gestures on by default with `--demo`; `--gestures/--no-gestures` override.

**Run (quick-start `sb01` = symlink to `scripts/run_sb01.sh`):** `sb01` = demo mode by default; `sb01 --no-gestures` = fallback, arms still; `sb01 --class` = original classroom assistant.

**Arm stop:** type `x` + Enter in the terminal → sends `release arm` immediately, disables gestures for the rest of the session, conversation keeps going. Ctrl-C now releases the arms first (no goodbye wave). Neither replaces the hardware e-stop / remote damping — the script can't stop locomotion or a robot-side fault.

**Tested:** offline parser/controller tests + Claude prompt check (correct gesture picks). **Not yet tested on the robot** — arm actions need the robot standing in its normal locomotion mode.

---

### 2026-08-18 — Per-person memory consent + topic tagging

**Goal:** Don't save conversation memory for a recognized person without asking first; tag saved sessions with a topic.

**Consent flow (`scripts/sb01_conversation.py`):**
- After the greeting, if a recognized person has never been asked (`profile["memory_consent"] is None`), sb01 asks aloud: "is it okay if I remember what we talk about today?"
- Answer parsed from the next ASR utterance (yes/no keyword match, 10s timeout, defaults to **no** on silence/unclear reply)
- Decision stored permanently in `memory/profiles/<name>.json` as `memory_consent` — asked only once per person; declines are respected on future runs
- `_save_session()` now checks `self.memory_consent` before writing anything (facts or summary)

**Topic tagging:**
- End-of-session summary prompt now also asks Claude for a `TOPIC:` line (3-6 word label)
- Sessions saved as `memory/sessions/<name>/<timestamp>.json` with `{name, date, topic, summary}` instead of plain `.txt`
- `MemoryManager.load_recent_sessions()` reads both new `.json` records and legacy `.txt` files for backward compatibility with pre-existing session history

**Files:** `teleop/memory_manager.py` (`get_consent`/`set_consent`, JSON session records), `scripts/sb01_conversation.py` (`_ask_memory_consent`, topic parsing in `_save_session`)

---

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

## Pending / Next Steps

- [ ] Implement Claude streaming response + per-sentence TTS (reduce latency)
- [ ] Test ElevenLabs streaming TTS as alternative to edge-tts
- [ ] Add arm gesture during speech (wave hello on greeting)
- [ ] Test sb01 with multiple people talking
