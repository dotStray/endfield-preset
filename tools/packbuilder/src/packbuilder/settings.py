"""Read hand corrections strictly; a misspelt rule must never look successfully applied."""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

from packbuilder.files import BuildError, reject_links
from packbuilder.names import is_valid_id

OVERRIDE_KEYS = {"notes", "join", "parents", "partOf", "displayNames", "aliases", "exclude", "ignoredHashes", "retired", "allowShrink"}


@dataclass
class Overrides:
    join: dict[str, str] = field(default_factory=dict)
    parents: dict[str, str | None] = field(default_factory=dict)
    part_of: dict[str, str] = field(default_factory=dict)
    display_names: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    exclude: list[str] = field(default_factory=list)
    ignored_hashes: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    allow_shrink: list[str] = field(default_factory=list)


def character_id(value: object) -> bool:
    return isinstance(value, str) and is_valid_id(value)


def source_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def part_targets(parts: dict[str, str]) -> dict[str, str]:
    """Flatten model-part ownership, rejecting loops before any source requests."""
    direct = {source_name(name): target for name, target in parts.items()}
    result = {}
    for name, target in direct.items():
        visited = {name}
        while source_name(target) in direct:
            key = source_name(target)
            if key in visited:
                raise BuildError("overrides partOf contains a cycle")
            visited.add(key)
            target = direct[key]
        result[name] = target
    return result


def load_overrides(path: pathlib.Path) -> Overrides:
    reject_links(path)

    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise BuildError(f"{path}: Duplicate key {name!r}")
            result[name] = value
        return result

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs) if path.exists() else {}
    except (OSError, UnicodeError, ValueError) as error:
        raise BuildError(f"Cannot read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise BuildError(f"{path} must be a JSON object")
    unknown = sorted(set(raw) - OVERRIDE_KEYS)
    if unknown:
        raise BuildError(f"{path}: unknown key(s) {', '.join(unknown)}")
    if not isinstance(raw.get("notes", ""), str):
        raise BuildError(f"{path}: notes must be text")

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def mapping(key, *, ids=False, nullable=False):
        value = raw.get(key, {})
        if not isinstance(value, dict) or not all(text(name) and (nullable and target is None or text(target)) for name, target in value.items()):
            raise BuildError(f"{path}: {key} must map names to nonempty text" + (" or null" if nullable else ""))
        if ids and not all(character_id(target) for target in value.values() if target is not None):
            raise BuildError(f"{path}: {key} targets must be valid character names")
        return value

    def names(key):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(text(name) for name in value):
            raise BuildError(f"{path}: {key} must be a list of nonempty names")
        return value

    result = Overrides(join=mapping("join", ids=True), parents=mapping("parents", ids=True, nullable=True),
                       part_of=mapping("partOf", ids=True), display_names=mapping("displayNames"),
                       exclude=names("exclude"), retired=names("retired"), allow_shrink=names("allowShrink"))
    aliases = raw.get("aliases", {})
    if not isinstance(aliases, dict) or not all(character_id(name) and isinstance(values, list) and all(text(value) for value in values) for name, values in aliases.items()):
        raise BuildError(f"{path}: aliases must map character names to lists of nonempty names")
    result.aliases = aliases
    for key, values in [("displayNames", result.display_names), ("parents", result.parents),
                        ("exclude", result.exclude), ("retired", result.retired), ("allowShrink", result.allow_shrink)]:
        if not all(character_id(name) for name in values):
            raise BuildError(f"{path}: {key} keys must be valid character or source names")
    result.ignored_hashes = names("ignoredHashes")
    if not all(re.fullmatch(r"[0-9a-fA-F]{8}(?:[0-9a-fA-F]{8})?", value) for value in result.ignored_hashes):
        raise BuildError(f"{path}: ignoredHashes must contain 8- or 16-digit hexadecimal hashes")
    result.ignored_hashes = sorted({value.lower() for value in result.ignored_hashes})
    joined = {}
    for table in (result.join, result.part_of):
        for name, target in table.items():
            normalized = source_name(name)
            if not normalized or normalized in joined and joined[normalized] != target:
                raise BuildError(f"{path}: conflicting or empty source name {name!r}")
            joined[normalized] = target
    if set(result.part_of) & set(result.exclude):
        raise BuildError(f"{path}: a source cannot be both excluded and assigned through partOf")
    part_targets(result.part_of)
    return result
