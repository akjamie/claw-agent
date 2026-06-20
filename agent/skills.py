"""
Skill discovery and reading — data-access layer for claw-agent skill tools.

This module provides the low-level functions and handler classes that the
agent loop uses to discover installed skills (``list_skills``) and to read
the body text of a skill by name (``read_skill``).

Skills are stored on disk as ``<skill-name>/SKILL.md`` files under a
configurable skills directory.  Each ``SKILL.md`` may contain YAML frontmatter
delimited by ``---`` lines:

.. code-block:: markdown

    ---
    name: my-skill
    description: "Does a thing"
    platforms: [linux, macos]
    ---

    # My Skill

    Step 1. ...

The frontmatter is parsed by the internal ``_parse_frontmatter()`` helper.
The handler classes (``ListSkillsHandler``, ``ReadSkillHandler``) follow the
same callable-protocol pattern as :class:`agent.subagent.SubAgentRunner`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

__all__ = [
    "list_skills",
    "read_skill",
    "SKILL_LIST_TOOL_NAME",
    "SKILL_LIST_TOOL_DESCRIPTION",
    "SKILL_LIST_TOOL_SCHEMA",
    "SKILL_READ_TOOL_NAME",
    "SKILL_READ_TOOL_DESCRIPTION",
    "SKILL_READ_TOOL_SCHEMA",
    "ListSkillsHandler",
    "ReadSkillHandler",
]

logger = logging.getLogger(__name__)


# ── Module-level state ─────────────────────────────────────────────────────
#
# _SKILLS_DIR is the on-disk root for skill directories.  Starts as None and
# is lazily resolved to ``<repo-root>/cli/skills`` on first access.
# Tests override this attribute directly::
#
#     import agent.skills as sk
#     original = sk._SKILLS_DIR
#     sk._SKILLS_DIR = tmp_path
#     try:
#         …
#     finally:
#         sk._SKILLS_DIR = original

_SKILLS_DIR: Optional[Path] = None

# Regex that matches optional YAML frontmatter delimited by --- lines.
# Group 1 = raw frontmatter text (everything between the delimiters).
# Group 2 = body text (everything after the closing delimiter + blank lines).
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n*\n?(.*)", re.DOTALL)


# ── Tool metadata constants ─────────────────────────────────────────────────

SKILL_LIST_TOOL_NAME = "claw_list_skills"

SKILL_LIST_TOOL_DESCRIPTION = (
    "List all available skills with their names, descriptions, and target "
    "platforms.  Use this to discover which skills are installed on the "
    "current system."
)

SKILL_LIST_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {},
}

SKILL_READ_TOOL_NAME = "claw_read_skill"

SKILL_READ_TOOL_DESCRIPTION = (
    "Read the full body content of a named skill.  The returned text has "
    "the YAML frontmatter stripped so the LLM sees only the skill's "
    "instructions."
)

SKILL_READ_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The name of the skill to read.",
        },
    },
    "required": ["name"],
}


# ── Internal helpers ───────────────────────────────────────────────────────

def _resolve_skills_dir() -> Path:
    """Resolve the path to the skills directory.

    Returns the currently-assigned ``_SKILLS_DIR`` (which may have been
    overridden by tests) or, if it is still ``None``, resolves it relative
    to this module's location.
    """
    global _SKILLS_DIR  # noqa: PLW0603  — tests reassign this attribute
    if _SKILLS_DIR is None:
        _SKILLS_DIR = Path(__file__).resolve().parent.parent / "cli" / "skills"
    return _SKILLS_DIR


def _parse_frontmatter(content: str) -> Dict[str, Any]:
    """Return the frontmatter dict from *content*, or an empty dict.

    Expects the content to begin with a ``---``-delimited YAML block
    matching ``_FRONTMATTER_RE``.  Returns an empty dict when no
    frontmatter is present.
    """
    m = _FRONTMATTER_RE.match(content)
    if m is None:
        return {}
    raw = m.group(1)
    if not raw.strip():
        return {}
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse skill frontmatter: %s", exc)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


# ── Public API ─────────────────────────────────────────────────────────────

def list_skills() -> List[Dict[str, Any]]:
    """Discover installed skills and return their metadata.

    Scans ``<skills-dir>/<name>/SKILL.md`` for each sub-directory that
    contains a ``SKILL.md`` file.  Returns a list of dicts sorted by skill
    name, each with keys ``name``, ``description``, and ``platforms``.

    Returns an empty list if the skills directory does not exist.
    """
    skills_dir = _resolve_skills_dir()
    if not skills_dir.is_dir():
        return []

    result: List[Dict[str, Any]] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        content = skill_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        result.append({
            "name": fm.get("name", entry.name),
            "description": fm.get("description", ""),
            "platforms": fm.get("platforms", []),
        })
    return result


def read_skill(name: str) -> Optional[str]:
    """Return the body text of the skill named *name*, or ``None``.

    The returned body has the YAML frontmatter stripped.  If the skill file
    contains no frontmatter, the full content is returned as-is.
    """
    skills_dir = _resolve_skills_dir()
    skill_path = skills_dir / name / "SKILL.md"
    if not skill_path.is_file():
        return None
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(content)
    if m:
        return m.group(2)
    return content


# ── Native-tool handler classes ────────────────────────────────────────────

class ListSkillsHandler:
    """Callable handler for the ``claw_list_skills`` native tool.

    Usage::

        handler = ListSkillsHandler()
        result = handler({"some": "args"})
    """

    @staticmethod
    def __call__(args: dict) -> str:  # noqa: ARG004
        skills = list_skills()
        if not skills:
            return "No skills found."

        lines = ["Available skills:"]
        for skill in skills:
            name = skill["name"]
            desc = skill.get("description", "")
            platforms = skill.get("platforms", [])
            if platforms:
                lines.append(f"  - {name}: {desc}  [{', '.join(platforms)}]")
            else:
                lines.append(f"  - {name}: {desc}")
        return "\n".join(lines)


class ReadSkillHandler:
    """Callable handler for the ``claw_read_skill`` native tool.

    Usage::

        handler = ReadSkillHandler()
        result = handler({"name": "my-skill"})
    """

    @staticmethod
    def __call__(args: dict) -> str:
        name = args.get("name", "").strip()
        if not name:
            return "[skill_error] 'name' argument is required."
        body = read_skill(name)
        if body is None:
            return f"[skill_error] skill '{name}' not found."
        return body