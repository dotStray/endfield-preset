"""Readable pack names are pinned to Enka source IDs so users' folders stay stable."""

from __future__ import annotations

import re
import copy
from dataclasses import dataclass, field

from packbuilder.files import BuildError
from packbuilder.settings import Overrides
from packbuilder.manual import Manual, add_characters
from packbuilder.names import is_valid_id, pascal

ELEMENTS = {"Physical": "Physical", "Fire": "Heat", "Cryst": "Cryo", "Pulse": "Electric", "Natural": "Nature"}
CLASSES = {"VANGUARD": "Vanguard", "DEFENDER": "Defender", "SUPPORTER": "Supporter", "ASSAULT": "Striker", "GUARD": "Guard", "CASTER": "Caster"}
WEAPONS = {"Sword": "Sword", "Claymores": "Greatsword", "Lance": "Polearm", "Pistol": "Handcannon", "Wand": "Arts Unit"}


@dataclass
class Details:
    left_out: dict[str, tuple[str, str]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    migrations: dict[str, str] = field(default_factory=dict)
    overrides: Overrides = field(default_factory=Overrides)


def assemble(avatars: dict, locs: dict, previous: list[dict], config: dict, overrides: Overrides, hand: Manual | None = None, *, details: Details | None = None, saved_names: dict | None = None) -> tuple[dict, list[dict]]:
    details = details if details is not None else Details()
    details.left_out.clear()
    details.sources.clear()
    details.migrations.clear()
    details.names = dict(saved_names or {})
    if not all(isinstance(source, str) and isinstance(name, str) and is_valid_id(name) for source, name in details.names.items()):
        raise BuildError("Saved character names are invalid")
    if not isinstance(avatars, dict) or not avatars or not isinstance(locs, dict) or not isinstance(locs.get("en"), dict):
        raise BuildError("Enka returned an empty or unfamiliar character list")
    before = {row["internalName"]: row for row in previous}
    variants = []
    seen = set()
    used = set()
    for row in sorted(avatars.values(), key=lambda item: item.get("StrId", "")):
        source = row.get("StrId", "")
        if not re.fullmatch(r"chr_[a-z0-9_]+", source) or source in seen:
            raise BuildError(f"Duplicate or invalid Enka character ID: {source}")
        seen.add(source)
        if source in overrides.exclude:
            display = overrides.display_names.get(source, locs["en"].get(str(row.get("NameHash")), source))
            details.left_out[source] = (display, "Enka entry excluded by overrides.exclude")
            continue
        key = str(row["NameHash"])
        name = overrides.join.get(source) or details.names.get(source)
        display = overrides.display_names.get(name, overrides.display_names.get(source, locs["en"].get(key)))
        if not isinstance(display, str) or not display.strip():
            raise BuildError(f"Enka has no English name for {source}")
        if not name:
            wanted = pascal(display)
            if not is_valid_id(wanted):
                raise BuildError(f"Enka name {display!r} needs a readable name in overrides.join")
            name, number = wanted, 2
            reserved = {value.casefold() for value in details.names.values()}
            while name.casefold() in used | reserved:
                name, number = f"{wanted}{number}", number + 1
        if not is_valid_id(name) or name.casefold() in used:
            raise BuildError(f"Duplicate or invalid pack name for {source}: {name}")
        used.add(name.casefold())
        details.names[source] = name
        details.sources[name] = source
        if source in before:
            details.migrations[source] = name
        old = before.get(name, before.get(source, {}))
        if name in overrides.exclude:
            details.left_out[source] = (display, "Enka entry excluded by overrides.exclude")
            continue
        aliases = [table[key] for table in locs.values() if isinstance(table, dict) and isinstance(table.get(key), str)]
        aliases += old.get("aliases", []) + [old.get("displayName", display)]
        aliases += overrides.aliases.get(name, overrides.aliases.get(source, [])) + [source]
        attributes = {field: row[source] for field, source in [("element", "Element"), ("profession", "Profession"), ("weaponClass", "WeaponType"), ("rarity", "Rarity")]}
        if any(not isinstance(attributes[field], str) or not attributes[field] for field in ["element", "profession", "weaponClass"]) or type(attributes["rarity"]) is not int:
            raise BuildError(f"Enka has invalid attributes for {name}")
        variants.append({"internalName": name, "displayName": display, "baseCharacterId": None,
                         "isDefaultVariant": True, "modFilesName": old.get("modFilesName", name) if source not in before else name,
                         "aliases": list(dict.fromkeys(aliases)), "attributes": attributes})
    # Legacy source-ID corrections remain readable while new corrections use pack names.
    corrected = copy.deepcopy(overrides)
    def target(value):
        return details.names.get(value, value) if value is not None else None
    corrected.display_names = {target(name): value for name, value in overrides.display_names.items()}
    corrected.aliases = {target(name): value for name, value in overrides.aliases.items()}
    corrected.parents = {target(name): target(value) for name, value in overrides.parents.items()}
    corrected.join = {name: target(value) for name, value in overrides.join.items()}
    corrected.part_of = {name: target(value) for name, value in overrides.part_of.items()}
    corrected.retired = [target(name) for name in overrides.retired]
    corrected.allow_shrink = [target(name) for name in overrides.allow_shrink]
    details.overrides = overrides = corrected
    add_characters(variants, previous, hand or Manual(), overrides)
    for row in variants:
        if row["internalName"] in overrides.exclude:
            details.left_out.setdefault(row["internalName"], (row["displayName"], "Manual entry excluded by overrides.exclude"))
    variants = [row for row in variants if row["internalName"] not in overrides.exclude]
    for variant in variants:
        identifier = variant["internalName"]
        variant["displayName"] = overrides.display_names.get(identifier, variant["displayName"])
        variant["aliases"] = list(dict.fromkeys([*variant["aliases"], *overrides.aliases.get(identifier, [])]))
    ids = {row["internalName"] for row in variants}
    for field, names in [("displayNames", overrides.display_names), ("aliases", overrides.aliases),
                         ("parents", overrides.parents), ("allowShrink", overrides.allow_shrink),
                         ("join targets", overrides.join.values()), ("partOf targets", overrides.part_of.values())]:
        if set(names) - ids:
            raise BuildError(f"overrides {field} refers to missing or excluded characters: " + ", ".join(sorted(set(names) - ids)))
    for name in overrides.parents:
        parent = overrides.parents[name]
        if parent is not None and parent not in ids:
            raise BuildError(f"overrides parents refers to missing character {parent}")
        visited = {name}
        while parent is not None:
            if parent in visited:
                raise BuildError("overrides parents contains a cycle")
            visited.add(parent)
            next_parent = overrides.parents.get(parent)
            if next_parent is None:
                break
            parent = next_parent
        variant = next(row for row in variants if row["internalName"] == name)
        variant["baseCharacterId"] = parent
        variant["isDefaultVariant"] = parent is None
    old_ids = {details.migrations.get(name, name) for name in before}
    missing = sorted(old_ids - ids - set(overrides.retired))
    if missing:
        raise BuildError("Enka would remove existing characters; confirm intentional removals in overrides retired: " + ", ".join(missing))
    for name in sorted(set(before) - ids - set(details.migrations)):
        details.left_out.setdefault(name, (before[name]["displayName"], "No longer in the roster; removal permitted by overrides.retired"))
    if not variants:
        raise BuildError("Enka has no usable characters")
    variants.sort(key=lambda row: row["internalName"])
    def attribute(field, title, labels):
        return {"displayName": title, "values": [{"id": value, "displayName": labels.get(value, value)} for value in sorted({row["attributes"][field] for row in variants if field in row["attributes"]})]}
    game = {key: config[key] for key in ["gameId", "displayName", "shortName", "importer", "disabledPrefix"]}
    game["attributes"] = {"element": attribute("element", "Element", ELEMENTS),
                          "profession": attribute("profession", "Class", CLASSES),
                          "weaponClass": attribute("weaponClass", "Weapon", WEAPONS),
                          "rarity": {"displayName": "Rarity", "kind": "number"}}
    return game, variants
