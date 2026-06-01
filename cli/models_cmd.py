from cli.providers import (
    get_provider_ids,
    get_provider_info,
    get_provider_label,
    get_provider_base_url,
    get_api_key_env,
)
from cli.models import (
    curated_models_for_provider,
    verify_api_key,
    get_model_display_name,
)
from cli.config import set_model_config, get_model_config, save_env_value, get_env_value, remove_env_value
from cli.interactive_ui import (
    print_header,
    print_success,
    print_error,
    print_info,
    print_warning,
    prompt_choice,
    prompt_choice_with_cancel,
    prompt_password,
    prompt_text,
    is_interactive_stdin,
)

# ── Active provider lookup ─────────────────────────────────────────────────────

def _current_provider_id() -> str:
    config = get_model_config()
    return (config.get("provider") or "").strip()


def _current_model_id() -> str:
    config = get_model_config()
    return (config.get("model") or "").strip()


def _current_api_key_for(slug: str) -> str:
    """Check env var (process → .env file) for this provider's key."""
    env_var = get_api_key_env(slug)
    if env_var:
        return get_env_value(env_var) or ""
    return ""


# ── Step 1: Provider selection ────────────────────────────────────────────────

def _select_provider() -> str:
    print_header("Step 1: Select Provider")

    current_provider = _current_provider_id()
    current_model = _current_model_id()

    # Show current active config on top
    if current_provider and current_model:
        print_info(f"Current: {get_provider_label(current_provider)} → {current_model}")
    elif current_provider:
        print_info(f"Current provider: {get_provider_label(current_provider)}")
    else:
        print_info("No provider configured yet")
    print()

    choices = []
    slugs = get_provider_ids()
    default_idx = 0
    for i, slug in enumerate(slugs):
        info = get_provider_info(slug)
        label = info["label"] if info else slug
        hint = info["hint"] if info else ""
        line = f"{label}  ({hint})" if hint else label
        if slug == current_provider:
            line = f"{line}  ← current"
            default_idx = i
        choices.append(line)

    idx = prompt_choice_with_cancel("Choose a provider:", choices, default_index=default_idx)
    if idx < 0:
        raise RuntimeError("Cancelled by user")

    chosen = slugs[idx]
    if chosen != current_provider:
        print_info(f"Switching to {get_provider_label(chosen)}")
    return chosen


# ── Step 2: API key ───────────────────────────────────────────────────────────

def _prompt_api_key(slug: str) -> str:
    info = get_provider_info(slug)
    label = info["label"] if info else slug
    key_hint_url = (info or {}).get("key_hint", "")

    existing = _current_api_key_for(slug)

    if not existing:
        print_header("Step 2: API Key Setup")
        if key_hint_url:
            print_info(f"Get your {label} API key from: {key_hint_url}")
        print_info("Press Enter with no key to cancel.")
        print()

        for attempt in range(1, 4):
            key = prompt_password(f"Enter your {label} API key").strip()
            if not key:
                if attempt == 1:
                    print_info("Exiting setup.")
                    raise RuntimeError("Cancelled by user")
                raise RuntimeError("No API key provided")

            print_info("Verifying API key...")
            is_valid, msg = verify_api_key(slug, key)
            if is_valid:
                if msg:
                    print_warning(f"API key saved ({msg})")
                else:
                    print_success("API key verified successfully!")
                return key
            else:
                print_error(f"Verification failed: {msg}")
                remaining = 3 - attempt
                if remaining:
                    print_info(f"{remaining} attempt(s) remaining")
                else:
                    raise RuntimeError("API key verification failed after 3 attempts")

    print_header("Step 2: API Key (already configured)")
    print_info(f"{label} API key: {existing[:8]}...")
    choice = prompt_text("[K]eep / [R]eplace / [C]lear / E[x]it", default="k").strip().lower()

    if choice.startswith("x"):
        raise RuntimeError("Cancelled by user")

    if choice.startswith("r"):
        for attempt in range(1, 4):
            key = prompt_password(f"Enter new {label} API key").strip()
            if not key:
                if attempt == 1:
                    print_info("No change — keeping existing key.")
                    return existing
                raise RuntimeError("No API key provided")
            print_info("Verifying API key...")
            is_valid, msg = verify_api_key(slug, key)
            if is_valid:
                if msg:
                    print_warning(f"API key saved ({msg})")
                else:
                    print_success("API key verified successfully!")
                return key
            else:
                print_error(f"Verification failed: {msg}")
                remaining = 3 - attempt
                if remaining:
                    print_info(f"{remaining} attempt(s) remaining")
                else:
                    print_info("Keeping existing key.")
                    return existing

    if choice.startswith("c"):
        env_var = get_api_key_env(slug)
        if env_var:
            remove_env_value(env_var)
        print_info("API key cleared.")
        raise RuntimeError("API key cleared — run 'claw models' again to configure")

    print_info("Keeping existing API key.")
    return existing


# ── Steps 3 & 4: Model fetch + selection ──────────────────────────────────────

def _select_model(slug: str, api_key: str) -> str:
    info = get_provider_info(slug)
    label = info["label"] if info else slug

    print_header("Step 3: Fetch Available Models")
    print_info(f"Fetching available models from {label}...")
    models = curated_models_for_provider(slug, api_key)

    if not models:
        print_error("No models available")
        raise RuntimeError(f"Failed to fetch models for {label}")

    print_success(f"Found {len(models)} available model(s)")
    print()

    print_header("Step 4: Select a Model")

    current_model = _current_model_id()
    model_ids: list[str] = []
    choices: list[str] = []
    default_idx = 0
    for i, (mid, desc) in enumerate(models):
        model_ids.append(mid)
        label = get_model_display_name(mid, desc)
        if mid == current_model:
            label += "  ← current"
            default_idx = i
        choices.append(label)

    choices.append("Enter custom model name")
    model_ids.append("__custom__")
    choices.append("Cancel (go back)")
    model_ids.append("__cancel__")

    idx = prompt_choice("Choose a model:", choices, default_index=default_idx)
    if idx < 0 or idx >= len(model_ids):
        raise RuntimeError("Model selection cancelled")
    if model_ids[idx] == "__cancel__":
        raise RuntimeError("Cancelled by user")

    selected = model_ids[idx]
    if selected == "__custom__":
        custom = prompt_text("Enter model name").strip()
        if not custom:
            raise RuntimeError("No model name entered")
        selected = custom

    print_success(f"Selected: {selected}")
    return selected


# ── Main entry point ──────────────────────────────────────────────────────────

def run_models_command() -> bool:
    if not is_interactive_stdin():
        print_error("'claw models' requires an interactive terminal.")
        print_info("Run this command directly in your terminal, not through a pipe.")
        return False

    try:
        provider = _select_provider()
        print()

        api_key = _prompt_api_key(provider)
        print()

        model = _select_model(provider, api_key)
        print()

        print_header("Saving Configuration")
        env_var = get_api_key_env(provider)
        if env_var:
            save_env_value(env_var, api_key)
        if set_model_config(provider, model, get_provider_base_url(provider)):
            print_success("Configuration saved to ~/.claw/config.json")
            print()
            print_info(f"Provider: {provider}")
            print_info(f"Model:    {model}")
            print_info(f"Base URL: {get_provider_base_url(provider)}")
            print()
            return True
        else:
            print_error("Failed to save configuration")
            return False

    except KeyboardInterrupt:
        print()
        print_info("Setup cancelled.")
        return False
    except RuntimeError as e:
        if str(e) != "Cancelled by user":
            print_error(str(e))
        return False
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        return False


def show_current_model() -> None:
    config = get_model_config()

    if not config:
        print_info("No model configured yet. Run 'claw models' to set up.")
        return

    provider = config.get("provider", "unknown")
    model = config.get("model", "unknown")
    base_url = get_provider_base_url(provider)

    print()
    print_header("Current Model Configuration")
    print_info(f"Provider: {provider}")
    print_info(f"Model:    {model}")
    if base_url:
        print_info(f"Base URL: {base_url}")
    print()
