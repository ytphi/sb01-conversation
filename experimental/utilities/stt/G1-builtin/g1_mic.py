"""
g1_mic.py  -  raw microphone audio from the G1, for speech recognition on the PC

The G1's voice service multicasts its microphone as raw 16 kHz mono int16 PCM
over UDP (group 239.168.123.161, port 5555 per Unitree's G1 audio docs). The
group is joined on the interface wired to the robot (eno0 = 192.168.123.222).
"""

import socket
import struct
import threading

from stt_common import AudioSource


class G1MicSource(AudioSource):
    name = "g1-mic"
    hint = ("check stt.g1_mic (group/port/local_ip) and that the robot's voice service is "
            "running, or use --stt g1-asr")

    def __init__(self, local_ip: str = "192.168.123.222", group: str = "239.168.123.161",
                 port: int = 5555):
        super().__init__()
        self.local_ip = local_ip
        self.group    = group
        self.port     = port
        self._sock: socket.socket | None = None

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(self.local_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self._sock = sock
        threading.Thread(target=self._recv_loop, daemon=True).start()
        print(f"[stt] receiving G1 mic audio from {self.group}:{self.port} via {self.local_ip}")

    def _recv_loop(self):
        while True:
            data, _addr = self._sock.recvfrom(65536)
            self.chunks.put(data)
