#!/usr/bin/env python3
"""
test_voice.py  -  offline check of the optional Chatterbox voice

The real conversation program and the real teleop/voice_chatterbox.py, with a
stand-in voice engine in place of the Chatterbox models (no model, GPU, robot,
Claude or network needed). Same stand-ins as tests/test_language.py otherwise.

  python3 -m unittest tests.test_voice -v      (from the project root)
"""

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from tests import test_language as base     # loads the real script once, with stand-ins
from teleop import voice_chatterbox as vc

script = base.script
RATE = 24000                                # what the stand-in engine produces
SECONDS_PER_LETTER = 0.05
EDGE = 0.1                                  # silence the stand-in puts at each end


class _Engine:
    """Stands in for the speech framework's SpeechSynth: a tone as long as the text."""

    def __init__(self):
        self.preloads, self.said, self.fail, self.bad = 0, [], None, None
        self.fail_on = None                 # fail only when this text is asked for
        # what the real SpeechSynth keeps: its loaded engines, each holding a model
        self._engines = {"nano": types.SimpleNamespace(model=object()),
                         "multilingual": types.SimpleNamespace(model=object())}

    def preload(self):
        self.preloads += 1
        if isinstance(self.fail, Exception):
            raise self.fail

    def synthesize(self, text, language):
        self.said.append((text, language))
        if isinstance(self.fail, Exception) and self.fail_on in (None, text):
            raise self.fail
        if self.bad is not None:
            return self.bad
        tone = 0.5 * np.sin(np.arange(round(len(text) * SECONDS_PER_LETTER * RATE)) * 0.05, dtype=np.float32)
        quiet = np.zeros(round(EDGE * RATE), dtype=np.float32)
        return np.concatenate((quiet, tone, quiet)), RATE


def _to_pcm16(wave, rate, target):
    index = np.arange(0, len(wave), rate / target).astype(int)
    return (np.clip(wave[index], -1, 1) * 32767).astype(np.int16).tobytes()


def _voice(engine=None):
    return vc.ChatterboxVoice(synth=engine or _Engine(), to_pcm16=_to_pcm16)


class _Robot:
    """Robot audio and gesture client in one, recording the order of everything."""

    def __init__(self):
        self.events, self.played, self.starts = [], [], []

    def LedControl(self, *rgb):
        pass

    def PlayStream(self, app, stream, pcm):
        self.events.append("audio")
        self.played.append(bytes(pcm))

    def start(self, pcm, **kwargs):
        self.events.append("gesture-start")
        self.starts.append((pcm, kwargs))
        return True

    def begin(self, delay=0.0):
        self.events.append("gesture-begin")

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _loop(engine=None, gestures=True):
    loop = base._new_loop()
    robot = _Robot()
    loop.audio = robot
    loop.gestures = robot if gestures else None
    loop.tts_done.wait = lambda timeout=None: True
    if engine is not False:
        loop.voice = _voice(engine)
        loop.voice.load()
    return loop, robot


def _speak(loop, text, language=None):
    out = io.StringIO()
    with mock.patch.object(script.time, "sleep", lambda seconds: None), contextlib.redirect_stdout(out):
        loop._speak(text, language)
    return out.getvalue()


class ChoosingTheVoice(unittest.TestCase):
    def test_edge_tts_is_the_default_and_nothing_is_loaded(self):
        self.assertEqual(script.VOICE_BACKEND, "edge")
        loop, robot = _loop(engine=False)
        with mock.patch.object(script, "ChatterboxVoice", side_effect=AssertionError("must not be created")):
            loop._load_voice()
        self.assertIsNone(loop.voice)
        base.tts_calls.clear()
        _speak(loop, "Hello there, nice to meet you.")
        self.assertEqual(len(base.tts_calls), 1)                         # Edge TTS spoke
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)

    def test_the_setting_is_read_from_the_environment(self):
        with open(base.SCRIPT, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('VOICE_BACKEND = os.environ.get("SB01_VOICE", "").strip().lower() or "edge"', source)

    def test_an_unknown_voice_name_is_reported_and_edge_is_used(self):
        loop, _ = _loop(engine=False)
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "robotvoice"), contextlib.redirect_stdout(out):
            loop._load_voice()
        self.assertIsNone(loop.voice)
        self.assertIn("is not a voice", out.getvalue())
        self.assertIn("using Edge TTS", out.getvalue())

    def test_chatterbox_is_loaded_when_asked_for(self):
        loop, _ = _loop(engine=False)
        engine = _Engine()
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "chatterbox"), \
                mock.patch.object(script, "ChatterboxVoice", lambda: _voice(engine)), contextlib.redirect_stdout(out):
            loop._load_voice()
        self.assertIsNotNone(loop.voice)
        self.assertEqual(engine.preloads, 1)
        self.assertIn("Chatterbox ready", out.getvalue())
        self.assertIn("Edge TTS is the fallback", out.getvalue())


class WhenChatterboxIsNotAvailable(unittest.TestCase):
    def _load(self, **patches):
        loop, robot = _loop(engine=False)
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "chatterbox"), contextlib.redirect_stdout(out):
            with contextlib.ExitStack() as stack:
                for target, value in patches.items():
                    stack.enter_context(mock.patch.object(script, target, value))
                loop._load_voice()
        return loop, robot, out.getvalue()

    def _assert_edge_still_speaks(self, loop, robot):
        base.tts_calls.clear()
        _speak(loop, "Hello there, nice to meet you.")
        self.assertEqual(len(base.tts_calls), 1)
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])     # gestures still run

    def test_a_missing_python_package_is_named_with_how_to_install_it(self):
        framework_modules = {name: None for name in ("yaml",)}
        forgotten = {name: sys.modules.pop(name) for name in ("settings", "speech_synth", "_paths", "tts_common")
                     if name in sys.modules}
        try:
            with mock.patch.dict(sys.modules, framework_modules):
                loop, robot, printed = self._load()
        finally:
            sys.modules.update(forgotten)
        self.assertIsNone(loop.voice)
        self.assertIn("Chatterbox is NOT in use", printed)
        self.assertIn("a Python package is missing (yaml)", printed)
        self.assertIn("pip install -r experimental/speech-framework/requirements.txt", printed)
        self.assertIn("using Edge TTS instead", printed)
        self._assert_edge_still_speaks(loop, robot)

    def test_a_missing_speech_framework_folder_is_reported(self):
        with mock.patch.object(vc, "FRAMEWORK_DIR", os.path.join(base.ROOT, "no-such-folder")):
            loop, robot, printed = self._load()
        self.assertIn("experimental/speech-framework is not in this copy", printed)
        self._assert_edge_still_speaks(loop, robot)

    def test_missing_model_files_are_reported(self):
        engine = _Engine()
        engine.fail = OSError("snapshot_download failed: model files for ResembleAI/chatterbox not found")
        loop, robot, printed = self._load(ChatterboxVoice=lambda: _voice(engine))
        self.assertIsNone(loop.voice)
        self.assertIn("a model or voice file could not be read or downloaded", printed)
        self._assert_edge_still_speaks(loop, robot)

    def test_a_full_graphics_card_is_reported_with_what_to_do(self):
        engine = _Engine()
        engine.fail = RuntimeError("CUDA out of memory. Tried to allocate 512.00 MiB")
        loop, robot, printed = self._load(ChatterboxVoice=lambda: _voice(engine))
        self.assertIsNone(loop.voice)
        self.assertIn("ran out of memory", printed)
        self.assertIn("runs on the processor", printed)               # what to do about it
        self.assertEqual(engine._engines, {})                          # the half-loaded models were let go
        self._assert_edge_still_speaks(loop, robot)

    def test_a_package_version_that_does_not_match_is_reported(self):
        engine = _Engine()
        engine.fail = TypeError("from_pretrained() got an unexpected keyword argument 'nano'")
        _, _, printed = self._load(ChatterboxVoice=lambda: _voice(engine))
        self.assertIn("does not match the speech framework", printed)


MADE = []                                    # the engines each stand-in SpeechSynth was asked to make


@contextlib.contextmanager
def _framework(tts_config, to_pcm16=_to_pcm16):
    """A throw-away copy of the speech framework's shape (settings, SpeechSynth,
    to_pcm16) holding `tts_config`, so the real loading code can be run without
    the Chatterbox package. Yields the list of configs SpeechSynth was built with."""
    folder = tempfile.mkdtemp(prefix="sb01-fake-framework-")
    built = []
    names = ("_paths", "settings", "speech_synth", "tts_common")
    saved = {name: sys.modules.pop(name, None) for name in names}
    for name in names:
        open(os.path.join(folder, name + ".py"), "w").close()

    class Synth(_Engine):
        def __init__(self, config):
            super().__init__()
            built.append(dict(config))
            self.made = {}                                   # engine name -> the engine, as SpeechSynth._engine makes them
            self.config = config
            MADE.append(self.made)

        def _engine(self, kind):
            return self.made.setdefault(kind, types.SimpleNamespace(device=self.config["device"], model=None))

    fakes = {
        "_paths": types.ModuleType("_paths"),
        "settings": types.ModuleType("settings"),
        "speech_synth": types.ModuleType("speech_synth"),
        "tts_common": types.ModuleType("tts_common"),
    }
    fakes["settings"].load_settings = lambda: {"tts": tts_config}
    fakes["settings"].resolve_path = lambda path: os.path.join(folder, path) if path else None
    fakes["speech_synth"].SpeechSynth = Synth
    fakes["tts_common"].to_pcm16 = to_pcm16
    path_before = list(sys.path)
    try:
        with mock.patch.object(vc, "FRAMEWORK_DIR", folder), mock.patch.dict(sys.modules, fakes):
            yield built
    finally:
        sys.path[:] = path_before
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module
        shutil.rmtree(folder, ignore_errors=True)


GOOD_CONFIG = {"device": "auto", "preload": True,
               "languages": {"en": {"engine": "nano", "voice": None},
                             "es": {"engine": "multilingual", "voice": None}}}


class WhereChatterboxRuns(unittest.TestCase):
    """The graphics card is the gesture server's: Chatterbox uses it only when told to, and only if it fits."""

    def setUp(self):
        self._env = {name: os.environ.pop(name, None)
                     for name in ("SB01_CHATTERBOX_DEVICE", "SB01_CHATTERBOX_GPU_GB", "SB01_CHATTERBOX_GPU_ENGINES")}

    def tearDown(self):
        for name, value in self._env.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def test_the_processor_is_the_default(self):
        for asked in (None, "", "auto", "AUTO", " cpu "):
            with mock.patch.object(vc, "gpu_free_gb", side_effect=AssertionError("the card must not even be asked")):
                self.assertEqual(vc.choose_device(asked)[0], "cpu", asked)

    def test_auto_never_means_the_graphics_card_even_with_a_large_one(self):
        with mock.patch.object(vc, "gpu_free_gb", return_value=24.0):
            device, why = vc.choose_device("auto")
        self.assertEqual(device, "cpu")
        self.assertIn("left to the gesture server", why)

    def test_cuda_is_used_only_when_asked_for_and_enough_memory_is_free(self):
        with mock.patch.object(vc, "gpu_free_gb", return_value=11.5):
            device, why = vc.choose_device("cuda")
        self.assertEqual(device, "cuda")
        self.assertIn("11.5 GB", why)

    def test_cuda_on_a_6_gb_card_falls_back_to_the_processor_before_loading_anything(self):
        for free in (6.0, 4.7, 0.3):                      # an empty 6 GB card; the same card with the gesture server on it
            with mock.patch.object(vc, "gpu_free_gb", return_value=free):
                device, why = vc.choose_device("cuda")
            self.assertEqual(device, "cpu", free)
            self.assertIn(f"only {free:.1f} GB of graphics memory is free", why)
            self.assertIn("needs about 7.0 GB", why)
            self.assertIn("runs on the processor", why)

    def test_cuda_without_a_usable_card_runs_on_the_processor(self):
        with mock.patch.object(vc, "gpu_free_gb", return_value=None):
            device, why = vc.choose_device("cuda")
        self.assertEqual(device, "cpu")
        self.assertIn("no usable graphics card", why)

    def test_the_memory_requirement_can_be_changed_on_purpose(self):
        os.environ["SB01_CHATTERBOX_GPU_GB"] = "5"
        with mock.patch.object(vc, "gpu_free_gb", return_value=6.0):
            self.assertEqual(vc.choose_device("cuda")[0], "cuda")
        os.environ["SB01_CHATTERBOX_GPU_GB"] = "not a number"
        with mock.patch.object(vc, "gpu_free_gb", return_value=6.0):
            self.assertEqual(vc.choose_device("cuda")[0], "cpu")           # nonsense: the safe requirement stays

    def test_an_unknown_device_name_is_refused(self):
        with self.assertRaises(vc.VoiceUnavailable) as raised:
            vc.choose_device("gpu0")
        self.assertIn("use cpu or cuda", str(raised.exception))

    def test_the_framework_is_never_handed_auto(self):
        with _framework(dict(GOOD_CONFIG)) as built, mock.patch.object(vc, "gpu_free_gb", return_value=24.0):
            voice = vc.ChatterboxVoice()
        self.assertEqual(built[0]["device"], "cpu")                        # config said auto; a 24 GB card was there
        self.assertEqual(voice.device, "cpu")

    def test_the_setting_overrides_the_config_file(self):
        os.environ["SB01_CHATTERBOX_DEVICE"] = "cuda"
        with _framework(dict(GOOD_CONFIG)) as built, mock.patch.object(vc, "gpu_free_gb", return_value=12.0):
            voice = vc.ChatterboxVoice()
        self.assertEqual((built[0]["device"], voice.device), ("cuda", "cuda"))
        os.environ["SB01_CHATTERBOX_DEVICE"] = "cpu"
        with _framework(dict(GOOD_CONFIG, device="cuda")) as built, mock.patch.object(vc, "gpu_free_gb", return_value=12.0):
            self.assertEqual(vc.ChatterboxVoice().device, "cpu")

    def test_cuda_in_the_config_file_is_also_checked_against_free_memory(self):
        with _framework(dict(GOOD_CONFIG, device="cuda")) as built, mock.patch.object(vc, "gpu_free_gb", return_value=6.0):
            voice = vc.ChatterboxVoice()
        self.assertEqual(built[0]["device"], "cpu")
        self.assertIn("only 6.0 GB", voice.device_note)

    def test_startup_says_where_it_runs_and_what_that_costs(self):
        loop, _ = _loop(engine=False)
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "chatterbox"), \
                mock.patch.object(script, "ChatterboxVoice", lambda: vc.ChatterboxVoice(synth=_Engine(), to_pcm16=_to_pcm16, device="auto")), \
                contextlib.redirect_stdout(out):
            loop._load_voice()
        printed = out.getvalue()
        self.assertIn("Chatterbox ready on the processor", printed)
        self.assertIn("left to the gesture server", printed)
        self.assertIn("expect a pause before each one", printed)


class OneEngineOnTheCard(unittest.TestCase):
    """SB01_CHATTERBOX_GPU_ENGINES: the named engine uses the card, the rest stay on the processor."""

    setUp, tearDown = WhereChatterboxRuns.setUp, WhereChatterboxRuns.tearDown

    def _made(self, free, device="cuda", engines="nano"):
        if device:
            os.environ["SB01_CHATTERBOX_DEVICE"] = device
        os.environ["SB01_CHATTERBOX_GPU_ENGINES"] = engines
        with _framework(dict(GOOD_CONFIG)) as built, mock.patch.object(vc, "gpu_free_gb", return_value=free):
            voice = vc.ChatterboxVoice()
        return voice, built[0], {name: engine.device for name, engine in MADE[-1].items()}

    def test_the_english_engine_fits_beside_the_gesture_server_on_a_6_gb_card(self):
        voice, config, devices = self._made(free=4.7)
        self.assertEqual(config["device"], "cpu")                           # what every other engine is made with
        self.assertEqual(devices, {"nano": "cuda"})
        self.assertEqual(voice.device, "cuda")
        self.assertIn("only the nano engine is on the graphics card", voice.device_note)
        self.assertIn("en is fast, other languages are synthesized on the processor and are slow", voice.device_note)

    def test_it_needs_less_memory_than_both_engines_but_is_still_checked(self):
        for free, expected in ((3.9, "cpu"), (4.0, "cuda"), (6.0, "cuda")):
            with mock.patch.object(vc, "gpu_free_gb", return_value=free):
                self.assertEqual(vc.choose_device("cuda", ("nano",))[0], expected, free)
        for free, expected in ((5.4, "cpu"), (5.5, "cuda")):
            with mock.patch.object(vc, "gpu_free_gb", return_value=free):
                self.assertEqual(vc.choose_device("cuda", ("multilingual",))[0], expected, free)

    def test_the_requirement_is_what_the_engine_uses_plus_room_for_the_gesture_server(self):
        self.assertEqual(vc.GPU_GB_PER_ENGINE, {"nano": 4.0, "multilingual": 5.5})
        for name, needed in vc.GPU_GB_PER_ENGINE.items():
            self.assertGreaterEqual(needed - vc.GPU_GB_USED[name], 1.5)            # what is left once it has loaded

    def test_with_the_gesture_server_already_on_a_6_gb_card(self):
        with mock.patch.object(vc, "gpu_free_gb", return_value=4.7):                # 6.0 minus the gesture server
            self.assertEqual(vc.choose_device("cuda", ("nano",))[0], "cuda")
            self.assertEqual(vc.choose_device("cuda", ("multilingual",))[0], "cpu") # never shares a 6 GB card
            self.assertEqual(vc.choose_device("cuda")[0], "cpu")
        with mock.patch.object(vc, "gpu_free_gb", return_value=3.7):                # and another program holding 1 GB
            device, why = vc.choose_device("cuda", ("nano",))
        self.assertEqual(device, "cpu")
        self.assertIn("only 3.7 GB of graphics memory is free and Chatterbox needs about 4.0 GB", why)

    def test_the_requirement_cannot_be_set_below_what_the_engines_use(self):
        os.environ["SB01_CHATTERBOX_GPU_GB"] = "0.1"
        for engines, free, expected in ((("nano",), 2.4, "cpu"), (("nano",), 2.5, "cuda"),
                                        (("multilingual",), 3.9, "cpu"), ((), 5.9, "cpu"), ((), 6.0, "cuda")):
            with mock.patch.object(vc, "gpu_free_gb", return_value=free):
                self.assertEqual(vc.choose_device("cuda", engines)[0], expected, (engines, free))

    def test_no_usable_card_means_the_processor(self):
        voice, config, devices = self._made(free=None)
        self.assertEqual((voice.device, config["device"], devices), ("cpu", "cpu", {}))
        self.assertIn("no usable graphics card", voice.device_note)

    def test_without_enough_memory_everything_runs_on_the_processor_and_says_so(self):
        voice, config, devices = self._made(free=1.9)
        self.assertEqual((voice.device, config["device"], devices), ("cpu", "cpu", {}))
        self.assertIn("only 1.9 GB of graphics memory is free", voice.device_note)
        self.assertIn("needs about 4.0 GB", voice.device_note)

    def test_naming_engines_does_nothing_unless_cuda_is_asked_for(self):
        for device in ("", "cpu", "auto"):
            os.environ.pop("SB01_CHATTERBOX_DEVICE", None)
            voice, config, devices = self._made(free=24.0, device=device)
            self.assertEqual((voice.device, config["device"], devices), ("cpu", "cpu", {}), device)

    def test_naming_every_engine_is_the_same_as_naming_none(self):
        voice, config, devices = self._made(free=6.0, engines="nano, multilingual")
        self.assertEqual((voice.device, config["device"], devices), ("cpu", "cpu", {}))     # 7 GB needed, as before
        voice, config, devices = self._made(free=8.0, engines="multilingual,nano")
        self.assertEqual((voice.device, config["device"], devices), ("cuda", "cuda", {}))

    def test_an_engine_name_that_does_not_exist_is_refused(self):
        os.environ["SB01_CHATTERBOX_DEVICE"] = "cuda"
        os.environ["SB01_CHATTERBOX_GPU_ENGINES"] = "turbo"
        with _framework(dict(GOOD_CONFIG)):
            with self.assertRaises(vc.VoiceUnavailable) as raised:
                vc.ChatterboxVoice()
        self.assertIn("SB01_CHATTERBOX_GPU_ENGINES names 'turbo'; use nano or multilingual", str(raised.exception))


class TwoMemoryReadings(unittest.TestCase):
    """Free graphics memory is the lower of PyTorch's figure and nvidia-smi's."""

    def _free(self, torch_says, smi_says):
        with mock.patch.object(vc, "_torch_free_gb", return_value=torch_says), \
                mock.patch.object(vc, "_smi_free_gb", return_value=smi_says):
            return vc.gpu_free_gb()

    def test_the_lower_reading_counts(self):
        self.assertEqual(self._free(5.0, 1.3), 1.3)          # as seen under WSL with another program on the card
        self.assertEqual(self._free(4.6, 4.7), 4.6)

    def test_without_nvidia_smi_pytorch_is_believed(self):
        self.assertEqual(self._free(5.0, None), 5.0)

    def test_without_a_usable_card_there_is_no_reading(self):
        self.assertIsNone(self._free(None, 6.0))

    def test_nvidia_smi_is_read_as_total_minus_used(self):
        answer = types.SimpleNamespace(stdout="6144, 4825\n")
        with mock.patch.object(vc.subprocess, "run", return_value=answer) as run:
            self.assertAlmostEqual(vc._smi_free_gb(), (6144 - 4825) / 1024)
        self.assertEqual(run.call_args.kwargs.get("timeout"), 5)

    def test_a_missing_or_confused_nvidia_smi_gives_no_reading(self):
        with mock.patch.object(vc.subprocess, "run", side_effect=FileNotFoundError("nvidia-smi")):
            self.assertIsNone(vc._smi_free_gb())
        with mock.patch.object(vc.subprocess, "run", return_value=types.SimpleNamespace(stdout="No devices were found\n")):
            self.assertIsNone(vc._smi_free_gb())

    def test_a_full_card_seen_only_by_nvidia_smi_keeps_chatterbox_off_it(self):
        with mock.patch.object(vc, "_torch_free_gb", return_value=5.0), mock.patch.object(vc, "_smi_free_gb", return_value=1.3):
            device, why = vc.choose_device("cuda", ("nano",))
        self.assertEqual(device, "cpu")
        self.assertIn("only 1.3 GB", why)


class _Wave:
    """What a Chatterbox model returns, as far as the speech framework uses it."""

    def __init__(self, samples):
        self.samples = samples

    def squeeze(self, _):
        return self

    detach = cpu = lambda self: self

    def numpy(self):
        return self.samples


@contextlib.contextmanager
def _real_framework(tts_config):
    """The speech framework's own speech_synth.py, tts_common.py and both engine
    files, unchanged; only its settings reader (needs PyYAML) and the Chatterbox
    package itself are stand-ins. Yields every (engine, device, text, language)
    the stand-in models were asked to speak."""
    names = ("_paths", "settings", "speech_synth", "tts_common", "chatterbox_nano", "chatterbox_ml3")
    saved = {name: sys.modules.pop(name, None) for name in names}
    spoken = []

    def model_class(engine):
        class Model:
            sr, conds = RATE, object()

            def __init__(self, device):
                self.device = device

            @classmethod
            def from_pretrained(cls, device, **kwargs):
                return cls(device)

            def generate(self, text, language_id=None, **params):
                spoken.append((engine, self.device, text, language_id))
                return _Wave(0.5 * np.sin(np.arange(RATE) * 0.05, dtype=np.float32))
        return Model

    fakes = {name: types.ModuleType(name) for name in ("settings", "chatterbox", "chatterbox.tts_turbo", "chatterbox.mtl_tts")}
    fakes["settings"].load_settings = lambda: {"tts": tts_config}
    fakes["settings"].resolve_path = lambda path: path or None
    fakes["chatterbox.tts_turbo"].ChatterboxTurboTTS = model_class("nano")
    fakes["chatterbox.mtl_tts"].ChatterboxMultilingualTTS = model_class("multilingual")
    path_before = list(sys.path)
    try:
        with mock.patch.dict(sys.modules, fakes), contextlib.redirect_stdout(io.StringIO()):
            yield spoken
    finally:
        sys.path[:] = path_before
        for name in names:
            sys.modules.pop(name, None)
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module


# The languages as experimental/speech-framework/config.yaml ships them.
SHIPPED_LANGUAGES = {"en": {"engine": "nano", "voice": None},
                     "zh": {"engine": "multilingual", "voice": None, "exaggeration": 0.5, "cfg_weight": 0.5},
                     "ja": {"engine": "multilingual", "voice": None, "exaggeration": 0.5, "cfg_weight": 0.5},
                     "es": {"engine": "multilingual", "voice": None, "exaggeration": 0.5, "cfg_weight": 0.5}}
SHIPPED = {"device": "auto", "preload": True, "multilingual_t3_model": "v3", "languages": SHIPPED_LANGUAGES}
REPLIES = {"en": "Hello there, it is nice to meet you.", "es": "¡Hola! ¿Cómo estás hoy?", "zh": "你好，今天怎么样？"}


class WhichEngineSpeaksWhichLanguage(unittest.TestCase):
    """English: the nano engine. Spanish and Chinese: the multilingual engine, on the processor."""

    setUp, tearDown = WhereChatterboxRuns.setUp, WhereChatterboxRuns.tearDown

    def _voice(self, free=None, **settings):
        os.environ.update(settings)
        with mock.patch.object(vc, "gpu_free_gb", return_value=free):
            voice = vc.ChatterboxVoice()
        voice._to_pcm16 = _to_pcm16                 # the framework's own resampler needs PyTorch
        voice.load()
        return voice

    def test_the_settings_file_ships_this_routing(self):
        with open(os.path.join(vc.FRAMEWORK_DIR, "config.yaml"), encoding="utf-8") as settings:
            text = settings.read()
        found = dict(re.findall(r"^    (\w+): \{ engine: (\w+),", text, flags=re.MULTILINE))
        # On this branch English ships on the multilingual engine too; the other
        # tests keep SHIPPED_LANGUAGES (English on nano) to cover that routing.
        self.assertEqual(found, {language: "multilingual" for language in SHIPPED_LANGUAGES})

    def test_by_default_every_engine_is_on_the_processor(self):
        with _real_framework(dict(SHIPPED)) as spoken:
            voice = self._voice(free=24.0)
            for language, text in REPLIES.items():
                voice.synthesize(text, language)
        self.assertEqual(spoken, [("nano", "cpu", REPLIES["en"], None),
                                  ("multilingual", "cpu", REPLIES["es"], "es"),
                                  ("multilingual", "cpu", REPLIES["zh"], "zh")])

    def test_with_nano_on_the_card_only_english_uses_it(self):
        with _real_framework(dict(SHIPPED)) as spoken:
            voice = self._voice(free=4.7, SB01_CHATTERBOX_DEVICE="cuda", SB01_CHATTERBOX_GPU_ENGINES="nano")
            loaded = {name: engine.model.device for name, engine in voice._synth._engines.items()}
            for language in ("en", "es", "zh", "ja", "en"):
                voice.synthesize(REPLIES.get(language, "こんにちは、元気ですか。"), language)
        self.assertEqual(loaded, {"nano": "cuda", "multilingual": "cpu"})
        self.assertEqual([(engine, device, language) for engine, device, _, language in spoken],
                         [("nano", "cuda", None), ("multilingual", "cpu", "es"), ("multilingual", "cpu", "zh"),
                          ("multilingual", "cpu", "ja"), ("nano", "cuda", None)])
        self.assertIn("en is fast", voice.device_note)

    def test_without_enough_memory_english_is_on_the_processor_too(self):
        with _real_framework(dict(SHIPPED)) as spoken:
            voice = self._voice(free=3.0, SB01_CHATTERBOX_DEVICE="cuda", SB01_CHATTERBOX_GPU_ENGINES="nano")
            voice.synthesize(REPLIES["en"], "en")
        self.assertEqual(spoken, [("nano", "cpu", REPLIES["en"], None)])
        self.assertIn("so it runs on the processor", voice.device_note)

    def test_a_language_with_no_setting_of_its_own_never_gets_the_english_engine(self):
        with _real_framework(dict(SHIPPED)) as spoken:
            voice = self._voice(free=4.7, SB01_CHATTERBOX_DEVICE="cuda", SB01_CHATTERBOX_GPU_ENGINES="nano")
            voice.synthesize("Bonjour, comment allez-vous ?", "fr")
        self.assertEqual([(engine, device, language) for engine, device, _, language in spoken], [("multilingual", "cpu", "fr")])

    def test_the_program_sends_each_reply_to_the_engine_for_its_language(self):
        with _real_framework(dict(SHIPPED)) as spoken:
            loop, robot = _loop(engine=False)
            loop.voice = self._voice(free=4.7, SB01_CHATTERBOX_DEVICE="cuda", SB01_CHATTERBOX_GPU_ENGINES="nano")
            base.tts_calls.clear()
            _speak(loop, REPLIES["en"], "en")
            _speak(loop, REPLIES["es"], "es")
            _speak(loop, "Muy bien, ahora mira el diagrama.", "es")        # Spanish with no accented letter
            _speak(loop, REPLIES["zh"], "zh")
        self.assertEqual([(engine, device, language) for engine, device, _, language in spoken],
                         [("nano", "cuda", None), ("multilingual", "cpu", "es"), ("multilingual", "cpu", "es"),
                          ("multilingual", "cpu", "zh")])
        self.assertEqual(base.tts_calls, [])                                # Edge TTS was not needed
        self.assertEqual(robot.events.count("audio"), 4)

    def test_edge_tts_takes_over_in_every_language(self):
        expected = {"en": script.VOICE_EN, "es": script.VOICE_ES, "zh": script.VOICE_ZH}
        for language, text in REPLIES.items():
            with self.subTest(language=language):
                engine = _Engine()
                loop, robot = _loop(engine)
                engine.fail = RuntimeError("CUDA out of memory")
                base.tts_calls.clear()
                printed = _speak(loop, text, language)
                self.assertIn("using Edge TTS for it", printed)
                self.assertEqual([voice for voice, _ in base.tts_calls], [expected[language]])
                self.assertEqual(b"".join(robot.played), base._Segment.raw_data)       # the whole reply, once
                self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])


class NoMemoryKeptPerReply(unittest.TestCase):
    """Every sentence is synthesized with gradient bookkeeping off (it otherwise keeps memory for good)."""

    def test_each_sentence_is_synthesized_inside_it_and_nothing_else_is(self):
        engine = _Engine()
        inside, seen = [], []

        @contextlib.contextmanager
        def off():
            inside.append(True)
            try:
                yield
            finally:
                inside.pop()

        real = engine.synthesize
        engine.synthesize = lambda text, language: seen.append(bool(inside)) or real(text, language)
        with mock.patch.object(vc.ChatterboxVoice, "_no_gradients", staticmethod(off)):
            _voice(engine).synthesize("First sentence of the reply. Second sentence of the reply.", "en", split=True)
        self.assertEqual(seen, [True, True])
        self.assertEqual(inside, [])

    def test_a_failure_inside_it_still_reaches_edge_tts(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        engine.fail = RuntimeError("CUDA out of memory")
        base.tts_calls.clear()
        _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(len(base.tts_calls), 1)

    def test_it_works_where_pytorch_is_not_installed(self):
        with mock.patch.dict(sys.modules, {"torch": None}):
            with vc.ChatterboxVoice._no_gradients():
                pass


class OriginalVolumeIsKept(unittest.TestCase):
    """Chatterbox audio is played at the amplitude the model generated: gain 1.0 in every language,
    no matching to Edge TTS, no limiter."""

    @staticmethod
    def _tone(amplitude, seconds=1.0):
        return (amplitude * np.sin(np.arange(round(seconds * RATE)) * 0.05)).astype(np.float32)

    def _made(self, wave, language):
        engine = _Engine()
        engine.bad = (wave, RATE)
        return _voice(engine).synthesize(REPLIES[language], language)

    def test_the_gain_is_exactly_one_in_english_spanish_and_chinese(self):
        for language in ("en", "es", "zh"):
            for amplitude in (0.02, 0.09, 0.5, 0.97):         # very quiet to nearly full scale
                with self.subTest(language=language, amplitude=amplitude):
                    wave = self._tone(amplitude, 2.0)
                    speech = self._made(wave, language)
                    self.assertEqual(speech.pcm, _to_pcm16(wave, RATE, 16000))        # byte for byte: nothing scaled
                    self.assertEqual(speech.gesture_pcm, _to_pcm16(wave, RATE, 24000))

    def test_a_quiet_reply_stays_quiet_and_a_loud_one_stays_loud(self):
        quiet = np.frombuffer(self._made(self._tone(0.05), "en").pcm, dtype="<i2")
        loud = np.frombuffer(self._made(self._tone(0.97), "en").pcm, dtype="<i2")
        self.assertAlmostEqual(int(np.abs(quiet).max()) / 32767, 0.05, places=3)
        self.assertAlmostEqual(int(np.abs(loud).max()) / 32767, 0.97, places=3)

    def test_a_sample_at_full_scale_is_left_as_it_is(self):
        wave = self._tone(0.6)
        wave[1000] = 1.0                                     # as the multilingual engine sometimes produces
        robot = np.frombuffer(self._made(wave, "es").pcm, dtype="<i2")
        self.assertEqual(int(np.abs(robot).max()), 32767)    # not pulled down by a limiter
        self.assertEqual(self._made(wave, "es").gesture_pcm, _to_pcm16(wave, RATE, 24000))

    def test_the_gesture_copy_is_the_same_unchanged_waveform(self):
        wave = self._tone(0.09, 2.0)
        speech = self._made(wave, "en")
        gesture = np.frombuffer(speech.gesture_pcm, dtype="<i2").astype(np.float32) / 32767
        self.assertEqual(len(gesture), len(wave))
        self.assertLess(float(np.abs(gesture - wave).max()), 1.0 / 32767)               # only 16-bit rounding

    def test_the_length_of_a_reply_spoken_in_one_piece_is_not_changed(self):
        for language in ("en", "es", "zh"):
            speech = self._made(self._tone(0.3, 2.5), language)
            self.assertEqual(len(speech.pcm), 2 * round(2.5 * 16000))
            self.assertEqual(len(speech.gesture_pcm), 2 * round(2.5 * RATE))
            self.assertAlmostEqual(speech.seconds, 2.5, places=3)

    def test_sentences_joined_together_are_not_scaled_either(self):
        engine = _Engine()
        real = engine.synthesize

        def quiet(text, language):                           # the first sentence twice as loud as the second
            wave, rate = real(text, language)
            return wave * (0.2 if text.startswith("First") else 0.1), rate

        engine.synthesize = quiet
        speech = _voice(engine).synthesize("First sentence of the reply. Second sentence of the reply.", "en", split=True)
        samples = np.abs(np.frombuffer(speech.gesture_pcm, dtype="<i2").astype(np.float32)) / 32767
        half = len(samples) // 2
        self.assertAlmostEqual(float(samples[:half].max()), 0.5 * 0.2, places=3)        # the stand-in tone is 0.5
        self.assertAlmostEqual(float(samples[half:].max()), 0.5 * 0.1, places=3)

    def test_there_is_no_volume_matching_code_or_setting(self):
        for name in ("_levelled", "_speech_level", "_pcm_peak", "SPEECH_LEVEL", "PEAK_CEILING", "MAX_GAIN"):
            self.assertFalse(hasattr(vc.ChatterboxVoice, name) or hasattr(vc, name), name)
        with open(vc.__file__, encoding="utf-8") as source:
            code = source.read()
        self.assertNotIn("SB01_CHATTERBOX_VOLUME", code)
        self.assertEqual(code.count("* np.float32("), 2)     # only the clip guard: its probe, and the one reply it turns down

    def test_edge_tts_audio_goes_through_no_chatterbox_code(self):
        loop, robot = _loop(engine=False)                    # no Chatterbox voice at all
        base.tts_calls.clear()
        with mock.patch.object(vc.ChatterboxVoice, "synthesize", side_effect=AssertionError("Edge TTS must not use Chatterbox")), \
                mock.patch.object(vc.ChatterboxVoice, "_tidied", side_effect=AssertionError("Edge TTS must not use Chatterbox")):
            _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)                # exactly Edge TTS's own audio
        self.assertEqual(len(base.tts_calls), 1)

    def test_edge_tts_fallback_audio_is_not_touched_either(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        engine.fail = RuntimeError("boom")
        with mock.patch.object(vc.ChatterboxVoice, "_tidied", side_effect=AssertionError("Edge TTS must not use Chatterbox")):
            _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)


class WarmToneWhenAskedFor(unittest.TestCase):
    """SB01_CHATTERBOX_TONE=warm: warmer and louder, never clipping, same length. Unset: nothing changes."""

    def setUp(self):
        self._saved = os.environ.pop("SB01_CHATTERBOX_TONE", None)

    def tearDown(self):
        os.environ.pop("SB01_CHATTERBOX_TONE", None)
        if self._saved is not None:
            os.environ["SB01_CHATTERBOX_TONE"] = self._saved

    @staticmethod
    def _speechlike(seconds=2.0, peak=0.9):
        clock = np.arange(round(seconds * RATE)) / RATE
        wave = (np.sin(2 * np.pi * 180 * clock) + 0.5 * np.sin(2 * np.pi * 3000 * clock)) * (0.25 + 0.75 * np.abs(np.sin(2 * np.pi * 2.0 * clock)))
        return (peak * wave / np.abs(wave).max()).astype(np.float32)

    def _made(self, wave, **kwargs):
        engine = _Engine()
        engine.bad = (wave, RATE)
        return _voice(engine).synthesize(REPLIES["en"], "en", **kwargs)

    @staticmethod
    def _band(samples, low, high):
        spectrum = np.abs(np.fft.rfft(samples)) ** 2
        frequency = np.fft.rfftfreq(len(samples), 1 / RATE)
        return float(spectrum[(frequency >= low) & (frequency < high)].sum())

    def test_without_the_setting_the_audio_is_untouched(self):
        wave = self._speechlike()
        self.assertEqual(self._made(wave).pcm, _to_pcm16(wave, RATE, 16000))

    def test_warm_is_louder_and_never_above_its_ceiling(self):
        wave = self._speechlike()
        os.environ["SB01_CHATTERBOX_TONE"] = "warm"
        speech = self._made(wave)
        plain = np.frombuffer(_to_pcm16(wave, RATE, 16000), dtype="<i2").astype(np.float64)
        warm = np.frombuffer(speech.pcm, dtype="<i2").astype(np.float64)
        self.assertEqual(len(warm), len(plain))                                     # same length: timing is kept
        self.assertGreater(20 * np.log10(np.sqrt(np.mean(warm ** 2)) / np.sqrt(np.mean(plain ** 2))), 2.0)
        self.assertLessEqual(np.abs(warm).max() / 32767, vc.WARM_CEILING + 0.005)
        self.assertEqual(speech.scale, 1.0)                                         # the clip guard had nothing to do

    def test_warm_has_more_low_and_less_high(self):
        wave = self._speechlike(peak=0.2)                                           # quiet enough that the ceiling is not involved
        shaped = vc.warm_tone(wave, RATE)
        low = 10 * np.log10(self._band(shaped, 100, 400) / self._band(wave, 100, 400))
        high = 10 * np.log10(self._band(shaped, 2500, 3500) / self._band(wave, 2500, 3500))
        self.assertGreater(low, high + 2.0)                                         # more body, less edge
        self.assertAlmostEqual(high, vc.WARM_LOUDER_DB + vc.WARM_HIGH_DB * 0.26, delta=0.5)

    def test_both_copies_carry_the_same_tone(self):
        wave = self._speechlike()
        os.environ["SB01_CHATTERBOX_TONE"] = "warm"
        speech = self._made(wave)
        shaped = vc.warm_tone(wave, RATE)
        self.assertEqual(speech.pcm, _to_pcm16(shaped, RATE, 16000))
        self.assertEqual(speech.gesture_pcm, _to_pcm16(shaped, RATE, 24000))

    def _loaded_by_the_program(self):
        loop, _ = _loop(engine=False)
        out = io.StringIO()
        with mock.patch.object(script, "VOICE_BACKEND", "chatterbox"),                 mock.patch.object(script, "ChatterboxVoice", lambda: vc.ChatterboxVoice(synth=_Engine(), to_pcm16=_to_pcm16, device="auto")),                 contextlib.redirect_stdout(out):
            loop._load_voice()
        return loop, out.getvalue()

    def test_the_conversation_program_uses_the_warm_tone_without_being_asked(self):
        loop, printed = self._loaded_by_the_program()
        self.assertEqual(loop.voice.tone, "warm")
        self.assertIn("[voice] tone: warm", printed)

    def test_plain_gives_the_voice_as_the_model_makes_it(self):
        os.environ["SB01_CHATTERBOX_TONE"] = "plain"
        loop, printed = self._loaded_by_the_program()
        self.assertEqual(loop.voice.tone, "")
        self.assertIn("[voice] tone: plain", printed)
        wave = self._speechlike()
        loop.voice._synth.bad = (wave, RATE)
        self.assertEqual(loop.voice.synthesize(REPLIES["en"], "en").pcm, _to_pcm16(wave, RATE, 16000))

    def test_a_tone_that_does_not_exist_is_refused_in_plain_words(self):
        os.environ["SB01_CHATTERBOX_TONE"] = "sparkly"
        with self.assertRaises(vc.VoiceUnavailable) as refused:
            _voice()
        self.assertIn("SB01_CHATTERBOX_TONE", str(refused.exception))


class ClippingIsPrevented(unittest.TestCase):
    """Only a reply that would exceed full scale is turned down, and only just enough.
    Every other reply is untouched: scale exactly 1.0."""

    @staticmethod
    def _tone(amplitude, seconds=1.0):
        return (amplitude * np.sin(np.arange(round(seconds * RATE)) * 0.05)).astype(np.float32)

    def _made(self, wave, language="en", to_pcm16=_to_pcm16, **kwargs):
        engine = _Engine()
        engine.bad = (wave, RATE)
        return vc.ChatterboxVoice(synth=engine, to_pcm16=to_pcm16).synthesize(REPLIES[language], language, **kwargs)

    @staticmethod
    def _peak(pcm):
        return int(np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.int32)).max())

    def test_audio_within_range_has_scale_exactly_one_and_is_unchanged_byte_for_byte(self):
        for language in ("en", "es", "zh"):
            for amplitude in (0.02, 0.5, 0.97, 0.9999, 1.0):          # up to and including exactly full scale
                with self.subTest(language=language, amplitude=amplitude):
                    wave = self._tone(amplitude, 2.0)
                    wave[100] = amplitude                                  # the peak is really there
                    speech = self._made(wave, language)
                    self.assertEqual(speech.scale, 1.0)
                    self.assertIsInstance(speech.scale, float)
                    self.assertEqual(speech.pcm, _to_pcm16(wave, RATE, 16000))
                    self.assertEqual(speech.gesture_pcm, _to_pcm16(wave, RATE, 24000))

    def test_audio_over_range_gets_only_the_smallest_reduction_that_fits(self):
        for over in (1.02, 1.3, 2.5):
            with self.subTest(over=over):
                wave = self._tone(0.6, 2.0)
                wave[4800] = over                                         # one sample above full scale
                speech = self._made(wave)
                self.assertLess(speech.scale, 1.0 / over)                 # enough to fit
                self.assertGreater(speech.scale, 0.997 / over)            # and no more: within 0.03 dB of the least possible
                self.assertAlmostEqual(speech.scale, vc.SAFE_PEAK / over, places=3)

    def test_after_it_nothing_clips_in_either_copy(self):
        wave = self._tone(0.6, 2.0)
        wave[4800:4810] = 1.4
        wave[9600] = -1.6
        speech = self._made(wave)
        for pcm in (speech.pcm, speech.gesture_pcm):
            self.assertLess(self._peak(pcm), 32767)
            self.assertGreater(self._peak(pcm), 32767 * 0.99)             # just under full scale, not far under
        self.assertGreater(int(np.frombuffer(speech.pcm, dtype="<i2").min()), -32768)

    def test_the_robot_audio_and_the_gesture_copy_get_the_same_scale(self):
        wave = self._tone(0.6, 2.0)
        wave[4800] = 1.3
        speech = self._made(wave)
        turned_down = wave * np.float32(speech.scale)
        self.assertEqual(speech.pcm, _to_pcm16(turned_down, RATE, 16000))
        self.assertEqual(speech.gesture_pcm, _to_pcm16(turned_down, RATE, 24000))

    def test_a_peak_that_only_conversion_pushes_over_is_caught_too(self):
        def overshooting(wave, rate, target):                # converting to 16 kHz raises peaks by 8%; 24 kHz does not
            return _to_pcm16(np.clip(wave * (1.08 if target == 16000 else 1.0), -1, 1), rate, target)

        wave = self._tone(0.97, 2.0)                         # within range as generated
        speech = self._made(wave, to_pcm16=overshooting)
        self.assertAlmostEqual(speech.scale, vc.SAFE_PEAK / (0.97 * 1.08), places=3)
        self.assertLess(self._peak(speech.pcm), 32767)
        self.assertEqual(speech.gesture_pcm, _to_pcm16(wave * np.float32(speech.scale), RATE, 24000))   # the same scale

    def test_only_the_amplitude_changes_not_the_length_or_the_word_times(self):
        wave = self._tone(0.6, 2.0)
        normal = self._made(wave)
        wave = wave.copy()
        wave[4800] = 1.3
        guarded = self._made(wave)
        self.assertEqual((len(guarded.pcm), len(guarded.gesture_pcm), guarded.seconds), (len(normal.pcm), len(normal.gesture_pcm), normal.seconds))
        self.assertEqual(guarded.words, normal.words)

    def test_joined_sentences_get_one_scale_for_the_whole_reply(self):
        engine = _Engine()
        real = engine.synthesize

        def one_too_loud(text, language):
            wave, rate = real(text, language)
            return wave * (2.4 if text.startswith("Second") else 1.0), rate          # 0.5 and 1.2

        engine.synthesize = one_too_loud
        speech = _voice(engine).synthesize("First sentence of the reply. Second sentence of the reply.", "en", split=True)
        samples = np.abs(np.frombuffer(speech.gesture_pcm, dtype="<i2").astype(np.float32))
        half = len(samples) // 2
        self.assertAlmostEqual(speech.scale, vc.SAFE_PEAK / 1.2, places=3)
        self.assertAlmostEqual(float(samples[half:].max() / samples[:half].max()), 2.4, places=1)   # their balance is kept
        self.assertLess(float(samples.max()), 32767)

    def test_without_gesture_audio_only_the_robot_audio_is_looked_at(self):
        wave = self._tone(0.6)
        wave[4800] = 1.3
        speech = self._made(wave, gesture_audio=False)
        self.assertIsNone(speech.gesture_pcm)
        self.assertLess(self._peak(speech.pcm), 32767)

    def test_the_program_says_when_it_happened_and_by_how_much(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        wave = self._tone(0.6, 2.0)
        wave[4800] = 1.3
        engine.bad = (wave, RATE)
        printed = _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertIn("went above full scale: turned down by 2.30 dB so it does not clip", printed)
        self.assertLess(self._peak(b"".join(robot.played)), 32767)
        engine.bad = (self._tone(0.6, 2.0), RATE)
        self.assertNotIn("turned down", _speak(loop, "Hello there, nice to meet you.", "en"))

    def test_edge_tts_never_passes_through_it(self):
        guard = mock.patch.object(vc.ChatterboxVoice, "_clip_guard", side_effect=AssertionError("Edge TTS must not be scaled"))
        loop, robot = _loop(engine=False)                    # Edge TTS as the only voice
        with guard:
            _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)
        engine = _Engine()                                   # and Edge TTS as the fallback
        loop, robot = _loop(engine)
        engine.fail = RuntimeError("boom")
        with guard:
            _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)


class VoiceSettings(unittest.TestCase):
    """A wrong voice setting is found at startup, with a reason, and Edge TTS carries on."""

    def _refused(self, config):
        with _framework(config):
            with self.assertRaises(vc.VoiceUnavailable) as raised:
                vc.ChatterboxVoice()
        return str(raised.exception)

    def test_a_voice_clip_that_is_not_there(self):
        config = dict(GOOD_CONFIG, languages={"en": {"engine": "nano", "voice": "../voice-refs/en_yotie.wav"}})
        reason = self._refused(config)
        self.assertIn("the voice clip for 'en' was not found: en_yotie.wav", reason)
        self.assertIn("tts.languages.en.voice", reason)

    def test_an_engine_name_that_does_not_exist(self):
        reason = self._refused(dict(GOOD_CONFIG, languages={"en": {"engine": "turbo-max"}}))
        self.assertIn("tts.languages.en.engine is 'turbo-max'", reason)
        self.assertIn("nano, multilingual", reason)

    def test_a_voice_setting_that_is_not_a_path(self):
        import ntpath                                        # a real path function, so a number makes it fail as it would for real
        config = dict(GOOD_CONFIG, languages={"en": {"engine": "nano", "voice": 123}})
        with _framework(config):
            sys.modules["settings"].resolve_path = lambda path: ntpath.join("voices", path) if path else None
            with self.assertRaises(vc.VoiceUnavailable) as raised:
                vc.ChatterboxVoice()
        self.assertIn("tts.languages.en.voice in the speech framework's config is not a file path (123)", str(raised.exception))

    def test_no_languages_at_all(self):
        self.assertIn("no tts.languages", self._refused({"device": "cpu"}))

    def test_a_voice_clip_that_exists_is_accepted(self):
        with _framework(dict(GOOD_CONFIG, languages={"en": {"engine": "nano", "voice": "settings.py"}})) as built:
            vc.ChatterboxVoice()                                           # any existing file stands in for a clip
        self.assertEqual(len(built), 1)

    def test_the_program_says_so_and_speaks_with_edge(self):
        loop, robot = _loop(engine=False)
        out = io.StringIO()
        config = dict(GOOD_CONFIG, languages={"en": {"engine": "nano", "voice": "missing.wav"}})
        with _framework(config), mock.patch.object(script, "VOICE_BACKEND", "chatterbox"), contextlib.redirect_stdout(out):
            loop._load_voice()
        self.assertIsNone(loop.voice)
        self.assertIn("Chatterbox is NOT in use: the voice clip for 'en' was not found", out.getvalue())
        base.tts_calls.clear()
        _speak(loop, "Hello there, nice to meet you.")
        self.assertEqual(len(base.tts_calls), 1)
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])


class LettingGoOfMemory(unittest.TestCase):
    def test_a_failed_load_lets_go_of_what_it_had_loaded(self):
        engine = _Engine()
        engine.fail = RuntimeError("CUDA out of memory")
        voice = _voice(engine)
        models = list(engine._engines.values())
        with mock.patch.object(vc.ChatterboxVoice, "_free_gpu") as freed:
            with self.assertRaises(vc.VoiceUnavailable):
                voice.load()
        self.assertEqual(engine._engines, {})
        self.assertTrue(all(model.model is None for model in models))
        self.assertTrue(freed.called)
        self.assertFalse(voice.loaded)

    def test_out_of_memory_while_speaking_clears_the_card_and_the_reply_is_spoken_once_by_edge(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        engine.fail = RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
        base.tts_calls.clear()
        with mock.patch.object(vc.ChatterboxVoice, "_free_gpu") as freed:
            printed = _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertTrue(freed.called)
        self.assertIn("ran out of memory", printed)
        self.assertEqual(len(base.tts_calls), 1)
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])     # gestures carried on

    def test_after_three_failures_the_models_are_let_go_and_never_tried_again(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        engine.fail = RuntimeError("boom")
        for i in range(3):
            _speak(loop, f"Reply number {i} for you.", "en")
        self.assertIsNone(loop.voice)
        self.assertEqual(engine._engines, {})
        tried = len(engine.said)
        for i in range(5):
            _speak(loop, f"Later reply number {i}.", "en")
        self.assertEqual(len(engine.said), tried)

    def test_close_is_safe_to_call_twice_and_before_loading(self):
        voice = _voice()
        voice.close()
        voice.load()
        voice.close()
        voice.close()
        self.assertFalse(voice.loaded)


class NoHalfReplies(unittest.TestCase):
    """A reply is either wholly Chatterbox or wholly Edge TTS; never part of one and then the other."""

    REPLY = "That is a very good answer today. Now look at the diagram on the board. Does anyone have a question?"

    def test_a_failure_on_a_later_sentence_plays_nothing_from_chatterbox(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        engine.fail, engine.fail_on = RuntimeError("boom"), "Now look at the diagram on the board."
        base.tts_calls.clear()
        with mock.patch.object(script, "GESTURE_CUES", True):
            printed = _speak(loop, self.REPLY, "en")
        self.assertEqual(len(engine.said), 2)                              # the first sentence had been made
        self.assertIn("using Edge TTS for it", printed)
        self.assertEqual(base.tts_calls[0][1], self.REPLY)                 # Edge said the whole reply
        self.assertEqual(len(base.tts_calls), 1)
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)   # and only Edge's audio was played
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])

    def test_audio_is_whole_before_anything_is_played_or_moved(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        order = []
        real = engine.synthesize
        engine.synthesize = lambda text, language: (order.append("made"), real(text, language))[1]
        robot.PlayStream = lambda app, stream, pcm: order.append("played")
        robot.start = lambda pcm, **kw: (order.append("gesture"), True)[1]
        with mock.patch.object(script, "GESTURE_CUES", True):
            _speak(loop, self.REPLY, "en")
        self.assertEqual(order[:5], ["made", "made", "made", "gesture", "played"])
        self.assertEqual(set(order[5:]), {"played"})                       # the rest is the same audio, in 3 s chunks


class JoinedSentences(unittest.TestCase):
    """Sentence-by-sentence audio: nothing repeated or lost, no clicks, no long gaps, right format."""

    TEXT = ("That is a very good answer today. Now look at the diagram on the board. "
            "The gap is very small, but the effect is huge. Does anyone have a question?")

    def _loud_runs(self, pcm, rate=vc.PLAYBACK_RATE):
        samples = np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.float32))
        loud = samples > 0.02 * samples.max()
        edges = np.flatnonzero(np.diff(np.concatenate(([0], loud.astype(np.int8), [0]))))
        runs = [(a / rate, b / rate) for a, b in zip(edges[::2], edges[1::2])]
        merged = [runs[0]]
        for a, b in runs[1:]:                           # zero crossings inside a tone are not gaps
            if a - merged[-1][1] < 0.05:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        return merged

    def test_every_sentence_is_there_once_and_in_order(self):
        speech = _voice().synthesize(self.TEXT, "en", split=True)
        pieces = vc.sentences(self.TEXT)
        runs = self._loud_runs(speech.pcm)
        self.assertEqual(speech.pieces, 4)
        self.assertEqual(len(runs), 4)                                     # four stretches of speech, not three or five
        for (start, end), piece in zip(runs, pieces):
            self.assertAlmostEqual(end - start, len(piece) * SECONDS_PER_LETTER, delta=0.03)

    def test_gaps_between_sentences_are_short_and_never_missing(self):
        runs = self._loud_runs(_voice().synthesize(self.TEXT, "en", split=True).pcm)
        gaps = [later[0] - earlier[1] for earlier, later in zip(runs, runs[1:])]
        longest = vc.TAIL_SILENCE_SECONDS + vc.SENTENCE_GAP_SECONDS + vc.LEAD_SILENCE_SECONDS + 0.02
        for gap in gaps:
            self.assertGreater(gap, vc.SENTENCE_GAP_SECONDS - 0.01)
            self.assertLessEqual(gap, longest)

    def test_long_silence_from_the_engine_is_cut_back_but_speech_is_not(self):
        wave = np.concatenate((np.zeros(RATE), 0.5 * np.ones(RATE // 2, dtype=np.float32), np.zeros(2 * RATE))).astype(np.float32)
        tidy = vc.ChatterboxVoice._tidied(wave, RATE)
        first, last = vc.ChatterboxVoice._spoken_span(tidy)
        self.assertAlmostEqual(first / RATE, vc.LEAD_SILENCE_SECONDS, delta=0.01)
        self.assertAlmostEqual((len(tidy) - 1 - last) / RATE, vc.TAIL_SILENCE_SECONDS, delta=0.01)
        self.assertAlmostEqual((last - first) / RATE, 0.5, delta=0.005)    # all of the speech is still there

    def test_each_piece_starts_and_ends_at_zero_so_a_join_cannot_click(self):
        loud = 0.8 * np.ones(RATE, dtype=np.float32)                       # audio that starts and stops abruptly
        tidy = vc.ChatterboxVoice._tidied(loud, RATE)
        self.assertEqual(tidy[0], 0.0)
        self.assertEqual(tidy[-1], 0.0)
        self.assertLess(np.abs(np.diff(tidy)).max(), 0.8 / (vc.FADE_SECONDS * RATE) + 1e-3)   # no jump anywhere

    def test_a_reply_spoken_in_one_piece_is_not_altered(self):
        engine = _Engine()
        text = "Hello there, nice to meet you."
        speech = _voice(engine).synthesize(text, "en")
        wave, rate = _Engine().synthesize(text, "en")
        self.assertEqual(speech.pcm, _to_pcm16(wave, rate, vc.PLAYBACK_RATE))
        self.assertEqual(speech.pieces, 1)

    def test_the_audio_is_what_the_robot_speaker_takes(self):
        speech = _voice().synthesize(self.TEXT, "en", split=True)
        samples = np.frombuffer(speech.pcm, dtype="<i2")                   # 16-bit little-endian, one channel
        self.assertEqual(len(speech.pcm) % 2, 0)
        self.assertAlmostEqual(len(samples) / vc.PLAYBACK_RATE, speech.seconds, delta=0.01)       # 16 kHz
        self.assertAlmostEqual(len(speech.gesture_pcm) / 2 / vc.GESTURE_RATE, speech.seconds, delta=0.01)
        self.assertLess(np.abs(samples).max(), 32767)                      # not clipped
        self.assertGreater(np.abs(samples).max(), 3000)                    # and not faint

    def test_audio_louder_than_full_scale_is_turned_down_not_clipped_or_wrapped(self):
        engine = _Engine()
        engine.bad = (1.7 * np.sin(np.arange(RATE) * 0.05).astype(np.float32), RATE)
        samples = np.frombuffer(_voice(engine).synthesize("Hello there, nice to meet you.", "en").pcm, dtype="<i2")
        self.assertLess(int(np.abs(samples.astype(np.int32)).max()), 32767)
        self.assertGreater(int(np.abs(samples.astype(np.int32)).max()), 32767 * 0.99)
        self.assertLess(np.abs(np.diff(samples.astype(np.int32))).max(), 6000)        # no wrap-around jumps

    def test_a_long_reply_with_gestures_keeps_each_gesture_in_its_own_sentence(self):
        loop, robot = _loop()
        reply = ("That is a very good answer today. Now look at the [point] diagram on the board. "
                 "The gap is very [small] small, but the effect is [big] huge. [ask] Does anyone have a question?")
        with mock.patch.object(script, "GESTURE_CUES", True):
            _speak(loop, reply, "en")
        spoken, _ = script.gesture_cues.split(reply)
        runs = self._loud_runs(b"".join(robot.played))
        cues = {cue["name"]: cue["time"] for cue in robot.starts[0][1]["cues"]}
        self.assertEqual(list(cues), ["point", "small", "big", "ask"])
        self.assertTrue(runs[1][0] < cues["point"] < runs[1][1])
        self.assertTrue(runs[2][0] < cues["small"] < cues["big"] < runs[2][1])
        self.assertTrue(runs[3][0] - 0.01 <= cues["ask"] < runs[3][1])
        self.assertEqual(robot.events[:3], ["gesture-start", "gesture-begin", "audio"])
        self.assertEqual(set(robot.events[3:]), {"audio"})                 # one gesture request; audio sent in chunks
        self.assertEqual(len(vc.sentences(spoken)), 4)


class SpeakingWithChatterbox(unittest.TestCase):
    def test_the_robot_plays_chatterbox_audio_and_edge_is_not_used(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        base.tts_calls.clear()
        printed = _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(printed, "")
        self.assertEqual(base.tts_calls, [])
        self.assertEqual(engine.said, [("Hello there, nice to meet you.", "en")])
        expected = loop.voice.synthesize("Hello there, nice to meet you.", "en")
        self.assertEqual(b"".join(robot.played), expected.pcm)
        self.assertEqual(len(expected.pcm) % 2, 0)

    def test_the_gesture_model_gets_the_same_speech_at_its_own_sample_rate(self):
        loop, robot = _loop()
        _speak(loop, "Hello there, nice to meet you.", "en")
        gesture_pcm, kwargs = robot.starts[0]
        self.assertEqual(kwargs, {})                                      # asked exactly as with Edge TTS
        self.assertAlmostEqual(len(gesture_pcm) / len(b"".join(robot.played)), 24000 / 16000, places=2)

    def test_audio_is_ready_before_the_gesture_and_the_gesture_begins_with_the_audio(self):
        loop, robot = _loop()
        _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])
        self.assertFalse(loop.speaking.is_set())

    def test_two_replies_never_overlap(self):
        loop, robot = _loop()
        _speak(loop, "This is the first reply to you.", "en")
        _speak(loop, "And this is the second reply.", "en")
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 2)

    def test_without_gestures_no_gesture_audio_is_made_and_speech_still_plays(self):
        loop, robot = _loop(gestures=False)
        with mock.patch.object(loop.voice, "synthesize", wraps=loop.voice.synthesize) as made:
            _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertFalse(made.call_args.kwargs["gesture_audio"])
        self.assertEqual(robot.events, ["audio"])

    def test_the_models_are_loaded_once_not_per_sentence(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        for text in ("First thing to say to you.", "Second thing to say.", "Third thing to say."):
            _speak(loop, text, "en")
            loop.voice.load()
        self.assertEqual(engine.preloads, 1)
        self.assertEqual(len(engine.said), 3)

    def test_each_language_goes_to_the_matching_voice(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        _speak(loop, "Hello there, nice to meet you.", "en")
        _speak(loop, "¡Hola! ¿Cómo estás hoy?", "es")
        _speak(loop, "Claro, con mucho gusto te ayudo", "es")                # no accents: the language decides
        _speak(loop, "你好，很高兴见到你。", "zh")
        _speak(loop, "Goodbye! It was great talking with you.", None)        # fixed English phrase
        self.assertEqual([language for _, language in engine.said], ["en", "es", "es", "zh", "en"])

    def test_the_name_and_campus_wording_reach_chatterbox_as_they_reach_edge(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        _speak(loop, "Welcome to CSUSB, I am Yotie.", "en")
        # the campus wording as for Edge; the name respelled so the English voice says "yoh-dee"
        self.assertEqual(engine.said[0][0], "Welcome to Cal State San Bernardino, I am Yohdee.")


class FallingBackToEdge(unittest.TestCase):
    def test_one_failed_reply_is_spoken_with_edge_and_chatterbox_stays_on(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        engine.fail = RuntimeError("CUDA out of memory")
        base.tts_calls.clear()
        printed = _speak(loop, "Hello there, nice to meet you.", "en")
        self.assertIn("Chatterbox could not say this", printed)
        self.assertIn("using Edge TTS for it", printed)
        self.assertEqual(len(base.tts_calls), 1)
        self.assertEqual(b"".join(robot.played), base._Segment.raw_data)   # the reply was still spoken, once
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"])
        self.assertIsNotNone(loop.voice)
        engine.fail = None
        robot.played.clear()
        _speak(loop, "Now it works again, thank you.", "en")
        self.assertEqual(len(base.tts_calls), 1)                           # back on Chatterbox
        self.assertEqual(loop._voice_failures, 0)

    def test_three_failures_in_a_row_leave_it_off_for_the_session(self):
        engine = _Engine()
        loop, _ = _loop(engine)
        engine.fail = RuntimeError("boom")
        printed = "".join(_speak(loop, f"Reply number {i} for you.", "en") for i in range(3))
        self.assertIn("failed 3 times in a row", printed)
        self.assertIsNone(loop.voice)
        said = len(engine.said)
        _speak(loop, "One more reply for you.", "en")
        self.assertEqual(len(engine.said), said)                           # not tried again

    def test_unusable_audio_is_refused_with_a_reason(self):
        tone = np.full(RATE, 0.3, dtype=np.float32)
        cases = {
            "no audio": (np.zeros(0, dtype=np.float32), RATE),
            "damaged audio": (np.array([0.1, np.nan, 0.2], dtype=np.float32), RATE),
            "silence": (np.zeros(RATE, dtype=np.float32), RATE),
            "impossible sample rate": (tone, 0),
            "which cannot be right": (np.full(RATE * 61, 0.3, dtype=np.float32), RATE),
            "not audio": ("oops", RATE),
        }
        for reason, bad in cases.items():
            with self.subTest(reason=reason):
                engine = _Engine()
                engine.bad = bad
                loop, robot = _loop(engine)
                base.tts_calls.clear()
                printed = _speak(loop, "Hello there, nice to meet you.", "en")
                self.assertIn(reason, printed)
                self.assertEqual(len(base.tts_calls), 1)                   # Edge spoke it
                self.assertEqual(b"".join(robot.played), base._Segment.raw_data)

    def test_nothing_to_say_is_refused(self):
        with self.assertRaises(vc.VoiceUnavailable):
            _voice().synthesize("   ", "en")


class TeachingGesturesWithChatterbox(unittest.TestCase):
    REPLY = "That is a very good answer today. Now look at the [point] diagram on the board."

    def test_sentences_are_measured_and_the_gesture_lands_inside_its_sentence(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        with mock.patch.object(script, "GESTURE_CUES", True):
            printed = _speak(loop, self.REPLY, "en")
        first, second = "That is a very good answer today.", "Now look at the diagram on the board."
        self.assertEqual([text for text, _ in engine.said], [first, second])       # one by one, marks removed
        _, kwargs = robot.starts[0]
        self.assertEqual([cue["name"] for cue in kwargs["cues"]], ["point"])
        first_len = 2 * EDGE + len(first) * SECONDS_PER_LETTER
        second_start = first_len + vc.SENTENCE_GAP_SECONDS
        second_end = second_start + 2 * EDGE + len(second) * SECONDS_PER_LETTER
        when = kwargs["cues"][0]["time"]
        self.assertGreater(when, second_start)
        self.assertLess(when, second_end)
        expected = second_start + EDGE + len(second) * SECONDS_PER_LETTER * second.index("diagram") / len(second)
        self.assertAlmostEqual(when, expected, delta=0.05)
        self.assertIn("placed by estimate", printed)                              # it says so
        self.assertIn("no word times", printed)

    def test_the_estimate_notice_is_printed_once(self):
        loop, _ = _loop()
        with mock.patch.object(script, "GESTURE_CUES", True):
            first = _speak(loop, self.REPLY, "en")
            second = _speak(loop, self.REPLY, "en")
        self.assertIn("placed by estimate", first)
        self.assertNotIn("placed by estimate", second)

    def test_with_teaching_gestures_off_the_reply_is_spoken_in_one_piece(self):
        engine = _Engine()
        loop, robot = _loop(engine)
        _speak(loop, "That is a very good answer today. Now look at the diagram.", "en")
        self.assertEqual(len(engine.said), 1)
        self.assertEqual(robot.starts[0][1], {})

    def test_word_times_run_forward_and_stay_inside_the_audio(self):
        speech = _voice().synthesize("One two three four. Five six seven eight nine ten.", "en", split=True)
        times = [when for when, _ in speech.words]
        self.assertEqual([word for _, word in speech.words],
                         ["One", "two", "three", "four", "Five", "six", "seven", "eight", "nine", "ten"])
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(times[0], vc.LEAD_SILENCE_SECONDS - 0.01)          # after the leading silence
        self.assertLess(times[-1], speech.seconds)

    def test_chinese_is_timed_character_by_character(self):
        speech = _voice().synthesize("现在请看黑板。", "zh", split=True)
        self.assertEqual("".join(word for _, word in speech.words), "现在请看黑板")

    def test_edge_tts_word_times_are_not_called_estimates(self):
        loop, _ = _loop(engine=False)
        with mock.patch.object(script, "GESTURE_CUES", True):
            printed = _speak(loop, self.REPLY, "en")
        self.assertNotIn("estimate", printed)
        self.assertFalse(loop._words_estimated)


class TheWholeProgram(unittest.TestCase):
    """setup() and run() end to end with stand-ins: greeting, one reply, goodbye."""

    def _run(self, backend, engine=None):
        import queue
        loop, robot = _loop(engine=False)
        loop.asr_queue.put(("How do robots move", ""))
        take = loop.asr_queue.get

        def get(timeout=None):
            try:
                return take(timeout=0.01)
            except queue.Empty:
                raise KeyboardInterrupt          # nothing left to say: the operator stops the program

        loop.asr_queue.get = get
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(script, "VOICE_BACKEND", backend))
            stack.enter_context(mock.patch.object(script, "ChatterboxVoice", lambda: _voice(engine)))
            stack.enter_context(mock.patch.object(script.time, "sleep", lambda seconds: None))
            stack.enter_context(contextlib.redirect_stdout(out))
            loop.setup()
            loop.audio, loop.gestures = robot, robot          # setup() made its own stand-ins; watch these
            loop.run()
        return loop, robot, out.getvalue()

    def test_it_starts_and_runs_with_edge_tts(self):
        base.tts_calls.clear()
        loop, robot, printed = self._run("edge")
        self.assertIsNone(loop.voice)
        self.assertNotIn("[voice]", printed)
        self.assertEqual(len(base.tts_calls), 3)                          # greeting, reply, goodbye
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 3)

    def test_it_starts_and_runs_with_chatterbox(self):
        engine = _Engine()
        base.tts_calls.clear()
        loop, robot, printed = self._run("chatterbox", engine)
        self.assertIn("Chatterbox ready", printed)
        self.assertEqual(engine.preloads, 1)
        self.assertEqual(len(engine.said), 3)                             # greeting, reply, goodbye
        self.assertEqual(base.tts_calls, [])                              # Edge TTS never needed
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 3)
        self.assertIn("Yohdee", engine.said[0][0])                         # the greeting uses the name, spelled for the voice

    def test_it_starts_and_runs_when_chatterbox_cannot_load(self):
        engine = _Engine()
        engine.fail = OSError("model files not found")
        base.tts_calls.clear()
        loop, robot, printed = self._run("chatterbox", engine)
        self.assertIn("Chatterbox is NOT in use", printed)
        self.assertIsNone(loop.voice)
        self.assertEqual(len(base.tts_calls), 3)                          # everything was still said
        self.assertEqual(robot.events, ["gesture-start", "gesture-begin", "audio"] * 3)


class Sentences(unittest.TestCase):
    def test_nothing_is_lost_or_repeated(self):
        for text in ("One sentence only", "First one here. Second one here! Third one?", "Yes. That is right.",
                     "回答得很好。对，完全正确。", "Ask Dr. Smith about the schedule. He knows."):
            pieces = vc.sentences(text)
            self.assertEqual("".join("".join(pieces).split()), "".join(text.split()), text)

    def test_a_very_short_sentence_stays_with_the_next(self):
        self.assertEqual(vc.sentences("Yes. That is exactly right."), ["Yes. That is exactly right."])


class NothingSensitiveIsTracked(unittest.TestCase):
    """No model weights, voice clips, generated audio or secrets belong in git."""

    FORBIDDEN = (".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".bin", ".gguf", ".wav", ".mp3", ".flac",
                 ".ogg", ".m4a", ".npy", ".npz", ".pem", ".key")

    def test_tracked_files(self):
        try:
            listed = subprocess.run(["git", "ls-files"], cwd=base.ROOT, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git is not available")
        if listed.returncode != 0:
            self.skipTest("not a git checkout")
        files = listed.stdout.split("\n")
        self.assertGreater(len(files), 20)
        bad = [name for name in files if name.lower().endswith(self.FORBIDDEN)
               or os.path.basename(name) in (".env", "env", "config.local.yaml")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
