"""Built-in provider definitions for Claw Agent.

Each entry defines label, base_url, api_key_env, key_hint, etc.
Model fallback lists live in :mod:`cli.models`.
"""

from typing import List, Optional, Dict, Any

ProviderInfo = Dict[str, Any]

PROVIDER_INFO: Dict[str, ProviderInfo] = {
    "openrouter": {
        "label": "OpenRouter",
        "hint": "aggregator with 200+ models",
        "base_url": "https://api.openrouter.ai/v1",
        "models_url": "https://api.openrouter.ai/api/v1/models",
        "api_key_env": "OPENROUTER_API_KEY",
        "key_hint": "https://openrouter.ai/keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "hint": "Google AI Studio — Gemini models",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "api_key_env": "GEMINI_API_KEY",
        "key_hint": "https://aistudio.google.com/app/apikey",
    },
    "deepseek": {
        "label": "DeepSeek",
        "hint": "DeepSeek direct API",
        "base_url": "https://api.deepseek.com/v1",
        "models_url": "https://api.deepseek.com/v1/models",
        "api_key_env": "DEEPSEEK_API_KEY",
        "key_hint": "https://platform.deepseek.com/api_keys",
    },
    "nous": {
        "label": "Nous Portal",
        "hint": "Nous Research subscription",
        "base_url": "https://inference-api.nousresearch.com/v1",
        "models_url": "https://inference-api.nousresearch.com/v1/models",
        "api_key_env": "NOUS_API_KEY",
        "key_hint": "https://portal.nousresearch.com",
    },
    "minimax": {
        "label": "MiniMax",
        "hint": "MiniMax direct API — M2.7 series",
        "base_url": "https://api.minimaxi.com/v1",
        "models_url": "https://api.minimaxi.com/v1/models",
        "api_key_env": "MINIMAX_API_KEY",
        "key_hint": "https://platform.minimaxi.com",
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "hint": "hosted API catalog",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "api_key_env": "NVIDIA_API_KEY",
        "key_hint": "https://build.nvidia.com",
    },
}


def get_provider_ids() -> List[str]:
    return list(PROVIDER_INFO.keys())


def get_provider_info(slug: str) -> Optional[ProviderInfo]:
    return PROVIDER_INFO.get(slug)


def get_provider_label(slug: str) -> str:
    info = PROVIDER_INFO.get(slug)
    if not info:
        return slug
    return info["label"]


def get_provider_base_url(slug: str) -> str:
    info = PROVIDER_INFO.get(slug)
    return info["base_url"] if info else ""


def get_api_key_env(slug: str) -> str:
    info = PROVIDER_INFO.get(slug)
    return info["api_key_env"] if info else ""
