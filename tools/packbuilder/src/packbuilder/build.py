"""Build in a staging tree; promote pack, preserved sources and provenance only after checks."""

from __future__ import annotations

import datetime
import copy
import hashlib
import pathlib
import shutil
import tempfile

from packbuilder import BUILDER, hashes, images, manual, release, reports, roster, upstream
from packbuilder.files import BuildError, build_lock, commit_files, dumps, read_json, reject_links, write_json
from packbuilder.http import FetchError, Fetcher
from packbuilder.settings import Overrides, load_overrides


def fingerprint(folder: pathlib.Path) -> str:
    digest = hashlib.sha256()
    if not folder.exists():
        return ""
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            digest.update(path.relative_to(folder).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def check(game: dict, variants: list[dict], payload: dict, pack: pathlib.Path, previous_variants: list[dict], previous_hashes: dict, overrides: Overrides, replacements=frozenset()) -> None:
    ids = {row["internalName"] for row in variants}
    if len(ids) != len(variants) or len({name.casefold() for name in ids}) != len(ids):
        raise BuildError("Character IDs are duplicated")
    if {row["internalName"] for row in previous_variants} - ids - set(overrides.retired):
        raise BuildError("Existing characters would disappear; confirm intentional removals in overrides retired")
    values = {field: {row["id"] for row in spec.get("values", [])} for field, spec in game["attributes"].items() if spec.get("kind") != "number"}
    entries = payload["entries"]
    keys = {hashes.key(entry) for entry in entries}
    if len(keys) != len(entries):
        raise BuildError("Hash associations were duplicated")
    lost = {hashes.key(entry) for entry in previous_hashes.get("entries", [])} - keys
    if any(variant not in set(overrides.allow_shrink) | set(replacements) and not (variant not in ids and variant in overrides.retired) for variant, _, _ in lost):
        raise BuildError("Existing hash associations would disappear; confirm intentional corrections in overrides allowShrink")
    owners = {entry["variant"] for entry in entries}
    if owners - ids:
        raise BuildError("Hash associations refer to characters outside the roster")
    if game.get("icon"):
        if game["icon"] != "images/_game.webp":
            raise BuildError("Unexpected game icon path")
        icon = pack / game["icon"]
        if not icon.is_file() or icon.stat().st_size > images.MAX_BYTES:
            raise BuildError("Missing or oversized game icon")
    now = {row["internalName"]: row for row in variants}
    for row in previous_variants:
        if row["internalName"] not in now:
            continue
        if row.get("image") and not now[row["internalName"]].get("image"):
            raise BuildError(f"Existing portrait for {row['internalName']} would disappear")
    for row in variants:
        name = row["internalName"]
        if not row["displayName"] or not row["modFilesName"] or any(character in row["modFilesName"] for character in '/\\\x00<>:"|?*'):
            raise BuildError(f"Invalid display or folder name for {name}")
        if row["hashesPending"] != (name not in owners):
            raise BuildError(f"Inconsistent hashesPending for {name}")
        for field, value in row["attributes"].items():
            if field not in game["attributes"] or field in values and value not in values[field]:
                raise BuildError(f"Undeclared attribute for {name}:{field}")
        if row.get("image"):
            path = pack / row["image"]
            if not path.is_file() or path.stat().st_size > images.MAX_BYTES:
                raise BuildError(f"Missing or oversized portrait for {name}")


def build(root: pathlib.Path, *, fetcher=None, no_network=False, today=None, publish=False, releases=None) -> dict:
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    root = root.resolve()
    config = read_json(root / "config/endfield.json")
    if not config or config.get("gameId") != "endfield":
        raise BuildError("Run in the preset folder, or provide --root with config/endfield.json")
    try:
        with build_lock(root), tempfile.TemporaryDirectory(prefix=".work-", dir=root) as directory:
            if "excludedIds" in config["roster"] or "fixerAliases" in config["hashes"]:
                raise BuildError("Move roster excludedIds and hashes fixerAliases to overrides/endfield.json (exclude and join)")
            overrides = load_overrides(root / "overrides/endfield.json")
            hand = manual.read(root / "manual/endfield")
            result = _build(root, pathlib.Path(directory), config, overrides, None if no_network else (fetcher or Fetcher(root / ".cache/http")), today, hand)
            if publish:
                result["published"] = release.publish(root, config, releases)
            return result
    except (BuildError, FetchError, KeyError, TypeError, ValueError) as error:
        reports.write_blocked(root / "reports/endfield", error)
        raise BuildError(str(error)) from error


def _build(root, stage, config, overrides, fetcher, today, hand):
    old_pack = root / "packs/endfield"
    old_upstream = root / "upstream/endfield"
    for path in [old_pack, old_upstream, root / "ledger/endfield.json"]:
        reject_links(path)
    previous_variants = read_json(old_pack / "variants.json", [])
    previous_hashes = read_json(old_pack / "hashes.json", {})
    previous_manifest = read_json(old_pack / "manifest.json", {})
    previous_report = read_json(root / "reports/endfield/build.json", {})
    saved_ledger = read_json(root / "ledger/endfield.json", {})
    saved_sources = read_json(old_upstream / "sources.json", {})
    stage_upstream = stage / "upstream/endfield"
    if old_upstream.exists():
        shutil.copytree(old_upstream, stage_upstream)
    else:
        stage_upstream.mkdir(parents=True)
    notes = []
    roster_details = roster.Details()
    hash_details = hashes.Details()

    def assemble_roster(avatars, locs):
        return roster.assemble(avatars, locs, previous_variants, config, overrides, hand, details=roster_details, saved_names=saved_ledger.get("names", {}))

    avatars, locs = read_json(old_upstream / "avatars.json", {}), read_json(old_upstream / "locs.json", {})
    if fetcher is not None:
        try:
            url = config["roster"]["url"]
            fresh_avatars, fresh_locs = fetcher.json(url + "avatars.json"), fetcher.json(url + "locs.json")
            roster.assemble(fresh_avatars, fresh_locs, previous_variants, config, overrides, copy.deepcopy(hand), saved_names=saved_ledger.get("names", {}))
            avatars, locs = fresh_avatars, fresh_locs
        except (FetchError, BuildError, KeyError, TypeError, ValueError) as error:
            notes.append(f"Roster not refreshed ({error}); using the saved Enka list")
    game, variants = assemble_roster(avatars, locs)
    overrides = roster_details.overrides
    # Migrate the old source-ID pack as one rename, preserving every existing association.
    def renamed(name):
        return roster_details.migrations.get(name, name)
    previous_variants = [{**row, "internalName": renamed(row["internalName"]),
                          "baseCharacterId": renamed(row.get("baseCharacterId"))} for row in previous_variants]
    previous_hashes = {**previous_hashes, "entries": [{**entry, "variant": renamed(entry["variant"])}
                                                     for entry in previous_hashes.get("entries", [])]}
    write_json(stage_upstream / "avatars.json", avatars)
    write_json(stage_upstream / "locs.json", locs)

    sources = dict(saved_sources)
    if fetcher is not None:
        for name in ["catalog", "fixer"]:
            try:
                sources[name] = upstream.refresh(config["hashes"][name], sources.get(name, {}), stage_upstream, fetcher, catalog=name == "catalog")
            except (FetchError, BuildError, KeyError, TypeError, ValueError) as error:
                notes.append(f"{name} not refreshed ({error}); using saved snapshots")
    if not all(name in sources for name in ["catalog", "fixer"]):
        raise BuildError("Both hash sources need a saved valid snapshot before an offline build")
    try:
        payload, ledger, audit = hashes.collect(stage_upstream, sources, variants, overrides, saved_ledger, details=hash_details, identities=roster_details.names)
    except (BuildError, KeyError, TypeError, ValueError, IndexError) as error:
        if sources == saved_sources or not all(name in saved_sources for name in ["catalog", "fixer"]):
            raise BuildError(f"Invalid hash source data: {error}") from error
        notes.append(f"New hash source data failed validation ({error}); using saved snapshots")
        sources = saved_sources
        payload, ledger, audit = hashes.collect(stage_upstream, sources, variants, overrides, saved_ledger, details=hash_details, identities=roster_details.names)
    write_json(stage_upstream / "sources.json", sources)
    write_json(stage / "ledger/endfield.json", ledger)
    for name in audit["unmatchedCatalogCharacters"]:
        notes.append(f"Catalog ID {name} is not in Enka; retained in history without adding a character")
    for name in audit["unmatchedFixerNames"]:
        notes.append(f"Fixer name {name} did not map uniquely; saved raw data for review")

    automatic_keys = {hashes.key(entry) for entry in payload["entries"]}
    payload, replaced = manual.apply(variants, payload, hand, sorted(set(ledger["ignoredHashes"]) | set(overrides.ignored_hashes)))
    reports.final_audit(audit, payload, automatic_keys, hash_details, set(ledger["ignoredHashes"]) | set(overrides.ignored_hashes))
    notes.extend(hand.notes)
    owners = {entry["variant"] for entry in payload["entries"]}
    for variant in variants:
        variant["hashesPending"] = variant["internalName"] not in owners
    pack = stage / "packs/endfield"
    manual_images = root / "manual/endfield/images"
    missing = {}
    records, image_notes = images.build(variants, old_pack, pack / "images", read_json(old_upstream / "images.json", {}), fetcher, manual_images, missing=missing, sources=roster_details.sources)
    notes.extend(image_notes)
    write_json(stage_upstream / "images.json", records)
    icon, icon_notes = images.game_icon(config.get("icon", {}), old_pack, pack / "images", read_json(old_upstream / "icon.json", {}), fetcher, manual_images)
    notes.extend(icon_notes)
    if icon:
        game["icon"] = icon["file"]
        write_json(stage_upstream / "icon.json", icon)
    elif read_json(old_pack / "game.json", {}).get("icon"):
        raise BuildError("Existing game icon would disappear")
    for name, value in [("game.json", game), ("variants.json", variants), ("hashes.json", payload)]:
        write_json(pack / name, value)
    attribution = "# Attribution\n\nPortraits are game art © HYPERGRYPH. Subsequent downloads use [Enka.Network](https://enka.network/).\n\nHashes: [Better-Endfield](https://github.com/Dr-hydra/Better-Endfield) (AGPL-3.0) and [EndField_Mod_Fixer](https://github.com/swaggerosts/EndField_Mod_Fixer), including saved historical observations.\n"
    seeds = sorted({row["source"] for row in records.values() if row.get("initialSeed")})
    if seeds:
        attribution += "\nInitial bundled artwork: " + ", ".join(seeds) + ". These addresses are credits, not builder download sources.\n"
    if icon:
        attribution += (f"\nGame icon: game art © HYPERGRYPH, from the [official Google Play listing]({icon['page']}).\n" if icon.get("page")
                        else "\nGame icon: supplied in the preset repository's manual image folder.\n")
    (pack / "ATTRIBUTION.md").write_text(attribution)
    # A deliberate replacement and restoring automatic hashes both keep the source ledger intact.
    allowed_replacements = replaced | {renamed(name) for name in previous_report.get("manualReplacements", [])}
    check(game, variants, payload, pack, previous_variants, previous_hashes, overrides, allowed_replacements)
    changed = fingerprint(pack) != fingerprint(old_pack) or not previous_manifest.get("packVersion")
    index = read_json(root / "index.json", {})
    taken = {row["packVersion"] for item in index.get("packs", []) for row in item.get("versions", [])}
    taken.add(previous_manifest.get("packVersion", ""))
    manifest = previous_manifest
    if changed:
        manifest = {"packSchemaVersion": 1, "gameId": "endfield", "packVersion": release.next_version(today, taken),
                    "generatedAt": today.isoformat() + "T00:00:00Z", "builder": BUILDER,
                    "authoredBy": "official", "minAppVersion": config["minAppVersion"],
                    "counts": {"variants": len(variants), "skins": sum(row["baseCharacterId"] is not None for row in variants), "images": len(records)},
                    "sources": [{"kind": "roster", "url": config["roster"]["url"]}, {"kind": "images", "url": "https://enka.network/ui/ef/"},
                                *([{"kind": "icon", "url": icon["source"]}] if icon else []),
                                *[{"kind": "hashes", "url": f"https://github.com/{source['repo']}", "commit": source["head"],
                                   **({"license": config["hashes"][name]["license"]} if config["hashes"][name].get("license") else {})} for name, source in sorted(sources.items())]]}
    write_json(pack / "manifest.json", manifest)
    local_index = release.export(pack, stage / "dist")
    report = {"changed": changed, "packVersion": manifest["packVersion"], "counts": manifest["counts"],
              "gameIcon": game.get("icon"),
              "hashes": audit, "warnings": notes, "networkRequests": len(fetcher.requests) if fetcher is not None else 0,
              "manualReplacements": sorted(replaced),
              "imageRequests": sum(url.startswith(("https://enka.network/", "https://play-lh.googleusercontent.com/")) for url in fetcher.requests) if fetcher is not None else 0,
              "archive": local_index["packs"][0]["versions"][0]}
    write_json(stage / "reports/endfield/build.json", report)
    write_json(stage / "reports/endfield/hash-audit.json", audit)
    reports.write(stage / "reports/endfield", pack, variants, payload, audit=audit, roster_details=roster_details,
                  hash_details=hash_details, ledger=ledger, avatars=avatars, hand=hand, records=records, icon=icon,
                  missing=missing, automatic_keys=automatic_keys, overrides=overrides)
    (stage / "reports/endfield/build.md").write_text(f"# Endfield {manifest['packVersion']}\n\n{len(variants)} characters, {len(records)} portraits, {len(payload['entries'])} hash associations.\n\n" + "\n".join("- " + note for note in notes) + "\n")
    commit_files(stage, root, ["packs/endfield", "upstream/endfield", "ledger/endfield.json", "reports/endfield", "dist"])
    return report
