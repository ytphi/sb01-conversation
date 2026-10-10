# G1 Development Log

## SDK Documentation References
- Python SDK: `unitree_sdk2_python/README.md`
- Python G1 examples: `unitree_sdk2_python/example/g1/readme.md`
- C++ SDK: `../../unitree_sdk2/README.md`
- G1 audio client: `unitree_sdk2_python/unitree_sdk2py/g1/audio/g1_audio_client.py`
- Audio example: `unitree_sdk2_python/example/g1/audio/g1_audio_client_example.py`

---

## Session Log

### 2026-10-10 — Repo split: G1 projects under `unitree/g1/`, teleop moved out

- Local layout: `unitree/g1/{g1-sb01, g1-qtm-teleop, g1-kendama, g1-text-to-motion}`. This repo was `unitree/g1`; shared deps (env, env310, TEXEDO, unitree_sdk2) stay in `unitree/`.
- `teleop/` → `sb01/` (face_id, memory_manager, knowledge_base, talk_gestures, whisper_asr). MoCap teleop (qtm_stream, retarget, gmr_retarget, safety, robot_control, live_viewer, record/, teleop_session.py, config/robot.yaml) now lives only in `../g1-qtm-teleop`, installed editable in env310 and texedo310; `sb01/talk_gestures.py` imports `teleop.safety` from there.
- The patched GMR clone moved from `teleop/gmr` to `../g1-qtm-teleop/GMR` (user-site editable install repointed).
- Old copies (GMM retargeter, adapter, BVH/FBX scripts, kendama and text-to-motion copies, `.orig` backups) archived in `~/archive/g1-sb01-legacy-2026-10-10/`.

### 2026-10-08 — sb01 speech recognition → Whisper (`--asr whisper`, now the default)

The G1's built-in ASR (`rt/audio_msg` text) made too many mistakes. New `teleop/whisper_asr.py`:
- **Audio:** the robot's own mic, which the G1 streams as raw 16 kHz mono int16 over UDP multicast `239.168.123.161:5555` (4096-byte packets = 128 ms; from `unitree_sdk2/example/g1/audio/g1_audio_client_example.cpp`). Joined on eno0's IP.
- **Endpointing:** Silero VAD (the ONNX model bundled with faster-whisper), streamed in 32 ms windows with state kept between windows. Start ≥ 0.5, end after 0.8 s < 0.35, min 0.3 s of speech, 20 s cap, 0.3 s pre-roll.
- **Whisper:** faster-whisper 1.2.1 `large-v3-turbo` fp16 on the RTX 4060 (loads in about 1 s once cached; the first download was 47 s). beam 5, a vocabulary prompt (Yotie, Unitree G1, CSUSB, IST 5930…). Language auto-detected; anything other than en/zh/es is re-run as English. Segments with no_speech > 0.6 or logprob < −1.0 and known hallucinations ("Thank you.", "谢谢观看"…) are dropped.
- **Echo:** mic audio is dropped while `speaking` is set, plus a 0.5 s tail; stale utterances are dropped too.
- **sb01:** `--asr whisper|robot` (default whisper; falls back to robot ASR if Whisper fails to start). The `rt/audio_msg` subscription stays, because `play_state` is how playback end is detected. Whisper gives no emotion, so emotion LEDs and hints only work with `--asr robot`.
- Installed `faster-whisper` into env310.

**Follow-up (same day):** the user says Whisper is no better and mishears English course terms (for example "Kalman filter").
- `PROMPT` now lists course vocabulary (Kalman/particle filter, Bayes, SLAM, PPO, retargeting…; 129 tokens, a question in the course's style).
- `--asr-lang en` (sb01) / `--lang en` (standalone) forces English, in case accented English is detected as Chinese.
- `_echoes_prompt()` drops transcripts made only of prompt words (5+ words). large-v3 output the whole prompt on a silent clip.
- `whisper_asr.py --save DIR` records each utterance; `scripts/compare_asr.py DIR` runs turbo/large-v3 × auto/en on them.
- Not yet diagnosed: no recordings of the user's speech yet. Still to rule out: a far-field mic (peak only 0.035 in a quiet room).

**Tested:** SDK `test.wav` → "Unitree G1人形智能体,解锁无线运动潜力。" in 0.68 s (the prompt fixed "Unitray"). A fake-mic test with noise, clip, noise and a muted clip gave exactly one transcript. Live robot mic: 16 kHz confirmed, room noise RMS ≈ 0.005, VAD median 0.05; 25 s of live listening with nobody talking gave no false transcripts (one hallucination, "Казахстан", was filtered). **Not yet tested with a real conversation.**

---

### 2026-10-08 — Talking gestures: first robot run failed, made gentler + balance watchdog

**First real run** (`sb01 --demo --talk-gestures`, walk mode, not on the gantry): while Yotie talked, the robot **stepped backward and the arms shook**. Stopped with SIGINT, and the arms were released.

**Likely causes (not confirmed):** v2 moves were full size and fast (peak 1.24 rad/s, fist bump 33 cm forward), so the walk controller had to step to recover from the center-of-mass shift and arm reaction. kd 1.5 gave little damping while the body moved under the arms. The waist hold (joints 12–14 at the measured pose) is kept: Unitree's arm7 example and xr_teleoperate motion mode both command the waist too, and dropping it might leave the waist limp under weight 1.

**Changes in `teleop/talk_gestures.py`** (backup: `talk_gestures.py.pre-1008`):
- `AMPLITUDE = 0.5` (all poses half size), `MAX_VEL` 1.5 → 1.0 rad/s, so the peak is now 0.47 rad/s
- `KD` 1.5 → 3.0 (as xr_teleoperate), kp stays 60
- Balance watchdog: torso roll/pitch change > `TILT_LIMIT` 0.10 rad since speech start, or gyro > `GYRO_LIMIT` 0.8 rad/s → `BALANCE STOP`, arms handed back over 0.2 s, talking gestures off for the session

**Tested (sim / fake robot):** 3 seeds, peak 0.46–0.47 rad/s, limit margin 1.40 rad; only one new near-contact (right wrist 5.9 cm from the hip during the fist bump). Watchdog: tilt 0.15 rad → released in 0.21 s; gyro 1.0 rad/s → 0.23 s; the next reply doesn't take the arms. **Not yet on the robot. Test on the gantry before using it on the floor.**

**Gantry test 1 (after the changes above):** the robot still stepped backward, sometimes *after* talking. Two more fixes:
- **Weight flicker bug (since v1):** `weight = 1.0 if target_w > weight else weight - step` dropped a step whenever the weight was already 1, so during speech it went 1.0 → 0.96 → 1.0 every tick and the arms switched owner at 25 Hz. This is likely the arm shaking. Now `1.0 if target_w > 0`. Fake-robot check: weight stays at exactly 1.000 over 150 ticks.
- **Hand-back too fast:** `WEIGHT_RAMP_S` 0.5 → 2.0 s (xr_teleoperate uses 2 s, Unitree's arm7 example 5 s); `YIELD_RAMP_S` 0.2 → 0.5 s. The watchdog doesn't cover this phase after weight 0, so stepping after talking was invisible to it.

**Also:** Chinese TTS says 育僮 as 育同 (edge-tts reads 僮 as zhuàng); Claude is told the name (`NAME_PROMPT`, `TTS_RESPELL` in `sb01_conversation.py`).

---

### 2026-10-08 — QTM → G1 teleop dry test in the mocap studio (GMR works)

First run of the teleop pipeline on live QTM data (`--dry-run`, MuJoCo viewer only, no robot).

- **Network:** the QTM PC is at `192.168.0.1` on a plain switch with no DHCP. It has no Wi-Fi, so a hotspot isn't possible. Laptop `eno0` uses the new NetworkManager profile `QTM-Direct` (`192.168.0.222/24`, autoconnect off). Run `nmcli con up QTM-Direct`, because `Robot-Direct` grabs eno0 on its own. QTM 2024.2 runs at 100 Hz with 13 cameras.
- **Port 22223, not 22222.** On QTM 2024.2, port 22222 accepts the connection but supports only RT protocol 1.0, so `qtm_rt.connect` hangs silently. Changed `config/robot.yaml` and the Obsidian runbook.
- **QTM setup:** wand-calibrate the volume (Z up), Animation marker set (42 markers labeled `Actor1_*`), save the AIM model, T-pose skeleton calibration. The skeleton is named `Actor1`, with 24 segments. Under Real-time actions, 3D tracking, Apply AIM and Solve skeletons must all be on.
- **Fix `teleop/qtm_stream.py`:** `packet.get_skeletons()` returns `(info, skeletons)` or None, but the code indexed it as a list. That raised `TypeError` on the first real skeleton packet. `stop()` no longer crashes when the transport is already gone. Live check: 24/24 segments, latency median 4.6 ms, max 11 ms, no gaps over 0.2 s.
- **Fix GMR offsets for the live stream:** the `qtm_bvh` rotation offsets were tuned for BVH files, so with live QTM data the arms came out reversed (left arm pointing −X while the actor's pointed +X). In a T-pose, every QTM RT segment has the Hips orientation, so offset = pelvis alignment × G1 body orientation in T-pose (shoulder roll ±π/2, elbow 1.431 = straight; at q=0 the G1 elbow is bent 90°). New `ik_configs/qtm_rt_to_g1.json`, registered as `qtm_rt` in `params.py`; `teleop/gmr_retarget.py` now uses it. The BVH config is unchanged.
- **Geometric retargeter:** the calibration frame maps to q = 0 (arms down, elbows 90° forward), so it can never show a T-pose. Its axis mapping is still unverified. Changed the prompt from "T-pose" to "copy the robot's zero pose". Use GMR for now.
- `scripts/teleop_session.py` now changes to the repo root at startup, so it runs from any directory (it used to fail with `config/robot.yaml` not found when started from `scripts/`).

**Tested:** GMR output was checked against the live actor through MuJoCo forward kinematics. In a T-pose facing −Y and facing −X, robot facing and upper-arm and forearm directions match the actor within a few degrees. The user ran a live viewer session with `--dry-run --retargeter gmr`: "works perfect". **Not yet on the robot.** That needs a USB Ethernet adapter, since QTM and G1 both need a wired connection, plus the gantry.

**Note:** `teleop/` is gitignored, so these fixes aren't in git.

---

### 2026-10-07 — Talking gestures v2: styles + fist bump

User feedback after v1: movements too small; wants different movement for different talk; no high wave when Yotie sees the user — a fist bump instead.

- `teleop/talk_gestures.py` rewritten: `STYLES` (explain, self, open, you, list, wide, shrug, fistbump) as strokes (rise/hold/fall to an absolute pose, blended from a bigger READY posture); `pick_style()` regex rules on the sentence text; same big style twice in a row → 50% explain; extra explain beats in long sentences; `resume()` so a reply starting while the last one's arms are still settling continues smoothly (v1 skipped it — found in the end-to-end test)
- Stroke durations stretch automatically: `1.5 × distance / (0.85 × MAX_VEL)`. MAX_VEL raised 1.0 → 1.5 rad/s for this module (hand ≈ 0.4 m/s)
- Poses from MuJoCo FK on the real rest pose. **First "self" (hand-to-chest) pose rolled the shoulder inward and went 43 mm into the torso** — replaced with a yaw-based pose (Δ sp −0.80, roll out 0.20, yaw 0.90, elbow −1.40 for the right arm), ≥3 cm clearance. Fist bump: Δ sp −1.00, elbow −0.15 → hand 33 cm forward at chest height
- `sb01_conversation.py`: sentence text (and a cue) passed to `talk.sentence()`; greeting a recognized person → `cue="fistbump"` (or built-in "high five" if talking gestures are off); unknown → wave
- `scripts/preview_talk_gestures.py`: scripted reply with every style; speed/limit report; contact check with 3 cm margin, separated from the rest pose's own near-contacts (hands ≈4 cm from hips); one labelled frame per style

**Tested:** 3 seeds — peak 1.24–1.27 rad/s, ≥1.36 rad limit margin, no new contacts. End-to-end speech path (fake audio + fake arm_sdk): fistbump → self → open → you → shrug, 1.21 rad/s, back to rest, weight 0. Regressions: built-in gesture → 0 msgs; arm stop → weight 0 in 0.20 s; no lowstate → skip. **Not yet on the robot.**

---

### 2026-10-07 — Talking gestures (`--talk-gestures`)

**Goal:** Yotie moves her arms naturally while speaking (she stood still before; built-in actions are big emblem moves only).

**`teleop/talk_gestures.py`:**
- `TalkMotion` (pure, testable): offsets on the 14 arm joints relative to the pose measured at speech start. Engage posture (hands up in front, eased over 1 s), beats (sin² stroke 0.9 s, alternating arms, 25% both) at each sentence start + every 1.6–2.6 s, slow sway, ease back at the end. Beats never stack; first beat waits for the ease-in
- `TalkGestures`: 50 Hz loop publishing `rt/arm_sdk` (`LowCmd_`, kp 60 / kd 1.5, CRC, weight in slot 29 — as in Unitree's arm7 example; waist held at the measured pose like the example). Every command through `SafetyFilter` (limits + 1 rad/s). Takes the arms at the measured pose (no jump), hands back with weight 1→0 over 0.5 s after the pose returns to rest. Yields to built-in arm actions (`GestureController.busy()`): skips the reply, or hands back over 0.2 s. `x` / Ctrl-C → `stop()`
- G1 signs (MuJoCo FK on the real walk-mode pose, which has elbows ≈ 1.18 rad): shoulder pitch − = forward, **elbow − = forearm up/forward (+ = back)**, roll L+ / R− = outward
- Walk-mode rest pose read from `rt/lowstate`: sh pitch ≈ +0.20, roll L +0.21 / R −0.16, elbow ≈ 1.18–1.19, waist ≈ 0

**`sb01_conversation.py`:** `--talk-gestures` flag (off by default); hooks in `_speak_sentences` (begin at first PCM, beat per sentence, end in finally).

**`scripts/preview_talk_gestures.py`:** sim of a 9 s reply → speed/offset/limit report + MuJoCo filmstrip (front + side).

**Tested (no robot motion yet):** 5 seeds: max offset 0.67 rad (elbow), peak 0.91 rad/s, limit margin 1.4 rad. Fake-robot logic tests: no takeover jump (0.0005 rad), returns weight 0 and stops publishing; built-in gesture → nothing sent; arm stop → weight 0 in 0.20 s, stays off; no lowstate → skips. First version had 1.5 rad/s peaks (first beat overlapped the ease-in) — fixed.

**Not tested on the robot yet.** Unknown: how the walk controller handles a long arm_sdk hold, and whether the 0.2 s fast hand-back (arm stop) makes the controller snap the arms back.

---

### 2026-10-07 — Gestures verified on the real G1

- Arm example `send request error` = no reader on `rt/api/arm/request`. In the locked/ready standing pose the robot runs only audio/system services (motion_switcher reports mode `ai`, but no `arm`/`sport` readers). After entering **walk mode** (main operation control) both `rt/api/arm/request` and `rt/api/sport/request` appear, and gestures work.
- Develop/debug mode would shut these services down — gestures need walk mode.
- Check robot services without moving anything: CycloneDDS trace (Verbosity finest) for ~6 s and grep `SubscriptionBuiltinTopicData:{topic_name="rt/api/.../request"`. (The Python builtin-topic DataReader core-dumps in this cyclonedds build.)
- During the test the remote briefly stopped responding. Something on the network was publishing `rt/user_lowcmd` / sport requests, and an unknown host `192.168.123.16` was present (robot = .161, .164). Not this laptop (no robot scripts running). Worth identifying before the demo.

---

### 2026-10-07 — sb01 moved to env310 (DDS was broken in unitree/env)

**Symptom:** `g1_arm_action_example.py` → `DDS_RETCODE_PRECONDITION_NOT_MET ... initialisation of a cyclonedds.topic.Topic`. Same for `AudioClient`, an `rt/audio_msg` subscriber, and a plain cyclonedds topic, on any interface (eno0, lo, wifi), with either `.cyclonedds-home` or its August backup. So `sb01` itself could not start on the robot.

**Cause:** `unitree/env` = Python 3.12 + cyclonedds-python 0.10.2 *built from source* against `.cyclonedds-home` (0.10.x has no cp312 wheel). Topic creation fails inside the C layer (nothing in the DDS trace). Discovery itself works (robot's topics visible). Not caused by today's package installs (only lxml/openpyxl/python-docx/python-pptx/xlsxwriter/et_xmlfile, not loaded by the probe). cyclonedds 11.0.1 (the only cp312 wheel) creates topics but never matches the robot's endpoints → RPC send always fails.

**Fix:** new venv `unitree/env310` = conda `texedo310`'s Python 3.10 with `--system-site-packages` (reuses its compiled dlib/face_recognition/cv2) + its own: official `cyclonedds==0.10.2` wheel (bundles libddsc 0.11), anthropic 1.12, edge-tts, scikit-learn, pypdf, python-pptx/docx, openpyxl, urllib3, face_recognition_models, `-e unitree_sdk2_python`. `texedo310` and `unitree/env` untouched. `scripts/run_sb01.sh` now uses env310, no LD_LIBRARY_PATH.

**Verified in env310:** all sb01 imports; KB rebuilt (fingerprint now includes the scikit-learn version — the old index was pickled with 1.9.0, env310 has 1.7.2); `rt/audio_msg` subscriber; AudioClient + G1ArmActionClient created; **robot replied GetVolume = 100** (first call after start can return 3102/3104 until discovery finishes, ~3 s); edge-tts; Claude streaming answers incl. gesture pick.

**Not fixed:** `run_g1_rviz.sh` and `run_g1_mirror.sh` still use `unitree/env`, so live DDS there likely fails the same way (they were "live untested").

---

### 2026-10-07 — Week 8 Canvas final + query expansions

- Saved `memory/course_files/Canvas/Week 8 — We Present at October Tech (Canvas final).md` (pasted Canvas text; ASCII map, image placeholder and [RSVP link] placeholder left out)
- Supersede rule changed: Canvas finals now name their draft explicitly in frontmatter (`replaces: <draft>.md`, `_replaced_drafts()`). The label rule ("Week 8") would also have hidden the separate Week 8 Reflection Paper page. Lab 3 final updated with its `replaces:` line
- `sb01_conversation.py`: `QUERY_EXPANSIONS` / `expand_query()` — "how long" → length minutes, "assigned" → assignment, "deadline" → due, "turn in" → submit due, "what time" → schedule. Fixed "How long is our presentation?" (12 + 3 min) and "What reading is assigned for week 5?" (FaceNet)
- **Week 8 page conflicts (fix on Canvas, then re-paste):** keynote 9:05 AM (text) vs 9:15–10:15 (schedule); "keynote and panel 9:05–10:05" vs panel at 10:30; team talks "2:00 PM" (instructions) vs 2:30–3:30 (schedule) vs Lab 3 page "2:00–4:00"; conference ends 4:30 (due dates) vs pack-up 4:00; template table lists only slides 1–6, 11, 13; "[RSVP link]" and "image.png" placeholders still on the page

---

### 2026-10-07 — Canvas final pages override Obsidian drafts

- New folder `memory/course_files/Canvas/` (gitignored, class-only): final Canvas pages as posted to students, as `.md`. First one: `Lab 3 — From Speech to Response (Canvas final).md`, transcribed from a screenshot
- `knowledge_base.py`: reads `.md` in course_files; a Canvas final labelled "Lab N"/"Week N" removes the Obsidian "(Canvas)" draft with the same label from the index (`_page_label`). Obsidian notes themselves untouched
- Markdown sections now kept whole up to 1600 chars (`NOTE_CHUNK_SIZE`), so a team's deliverables list isn't split and half-dropped
- Effect: Lab 3 no longer reports the draft's *assumed* due date 10/05; handouts are "Lab3 From Speech to Response_T1/T2/T3.pdf"; October Tech = 10/12, 2–4 PM
- Lab 2 Canvas final added (Week 4 lab practice, due 09/28 5:30 PM). Canvas finals get +0.5 in pinned (week/lab/team) searches — they're the official version
- Posted labs = `POSTED_LABS = [1, 2, 3]` in `sb01_conversation.py` (Lab 4 undecided; Obsidian Lab 4 draft in `NOTE_EXCLUDE`). A question naming an unposted lab skips the course search and gets "not released yet"; every turn ends with a posted-labs note placed *after* the course material, because the schedule/slides say "Lab 4 Released" and Sonnet at low effort followed the snippet over the system prompt
- Tested 3× each: week 7 / this week / Lab 2 due / Lab 4 → 0 "Lab 4 released" leaks
- Open: Lab 3 due date (cut off in screenshot). **When Lab 4 is posted:** add 4 to `POSTED_LABS`, save its Canvas page in `memory/course_files/Canvas/`

---

### 2026-10-07 — Fix: Yotie didn't know Week 7

**Causes:** (1) plain `sb01` = demo mode, which hid *all* course material; (2) ASR writes "week seven", only digits were normalized; (3) "week 7 *topics*" ranked the "DDS *Topics*" note above the Week 7 deck/schedule.

**Fixes (`teleop/knowledge_base.py`):**
- Name-free course files (`COURSE_FILE_PUBLIC`: schedule .xlsx, course description .docx, `Reading/`) are `kind: public` → searchable in demo mode. Slide decks (Weeks 3/4/6/7 have team rosters), lab docs, Obsidian pages stay class-only
- Number words one–twenty: "week seven" → week7, "lab four" → lab4
- Chunks whose title/section *is* the week (Week7 deck, "Sheet1: Week 7 10/05" schedule row — rows now labelled by first cell) rank above passing mentions; pinned search returns k+2
- Fixed crash: public chunks have no page number (label used `c['page']`)
- Cleaner startup: pypdf warnings silenced; rebuild message says ~30s, don't interrupt; `import_course_zip.py` prebuilds the index

**`sb01_conversation.py`:** KB search errors are caught → answer without course material instead of going silent.

**Tested:** class — week 7 / week seven / lab four / week three answered from slides + Canvas + schedule; demo — week 7 from the schedule row only, "what do students learn" from the syllabus; broken KB → Yotie still answers.

---

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
