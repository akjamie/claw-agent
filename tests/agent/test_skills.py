"""Tests for agent/skills.py — skill discovery and reading."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.skills import list_skills, read_skill


def test_list_skills_with_temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk

        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir
        try:
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
            assert result[0]["name"] == "alpha"
            assert result[0]["description"] == "Alpha skill"
            assert result[0]["platforms"] == ["linux"]
            assert result[1]["name"] == "beta"
            assert result[1]["platforms"] == []
        finally:
            sk._SKILLS_DIR = original


def test_list_skills_empty_dir():
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
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk

        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir
        try:
            (skills_dir / "not-a-skill").mkdir()
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


def test_read_skill_io_error_returns_none():
    """read_skill returns None when read_text raises OSError."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk

        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir
        try:
            (skills_dir / "locked").mkdir()
            (skills_dir / "locked" / "SKILL.md").write_text(
                "---\nname: locked\n---\n\nContent\n", encoding="utf-8",
            )
            with patch.object(Path, "read_text", side_effect=OSError("read error")):
                assert read_skill("locked") is None
        finally:
            sk._SKILLS_DIR = original


def test_list_skills_io_error_skips_unreadable():
    """list_skills skips a skill whose SKILL.md cannot be read."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk

        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir
        try:
            (skills_dir / "good").mkdir()
            (skills_dir / "good" / "SKILL.md").write_text(
                "---\nname: good\ndescription: Fine\n---\n\nContent\n",
                encoding="utf-8",
            )
            (skills_dir / "bad").mkdir()
            (skills_dir / "bad" / "SKILL.md").write_text(
                "---\nname: bad\n---\n\nNope\n", encoding="utf-8",
            )
            # Patch only the bad skill's read_text — match by parent dir name
            # so a path containing "bad" elsewhere (e.g. a temp dir) doesn't
            # produce false positives.
            original_read_text = Path.read_text
            def mock_read_text(self, *a, **kw):
                if self.parent.name == "bad" and self.name == "SKILL.md":
                    raise OSError("read error")
                return original_read_text(self, *a, **kw)
            with patch.object(Path, "read_text", mock_read_text):
                result = list_skills()
                assert len(result) == 1
                assert result[0]["name"] == "good"
        finally:
            sk._SKILLS_DIR = original


from agent.skills import (
    SKILL_LIST_TOOL_NAME,
    SKILL_LIST_TOOL_DESCRIPTION,
    SKILL_LIST_TOOL_SCHEMA,
    SKILL_READ_TOOL_NAME,
    SKILL_READ_TOOL_DESCRIPTION,
    SKILL_READ_TOOL_SCHEMA,
    ListSkillsHandler,
    ReadSkillHandler,
)


def test_register_skill_tools_adds_two_tools():
    """register_skill_tools registers claw_list_skills and claw_read_skill."""
    from unittest.mock import MagicMock
    from agent.loop import AgentLoop
    from agent.config import AgentConfig

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

    assert loop._registry.register_native.call_count == 2

    call1 = loop._registry.register_native.call_args_list[0]
    assert call1[0][0] == SKILL_LIST_TOOL_NAME
    assert call1[0][1] == SKILL_LIST_TOOL_DESCRIPTION
    assert call1[0][2] == SKILL_LIST_TOOL_SCHEMA
    assert isinstance(call1[0][3], ListSkillsHandler)

    call2 = loop._registry.register_native.call_args_list[1]
    assert call2[0][0] == SKILL_READ_TOOL_NAME
    assert call2[0][1] == SKILL_READ_TOOL_DESCRIPTION
    assert call2[0][2] == SKILL_READ_TOOL_SCHEMA
    assert isinstance(call2[0][3], ReadSkillHandler)


def test_list_skills_malformed_frontmatter():
    """list_skills handles malformed YAML frontmatter gracefully."""
    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)
        import agent.skills as sk

        original = sk._SKILLS_DIR
        sk._SKILLS_DIR = skills_dir
        try:
            (skills_dir / "borked").mkdir()
            (skills_dir / "borked" / "SKILL.md").write_text(
                "---\nname: borked\ninvalid: [unclosed\n---\n\nContent\n",
                encoding="utf-8",
            )
            # Should still be listed (no crash) with name from dir name
            result = list_skills()
            assert len(result) >= 1
            assert result[0]["name"] == "borked"
        finally:
            sk._SKILLS_DIR = original