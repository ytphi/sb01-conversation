"""Offline tests. `python3 -m unittest discover` from the project root runs them all."""

import os

# Offline: the conversation script under test uses the onboard-ASR path unless a
# test asks otherwise, so no test loads Whisper or listens to the robot's mics.
os.environ["SB01_STT"] = "g1-asr"
