from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any


MAX_SKILL_BYTES = 512 * 1024
_KEBAB_CASE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk)-(?:live|test)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(
        r"(?im)^\s*(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\s*[:=]\s*['\"]?[^\s'\"<>{}]{8,}"
    ),
)


class SkillValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedSkill:
    name: str
    description: str
    body: str
    content: str


def _frontmatter(content: str) -> tuple[dict[str, str], str]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError("SKILL.md must start with YAML frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise SkillValidationError("SKILL.md frontmatter is not closed") from exc
    if end == 1:
        raise SkillValidationError("SKILL.md frontmatter is empty")
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)", line)
        if not match:
            raise SkillValidationError("SKILL.md frontmatter must use scalar key/value fields")
        key, value = match.groups()
        if key in values:
            raise SkillValidationError(f"Duplicate frontmatter field: {key}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value.strip()
    return values, "\n".join(lines[end + 1 :]).strip()


def validate_skill_name(name: str) -> str:
    if not isinstance(name, str) or not _KEBAB_CASE.fullmatch(name) or len(name) > 64:
        raise SkillValidationError("Skill name must be kebab-case and at most 64 characters")
    return name


def validate_skill(content: str | bytes, expected_name: str | None = None) -> ValidatedSkill:
    if isinstance(content, bytes):
        raw = content
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillValidationError("SKILL.md must be UTF-8") from exc
    elif isinstance(content, str):
        text = content
        raw = text.encode("utf-8")
    else:
        raise SkillValidationError("SKILL.md content must be text")
    if not raw or len(raw) > MAX_SKILL_BYTES:
        raise SkillValidationError("SKILL.md must be between 1 byte and 512 KiB")
    if "\x00" in text:
        raise SkillValidationError("SKILL.md contains a NUL byte")
    metadata, body = _frontmatter(text)
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    validate_skill_name(name)
    if expected_name is not None and name != validate_skill_name(expected_name):
        raise SkillValidationError("Skill name does not match the requested name")
    if not description or len(description) > 1024:
        raise SkillValidationError("Skill description must be 1 to 1024 characters")
    if "\n" in description or description in {"|", ">"}:
        raise SkillValidationError("Skill description must be a scalar string")
    if not body:
        raise SkillValidationError("SKILL.md body must not be empty")
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise SkillValidationError("SKILL.md appears to contain a secret")
    return ValidatedSkill(name=name, description=description, body=body, content=text)


def build_skill_zip(content: str | bytes, expected_name: str | None = None) -> bytes:
    skill = validate_skill(content, expected_name)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(f"{skill.name}/SKILL.md")
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o600 << 16
        archive.writestr(info, skill.content.encode("utf-8"))
    return output.getvalue()


class SkillManager:
    def __init__(self, dashboard: Any):
        self.dashboard = dashboard
        self._drafts: dict[str, str] = {}

    @staticmethod
    def _entries(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            for key in ("skills", "items", "data"):
                if key in value:
                    return SkillManager._entries(value[key])
            return [dict(value)] if value.get("name") else []
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        return []

    async def list(self) -> list[dict[str, Any]]:
        getter = getattr(self.dashboard, "list_skills", None)
        if not callable(getter):
            return []
        entries = self._entries(await getter())
        result = []
        for entry in entries:
            item = dict(entry)
            item["readonly"] = bool(
                item.get("readonly")
                or item.get("source_type") in {"plugin", "workspace"}
                or item.get("plugin_name")
            )
            result.append(item)
        return result

    async def list_skills(self) -> list[dict[str, Any]]:
        return await self.list()

    def save_draft(self, name: str, skill_md: str) -> ValidatedSkill:
        skill = validate_skill(skill_md, validate_skill_name(name))
        self._drafts[name] = skill.content
        return skill

    def get_draft(self, name: str) -> str | None:
        return self._drafts.get(validate_skill_name(name))

    def list_drafts(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "content": content}
            for name, content in sorted(self._drafts.items())
        ]

    def delete_draft(self, name: str) -> bool:
        return self._drafts.pop(validate_skill_name(name), None) is not None

    async def _entry(self, name: str) -> dict[str, Any] | None:
        name = validate_skill_name(name)
        for entry in await self.list():
            if str(entry.get("name") or "") == name:
                return entry
        return None

    async def _ensure_mutable(self, name: str) -> None:
        entry = await self._entry(name)
        if entry is not None and entry["readonly"]:
            raise PermissionError("Plugin-owned or workspace Skill is read-only")

    async def create(self, name: str, skill_md: str) -> Any:
        name = validate_skill_name(name)
        entry = await self._entry(name)
        if entry is not None:
            if entry["readonly"]:
                raise PermissionError("Plugin-owned or workspace Skill is read-only")
            raise SkillValidationError("Skill already exists")
        archive = build_skill_zip(skill_md, name)
        return await self.dashboard.upload_skill(archive, f"{name}.zip")

    async def create_skill(self, name: str, skill_md: str) -> Any:
        return await self.create(name, skill_md)

    async def read(self, name: str) -> str:
        name = validate_skill_name(name)
        value = await self.dashboard.read_skill_file(name, "SKILL.md")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("content", "text", "data"):
                if isinstance(value.get(key), str):
                    return value[key]
        raise RuntimeError("Dashboard returned an invalid SKILL.md response")

    async def read_skill(self, name: str) -> str:
        return await self.read(name)

    async def update(self, name: str, skill_md: str) -> Any:
        name = validate_skill_name(name)
        await self._ensure_mutable(name)
        skill = validate_skill(skill_md, name)
        return await self.dashboard.update_skill_file(name, skill.content, "SKILL.md")

    async def update_skill(self, name: str, skill_md: str) -> Any:
        return await self.update(name, skill_md)

    async def set_enabled(self, name: str, enabled: bool) -> Any:
        name = validate_skill_name(name)
        await self._ensure_mutable(name)
        return await self.dashboard.set_skill_enabled(name, enabled)

    async def enable(self, name: str) -> Any:
        return await self.set_enabled(name, True)

    async def disable(self, name: str) -> Any:
        return await self.set_enabled(name, False)

    async def enable_skill(self, name: str) -> Any:
        return await self.enable(name)

    async def disable_skill(self, name: str) -> Any:
        return await self.disable(name)
