# Week 6 (Sep 28 and 30): How do robots understand language?

Essential question: how does Yotie turn what we say into a useful response, and what can go wrong along the way?

## What powers Yotie
- G1 speech recognition: converts speech into text, on the robot.
- DDS: delivers the recognized text to our application.
- The Python conversation script: coordinates messages, context and services.
- Claude API: generates the text response.
- Text-to-speech: converts the response into speech audio.
- G1 audio interface: plays the response.
Yotie is our prototype teaching assistant running on the Unitree G1.

## How topics reach the workstation
The G1's robot computer is at 192.168.123.161. The workstation connects by Ethernet with the address 192.168.123.222. The G1 publishes `rt/lowstate` (joints, IMU) and `rt/audio_msg` (speech text); our program can publish `rt/arm_sdk` (arm commands). The script calls `ChannelFactoryInitialize(0, <network interface>)`, then subscribes and publishes by topic name, with no IP addresses in the code. WiFi stays on for the internet and the Claude API.
Connect in three steps: plug in the Ethernet cable and set the address; check the link with `ping 192.168.123.161`; subscribe to `rt/lowstate` and watch joint angles stream in.

## ASR and DDS
- ASR, automatic speech recognition, converts speech into text. Yotie subscribes to the DDS topic `rt/audio_msg`, which carries a JSON string inside a `String_` message.
- `is_final: true` marks a completed recognition result, seen after about 1 to 2 seconds of silence.
- `play_state: 1 / 0` reports that playback started or finished.
- `LedControl(R, G, B)` sets the robot's LED color. `PlayStream(app, id, pcm)` sends PCM audio to the robot. PCM is pulse-code modulation: sound as a sequence of numerical samples.
- Does DDS recognize speech? No. DDS is the mail carrier: it delivers the text without reading it. "ASR is like a person writing down what you say. DDS is the messaging system that delivers that written message to our program." Our code receives text, never audio.
- So if Yotie mishears a word, look at the robot's ASR, not DDS or our code.
- A callback is a function that another part of the system calls when an event happens. Here the event is a message arriving.
- A timer is a fallback: it waits 1.5 seconds after the most recent non-final message before submitting the pending text, so Yotie does not wait forever. It measures time between messages, not microphone silence.

## Yotie's conversation loop
1. Hear: the G1 microphone and onboard ASR turn speech into text.
2. Wait: for `is_final`, or 1.5 seconds with no new text.
3. Add context: weather, reference pages, course notes, an emotion hint and the recent turns.
4. Think: Claude writes the reply.
5. Voice: text-to-speech turns the reply into audio, converted to 16 kHz PCM.
6. Play: `PlayStream` sends 3-second chunks to the speaker; the microphone is ignored while speaking.
When playback ends, Yotie listens again.

## What can go wrong
- Hear: ASR mishears names, accents or background noise, so Yotie answers a different question.
- Wait: the timer fires while someone pauses mid-sentence, so they are cut off. Very short text can be dropped.
- Context: word matching misses notes phrased differently, giving a vague answer.
- Think: the whole reply is written before any sound, so there is a long silence. The model predicts likely text and does not check facts, so it can give a confident wrong answer.
- Play: the microphone is ignored while Yotie speaks, so you cannot interrupt with "stop".

## How Claude reads a question: the transformer
A transformer turns words into numbers, lets every word look at every other word many times over, then guesses the next word, one at a time.
1. Tokens: text is cut into pieces. Rare words like "Yotie" often split into several pieces; common words usually stay whole. The model never sees letters, only token IDs. `max_tokens` caps the reply length, and recent turns are re-sent on every call, so each question uses more tokens than the one before.
2. Embeddings: each token becomes a list of numbers, and similar meanings land close together. This is the same idea as comparing face embeddings with cosine similarity in week 5. Searching notes by shared words would miss "two-legged robot" for "humanoid"; embedding search would find it.
3. Attention: every token looks at every other token, in every layer. In "The robot picked up the marker because it was on the floor", "it" attends to the marker; change the ending to "because it was being helpful" and "it" attends to the robot. Attention only sees the prompt, which is why follow-up questions work only because earlier turns are re-sent.
4. Predict, repeat: score every possible next token, pick one, append it, and repeat. Likely is not the same as true: course notes in the prompt make the right answer more likely. Prompt instructions such as "1-3 short sentences, no markdown" shift the odds the same way.

## Improving the pipeline
- Long silence: stream Claude's reply and speak each sentence as soon as it is ready. Streaming feels faster because the first sound comes after the first sentence. The catches: split text at sentence ends, keep the speaking flag on across sentences, and check playback does not gap or overlap.
- Course notes missed: search by embeddings, not shared words.
- Cut off or slow to start: tune the timer from measured pauses.
- Cannot interrupt: keep listening for "stop" while speaking.
- Long chats get slow and costly: summarize old turns.
- Nobody knows what is slow: log the time of every stage.
- One robot, three voices: the Unitree system voice announces robot modes, the Unitree app voice prompts "How can I help?", and our Yotie application answers as the classroom teaching assistant.

## Lab: three teams, one pipeline
- Deploy team, "answer sooner": log a timestamp at ASR final, Claude reply ready, PCM ready and first PlayStream; ask the same 10 course questions for a baseline; switch to streaming; ask again and compare. Hand in a before/after table, a git diff and a 1-minute demo. Log timings and status only, never the text.
- Security team, "follow the data": find everything that leaves the room, including what is sent to Claude, the speech service and the weather API, and propose one change that sends less data. Hand in a data-flow table and one recommendation.
- Simulation team, "words to actions": write 10 classroom commands including 3 vague ones, ask Claude to turn each into JSON with a fixed action list (move_to, pick, place, point), score them, and run 3 in the CGI-112 Lucky Engine scene. Physical robot trials need instructor supervision and a safety review.
