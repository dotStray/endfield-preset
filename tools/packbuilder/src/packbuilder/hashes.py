"""Endfield's two upstream formats, read as data and merged into an append-only ledger.

The catalog's entry/texture keys are EFMI hashes. Its CRCs, payload digests and resource IDs
are evidence rather than mod hashes. The fixer carries old and replacement IB/VB0/LOD
values, wet-effect overrides, texture pairs and patched IBs outside its main table.
"""

from __future__ import annotations

import ast
import collections
import json
import re
from dataclasses import dataclass, field

from packbuilder.files import BuildError
from packbuilder.settings import Overrides, character_id, part_targets, source_name
from packbuilder.upstream import blob


def clean(value: object, *, global_hash: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:[0-9a-fA-F]{8})?" if global_hash else r"[0-9a-fA-F]{8}", value):
        raise BuildError(f"Invalid upstream hash: {value!r}")
    return value.lower()


def key(entry: dict) -> tuple[str, str, str]:
    # Component numbers change across dumps. Counting them twice would inflate sort evidence.
    return entry["variant"], entry["kind"], entry["hash"]


@dataclass
class Details:
    """Accepted source decisions for reports; these are not part of the pack or ledger."""

    current_keys: set[tuple[str, str, str]] = field(default_factory=set)
    matches: set[tuple[str, str, str]] = field(default_factory=set)


def shared_hashes(payload: dict) -> list[dict]:
    """A collision is a hash with several owners, regardless of component or kind."""
    ignored = set(payload.get("ignoredHashes", []))
    owners = collections.defaultdict(set)
    for entry in payload["entries"]:
        if entry["hash"] not in ignored:
            owners[entry["hash"]].add(entry["variant"])
    return [{"hash": value, "characters": sorted(names)}
            for value, names in sorted(owners.items(), key=lambda item: (-len(item[1]), item[0])) if len(names) > 1]


def literals(data: bytes) -> dict:
    """No import, eval or constructor call; only literal AST nodes are evaluated."""
    try:
        tree = ast.parse(data.decode("utf-8-sig"))
        found = {}
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "SPECIAL_CASES":
                    if not isinstance(node.value, ast.List):
                        raise BuildError("Fixer's special-case table is no longer literal data")
                    cases = []
                    for item in node.value.elts:
                        if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Name) or item.func.id != "SpecialCase" or item.keywords or len(item.args) != 5:
                            raise BuildError("Unfamiliar fixer special-case entry")
                        values = [ast.literal_eval(argument) for argument in item.args]
                        cases.append(dict(zip(["name", "trigger_hash", "new_hash", "index_count", "suffix"], values)))
                    found[target.id] = cases
                elif target.id in {"CHARACTERS", "BUILTIN_WET_FIX_TEMPLATES", "OLD_SHADER_HASHES", "NEW_SHADER_HASHES", "HASH_RULES", "END_FIELD_13_OLD_HASH", "END_FIELD_13_NEW_HASH"}:
                    found[target.id] = ast.literal_eval(node.value)
        if not isinstance(found.get("CHARACTERS"), list) or not found["CHARACTERS"]:
            raise BuildError("Fixer has no literal character table")
        return found
    except (UnicodeError, SyntaxError, ValueError, TypeError) as error:
        raise BuildError(f"Fixer's hash data could not be read safely: {error}") from error


def normalize(name: str) -> str:
    return source_name(name)


def collect(folder, sources: dict, variants: list[dict], overrides: Overrides, previous: dict, *, details: Details | None = None, identities: dict[str, str] | None = None) -> tuple[dict, dict, dict]:
    details = details if details is not None else Details()
    details.current_keys.clear()
    details.matches.clear()
    identities = dict(identities or previous.get("names", {}))
    source_ids = {name: source for source, name in identities.items()}
    records = {}
    assignments = {}
    ignored = {clean(value, global_hash=True) for value in previous.get("ignoredHashes", [])}
    current = set()
    issues = []
    unmatched_fixer = set()
    roster = {row["internalName"] for row in variants}

    def observation(entry, origin):
        return (entry["kind"], entry["hash"], origin["repo"], origin["commit"], origin["path"], origin.get("field", ""), origin.get("role", ""))

    for record in previous.get("records", []):
        entry = dict(record["entry"])
        entry["hash"] = clean(entry["hash"])
        if entry["kind"] not in {"ib", "position_vb", "texture"}:
            raise BuildError("Saved Endfield ledger has an unfamiliar hash kind")
        records[key(entry)] = {"entry": entry, "origins": list(record["origins"])}

    def add(variant, kind, value, component, origin, *, is_current=False):
        variant = source_ids.get(variant, variant)
        if not character_id(variant):
            raise BuildError(f"Invalid source character ID: {variant}")
        entry = {"variant": variant, "component": component, "kind": kind, "hash": clean(value)}
        if kind == "texture":
            entry["textureKind"] = "Unknown"
        identifier = key(entry)
        assignments[observation(entry, origin)] = variant
        record = records.setdefault(identifier, {"entry": entry, "origins": []})
        if origin not in record["origins"]:
            record["origins"].append(origin)
        if is_current:
            current.add(identifier)

    catalog = sources["catalog"]
    for snapshot in catalog["snapshots"]:
        is_current = snapshot["commit"] == catalog["head"]
        for file in snapshot["files"]:
            try:
                data = json.loads(blob(folder, file["sha"]).decode("utf-8-sig"))
            except (ValueError, UnicodeError) as error:
                raise BuildError(f"Invalid catalog JSON: {file['path']}") from error
            if data.get("kind") != "bem-character-catalog":
                continue
            if data.get("identity_policy") != "efmi-dx11-region-texture0-fullchain-v1":
                raise BuildError(f"Unfamiliar catalog identity policy in {file['path']}")
            variant = data["character_id"]
            if not isinstance(variant, str) or not re.fullmatch(r"chr_[a-z0-9_]+", variant):
                raise BuildError(f"Invalid catalog character ID: {variant}")
            origin = {"repo": catalog["repo"], "commit": snapshot["commit"], "path": file["path"],
                      "snapshotDate": snapshot["date"], "role": "current-catalog" if is_current else "catalog-history"}
            for value, components in data["entries"].items():
                components = components if isinstance(components, list) else [components]
                alias = data.get("legacy_source_aliases", {}).get(value)
                if is_current:
                    for component in components:
                        if data.get("entry_identity_evidence", {}).get(str(component), {}).get("source_hash") != value and not alias:
                            raise BuildError(f"No current IB identity evidence for {variant}:{value}")
                add(variant, "ib", value, ", ".join(f"Component{index}" for index in components),
                    {**origin, "field": "entries/" + value, "legacyAlias": bool(alias)}, is_current=is_current and not alias)
            for value, texture in data["textures"].items():
                add(variant, "texture", value, texture["name"], {**origin, "field": "textures/" + value}, is_current=is_current)
            ignored.update(clean(value) for value in data.get("preserved_globals", {}))
            if is_current and (data.get("identity_issues") or data.get("excluded_components")):
                issues.append({"character": variant, "issues": data.get("identity_issues", []), "excludedComponents": data.get("excluded_components", [])})
    if not current:
        raise BuildError("Catalog has no current character identities")

    parts = part_targets(overrides.part_of)
    def owner(source):
        name = identities.get(source, source)
        return parts.get(normalize(source), parts.get(normalize(name), name))

    def excluded(source):
        return source in overrides.exclude or identities.get(source, source) in overrides.exclude

    names = collections.defaultdict(set)
    for row in variants:
        for name in [row["internalName"], row["displayName"], *row.get("aliases", [])]:
            names[normalize(name)].add(row["internalName"])
    for table in (overrides.join, overrides.part_of):
        for name, variant in table.items():
            if variant not in roster:
                raise BuildError(f"Override source name {name} refers to missing character {variant}")
            if table is overrides.part_of and normalize(name) in {normalize(identifier) for identifier in roster}:
                continue  # Existing owners are projected through partOf below; their observations stay intact.
            names[normalize(name)] = {variant}

    def resolve(name):
        candidates = names.get(normalize(name.removesuffix("_LOD")), set())
        if len(candidates) == 1:
            target = next(iter(candidates))
            normalized = normalize(name.removesuffix("_LOD"))
            rule = next(("overrides." + label for label, table in [("partOf", overrides.part_of), ("join", overrides.join)]
                         if any(normalize(written) == normalized for written in table)), "unique roster name or alias")
            final_target = owner(target)
            if final_target != target and rule != "overrides.partOf":
                rule += " + overrides.partOf"
            details.matches.add((name, final_target, rule))
            return target
        unmatched_fixer.add(name)
        return None

    fixer = sources["fixer"]
    for snapshot in fixer["snapshots"]:
        for file in snapshot["files"]:
            data = literals(blob(folder, file["sha"]))
            base = {"repo": fixer["repo"], "commit": snapshot["commit"], "path": file["path"],
                    "snapshotDate": snapshot["date"], "role": "fixer-history"}
            for character in data["CHARACTERS"]:
                variant = resolve(character["name"])
                if variant is None:
                    continue
                for component in character["components"]:
                    for field, kind in [("ib", "ib"), ("lod_ib", "ib"), ("vb0", "position_vb"), ("lod_vb0", "position_vb")]:
                        pair = component[field]
                        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                            raise BuildError(f"Unfamiliar fixer hash pair for {character['name']}:{field}")
                        for position, value in enumerate(pair):
                            if value is not None:
                                name = f"Component{component['index']}" + (".LOD" if field.startswith("lod_") else "")
                                add(variant, kind, value, name, {**base, "field": f"CHARACTERS/{character['name']}/{component['index']}/{field}/{position}"})
                for index, pair in enumerate(character.get("textures", [])):
                    for position, value in enumerate(pair):
                        if value is not None:
                            add(variant, "texture", value, "", {**base, "field": f"CHARACTERS/{character['name']}/textures/{index}/{position}"})
            for template in data.get("BUILTIN_WET_FIX_TEMPLATES", []):
                variant = resolve(template["name"])
                if variant is not None:
                    for section, value, count, resource in template["overrides"]:
                        add(variant, "ib", value, section, {**base, "field": "BUILTIN_WET_FIX_TEMPLATES/" + section})
            for case in data.get("SPECIAL_CASES", []):
                variant = resolve(case["name"])
                if variant is not None:
                    for field in ["trigger_hash", "new_hash"]:
                        add(variant, "ib", case[field], "SpecialCase." + case["name"], {**base, "field": "SPECIAL_CASES/" + case["name"] + "/" + field})
            for field in ["OLD_SHADER_HASHES", "NEW_SHADER_HASHES"]:
                ignored.update(clean(value, global_hash=True) for value in data.get(field, set()))
            for values in data.get("HASH_RULES", {}).values():
                ignored.update(clean(value, global_hash=True) for value in values)
            for field in ["END_FIELD_13_OLD_HASH", "END_FIELD_13_NEW_HASH"]:
                if data.get(field):
                    ignored.add(clean(data[field], global_hash=True))

    ordered = [records[identifier] for identifier in sorted(records)]
    for record in ordered:
        record["origins"].sort(key=lambda origin: (origin["repo"], origin["commit"], origin["path"], origin.get("field", ""), origin.get("role", "")))
    entries_by_key = {}
    for record in ordered:
        entry = record["entry"]
        if excluded(entry["variant"]):
            continue
        # Reapply hand assignments to saved observations; old matches must not keep wrong owners alive.
        targets = {assignments.get(observation(entry, origin), entry["variant"]) for origin in record["origins"]}
        for target in sorted(targets or {entry["variant"]}):
            target = owner(target)
            if target in roster:
                projected = {**entry, "variant": target}
                entries_by_key.setdefault(key(projected), projected)
                if target != entry["variant"] and normalize(entry["variant"]) in parts:
                    details.matches.add((entry["variant"], target, "overrides.partOf"))
    entries = [entries_by_key[identifier] for identifier in sorted(entries_by_key)]
    ledger = {"schemaVersion": 1, "records": ordered, "ignoredHashes": sorted(ignored), "names": dict(sorted(identities.items())),
              "notes": "Append-only observations. Snapshot dates record upstream commits, not inferred game versions."}
    # Keep all global exclusions in the ledger; the pack needs only those overlapping an entry.
    pack_ignored = ignored | set(overrides.ignored_hashes)
    payload = {"ignoredHashes": sorted(pack_ignored & {entry["hash"] for entry in entries}), "entries": entries}
    owners = collections.defaultdict(set)
    for entry in entries:
        owners[entry["hash"]].add(entry["variant"])
    mapped = {(owner(variant), kind, value) for variant, kind, value in current
              if not excluded(variant) and owner(variant) in roster}
    details.current_keys.update(mapped)
    report = {"rosterVariants": len(roster), "currentEntries": len(mapped),
              "historicalOnlyEntries": sum(key(entry) not in mapped for entry in entries),
              "totalEntries": len(entries), "distinctHashes": len(owners),
              "kinds": dict(collections.Counter(entry["kind"] for entry in entries)),
              "excludedGlobalHashCount": len(pack_ignored),
              "unmatchedCatalogCharacters": sorted({record["entry"]["variant"] for record in ordered
                                                    if owner(record["entry"]["variant"]) not in roster
                                                    and not excluded(record["entry"]["variant"])}),
              "unmatchedFixerNames": sorted(unmatched_fixer), "catalogIssues": issues,
              "sharedHashes": shared_hashes(payload)}
    return payload, ledger, report
