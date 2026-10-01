"""
bootstrap.py  -  shared CLI flags + object construction for the runners
"""

import argparse

import _paths
from conversation import Conversation, load_persona
from llm_providers import available_providers, create_provider
from settings import load_settings


def add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--provider", choices=available_providers(),
                        help="LLM provider (default from config.yaml: gemini)")
    parser.add_argument("--model", help="override the provider's model id")
    parser.add_argument("--config", help="extra YAML file merged over config.yaml")


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
    )
    return cfg, conv
