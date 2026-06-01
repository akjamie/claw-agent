"""
Configuration management for Claw Agent CLI.

Mirrors hermes-agent's pattern:
  {cwd}/config.json       — project-local settings (optional, highest priority)
  ~/.claw/config.json     — user-global settings (fallback)
  {cwd}/.env              — project-local secrets (optional, highest priority)
  ~/.claw/.env            — user-global secrets (fallback)

API key resolution: os.environ → CWD .env → ~/.claw/.env

Config/secret "writes" go to CWD when a CWD-level file already exists or when
no file exists anywhere; otherwise to ~/.claw/.
"""

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


_KNOWN_SECRETS = frozenset({
    "OPENROUTER_API_KEY", "NOUS_API_KEY", "MINIMAX_API_KEY", "NVIDIA_API_KEY",
    "WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID", "WEIXIN_BASE_URL", "WEIXIN_USER_ID",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
})


# ── Paths ──────────────────────────────────────────────────────────────────────

def get_claw_home() -> Path:
    home = Path.home()
    claw_home = home / ".claw"
    claw_home.mkdir(parents=True, exist_ok=True)
    return claw_home


def get_config_path() -> Path:
    return get_claw_home() / "config.json"


def get_env_path() -> Path:
    return get_claw_home() / ".env"


def _cwd_config() -> Path:
    return Path.cwd() / "config.json"


def _cwd_env() -> Path:
    return Path.cwd() / ".env"


# ── Config file (JSON — settings only, NO api_key) ─────────────────────────────

def load_config() -> Dict[str, Any]:
    config_path = _cwd_config() if _cwd_config().exists() else get_config_path()
    if not config_path.exists():
        return {}

    try:
        with open(config_path, "r") as f:
            cfg: dict = json.load(f)
    except Exception:
        return {}

    _migrate_config(cfg)
    return cfg


def _write_config_path() -> Path:
    """Determine where to write config.json — CWD if local exists or no global, else ~/.claw/."""
    cwd = _cwd_config()
    if cwd.exists():
        return cwd
    home = get_config_path()
    if home.exists():
        return home
    return cwd


def save_config(config: Dict[str, Any]) -> bool:
    config_path = _write_config_path()
    try:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception:
        return False


def get_config_value(key: str, default: Any = None) -> Any:
    config = load_config()
    parts = key.split(".")
    value = config
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return default
    return value if value is not None else default


def set_config_value(key: str, value: Any) -> bool:
    config = load_config()
    parts = key.split(".")
    current = config
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value
    return save_config(config)


def get_model_config() -> Dict[str, Any]:
    config = load_config()
    return config.get("model", {})


def set_model_config(provider: str, model: str, base_url: str = "") -> bool:
    config = load_config()
    config["model"] = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
    }
    return save_config(config)


def get_selected_model() -> Optional[str]:
    return get_config_value("model.model")


def get_selected_provider() -> Optional[str]:
    return get_config_value("model.provider")


# ── Secrets (.env) ─────────────────────────────────────────────────────────────

def _read_env_file(env_path: Path, key: str) -> Optional[str]:
    """Parse a single KEY=VALUE line from a .env file."""
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line[len(key) + 1:]
    except Exception:
        pass
    return None


def get_env_value(key: str) -> Optional[str]:
    if key in os.environ:
        return os.environ[key]
    val = _read_env_file(_cwd_env(), key)
    if val is not None:
        return val
    return _read_env_file(get_env_path(), key)


def _write_env_path() -> Path:
    """Determine where to write .env — CWD if local exists or no global, else ~/.claw/."""
    cwd = _cwd_env()
    if cwd.exists():
        return cwd
    home = get_env_path()
    if home.exists():
        return home
    return cwd


def save_env_value(key: str, value: str) -> bool:
    value = value.replace("\n", "").replace("\r", "")
    env_path = _write_env_path()
    lines: list[str] = []
    if env_path.exists():
        try:
            with open(env_path, "r") as f:
                lines = f.readlines()
        except Exception:
            lines = []

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break

    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")

    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix=".tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False

    _secure_file(env_path)
    os.environ[key] = value
    return True


def remove_env_value(key: str) -> bool:
    env_path = _write_env_path()
    if not env_path.exists():
        os.environ.pop(key, None)
        return False

    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
    except Exception:
        os.environ.pop(key, None)
        return False

    new_lines = [line for line in lines if not line.strip().startswith(f"{key}=")]
    found = len(new_lines) < len(lines)

    if found:
        fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix=".tmp", prefix=".env_")
        try:
            with os.fdopen(fd, "w") as f:
                f.writelines(new_lines)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, env_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
        _secure_file(env_path)

    os.environ.pop(key, None)
    return found


# ── Migration (old config.json with api_key → .env) ────────────────────────────

def _migrate_config(cfg: Dict[str, Any]) -> None:
    model = cfg.get("model", {})
    api_key = model.get("api_key", "") or ""
    if not api_key:
        return

    provider = model.get("provider", "")
    from cli.providers import get_api_key_env
    env_var = get_api_key_env(provider)
    if env_var:
        current = get_env_value(env_var)
        if not current:
            save_env_value(env_var, api_key)

    model.pop("api_key", None)
    if model:
        cfg["model"] = model
    else:
        cfg.pop("model", None)
    save_config(cfg)


# ── Security helpers ───────────────────────────────────────────────────────────

def _secure_file(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        restricted = current & 0o700
        if restricted < current:
            path.chmod(restricted)
    except OSError:
        pass


def redact_key(key: str) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "***"
    return key[:4] + "..." + key[-4:]
