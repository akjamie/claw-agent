# Skill Tools for Agent / Sub-Agent

**Date:** 2026-06-19
**Status:** Approved design

## Problem

The `claw-agent` ships with bundled skills — markdown files under `cli/skills/<name>/SKILL.md` — that contain step-by-step guides for common tasks (e.g., OpenRouter setup, Nous Portal setup). These skills are currently only accessible via the `claw skills list/show` CLI commands, meant for human reading. The AI agent (and its sub-agents) has no way to discover or read skills during a chat session, limiting its ability to follow guided instructions autonomously.

## Design

### Approach

Register two native tools on the `ToolRegistry` following the same pattern as `claw_subagent`:

1. **`claw_list_skills`** — the LLM calls this (zero parameters) to discover available skills and their descriptions. Returns a JSON array of `{name, description, platforms}` objects parsed from the YAML frontmatter of each skill's `SKILL.md`.

2. **`claw_read_skill`** — takes a `name` parameter, returns the full markdown body of the requested skill (frontmatter stripped). Returns a descriptive error message when the skill does not exist.

### Rationale

- **Zero context overhead** — skills consume no tokens unless the LLM explicitly calls one of the tools, preserving the full context window for conversation.
- **Architectural consistency** — uses the exact same `ToolRegistry.register_native()` mechanism as sub-agents, with the same `_NEVER_PARALLEL` safety class.
- **Sub-agent inheritance** — sub-agents share the parent loop's `ToolRegistry`, so skill tools are automatically available to child loops at no additional cost.
- **Scales infinitely** — adding a new skill is a matter of dropping a `SKILL.md` file; no code changes needed.

### Components

#### `agent/skills.py` (new)

Data-access layer for skill discovery and reading. Exposes two public functions:

- `list_skills() -> List[Dict[str, str]]` — scans `cli/skills/<name>/SKILL.md` for each subdirectory, parses the YAML frontmatter, returns a list of metadata dicts.
- `read_skill(name: str) -> Optional[str]` — reads the full content of a named skill, stripping the YAML frontmatter. Returns `None` when the skill is not found.

The skills directory is resolved relative to the `agent/` package path (`Path(__file__).parent.parent / "cli" / "skills"`), which works for both development installs and installed packages.

Also contains two native-tool handler callables used as `ToolRegistry` native handlers.

#### `agent/loop.py` — `AgentLoop.register_skill_tools()` (new method)

Registers `claw_list_skills` and `claw_read_skill` on the loop's `ToolRegistry`. Called from `cli/chat_cmd.py` right after `register_subagent_tool()`.

### Tool Schemas

**`claw_list_skills`:**
```json
{
  "type": "object",
  "properties": {}
}
```

**`claw_read_skill`:**
```json
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "The name of the skill to read (e.g. 'openrouter-setup')"
    }
  },
  "required": ["name"]
}
```

### Error Handling

| Scenario | Behaviour |
|---|---|
| No skills directory exists | `list_skills()` returns an empty list |
| Skill not found by `read_skill()` | Returns `None` → tool handler returns `[skill_error]` message |
| Malformed YAML frontmatter | Parser returns empty metadata dict for that skill; skill is still listed with just its name |
| Read error (permissions, I/O) | Returns `None` → same error path |

### Sub-agent Access

No additional work needed. Sub-agents share the parent loop's `ToolRegistry` (see `agent/subagent.py` line where `child_dispatcher` is constructed with `parent._registry`), so any native tool registered on the parent is automatically available to child loops.

### Non-goals

- **User-defined skills in `~/.claw/skills/`** — not implemented. The data layer can be extended to merge a user directory with the bundled directory in a future iteration.
- **Auto-injection into system prompts** — explicitly rejected in favour of the tool-based approach to avoid context waste.
- **Executable/scripting skills** — skills remain markdown documentation for the LLM to read and follow, not scripts to execute.