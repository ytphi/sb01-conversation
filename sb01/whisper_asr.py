"""
Speech recognition for sb01: Whisper on the laptop GPU, listening through the robot's own mic.

Replaces the G1's built-in ASR (rt/audio_msg text), which made too many mistakes. The G1
streams its microphone as raw 16 kHz mono int16 PCM over UDP multicast 239.168.123.161:5555
(see unitree_sdk2/example/g1/audio/g1_audio_client_example.cpp). Silero VAD (the ONNX model
shipped with faster-whisper) finds where each utterance starts and ends; the utterance is
transcribed with faster-whisper and handed to on_text(text).

Audio is thrown away while muted() is true (Yotie is speaking) and for a short tail after,
so she doesn't hear herself.

Standalone check (prints what it hears, no robot motion):
  python3 sb01/whisper_asr.py [interface] [--save DIR]
--save writes each utterance as DIR/NNN.wav + DIR/NNN.txt, to compare models/settings later
(scripts/compare_asr.py).
"""

import fcntl
import os
import queue
import re
import socket
import struct
import threading
import time
import wave

import numpy as np

MIC_GROUP, MIC_PORT = "239.168.123.161", 5555
SAMPLE_RATE = 16000
VAD_WIN = 512                   # 32 ms windows (what Silero expects at 16 kHz)
VAD_CONTEXT = 64
START_PROB, END_PROB = 0.5, 0.35
END_SILENCE_S = 0.8             # a pause this long ends the utterance
MIN_SPEECH_S = 0.3              # shorter blips (a cough, a click) are ignored
MAX_UTTERANCE_S = 20.0
PRE_ROLL_S = 0.3                # keep a little audio from before speech started
MUTE_TAIL_S = 0.5               # still deaf this long after Yotie stops speaking

MODEL = "large-v3-turbo"
LANGUAGES = ("en", "zh", "es")  # the languages sb01 can answer in; anything else → English
# Names and course terms, so Whisper picks them over common look-alikes ("Unitray", "common
# filter" for Kalman filter…). Written as a question in the course's style; Whisper reads it as
# the preceding text. Keep it short: only the last ~220 tokens count.
PROMPT = (
    "Hi Yotie, a question about IST 5930 at CSUSB: the Unitree G1, embodied AI, Kalman filter, "
    "particle filter, Bayes' rule, Markov assumption, SLAM, Gaussian, inverse kinematics, "
    "quaternion, PID controller, reinforcement learning, PPO, reward shaping, imitation learning, "
    "behavior cloning, sim-to-real, domain randomization, MuJoCo, Isaac Sim, motion capture, "
    "Qualisys, retargeting, GMR, gradient descent, backpropagation, transformer, LeCun's world model."
)
_PROMPT_WORDS = set(re.findall(r"[a-z0-9]+", PROMPT.lower()))
# Whisper's usual hallucinations on noise or silence
HALLUCINATIONS = {
    "thank you", "thank you.", "thanks for watching!", "thanks for watching.", "you", "bye.",
    "谢谢观看", "谢谢大家", "字幕由amara.org社区提供", "請不吝點贊 訂閱 轉發 打賞支持明鏡與點點欄目",
    "gracias.", "subtítulos realizados por la comunidad de amara.org",
}


def interface_ip(name: str) -> str:
    """IPv4 address of a network interface, e.g. eno0 → 192.168.123.222."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    packed = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))  # SIOCGIFADDR
    return socket.inet_ntoa(packed[20:24])


class StreamingVAD:
    """Silero VAD fed 512-sample windows one at a time, keeping its state between calls."""

    def __init__(self):
        import faster_whisper
        import onnxruntime
        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"],
                                                    sess_options=opts)
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros(VAD_CONTEXT, np.float32)

    def __call__(self, window: np.ndarray) -> float:
        x = np.concatenate([self.ctx, window])[None]
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = window[-VAD_CONTEXT:]
        return float(out.ravel()[0])


class WhisperASR:
    def __init__(self, on_text, interface: str = "eno0", muted=lambda: False,
                 model: str = MODEL, prompt: str = PROMPT, save_dir: str | None = None,
                 language: str | None = None):
        self.on_text = on_text
        self.interface = interface
        self.muted = muted
        self.model_name = model
        self.prompt = prompt
        self.language = language            # None = auto-detect (en/zh/es); "en" = English only
        self._jobs: queue.Queue[np.ndarray] = queue.Queue()
        self.save_dir = save_dir
        self._saved = 0
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    def start(self):
        """Load Whisper (a few seconds) and start listening. Raises if the mic stream or GPU fails."""
        from faster_whisper import WhisperModel
        t = time.time()
        self.model = WhisperModel(self.model_name, device="cuda", compute_type="float16")
        self.vad = StreamingVAD()
        print(f"[asr] whisper {self.model_name} loaded on GPU in {time.time() - t:.1f}s")

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", MIC_PORT))
        local_ip = interface_ip(self.interface)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(MIC_GROUP) + socket.inet_aton(local_ip))
        self.sock.settimeout(1.0)
        print(f"[asr] listening to the robot mic ({MIC_GROUP}:{MIC_PORT} on {local_ip})")

        threading.Thread(target=self._listen, daemon=True, name="asr_listen").start()
        threading.Thread(target=self._transcribe_loop, daemon=True, name="asr_whisper").start()

    # ── mic → utterances ─────────────────────────────────────────────────────
    def _listen(self):
        pending = np.zeros(0, np.float32)       # samples not yet a full VAD window
        pre_roll: list[np.ndarray] = []         # recent windows before speech started
        speech: list[np.ndarray] = []
        in_speech, silent_s, speech_s = False, 0.0, 0.0
        deaf_until = 0.0
        win_s = VAD_WIN / SAMPLE_RATE
        warned = False

        while True:
            try:
                data, _ = self.sock.recvfrom(8192)
            except socket.timeout:
                if not warned:
                    print("[asr] no audio from the robot mic (robot off, or eno0 down?)")
                    warned = True
                continue
            warned = False

            now = time.monotonic()
            if self.muted():
                deaf_until = now + MUTE_TAIL_S
            if now < deaf_until:                # Yotie is talking: drop everything
                pending, pre_roll, speech = np.zeros(0, np.float32), [], []
                in_speech, silent_s, speech_s = False, 0.0, 0.0
                self.vad.reset()
                continue

            pending = np.concatenate([pending, np.frombuffer(data, np.int16).astype(np.float32) / 32768])
            while len(pending) >= VAD_WIN:
                win, pending = pending[:VAD_WIN], pending[VAD_WIN:]
                p = self.vad(win)
                if not in_speech:
                    if p >= START_PROB:
                        in_speech, speech, silent_s, speech_s = True, pre_roll, 0.0, 0.0
                        pre_roll = []
                    else:
                        pre_roll = (pre_roll + [win])[-int(PRE_ROLL_S / win_s):]
                if in_speech:
                    speech.append(win)
                    speech_s += win_s
                    silent_s = silent_s + win_s if p < END_PROB else 0.0
                    if silent_s >= END_SILENCE_S or speech_s >= MAX_UTTERANCE_S:
                        if speech_s - silent_s >= MIN_SPEECH_S:
                            self._jobs.put(np.concatenate(speech))
                        in_speech, speech = False, []
                        self.vad.reset()

    # ── utterances → text ────────────────────────────────────────────────────
    def _transcribe_loop(self):
        while True:
            audio = self._jobs.get()
            if self.muted():
                continue                        # Yotie started talking meanwhile; it's stale
            try:
                text, lang, dt = self.transcribe(audio)
            except Exception as exc:
                print(f"[asr] whisper failed ({exc})")
                continue
            if self.save_dir:
                self._save(audio, text, lang)
            if text:
                print(f"[asr] ({lang}, {len(audio) / SAMPLE_RATE:.1f}s audio, {dt:.2f}s) {text}")
                self.on_text(text)

    def _save(self, audio: np.ndarray, text: str, lang: str):
        self._saved += 1
        base = os.path.join(self.save_dir, f"{self._saved:03d}")
        with wave.open(base + ".wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(f"{lang}\t{text}\n")
        print(f"[asr] saved {base}.wav  (peak {np.abs(audio).max():.3f})")

    def transcribe(self, audio: np.ndarray) -> tuple[str, str, float]:
        t = time.time()
        segments, info = self.model.transcribe(
            audio, language=self.language, beam_size=5, initial_prompt=self.prompt,
            condition_on_previous_text=False, vad_filter=False)
        lang = info.language
        if lang not in LANGUAGES:               # short English often gets misdetected
            segments, info = self.model.transcribe(
                audio, language="en", beam_size=5, initial_prompt=self.prompt,
                condition_on_previous_text=False, vad_filter=False)
            lang = "en"
        kept = [s.text for s in segments if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0]
        text = " ".join(t.strip() for t in kept).strip()
        if text.lower() in HALLUCINATIONS or text.strip(" .,!?。，！？") == "" or _echoes_prompt(text):
            text = ""
        return text, lang, time.time() - t


def _echoes_prompt(text: str) -> bool:
    """Whisper sometimes outputs its prompt on noise: 5+ words, all of them from the prompt."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return len(words) >= 5 and all(w in _PROMPT_WORDS for w in words)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("interface", nargs="?", default="eno0")
    ap.add_argument("--save", metavar="DIR", help="save each utterance (wav + transcript) here")
    ap.add_argument("--lang", choices=("auto", "en"), default="auto")
    args = ap.parse_args()
    asr = WhisperASR(on_text=lambda text: None, interface=args.interface, save_dir=args.save,
                     language=None if args.lang == "auto" else args.lang)
    asr.start()
    print("[asr] speak near the robot (Ctrl-C to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
