# AGENTS.md — claw-agent

## Entry point

- **CLI command**: `claw` (installed via `cli.main:main`)
- **Package**: `cli/` (single package, no `src/` layout)
- **Runtime**: Python >=3.11, setuptools build

## Commands

| Command | Description |
|---|---|
| `claw version` / `claw --version` | Print version |
| `claw models` | Interactive 4-step model/provider wizard |
| `claw models --show` | Show saved config |

## Development

```sh
pip install -e ".[dev,cli]"   # dev deps + simple-term-menu
ruff check cli/          # lint (no formatter configured)
```

- **Lint only** (`ruff check`), no formatter (black/ruff-format), no typechecker, no pre-commit.
- No CI or Makefile — test manually via `python test_models_implementation.py`.

## Project structure

```
cli/
  main.py           # argparse dispatcher, entry point
  config.py         # JSON config at ~/.claw/config.json
  models.py         # OpenRouter API client + fallback model list
  models_cmd.py     # 4-step interactive wizard (provider → API key → model → save)
  interactive_ui.py # TerminalMenu via simple_term_menu, fallback to numbered input
```

## Key facts

- **Config**: `~/.claw/config.json` — JSON (not YAML). Fields: `model.provider`, `model.model`, `model.api_key`. API key is stored in plaintext.
- **API key fallback**: `config.json` checked first, then `OPENROUTER_API_KEY` env var.
- **Menu priority**: `simple-term-menu` (Unix/macOS, cli extra) → `prompt_toolkit.radiolist_dialog` (cross-platform, core dep) → numbered `input()`. Arrow keys work in either of the first two tiers. `prompt_toolkit` is always available on every platform.
- **Windows**: `claw_bootstrap.py` sets `PYTHONUTF8=1` for UTF-8 stdio in child processes — import at the top of any Windows entry point.
- **Providers**: Registered in `models.py` `PROVIDER_INFO` dict. Each entry defines base_url, models_url, api_key_env, fallback_models. Currently: openrouter, nous, minimax, nvidia.
- **Extras**: `cli` → simple-term-menu; `dev` → pytest, ruff, debugpy, mcp.

## OpenRouter API

- Base: `https://api.openrouter.ai/v1`
- Models endpoint: `https://api.openrouter.ai/api/v1/models` (Bearer auth)
- User-Agent: `claw-agent/0.13.0`
- `urllib` only (no `requests` used for this — check `models.py` before adding it)
