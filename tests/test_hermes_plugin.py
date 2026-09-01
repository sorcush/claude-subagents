#!/usr/bin/env python3
"""Hermes plugin manifest and skill registration tests."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class FakeContext:
    def __init__(self, *, has_superpowers: bool = True) -> None:
        self.skills: list[tuple[str, Path, str, dict]] = []
        self._has_superpowers = has_superpowers

    def has_plugin(self, plugin_id: str) -> bool:
        if plugin_id == "superpowers":
            return self._has_superpowers
        return False

    def register_skill(
        self,
        name: str,
        path: Path | str,
        description: str = "",
        frontmatter: dict | None = None,
    ) -> None:
        skill_path = Path(path)
        if not skill_path.is_file():
            raise FileNotFoundError(path)
        self.skills.append((name, skill_path, description, dict(frontmatter or {})))


class HermesPluginManifestTests(unittest.TestCase):
    def test_plugin_yaml_exists_and_matches_claude_version(self) -> None:
        plugin_yaml = REPO_ROOT / "plugin.yaml"
        plugin_json = REPO_ROOT / ".claude-plugin" / "plugin.json"
        self.assertTrue(plugin_yaml.is_file())

        manifest = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
        claude_version = yaml.safe_load(
            f"version: {__import__('json').loads(plugin_json.read_text())['version']}"
        )["version"]
        self.assertEqual(manifest["version"], claude_version)
        self.assertEqual(manifest["name"], "claude-subagents")
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["api_version"], 1)
        self.assertIn("coder_model", manifest["config_schema"])
        self.assertIn("reviewer_model", manifest["config_schema"])
        self.assertTrue(manifest["config_schema"]["coder_model"]["required"])
        self.assertTrue(manifest["config_schema"]["reviewer_model"]["required"])

    def test_manifest_declares_superpowers_dependency(self) -> None:
        manifest = yaml.safe_load(
            (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        )
        requires = manifest["requires_plugins"]
        self.assertEqual(len(requires), 1)
        self.assertEqual(requires[0]["id"], "superpowers")
        self.assertEqual(requires[0]["version_range"], ">=6.3.0,<7.0.0")


class HermesPluginRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if "claude_subagents_plugin" in sys.modules:
            del sys.modules["claude_subagents_plugin"]
        spec = importlib.util.spec_from_file_location(
            "claude_subagents_plugin", REPO_ROOT / "__init__.py"
        )
        assert spec and spec.loader
        self.plugin = importlib.util.module_from_spec(spec)
        sys.modules["claude_subagents_plugin"] = self.plugin
        spec.loader.exec_module(self.plugin)

    def test_register_both_skills(self) -> None:
        ctx = FakeContext()
        self.plugin.register(ctx)

        names = [name for name, *_ in ctx.skills]
        self.assertEqual(names, ["cursor-coder", "cursor-reviewer"])
        for name, path, description, frontmatter in ctx.skills:
            self.assertTrue(str(path).endswith("SKILL.md"))
            self.assertTrue(description)
            self.assertEqual(frontmatter["name"], name)

    def test_register_succeeds_in_doctor_style_isolation(self) -> None:
        ctx = FakeContext(has_superpowers=False)
        self.plugin.register(ctx)
        self.assertEqual(
            [name for name, *_ in ctx.skills],
            ["cursor-coder", "cursor-reviewer"],
        )

    def test_missing_skill_file_fails_closed_through_register(self) -> None:
        ctx = FakeContext()
        skill = REPO_ROOT / "hermes" / "skills" / "cursor-coder" / "SKILL.md"
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin.register(ctx)
        self.assertIn("failed to register skill cursor-coder", str(ctx_err.exception))
        self.assertEqual(ctx.skills, [])

    def test_malformed_yaml_fails_through_register(self) -> None:
        ctx = FakeContext()
        with patch.object(
            yaml,
            "safe_load",
            side_effect=yaml.YAMLError("invalid yaml"),
        ):
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin.register(ctx)
        self.assertIn("failed to register skill cursor-coder", str(ctx_err.exception))
        cause = ctx_err.exception.__cause__
        assert cause is not None
        self.assertIn("invalid YAML frontmatter", str(cause))
        self.assertIsInstance(cause.__cause__, yaml.YAMLError)
        self.assertEqual(ctx.skills, [])

    def test_register_skill_failure_names_skill(self) -> None:
        ctx = FakeContext()
        with patch.object(
            ctx,
            "register_skill",
            side_effect=RuntimeError("registration rejected"),
        ):
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin.register(ctx)
        self.assertIn("failed to register skill cursor-coder", str(ctx_err.exception))
        self.assertIsInstance(ctx_err.exception.__cause__, RuntimeError)
        self.assertEqual(ctx.skills, [])

    def test_malformed_frontmatter_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text("# no frontmatter\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.plugin._load_skill(path, "cursor-coder")

    def test_inline_closing_delimiter_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\nname: cursor-coder\ndescription: ok\n--- body\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin._load_skill(path, "cursor-coder")
            self.assertIn("invalid frontmatter delimiters", str(ctx_err.exception))

    def test_null_description_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\nname: cursor-coder\ndescription:\n---\n\n# Body\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin._load_skill(path, "cursor-coder")
            self.assertIn("invalid skill description", str(ctx_err.exception))

    def test_empty_name_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\nname: ''\ndescription: ok\n---\n\n# Body\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin._load_skill(path, "cursor-coder")
            self.assertIn("invalid skill name", str(ctx_err.exception))

    def test_non_mapping_frontmatter_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text("---\n- one\n---\n\n# Body\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin._load_skill(path, "cursor-coder")
            self.assertIn("YAML mapping", str(ctx_err.exception))

    def test_skill_name_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\nname: wrong-name\ndescription: ok\n---\n\n# Body\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx_err:
                self.plugin._load_skill(path, "cursor-coder")
            self.assertIn("skill name mismatch", str(ctx_err.exception))


if __name__ == "__main__":
    unittest.main()
