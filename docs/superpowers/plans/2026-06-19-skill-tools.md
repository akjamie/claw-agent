# Skill Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register `claw_list_skills` and `claw_read_skill` as native tools so the agent and sub-agents can discover and read bundled skills during chat.

**Architecture:** New `agent/skills.py` module provides data-access functions (`list_skills()`, `read_skill()`) and callable tool handlers. `AgentLoop.register_skill_tools()` registers them on the shared `ToolRegistry`. Sub-agents inherit them automatically via the parent registry.

**Tech Stack:** Python 3.11+, no new dependencies. Uses `pathlib` for filesystem access, `json` for frontmatter-safe content extraction.

---

### Task 1: Create `agent/skills.py` — data-access layer

**Files:**
- Create: `agent/skills.py`
- Test: `tests/agent/test_skills.py`

- [ ] **Step 1: Install dev deps and verify current test suite passes**

```bash
cd d:/workbench/ai-agent/claw-agent
pip install -e ".[dev]" 2>&1 | tail -5
pytest tests/ -v 2>&1 | tail -20
```

- [ ] **Step 2: Write the failing test for `list_skills` with a temporary skills directory**

```python
"""Tests for agent/skills.py — skill discovery and reading."""

import tempfile
from pathlib import Path

import pytest

from agent.skills import list_skills, read_skill


def test_list_skills_with_temp_dir():
    """list_skills returns metadata for each SKILL.md in the skills directory."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        # Override the module's SKILLS_DIR for the test
        import agent.skills as sk
        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir

        try:
            # Create two skill subdirectories with valid SKILL.md files
            (skills_dir / "alpha").mkdir()
            (skills_dir / "alpha" / "SKILL.md").write_text(
                "---\nname: alpha\n"
                'description: "Alpha skill"\n'
                "platforms: [linux]\n"
                "---\n\n# Alpha\n\nStep 1. Do thing\n",
                encoding="utf-8",
            )

            (skills_dir / "beta").mkdir()
            (skills_dir / "beta" / "SKILL.md").write_text(
                "---\nname: beta\n"
                'description: "Beta skill"\n'
                "---\n\n# Beta\n\nStep 1. Other thing\n",
                encoding="utf-8",
            )

            result = list_skills()
            assert len(result) == 2

            names = {r["name"] for r in result}
            assert names == {"alpha", "beta"}

            # Order is alphabetical
            assert result[0]["name"] == "alpha"
            assert result[0]["description"] == "Alpha skill"
            assert result[0]["platforms"] == ["linux"]

            assert result[1]["name"] == "beta"
            assert result[1]["platforms"] == []  # default when missing
        finally:
            sk._SKILLS_DIR = original


def test_list_skills_empty_dir():
    """list_skills returns [] when the skills directory is empty or missing."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk
        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir

        try:
            assert list_skills() == []
        finally:
            sk._SKILLS_DIR = original


def test_list_skills_ignores_non_skill_dirs():
    """Directories without SKILL.md are ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk
        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir

        try:
            (skills_dir / "not-a-skill").mkdir()  # no SKILL.md inside
            (skills_dir / "real-skill").mkdir()
            (skills_dir / "real-skill" / "SKILL.md").write_text(
                "---\nname: real-skill\ndescription: Real\n---\n\nContent\n",
                encoding="utf-8",
            )

            result = list_skills()
            assert len(result) == 1
            assert result[0]["name"] == "real-skill"
        finally:
            sk._SKILLS_DIR = original


def test_read_skill_returns_body_without_frontmatter():
    """read_skill returns the markdown body stripped of YAML frontmatter."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk
        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir

        try:
            (skills_dir / "myskill").mkdir()
            (skills_dir / "myskill" / "SKILL.md").write_text(
                "---\nname: myskill\ndescription: Test\n---\n\n"
                "# My Skill\n\nInstructions here.\n",
                encoding="utf-8",
            )

            body = read_skill("myskill")
            assert body is not None
            assert "---" not in body
            assert "# My Skill" in body
            assert "Instructions here." in body
        finally:
            sk._SKILLS_DIR = original


def test_read_skill_not_found():
    """read_skill returns None for a skill that does not exist."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk
        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir

        try:
            assert read_skill("nonexistent") is None
        finally:
            sk._SKILLS_DIR = original


def test_read_skill_no_frontmatter():
    """A SKILL.md without frontmatter is returned as-is."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk
        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir

        try:
            (skills_dir / "plain").mkdir()
            (skills_dir / "plain" / "SKILL.md").write_text(
                "# Plain Skill\n\nJust content.\n",
                encoding="utf-8",
            )

            body = read_skill("plain")
            assert body == "# Plain Skill\n\nJust content.\n"
        finally:
            sk._SKILLS_DIR = original
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd d:/workbench/ai-agent/claw-agent
python -m pytest tests/agent/test_skills.py -v 2>&1 | head -40
```

Expected: `ModuleNotFoundError: No module named 'agent.skills'` or import errors.

- [ ] **Step 4: Write minimal `agent/skills.py`**

```python
"""
Skill discovery and reading for the agent runtime.

Skills are markdown files with YAML frontmatter stored under
``cli/skills/<name>/SKILL.md``.  This module provides the data-access
layer that the ``claw_list_skills`` and ``claw_read_skill`` native tools
use to discover and read skills.

The skills directory is resolved relative to the package structure
(``<package_root>/cli/skills/``) and works for both development installs
(``pip install -e``) and installed packages.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "list_skills",
    "read_skill",
    "SKILL_LIST_TOOL_NAME",
    "SKILL_LIST_TOOL_DESCRIPTION",
    "SKILL_READ_TOOL_NAME",
    "SKILL_READ_TOOL_DESCRIPTION",
    "SKILL_READ_TOOL_SCHEMA",
    "ListSkillsHandler",
    "ReadSkillHandler",
]


# ── Path resolution ──────────────────────────────────────────────────────────

_SKILLS_DIR: Optional[Path] = None


def _resolve_skills_dir() -> Path:
    """Return the path to the bundled skills directory.

    Resolved relative to the agent package: ``<package_root>/cli/skills/``.
    Falls back to a reasonable guess based on the current file location.
    """
    global _SKILLS_DIR
    if _SKILLS_DIR is None:
        # agent/skills.py → agent/ → <package_root>/cli/skills/
        _SKILLS_DIR = Path(__file__).parent.parent / "cli" / "skills"
    return _SKILLS_DIR


def set_skills_dir(path: Path) -> None:
    """Override the skills directory (used by tests)."""
    global _SKILLS_DIR
    _SKILLS_DIR = path


# Note: tests override _SKILLS_DIR directly via import agent.skills; sk._SKILLS_DIR = tmp_path


# ── Frontmatter parsing ──────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> Dict[str, Any]:
    """Minimal YAML frontmatter parser.

    Returns a dict of key-value pairs from the ``---`` delimited block
    at the start of the file.  Values are kept as plain strings unless
    they look like JSON arrays (e.g. ``[linux, macos]``), in which case
    they are parsed into Python lists.
    """
    meta: Dict[str, Any] = {}
    m = re.match(r"^---\s*\n(.*?)\n(?:---|\.\.\.)", content, re.DOTALL)
    if not m:
        return meta
    for line in m.group(1).splitlines():
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            raw_val = val.strip().strip('"').strip("'")

            # Try to parse as JSON array (e.g. [linux, macos])
            if raw_val.startswith("[") and raw_val.endswith("]"):
                try:
                    import json
                    parsed = json.loads(raw_val)
                    if isinstance(parsed, list):
                        meta[key] = parsed
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass

            meta[key] = raw_val
    return meta


# ── Public data-access functions ──────────────────────────────────────────────

def list_skills() -> List[Dict[str, Any]]:
    """Scan the skills directory and return metadata for each skill.

    Returns a list of dicts, each with keys ``name``, ``description``,
    ``platforms``, sorted alphabetically by name.  An empty list is
    returned when the directory is missing or contains no valid skills.
    """
    skills_dir = _resolve_skills_dir()
    if not skills_dir.is_dir():
        return []

    result: List[Dict[str, Any]] = []
    for entry in sorted(skills_dir.iterdir()):
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        content = skill_file.read_text(encoding="utf-8")
        meta = _parse_frontmatter(content)
        result.append({
            "name": meta.get("name", entry.name),
            "description": meta.get("description", ""),
            "platforms": meta.get("platforms", []),
        })
    return result


def read_skill(name: str) -> Optional[str]:
    """Return the full markdown body of a skill (frontmatter stripped).

    Args:
        name: The skill directory name (e.g. ``"openrouter-setup"``).

    Returns:
        The markdown content with frontmatter removed, or ``None`` when
        the skill does not exist or cannot be read.
    """
    skills_dir = _resolve_skills_dir()
    skill_file = skills_dir / name / "SKILL.md"
    if not skill_file.is_file():
        return None
    try:
        content = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None

    # Strip the YAML frontmatter block.
    body = re.sub(r"^---.*?---\s*", "", content, count=1, flags=re.DOTALL)
    return body.strip()


# ── Native tool definitions ───────────────────────────────────────────────────

SKILL_LIST_TOOL_NAME = "claw_list_skills"
SKILL_LIST_TOOL_DESCRIPTION = (
    "List all available skills with their names and descriptions. "
    "Skills are step-by-step guides for common tasks (e.g. provider setup). "
    "Call this to discover what skills are available."
)

SKILL_READ_TOOL_NAME = "claw_read_skill"
SKILL_READ_TOOL_DESCRIPTION = (
    "Read the full content of a specific skill to get detailed "
    "step-by-step instructions. Call this after listing skills "
    "with claw_list_skills and finding one that matches the task."
)
SKILL_READ_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "The name of the skill to read "
                "(e.g. 'openrouter-setup')."
            ),
        },
    },
    "required": ["name"],
}


# ── Native tool handlers ──────────────────────────────────────────────────────

class ListSkillsHandler:
    """Native tool handler for ``claw_list_skills``.

    Callable that receives an empty args dict and returns a formatted
    list of available skills as plain text.
    """

    def __call__(self, args: dict) -> str:
        skills = list_skills()
        if not skills:
            return (
                "[skill_info] No skills found. "
                "Bundled skills are not available in this installation."
            )

        lines: list[str] = ["Available skills:"]
        for s in skills:
            name = s["name"]
            desc = s["description"]
            platforms = s.get("platforms", [])
            pf = f" (platforms: {', '.join(platforms)})" if platforms else ""
            lines.append(f"  - {name}: {desc}{pf}")
        return "\n".join(lines)


class ReadSkillHandler:
    """Native tool handler for ``claw_read_skill``.

    Callable that receives ``{"name": "skill-name"}`` and returns the
    skill's markdown body, or an error message if not found.
    """

    def __call__(self, args: dict) -> str:
        name = args.get("name", "").strip()
        if not name:
            return "[skill_error] 'name' argument is required and must not be empty."

        content = read_skill(name)
        if content is None:
            return (
                f"[skill_error] Skill '{name}' not found. "
                "Use claw_list_skills to see available skills."
            )
        return content
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd d:/workbench/ai-agent/claw-agent
python -m pytest tests/agent/test_skills.py -v 2>&1
```

Expected: All 6 tests PASS.

- [ ] **Step 6: Run existing test suite to verify no regressions**

```bash
cd d:/workbench/ai-agent/claw-agent
pytest tests/ -v 2>&1 | tail -30
```

Expected: All existing tests still pass.

- [ ] **Step 7: Commit**

```bash
cd d:/workbench/ai-agent/claw-agent
git add agent/skills.py tests/agent/test_skills.py
git commit -m "feat: add skill data-access layer with list_skills and read_skill

Creates agent/skills.py with functions to discover and read bundled
skills from cli/skills/<name>/SKILL.md, plus native tool handler
classes for the LLM-facing tools.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Add `register_skill_tools()` to `AgentLoop`

**Files:**
- Modify: `agent/loop.py`

- [ ] **Step 1: Write test for `register_skill_tools`**

Add to `tests/agent/test_skills.py`:

```python
def test_register_skill_tools_adds_two_tools():
    """register_skill_tools registers claw_list_skills and claw_read_skill."""
    from unittest.mock import MagicMock
    from agent.loop import AgentLoop
    from agent.config import AgentConfig
    from agent.skills import (
        SKILL_LIST_TOOL_NAME,
        SKILL_LIST_TOOL_DESCRIPTION,
        SKILL_READ_TOOL_NAME,
        SKILL_READ_TOOL_DESCRIPTION,
        SKILL_READ_TOOL_SCHEMA,
        ListSkillsHandler,
        ReadSkillHandler,
    )

    # Build a minimal AgentLoop with mocks.
    cfg = AgentConfig()
    loop = AgentLoop(
        cfg=cfg,
        llm=MagicMock(),
        mcp=MagicMock(),
        registry=MagicMock(),
        dispatcher=MagicMock(),
        compressor=MagicMock(),
        guardrails=MagicMock(),
        persistence=MagicMock(),
        session=MagicMock(),
        token_estimator=lambda s: len(s) // 4,
        title_generator=None,
    )

    loop.register_skill_tools()

    # Should have registered two native tools.
    assert loop._registry.register_native.call_count == 2

    # First call: claw_list_skills (no schema)
    call1 = loop._registry.register_native.call_args_list[0]
    assert call1[0][0] == SKILL_LIST_TOOL_NAME
    assert call1[0][1] == SKILL_LIST_TOOL_DESCRIPTION
    assert call1[0][2] == {"type": "object", "properties": {}}  # empty schema
    assert isinstance(call1[0][3], ListSkillsHandler)

    # Second call: claw_read_skill (with schema)
    call2 = loop._registry.register_native.call_args_list[1]
    assert call2[0][0] == SKILL_READ_TOOL_NAME
    assert call2[0][1] == SKILL_READ_TOOL_DESCRIPTION
    assert call2[0][2] == SKILL_READ_TOOL_SCHEMA
    assert isinstance(call2[0][3], ReadSkillHandler)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd d:/workbench/ai-agent/claw-agent
python -m pytest tests/agent/test_skills.py::test_register_skill_tools_adds_two_tools -v 2>&1
```

Expected: FAIL because `AgentLoop.register_skill_tools()` doesn't exist yet.

- [ ] **Step 3: Add `register_skill_tools()` to `AgentLoop`**

Add this method to `agent/loop.py` after `register_subagent_tool()`:

```python
def register_skill_tools(self) -> None:
    """Register the ``claw_list_skills`` and ``claw_read_skill`` native tools.

    These tools let the LLM (and sub-agents) discover and read bundled
    skills during a chat session.  Safe to call multiple times —
    ``register_native`` replaces in-place.
    """
    from agent.skills import (
        SKILL_LIST_TOOL_DESCRIPTION,
        SKILL_LIST_TOOL_NAME,
        SKILL_READ_TOOL_DESCRIPTION,
        SKILL_READ_TOOL_NAME,
        SKILL_READ_TOOL_SCHEMA,
        ListSkillsHandler,
        ReadSkillHandler,
    )

    self._registry.register_native(
        SKILL_LIST_TOOL_NAME,
        SKILL_LIST_TOOL_DESCRIPTION,
        {"type": "object", "properties": {}},
        ListSkillsHandler(),
    )
    logger.debug("claw_list_skills native tool registered")

    self._registry.register_native(
        SKILL_READ_TOOL_NAME,
        SKILL_READ_TOOL_DESCRIPTION,
        SKILL_READ_TOOL_SCHEMA,
        ReadSkillHandler(),
    )
    logger.debug("claw_read_skill native tool registered")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd d:/workbench/ai-agent/claw-agent
python -m pytest tests/agent/test_skills.py::test_register_skill_tools_adds_two_tools -v 2>&1
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

```bash
cd d:/workbench/ai-agent/claw-agent
pytest tests/ -v 2>&1 | tail -30
```

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
cd d:/workbench/ai-agent/claw-agent
git add agent/loop.py tests/agent/test_skills.py
git commit -m "feat: add register_skill_tools to AgentLoop

Registers claw_list_skills and claw_read_skill as native tools
on the ToolRegistry. Follows the same pattern as register_subagent_tool.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Wire `register_skill_tools()` in `cli/chat_cmd.py`

**Files:**
- Modify: `cli/chat_cmd.py`

- [ ] **Step 1: Insert call after `register_subagent_tool()`**

In `cli/chat_cmd.py`, after line 180 (`loop.register_subagent_tool()`):

```python
    # Register skill tools so the LLM can discover and read bundled
    # skills during the session.
    loop.register_skill_tools()
```

- [ ] **Step 2: Verify the module still imports and parses correctly**

```bash
cd d:/workbench/ai-agent/claw-agent
python -c "from cli.chat_cmd import register_chat_parser, run_chat_command; print('OK')"
```

Expected: `OK` printed, no import errors.

- [ ] **Step 3: Run full test suite**

```bash
cd d:/workbench/ai-agent/claw-agent
pytest tests/ -v 2>&1 | tail -30
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
cd d:/workbench/ai-agent/claw-agent
git add cli/chat_cmd.py
git commit -m "feat: wire register_skill_tools into chat command

Sub-agents automatically inherit these tools via the shared ToolRegistry.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Add a bundled skill to exercise the feature

**Files:**
- Create: `cli/skills/gateway-setup/SKILL.md`

- [ ] **Step 1: Create a gateway-setup skill**

```markdown
---
name: gateway-setup
description: "Connect the claw gateway to a messaging platform (WeChat, Telegram, Feishu)"
platforms: [linux, macos, windows]
---

# Gateway Setup Guide

The claw gateway connects the AI agent to messaging platforms so
you can interact with it through WeChat, Telegram, or Feishu.

## Step 1: Check current gateway status

Run:

```
claw gateway status
```

This shows which platforms are configured and whether MCP servers are running.

## Step 2: Add a messaging platform

### WeChat (Weixin)

```
claw gateway add weixin
```

Follow the QR code prompt to scan with your phone.

### Telegram (planned)

```
claw gateway add telegram
```

### Feishu / Lark (planned)

```
claw gateway add feishu
```

## Step 3: Configure MCP servers

```
claw gateway mcp
```

Select the servers you want the gateway to manage.
Available: SQLite, GitHub.

## Step 4: Start the gateway

```
claw gateway start
```

Add ``--verbose`` to see detailed logs.

## Step 5: Verify

Check the gateway status:

```
claw gateway status
```

The running platform and MCP servers should show as active.
```

- [ ] **Step 2: Run skills list test**

```bash
cd d:/workbench/ai-agent/claw-agent
python -c "
import json
from agent.skills import list_skills
skills = list_skills()
for s in skills:
    print(f'{s[\"name\"]}: {s[\"description\"]}')
"
```

Expected: Shows gateway-setup alongside the existing skills.

- [ ] **Step 3: Run full test suite**

```bash
cd d:/workbench/ai-agent/claw-agent
pytest tests/ -v 2>&1 | tail -30
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
cd d:/workbench/ai-agent/claw-agent
git add cli/skills/gateway-setup/SKILL.md
git commit -m "feat: add gateway-setup skill

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Manual smoke test (optional / verify in TUI)

- [ ] **Step 1: Run `claw chat -q` one-shot to verify tools show up**

```bash
cd d:/workbench/ai-agent/claw-agent
PYTHONPATH=. python -c "
import logging
logging.basicConfig(level=logging.DEBUG)

# Skip actual chat — just verify the registry has skill tools
from agent.config import load_agent_config
from agent.loop import AgentLoop
from agent.llm_client import LLMClient
from unittest.mock import MagicMock

cfg = load_agent_config()
# We can't fully create an LLMClient without real creds, but we can
# verify the tools exist by inspecting the ToolRegistry.
print('Skills module imports OK')
from agent.skills import list_skills
skills = list_skills()
print(f'Found {len(skills)} skills: {[s[\"name\"] for s in skills]}')
"
```

Expected: Prints skills list without errors.