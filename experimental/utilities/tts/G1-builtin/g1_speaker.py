"""
g1_speaker.py  -  play 16 kHz mono int16 PCM through the G1 speaker (AudioClient.PlayStream)

Also wraps the LED ring so the conversation loop can show listening / thinking / speaking.
"""

import threading
import time

from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

PCM_SAMPLE_RATE = 16000
PCM_CHUNK_BYTES = 96000          # 3 s of audio per PlayStream call

LED_OFF       = (0, 0, 0)
LED_LISTENING = (0, 128, 0)
LED_THINKING  = (128, 128, 0)
LED_SPEAKING  = (0, 0, 128)


class G1Speaker:
    def __init__(self, app_name: str = "sb01", volume: int = 100):
        self.app_name      = app_name
        self.volume        = volume
        self.audio         = AudioClient()
        self.speaking      = threading.Event()   # shared with G1ASRInput
        self.playback_done = threading.Event()   # set by G1ASRInput on play_state == 0

    def start(self):
        self.audio.SetTimeout(10.0)
        self.audio.Init()
        self.audio.SetVolume(self.volume)

    def led(self, rgb: tuple[int, int, int]):
        self.audio.LedControl(*rgb)

    def play_pcm(self, pcm: bytes):
        self.speaking.set()
        self.playback_done.clear()
        try:
            self.led(LED_SPEAKING)
            stream_id = str(int(time.time() * 1000))
            total = len(pcm)
            for offset in range(0, total, PCM_CHUNK_BYTES):
                self.audio.PlayStream(self.app_name, stream_id,
                                      list(pcm[offset:offset + PCM_CHUNK_BYTES]))
                if offset + PCM_CHUNK_BYTES < total:
                    time.sleep(1.0)
            self.playback_done.wait(timeout=total / (PCM_SAMPLE_RATE * 2) + 3.0)
            time.sleep(0.4)      # let the room echo die before listening again
        finally:
            self.led(LED_OFF)
            self.speaking.clear()
