"""claw skills — list and view bundled skills.

Each skill is a directory under cli/skills/ containing a SKILL.md
file with YAML frontmatter.  Mirrors hermes-agent's skill format.
"""

import re
from pathlib import Path
from typing import List, Dict, Optional

SKILLS_DIR = Path(__file__).parent / "skills"


def _parse_frontmatter(content: str) -> Dict[str, str]:
    """Minimal YAML frontmatter parser — returns {key: value, ...}."""
    meta: Dict[str, str] = {}
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return meta
    for line in m.group(1).splitlines():
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta


def _find_skills() -> List[Dict[str, str]]:
    """Scan the bundled skills directory for SKILL.md files."""
    skills: List[Dict[str, str]] = []
    if not SKILLS_DIR.is_dir():
        return skills
    for entry in sorted(SKILLS_DIR.iterdir()):
        skill_file = entry / "SKILL.md"
        if skill_file.is_file():
            content = skill_file.read_text(encoding="utf-8")
            meta = _parse_frontmatter(content)
            meta["_path"] = str(skill_file)
            meta["_name"] = meta.get("name", entry.name)
            skills.append(meta)
    return skills


def _show_skill_content(name: str) -> Optional[str]:
    """Return the full content of a skill's SKILL.md (without frontmatter)."""
    skill_dir = SKILLS_DIR / name
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return None
    content = skill_file.read_text(encoding="utf-8")
    body = re.sub(r"^---.*?---\s*", "", content, count=1, flags=re.DOTALL)
    return body.strip()


def run_skills_command(args) -> bool:
    if args.skills_action == "list" or args.skills_action is None:
        skills = _find_skills()
        if not skills:
            print("No skills found.")
            return True
        print()
        print("Available skills:")
        print("-" * 60)
        for s in skills:
            name = s.get("_name", "?")
            desc = s.get("description", "")
            print(f"  {name}")
            if desc:
                print(f"    {desc}")
            print()
        print_info = "Use: claw skills show <name>"
        print(print_info)
        return True

    elif args.skills_action == "show":
        content = _show_skill_content(args.name)
        if content is None:
            print(f"Skill '{args.name}' not found.")
            return False
        print()
        print(content)
        return True

    return False
