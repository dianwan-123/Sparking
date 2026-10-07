from __future__ import annotations

import asyncio
import io
import unittest
import zipfile

from src.skill_manager import SkillManager, SkillValidationError, build_skill_zip, validate_skill


VALID = """---
name: weather-helper
description: Looks up weather safely
---
# Instructions
Use the configured weather source.
"""


class FakeDashboard:
    def __init__(self, skills=None):
        self.calls = []
        self.skills = skills or []

    async def list_skills(self):
        self.calls.append(("list",))
        return self.skills

    async def upload_skill(self, archive, filename):
        self.calls.append(("upload", archive, filename))
        return {"ok": True}

    async def read_skill_file(self, name, path):
        self.calls.append(("read", name, path))
        return {"content": VALID}

    async def update_skill_file(self, name, content, path):
        self.calls.append(("update", name, content, path))
        return {"ok": True}

    async def set_skill_enabled(self, name, enabled):
        self.calls.append(("enabled", name, enabled))
        return {"ok": True}


class SkillManagerTests(unittest.TestCase):
    def test_validate_and_build_memory_zip(self):
        skill = validate_skill(VALID)
        self.assertEqual(skill.name, "weather-helper")
        payload = build_skill_zip(VALID)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["weather-helper/SKILL.md"])
            self.assertEqual(archive.read(archive.namelist()[0]).decode(), VALID)

    def test_rejects_invalid_structure_and_name(self):
        invalid = [
            "name: x\nbody",
            "---\nname: Bad_Name\ndescription: d\n---\nbody",
            "---\nname: x\ndescription: d\n---\n",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SkillValidationError):
                validate_skill(value)

    def test_rejects_size_and_coarse_secrets(self):
        secret = VALID + "\napi_key = sk-live-abcdefghijklmnopqrstuvwxyz"
        with self.assertRaises(SkillValidationError):
            validate_skill(secret)
        with self.assertRaises(SkillValidationError):
            validate_skill(b"x" * (512 * 1024 + 1))

    def test_manager_create_read_update_toggle(self):
        dashboard = FakeDashboard()
        manager = SkillManager(dashboard)
        asyncio.run(manager.create("weather-helper", VALID))
        self.assertEqual(asyncio.run(manager.read("weather-helper")), VALID)
        asyncio.run(manager.update("weather-helper", VALID))
        asyncio.run(manager.disable("weather-helper"))
        self.assertEqual(dashboard.calls[1][0], "upload")
        self.assertEqual(dashboard.calls[-1], ("enabled", "weather-helper", False))

    def test_all_name_paths_validate(self):
        manager = SkillManager(FakeDashboard())
        for operation in (
            lambda: manager.read("../bad"),
            lambda: manager.update("../bad", VALID),
            lambda: manager.enable("../bad"),
        ):
            with self.assertRaises(SkillValidationError):
                asyncio.run(operation())

    def test_plugin_owned_skill_is_readonly(self):
        dashboard = FakeDashboard([
            {"name": "weather-helper", "source_type": "plugin", "plugin_name": "owner"}
        ])
        manager = SkillManager(dashboard)
        listed = asyncio.run(manager.list())
        self.assertTrue(listed[0]["readonly"])
        self.assertEqual(asyncio.run(manager.read("weather-helper")), VALID)
        with self.assertRaises(PermissionError):
            asyncio.run(manager.update("weather-helper", VALID))
        with self.assertRaises(PermissionError):
            asyncio.run(manager.disable("weather-helper"))

    def test_in_memory_drafts(self):
        manager = SkillManager(FakeDashboard())
        manager.save_draft("weather-helper", VALID)
        self.assertEqual(manager.get_draft("weather-helper"), VALID)
        self.assertEqual(manager.list_drafts()[0]["name"], "weather-helper")
        self.assertTrue(manager.delete_draft("weather-helper"))

    def test_expected_name_must_match(self):
        with self.assertRaises(SkillValidationError):
            build_skill_zip(VALID, "different-skill")


if __name__ == "__main__":
    unittest.main()
