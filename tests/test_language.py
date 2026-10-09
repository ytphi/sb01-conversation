#!/usr/bin/env python3
"""
test_language.py  -  offline check of the conversation script's language handling

Runs the real scripts/sb01_conversation.py with everything outside it replaced
by stand-ins: no robot, no DDS, no Claude, no Edge TTS, no camera, no network,
no gesture client and no monitoring storage.

  python3 -m unittest tests.test_language -v      (from the project root)
"""

import asyncio
import importlib.util
import inspect
import json
import os
import sys
import types
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "scripts", "sb01_conversation.py")

claude_calls = []     # keyword arguments of every messages.create() call
tts_calls = []        # (voice, text) of every synthesis
reply_text = {"value": "ok"}


class _Messages:
    def create(self, **kwargs):
        # The script keeps appending to its history list, so keep what was sent now.
        claude_calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=reply_text["value"])],
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


class _Communicate:
    def __init__(self, text, voice):
        tts_calls.append((voice, text))

    async def stream(self):
        yield {"type": "audio", "data": b"mp3"}


class _Segment:
    raw_data = b"\0\0" * 160

    @classmethod
    def from_mp3(cls, _file):
        return cls()

    def set_channels(self, _n):
        return self

    def set_sample_width(self, _n):
        return self

    def set_frame_rate(self, _rate):
        return self


class _Anything:
    """Stands in for the robot audio client, face recognition, memory and the
    monitor: every method exists and does nothing."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _NoFaces(_Anything):
    known_names = []                      # nobody enrolled
    last_result = {"outcome": "not_run"}


def _load_script():
    """Import the real conversation script with stand-ins for everything it talks to."""
    def module(name, **attributes):
        mod = types.ModuleType(name)
        mod.__dict__.update(attributes)
        return mod

    stand_ins = {
        "anthropic": module("anthropic", Anthropic=lambda api_key: types.SimpleNamespace(messages=_Messages())),
        "edge_tts": module("edge_tts", Communicate=_Communicate),
        "pydub": module("pydub", AudioSegment=_Segment),
        "unitree_sdk2py": module("unitree_sdk2py", __path__=[]),
        "unitree_sdk2py.core": module("unitree_sdk2py.core", __path__=[]),
        "unitree_sdk2py.core.channel": module("unitree_sdk2py.core.channel", ChannelSubscriber=_Anything,
                                              ChannelFactoryInitialize=lambda *a: None),
        "unitree_sdk2py.idl": module("unitree_sdk2py.idl", __path__=[]),
        "unitree_sdk2py.idl.std_msgs": module("unitree_sdk2py.idl.std_msgs", __path__=[]),
        "unitree_sdk2py.idl.std_msgs.msg": module("unitree_sdk2py.idl.std_msgs.msg", __path__=[]),
        "unitree_sdk2py.idl.std_msgs.msg.dds_": module("unitree_sdk2py.idl.std_msgs.msg.dds_", String_=object),
        "unitree_sdk2py.g1": module("unitree_sdk2py.g1", __path__=[]),
        "unitree_sdk2py.g1.audio": module("unitree_sdk2py.g1.audio", __path__=[]),
        "unitree_sdk2py.g1.audio.g1_audio_client": module("unitree_sdk2py.g1.audio.g1_audio_client", AudioClient=_Anything),
        "teleop.face_id": module("teleop.face_id", FaceIdentifier=_NoFaces),
        "teleop.memory_manager": module("teleop.memory_manager", MemoryManager=_Anything),
        "teleop.gesture_client": module("teleop.gesture_client", GestureClient=_Anything, GESTURE_SAMPLE_RATE=24000),
    }
    saved_modules = {name: sys.modules.get(name) for name in stand_ins}
    saved_env = {name: os.environ.get(name) for name in ("SB01_GESTURE_URL", "ANTHROPIC_API_KEY")}
    saved_path, saved_cwd = list(sys.path), os.getcwd()
    try:
        sys.modules.update(stand_ins)
        os.environ.pop("SB01_GESTURE_URL", None)          # gestures off: language only
        spec = importlib.util.spec_from_file_location("sb01_conversation_under_test", SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        os.environ.setdefault("ANTHROPIC_API_KEY", "not-a-real-key")
        return script
    finally:
        for name, previous in saved_modules.items():      # leave no stand-ins behind for other tests
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        sys.path[:] = saved_path
        os.chdir(saved_cwd)
        if saved_env["SB01_GESTURE_URL"] is not None:
            os.environ["SB01_GESTURE_URL"] = saved_env["SB01_GESTURE_URL"]


script = _load_script()


def _new_loop():
    claude_calls.clear()
    tts_calls.clear()
    # The loop takes a monitor argument only once the monitoring work is present.
    extra = (_Anything(),) if "mon" in inspect.signature(script.SB01ConversationLoop).parameters else ()
    return script.SB01ConversationLoop("eno0", "", *extra)


def _hear(loop, text, final=True):
    loop._asr_callback(types.SimpleNamespace(data=json.dumps({"text": text, "is_final": final, "emotion": ""})))


def _queued(loop):
    out = []
    while not loop.asr_queue.empty():
        out.append(loop.asr_queue.get()[0])
    return out


def _ask(loop, text):
    """Send one phrase through the real _ask_claude; returns the language note Claude was given."""
    loop._ask_claude(text, "")
    return claude_calls[-1]["messages"][-1]["content"].split("]")[0] + "]"


def _voice_for(loop, text, language):
    tts_calls.clear()
    asyncio.run(loop._text_to_pcm(text, language))
    return tts_calls[-1][0]


class VoicesAndLanguages(unittest.TestCase):
    def test_the_three_voice_constants_are_unchanged(self):
        self.assertEqual(script.VOICE_EN, "en-US-JennyNeural")
        self.assertEqual(script.VOICE_ES, "es-MX-DaliaNeural")
        self.assertEqual(script.VOICE_ZH, "zh-CN-XiaoxiaoNeural")

    def test_every_chatterbox_multilingual_language_is_enabled(self):
        self.assertTrue({"English", "Spanish", "Chinese"} <= set(script.LANGUAGE_NAMES.values()))
        self.assertEqual(len(script.LANGUAGE_NAMES), 23)
        for name in script.LANGUAGE_NAMES.values():
            self.assertIn(name, script.BASE_SYSTEM_PROMPT)

    def test_no_chinese_limiter(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("Chinese temporarily disabled", source)
        self.assertNotIn("CHINESE_ENABLED", source)
        self.assertNotIn("#     return VOICE_ZH", source)

    def test_chinese_reply_uses_the_chinese_voice(self):
        loop = _new_loop()
        self.assertEqual(_voice_for(loop, "你好，很高兴见到你。", "zh"), script.VOICE_ZH)
        self.assertEqual(_voice_for(loop, "CSUSB 是一所大学", None), script.VOICE_ZH)
        self.assertEqual(_voice_for(loop, "你好", "en"), script.VOICE_ZH)      # the characters decide

    def test_spanish_reply_uses_the_spanish_voice_with_or_without_accents(self):
        loop = _new_loop()
        self.assertEqual(_voice_for(loop, "¡Hola! ¿Cómo estás?", "es"), script.VOICE_ES)
        self.assertEqual(_voice_for(loop, "Hola, de que quieres hablar", "es"), script.VOICE_ES)
        self.assertEqual(_voice_for(loop, "Claro, con mucho gusto", "es"), script.VOICE_ES)

    def test_english_reply_and_fixed_phrases_use_the_english_voice(self):
        loop = _new_loop()
        self.assertEqual(_voice_for(loop, "Hi there! What would you like to talk about?", "en"), script.VOICE_EN)
        self.assertEqual(_voice_for(loop, "Goodbye! It was great talking with you.", None), script.VOICE_EN)


class ShortInputsAreKept(unittest.TestCase):
    def test_short_greetings_reach_the_language_handling(self):
        loop = _new_loop()
        for text in ("Hi", "Hola", "你好", "Hi.", "ok", "嗨", "No"):
            _hear(loop, text)
        self.assertEqual(_queued(loop), ["Hi", "Hola", "你好", "Hi.", "ok", "嗨", "No"])

    def test_noise_is_still_dropped(self):
        loop = _new_loop()
        for text in ("", " ", "?", "...", "a", "I", "-", "¿?"):
            _hear(loop, text)
        self.assertEqual(_queued(loop), [])

    def test_nothing_is_heard_while_the_robot_is_speaking(self):
        loop = _new_loop()
        loop.speaking.set()
        _hear(loop, "Hola")
        self.assertEqual(_queued(loop), [])


class ConversationLanguage(unittest.TestCase):
    def test_starts_in_english(self):
        loop = _new_loop()
        self.assertEqual(loop.language, "en")
        self.assertEqual(_ask(loop, "Hi"), "[Reply in English.]")

    def test_clear_phrases_switch(self):
        loop = _new_loop()
        self.assertEqual(_ask(loop, "Hola"), "[Reply in Spanish.]")
        self.assertEqual(_ask(loop, "你好"), "[Reply in Chinese.]")
        self.assertEqual(_ask(loop, "Hello"), "[Reply in English.]")
        self.assertEqual(_ask(loop, "como te llamas tu"), "[Reply in Spanish.]")          # no accents
        self.assertEqual(_ask(loop, "tell me about the robot"), "[Reply in English.]")
        self.assertEqual(_ask(loop, "这个机器人能做什么"), "[Reply in Chinese.]")
        self.assertEqual(_ask(loop, "¿Qué tiempo hace hoy?"), "[Reply in Spanish.]")

    def test_unclear_input_keeps_spanish(self):
        loop = _new_loop()
        _ask(loop, "Hola, ¿cómo estás?")
        for unclear in ("blorp", "asdf qwer", "嗨", "嗨嗨嗨", "mmm ok", "yeah", "no", "uh"):
            self.assertEqual(_ask(loop, unclear), "[Reply in Spanish.]", unclear)
        self.assertEqual(loop.language, "es")

    def test_unclear_input_keeps_chinese(self):
        loop = _new_loop()
        _ask(loop, "你好吗，今天怎么样")
        for unclear in ("uh huh", "sí", "okay", "blorp zzkt", "si si"):
            self.assertEqual(_ask(loop, unclear), "[Reply in Chinese.]", unclear)

    def test_unclear_input_keeps_english(self):
        loop = _new_loop()
        for unclear in ("嗨", "sí", "嗯嗯", "que", "blorp"):
            self.assertEqual(_ask(loop, unclear), "[Reply in English.]", unclear)
        self.assertEqual(loop.language, "en")

    def test_claude_is_told_to_ask_for_a_repeat_in_that_language(self):
        loop = _new_loop()
        _ask(loop, "Hola")
        _ask(loop, "blorp zzkt")
        system = claude_calls[-1]["system"]
        self.assertIn("did not catch it", system)
        self.assertIn("in that language", system)
        self.assertTrue(claude_calls[-1]["messages"][-1]["content"].startswith("[Reply in Spanish.]"))

    def test_the_reply_is_spoken_in_the_conversation_language(self):
        loop = _new_loop()
        _ask(loop, "Hola")
        self.assertEqual(_voice_for(loop, "Perdon, no te entendi", loop.language), script.VOICE_ES)


if __name__ == "__main__":
    unittest.main()
