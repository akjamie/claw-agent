"""Tests for agent/skills.py — skill discovery and reading."""

import tempfile
from pathlib import Path

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