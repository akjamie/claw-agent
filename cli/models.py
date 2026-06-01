"""Model resolution for Claw Agent.

Mirrors hermes-agent's three-tier pattern:
  1. Live API probe (fetch_api_models)
  2. Static fallback  (_PROVIDER_MODELS)
  3. Merged result    (curated_models_for_provider)

Built-in provider definitions live in :mod:`cli.providers`.
"""

import json
import urllib.request
import urllib.error
from typing import Dict, List, Tuple, Optional

from cli.providers import PROVIDER_INFO

USER_AGENT = "claw-agent/0.13.0"

# ── Static fallback model lists ───────────────────────────────────────────────
# (model_id, description) tuples — used when the live API is unreachable.

_PROVIDER_MODELS: Dict[str, List[Tuple[str, str]]] = {
    "openrouter": [
        # Anthropic
        ("anthropic/claude-opus-4.7", "recommended — most capable Claude"),
        ("anthropic/claude-opus-4.5", ""),
        ("anthropic/claude-sonnet-4.5", ""),
        ("anthropic/claude-haiku-4.5", "fast and affordable"),
        # Moonshot / Kimi
        ("moonshotai/kimi-k2.6", "recommended — strong coding + reasoning"),
        # OpenAI
        ("openai/gpt-5.4", ""),
        ("openai/gpt-5.4-mini", ""),
        # Google Gemini (via OpenRouter)
        ("google/gemini-3.1-pro-preview", ""),
        ("google/gemini-3.1-flash-lite", ""),
        ("google/gemini-3-flash-preview", ""),
        # DeepSeek (via OpenRouter)
        ("deepseek/deepseek-v4-pro", ""),
        ("deepseek/deepseek-v4-flash", "fast + cheap"),
        # Qwen
        ("qwen/qwen3-235b-a22b", ""),
        ("qwen/qwen3-30b-a3b", ""),
        # Free models
        ("deepseek/deepseek-r1", "free"),
        ("meta-llama/llama-4-maverick", "free"),
    ],
    "gemini": [
        # Gemini 3.x — current generation (May 2026)
        ("gemini-3.1-pro-preview", "recommended — most capable, 1M context"),
        ("gemini-3.1-flash-lite", "fast, cost-efficient"),
        ("gemini-3-flash-preview", ""),
        # Gemini 2.5 — stable GA
        ("gemini-2.5-pro", "stable GA — complex reasoning"),
        ("gemini-2.5-flash", "stable GA — balanced"),
        ("gemini-2.5-flash-lite", "stable GA — lightweight"),
    ],
    "deepseek": [
        # V4 — current as of April 2026
        ("deepseek-v4-pro", "recommended — DeepSeek V4 Pro"),
        ("deepseek-v4-flash", "fast + cheap — DeepSeek V4 Flash"),
        # Legacy aliases (deprecated July 2026)
        ("deepseek-chat", "alias for V4-Flash non-thinking (deprecated Jul 2026)"),
        ("deepseek-reasoner", "alias for V4-Flash thinking (deprecated Jul 2026)"),
    ],
    "nous": [
        # Hermes 4 — current (2025-2026)
        ("nousresearch/hermes-4-llama-3.1-405b", "recommended — frontier reasoning"),
        ("nousresearch/hermes-4-llama-3.1-70b", ""),
        ("nousresearch/hermes-4.3-seed-36b", "efficient — ~70B quality at 36B"),
        ("nousresearch/nouscoder-14b", "coding specialist"),
    ],
    "minimax": [
        ("MiniMax-M2.7", "recommended — recursive self-improvement, agent tasks"),
        ("MiniMax-M2.7-highspeed", "same quality as M2.7, significantly faster"),
        ("MiniMax-M2.5", "optimized for code generation, peak value"),
    ],
    "nvidia": [
        # Nemotron — current (2026)
        ("nvidia/llama-3.3-nemotron-super-49b-v1.5", "recommended — reasoning + tool use"),
        ("nvidia/llama-3.3-nemotron-super-49b-v1", ""),
        ("nvidia/llama-3.1-nemotron-70b-instruct", ""),
        # Qwen via NIM
        ("qwen/qwen3.5-vl-72b-instruct", "vision + language"),
        # DeepSeek via NIM
        ("deepseek-ai/deepseek-r1", "reasoning"),
    ],
}


def get_fallback_models(slug: str) -> List[Tuple[str, str]]:
    return list(_PROVIDER_MODELS.get(slug, []))


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _build_request(url: str, api_key: str, method: str = "GET"):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    return urllib.request.Request(url, headers=headers, method=method)


def _parse_models_response(data) -> List[Tuple[str, str]]:
    if not isinstance(data, dict):
        return []
    models = data.get("data")
    if not isinstance(models, list):
        return []
    result: List[Tuple[str, str]] = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if isinstance(mid, str) and mid.strip():
            desc = ""
            if "recommended" in str(mid).lower():
                desc = "recommended"
            elif "free" in str(mid).lower():
                desc = "free"
            result.append((mid.strip(), desc))
    return result


# ── API key verification ──────────────────────────────────────────────────────

def verify_api_key(slug: str, api_key: str, timeout: float = 5.0) -> Tuple[bool, str]:
    """Verify an API key by hitting the provider's models endpoint.

    Only definitive HTTP auth errors (401, 403) count as rejected.
    Network / SSL errors are ambiguous — the key may be valid but
    unreachable, so we warn and return True (proceed).

    Returns (is_valid, message).
    """
    if not api_key:
        return (False, "API key is empty")
    info = PROVIDER_INFO.get(slug)
    if not info:
        return (False, f"Unknown provider: {slug}")
    url = info["models_url"]
    try:
        req = _build_request(url, api_key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return (True, "")
        return (False, "Unexpected response format")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return (False, "Invalid API key (401 Unauthorized)")
        elif e.code == 403:
            return (False, "Access forbidden (403 Forbidden)")
        elif e.code == 429:
            return (False, "Rate limited (429 Too Many Requests)")
        return (False, f"HTTP Error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        reason = str(e.reason) if hasattr(e, 'reason') else str(e)
        return (True, f"warning: could not verify (network error: {reason}) — saved anyway")
    except json.JSONDecodeError:
        return (False, "Failed to parse API response")
    except Exception as e:
        return (True, f"warning: could not verify ({e}) — saved anyway")


# ── Model resolution (three-tier, mirroring hermes-agent) ─────────────────────

def fetch_api_models(api_key: str, models_url: str, timeout: float = 8.0) -> Optional[List[str]]:
    """Tier 1 — Live probe of a provider's models endpoint.

    Returns bare model ID strings, or None on any error.
    """
    try:
        req = _build_request(models_url, api_key)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        parsed = _parse_models_response(data)
        return [mid for mid, _ in parsed] if parsed else None
    except Exception:
        return None


def provider_model_ids(provider_slug: str, api_key: str) -> List[str]:
    """Tier 1→2 orchestrator — live API, then static fallback.

    Returns model ID strings only (no descriptions).
    """
    info = PROVIDER_INFO.get(provider_slug)
    if not info:
        return []

    live = fetch_api_models(api_key, info["models_url"])
    if live:
        return live

    return [mid for mid, _ in _PROVIDER_MODELS.get(provider_slug, [])]


def curated_models_for_provider(provider_slug: str, api_key: str) -> List[Tuple[str, str]]:
    """High-level API for the model picker UI.

    Returns (model_id, description) tuples by enriching live-API results
    with descriptions from the static catalog.  Falls back entirely to
    the static catalog when the live API is unreachable.
    """
    info = PROVIDER_INFO.get(provider_slug)
    if not info:
        return []

    model_ids = provider_model_ids(provider_slug, api_key)

    desc_map: Dict[str, str] = {}
    for mid, desc in _PROVIDER_MODELS.get(provider_slug, []):
        desc_map[mid] = desc

    result: List[Tuple[str, str]] = []
    for mid in model_ids:
        desc = desc_map.get(mid, "")
        result.append((mid, desc))
    return result


# ── Display helpers ───────────────────────────────────────────────────────────

def get_model_display_name(model_id: str, description: str = "") -> str:
    if description:
        return f"{model_id} [{description}]"
    return model_id
