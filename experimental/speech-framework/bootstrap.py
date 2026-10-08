"""
bootstrap.py  -  shared CLI flags + object construction for the runners
"""

import argparse

import _paths
from conversation import Conversation, load_persona, load_tag_guide
from llm_providers import available_providers, create_provider
from settings import load_settings, resolve_path
from timing_log import TimingLog


def add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--provider", choices=available_providers(),
                        help="LLM provider (default from config.yaml: gemini)")
    parser.add_argument("--model", help="override the provider's model id")
    parser.add_argument("--config", help="extra YAML file merged over config.yaml")
    parser.add_argument("--voice", help="voice from the registry (default from config: tts.voice); "
                                        "--list-voices shows them")
    parser.add_argument("--list-voices", action="store_true", help="print the voice registry and exit")
    parser.add_argument("--no-stream", action="store_true",
                        help="wait for the whole LLM reply before speaking (old behaviour)")


def list_voices(cfg: dict):
    from voices import VoiceRegistry
    reg = VoiceRegistry(cfg["tts"])
    for name in reg.names():
        mark = "*" if name == reg.default else " "
        print(f"{mark} {reg.voices[name].summary()}")
    for problem in reg.check():
        print(f"  ! {problem}")


def build_conversation(args) -> tuple[dict, Conversation]:
    _paths.load_env()
    cfg = load_settings(args.config)

    llm_cfg  = cfg["llm"]
    provider = args.provider or llm_cfg["provider"]
    options  = dict(llm_cfg.get("providers", {}).get(provider, {}))
    if args.model:
        options["model"] = args.model
    llm = create_provider(provider, **options)
    print(f"[llm] {llm.name} / {llm.model}")

    conv_cfg = cfg["conversation"]
    conv = Conversation(
        llm,
        system_prompt=load_persona(conv_cfg, cfg["robot"]["name"]),
        max_history_turns=conv_cfg.get("max_history_turns", 6),
        default_language=conv_cfg.get("default_language", "en"),
        stream=conv_cfg.get("stream", True) and not getattr(args, "no_stream", False),
        first_clause_words=conv_cfg.get("first_clause_words", 6),
    )
    return cfg, conv


def build_synth(cfg: dict, args, conv: Conversation | None = None):
    """SpeechSynth for --voice / tts.voice; adds the <tone> guide to the prompt if the voice uses it."""
    from speech_synth import SpeechSynth
    synth = SpeechSynth(cfg["tts"], voice=getattr(args, "voice", None))
    if conv is not None and synth.wants_tone_tags:
        conv.add_instructions(load_tag_guide(cfg["conversation"]))
        print("[llm] voice uses <tone> plans; tone guide added to the system prompt")
    return synth


def make_timing_log(cfg: dict) -> TimingLog:
    return TimingLog(resolve_path(cfg["conversation"].get("timing_log")))
