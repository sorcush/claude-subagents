"""Hermes native plugin registration for cursor-coder and cursor-reviewer."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _load_skill(path: Path, expected_name: str) -> tuple[str, str, dict]:
    if not path.is_file():
        raise ValueError(f"missing skill file for {expected_name}: {path}")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"unable to read skill file for {expected_name}: {path}"
        ) from exc

    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(
            f"invalid frontmatter delimiters for skill {expected_name}: {path}"
        )

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"invalid YAML frontmatter for skill {expected_name}: {path}"
        ) from exc

    if not isinstance(frontmatter, dict):
        raise ValueError(
            f"frontmatter must be a YAML mapping for skill {expected_name}: {path}"
        )

    if "name" not in frontmatter or "description" not in frontmatter:
        raise ValueError(
            f"missing required frontmatter fields for skill {expected_name}: {path}"
        )

    name = frontmatter["name"]
    description = frontmatter["description"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"invalid skill name in frontmatter for {expected_name}: {path}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"invalid skill description in frontmatter for {expected_name}: {path}"
        )

    name = name.strip()
    description = description.strip()
    if name != expected_name:
        raise ValueError(
            f"skill name mismatch for {expected_name}: expected {expected_name}, got {name}"
        )

    return name, description, frontmatter


def register(ctx) -> None:
    root = Path(__file__).resolve().parent
    for bare in ("cursor-coder", "cursor-reviewer"):
        path = root / "hermes" / "skills" / bare / "SKILL.md"
        try:
            name, description, frontmatter = _load_skill(path, bare)
            ctx.register_skill(name, path, description, frontmatter)
        except Exception as exc:
            raise ValueError(f"failed to register skill {bare}: {exc}") from exc
