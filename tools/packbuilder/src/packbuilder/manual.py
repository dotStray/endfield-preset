"""Hand-supplied hashes and characters; source history remains separate and unchanged."""

from __future__ import annotations

import copy
import pathlib
import re
from dataclasses import dataclass, field

from packbuilder.files import BuildError, read_json, reject_links
from packbuilder.hashes import key
from packbuilder.names import pascal
from packbuilder.settings import Overrides, character_id, source_name

HEX = set("0123456789abcdef")
KINDS = {"ib", "position_vb", "blend_vb", "texcoord_vb", "draw_vb", "texture", "root_vs", "unknown"}


@dataclass
class Manual:
    hashes: dict[str, list[dict]] = field(default_factory=dict)
    replacements: dict[str, list[dict]] = field(default_factory=dict)
    characters: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    declared_ids: set[str] = field(default_factory=set)


def json_entries(value: object) -> list[dict]:
    """Read the same component-list hash.json files accepted by the other presets."""
    if not isinstance(value, list):
        raise BuildError("Manual hash.json must be a list of components")
    result = []

    def add(kind, value, component, texture_kind=None):
        if value in (None, ""):
            return
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{16}" if kind == "root_vs" else r"[0-9a-fA-F]{8}", value):
            raise BuildError(f"Invalid manual {kind} hash: {value!r}")
        entry = {"variant": "", "component": component, "kind": kind, "hash": value.lower()}
        if kind == "texture":
            entry["textureKind"] = texture_kind or "Unknown"
        result.append(entry)

    for component in value:
        if not isinstance(component, dict) or not isinstance(component.get("component_name", ""), str):
            raise BuildError("Manual hash.json has an invalid component")
        name = component.get("component_name", "")
        for kind in KINDS - {"unknown", "texture"}:
            add(kind, component.get(kind), name)
        textures = component.get("texture_hashes", [])
        if not isinstance(textures, list):
            raise BuildError("Manual hash.json texture_hashes must be a list")
        for group in textures:
            if not isinstance(group, list):
                raise BuildError("Manual hash.json has an invalid texture group")
            for texture in group:
                if not isinstance(texture, list) or len(texture) < 3 or not isinstance(texture[0], str):
                    raise BuildError("Manual hash.json has an invalid texture")
                add("texture", texture[2], name, texture[0])
    return result


def read(folder: pathlib.Path) -> Manual:
    reject_links(folder)
    hand = Manual()
    for directory, into in [("hashes", hand.hashes), ("hashes/replace", hand.replacements)]:
        for path in sorted((folder / directory).glob("*")):
            if not path.is_file() or path.name.startswith(".") or path.name.lower() == "readme.md":
                continue
            if path.suffix.lower() not in {".txt", ".ini", ".json"}:
                raise BuildError(f"{path}: use .txt, .ini or .json for manual hashes")
            entries = json_entries(read_json(path)) if path.suffix.lower() == ".json" else parse_text(path.read_text(encoding="utf-8-sig"))
            if not entries:
                raise BuildError(f"{path}: no hashes found")
            into.setdefault(path.stem, []).extend(entries)
    listed = read_json(folder / "characters.json", [])
    if not isinstance(listed, list):
        raise BuildError("manual/endfield/characters.json must be a list")
    for item in listed:
        item = {"name": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or set(item) - {"name", "id", "displayName", "outfitOf"}:
            raise BuildError("Manual characters accept name, id, displayName and outfitOf")
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise BuildError("Every manual character needs a nonempty name")
        if any(not isinstance(value, str) or not value.strip() for value in item.values()):
            raise BuildError("Manual character fields must be nonempty text")
        if "id" in item and not character_id(item["id"]):
            raise BuildError("Manual character id must use letters, digits, hyphens or underscores")
        hand.characters.append(dict(item))
    return hand


def resolve(variants: list[dict], written: str) -> dict | None:
    direct = [row for row in variants if row["internalName"].casefold() == written.casefold()]
    matches = direct or [row for row in variants if source_name(written) in
                        {source_name(name) for name in [row["internalName"], row["displayName"], *row.get("aliases", [])]}]
    if len(matches) > 1:
        raise BuildError(f"Manual name {written!r} matches several characters; use its internal ID")
    return matches[0] if matches else None


def add_characters(variants: list[dict], previous: list[dict], hand: Manual, overrides: Overrides) -> None:
    before = {row["internalName"]: row for row in previous}
    automatic = {row["internalName"] for row in variants}
    declared = set()
    hand.declared_ids.clear()

    def add(item):
        written = item.get("id", item["name"])
        variant = resolve(variants, written)
        if variant is not None:
            return variant
        identifier = item.get("id") or pascal(item.get("displayName", item["name"]))
        if not character_id(identifier):
            raise BuildError(f"{item['name']!r} needs an explicit manual character id")
        if identifier.casefold() in {row["internalName"].casefold() for row in variants}:
            raise BuildError(f"Manual character ID collision: {identifier}")
        display = item.get("displayName", item["name"])
        old = before.get(identifier, {})
        variant = {"internalName": identifier, "displayName": display, "baseCharacterId": None,
                   "isDefaultVariant": True, "modFilesName": old.get("modFilesName", identifier),
                   "aliases": list(dict.fromkeys([item["name"], *old.get("aliases", [])])), "attributes": {}}
        variants.append(variant)
        hand.notes.append(f"Manual character {identifier} ({display}) excluded by overrides.exclude." if identifier in overrides.exclude
                          else f"Added manual character {identifier} ({display}).")
        return variant

    for item in hand.characters:
        variant = add(item)
        identifier = variant["internalName"]
        if identifier in declared:
            raise BuildError(f"Duplicate manual character declaration: {identifier}")
        declared.add(identifier)
        hand.declared_ids.add(identifier)
    # Resolve hash filenames against corrected names before treating an unmatched file as a new character.
    for variant in variants:
        identifier = variant["internalName"]
        variant["displayName"] = overrides.display_names.get(identifier, variant["displayName"])
        variant["aliases"] = list(dict.fromkeys([*variant["aliases"], *overrides.aliases.get(identifier, [])]))
    for written in hand.hashes:
        if resolve(variants, written) is None:
            add({"name": written})
            hand.notes.append(f"Hash file {written!r} matched no character and created one; check the spelling.")
    for item in hand.characters:
        if "outfitOf" not in item:
            continue
        variant, parent = resolve(variants, item.get("id", item["name"])), resolve(variants, item["outfitOf"])
        if variant["internalName"] in automatic:
            continue
        if parent is None:
            raise BuildError(f"Manual outfit {item['name']} has unknown parent {item['outfitOf']}")
        variant["baseCharacterId"] = parent["internalName"]
        variant["isDefaultVariant"] = False
    by_id = {row["internalName"]: row for row in variants}
    for variant in variants:
        parent = variant.get("baseCharacterId")
        visited = {variant["internalName"]}
        while parent is not None:
            if parent in visited:
                raise BuildError("Manual outfit relationships contain a cycle")
            visited.add(parent)
            base = by_id[parent]
            if base.get("baseCharacterId") is None:
                variant["baseCharacterId"] = parent
                variant["attributes"] = dict(base["attributes"])
                break
            parent = base["baseCharacterId"]


def apply(variants: list[dict], payload: dict, hand: Manual, global_hashes: list[str]) -> tuple[dict, set[str]]:
    result = copy.deepcopy(payload)
    replaced = set()
    for written, entries in hand.replacements.items():
        variant = resolve(variants, written)
        if variant is None:
            raise BuildError(f"Manual replacement {written!r} matches no character; use hashes/ to add one")
        identifier = variant["internalName"]
        removed = sum(entry["variant"] == identifier for entry in result["entries"])
        result["entries"] = [entry for entry in result["entries"] if entry["variant"] != identifier]
        result["entries"].extend({**entry, "variant": identifier} for entry in entries)
        replaced.add(identifier)
        hand.notes.append(f"Replacement {written}: {removed} automatic hashes replaced by {len(entries)} manual hashes. Delete the file to restore automatic hashes.")
    known = {key(entry) for entry in result["entries"]}
    for written, entries in hand.hashes.items():
        variant = resolve(variants, written)
        if variant is None:
            raise BuildError(f"Manual hashes {written!r} refer to an excluded character")
        identifier = variant["internalName"]
        existing_values = {entry["hash"] for entry in result["entries"] if entry["variant"] == identifier}
        added = 0
        for entry in entries:
            entry = {**entry, "variant": identifier}
            if key(entry) in known or entry["kind"] == "unknown" and entry["hash"] in existing_values:
                continue
            known.add(key(entry))
            result["entries"].append(entry)
            added += 1
        hand.notes.append(f"Hashes {written}: {added} added; duplicates already supplied by automatic sources are omitted.")
    unique = {key(entry): entry for entry in result["entries"]}
    result["entries"] = [unique[identifier] for identifier in sorted(unique)]
    ignored = set(payload["ignoredHashes"]) | set(global_hashes)
    ignored.update(entry["hash"] for entry in result["entries"] if entry["kind"] == "root_vs")
    result["ignoredHashes"] = sorted(ignored & {entry["hash"] for entry in result["entries"]})
    return result, replaced


SECTION_MARKERS = {
    "ib": "ib",
    "index": "ib",
    "indexbuffer": "ib",
    "position": "position_vb",
    "pos": "position_vb",
    "blend": "blend_vb",
    "texcoord": "texcoord_vb",
    "draw": "draw_vb",
    "vb": "draw_vb",
    "diffuse": "texture",
    "lightmap": "texture",
    "normalmap": "texture",
    "shadowramp": "texture",
    "metalmap": "texture",
    "materialmap": "texture",
    "texture": "texture",
}

# Words a person might put before a hash on a line of its own.
LINE_KINDS = {
    **{k: v for k, v in SECTION_MARKERS.items()},
    "position_vb": "position_vb",
    "blend_vb": "blend_vb",
    "texcoord_vb": "texcoord_vb",
    "draw_vb": "draw_vb",
    "root_vs": "root_vs",
    "unknown": "unknown",
}

SECTION_PREFIXES = ("textureoverride", "shaderoverride", "resource", "commandlist", "customshader")



def parse_text(text: str, *, texture_override_only: bool = False) -> list[dict]:
    """Every hash in pasted text or an ``.ini``, with its kind where the text says it.

    A 16-digit hash is a shader hash, which only ever means ``root_vs``. An 8-digit hash with
    nothing to say what it is becomes ``unknown``, which the app scores like a ``draw_vb``.
    A bare token must contain a digit to count, so an ordinary word such as "deadbeef" in a
    comment is not taken for a hash.
    """
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    section: str | None = None

    def add(kind: str, value: str) -> None:
        value = value.lower()
        if len(value) == 16:
            kind = "root_vs"
        if (kind, value) not in seen:
            seen.add((kind, value))
            found.append({"variant": "", "component": "", "kind": kind, "hash": value})

    for raw in text.splitlines():
        line = re.split(r"(?:^|\s)(?:;|#|//)", raw, maxsplit=1)[0].strip()
        if not line:
            continue
        header = re.fullmatch(r"\[([^\]]+)\]", line)
        if header:
            section = header.group(1).strip()
            continue
        assignment = re.fullmatch(r"hash\s*=\s*([0-9A-Fa-f]{8}|[0-9A-Fa-f]{16})", line, flags=re.IGNORECASE)
        if assignment:
            if texture_override_only and not (section or "").lower().startswith("textureoverride"):
                continue
            add(section_kind(section) or "unknown", assignment.group(1))
            continue
        if texture_override_only:
            continue
        tokens = re.findall(r"[A-Za-z_]+|[0-9A-Fa-f]+", line)
        word = tokens[0].lower() if tokens else ""
        for token in re.findall(r"\b[0-9A-Fa-f]{8}\b|\b[0-9A-Fa-f]{16}\b", line):
            if not any(c.isdigit() for c in token) or not set(token.lower()) <= HEX:
                continue
            add(LINE_KINDS.get(word, section_kind(section) or "unknown"), token)
    return found


def section_kind(section: str | None) -> str | None:
    """The kind a section name implies, from its last meaningful word, or ``None``."""
    if not section:
        return None
    name = section
    for prefix in SECTION_PREFIXES:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
            break
    tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", name)
    for i in range(len(tokens) - 1, -1, -1):
        token = tokens[i].lower()
        if i > 0 and (tokens[i - 1].lower() + token) in SECTION_MARKERS:
            return SECTION_MARKERS[tokens[i - 1].lower() + token]
        if token in SECTION_MARKERS:
            return SECTION_MARKERS[token]
        if token.isdigit() or len(token) <= 2:
            continue
        return None
    return None
