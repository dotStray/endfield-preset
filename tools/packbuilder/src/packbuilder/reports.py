"""Human-readable reports from the checked pack and accepted build decisions.

Every successful build regenerates these beside the pack, even when its version stays
the same. They are promoted together with the pack; a refused build keeps the last reports.
"""

from __future__ import annotations

import collections
import html
import pathlib
import re

from packbuilder import hashes, manual, roster
from packbuilder.files import write_bytes
from packbuilder.settings import Overrides, character_id


def cell(value: object) -> str:
    """Source names must not create Markdown rows, headings or links."""
    value = html.escape(" ".join(str(value).split()), quote=False)
    value = re.sub(r"([\\*_{}\[\]])", r"\\\1", value)
    return value.replace("|", "&#124;").replace("`", "&#96;")


def write_text(path: pathlib.Path, lines: list[str]) -> None:
    write_bytes(path, ("\n".join(lines).rstrip() + "\n").encode("utf-8"))


def characters(count: int) -> str:
    return f"{count} character{' has' if count == 1 else 's have'}"


def final_audit(audit: dict, payload: dict, automatic_keys: set, details: hashes.Details, ignored: set[str]) -> None:
    """Manual replacements must not leave automatic-source counts or collisions stale."""
    keys = {hashes.key(entry) for entry in payload["entries"]}
    current = keys & details.current_keys
    audit.update(currentEntries=len(current), historicalOnlyEntries=len((keys & automatic_keys) - current),
                 manualEntries=len(keys - automatic_keys), totalEntries=len(keys),
                 distinctHashes=len({entry["hash"] for entry in payload["entries"]}),
                 kinds=dict(collections.Counter(entry["kind"] for entry in payload["entries"])),
                 excludedGlobalHashCount=len(ignored | set(payload["ignoredHashes"])),
                 sharedHashes=hashes.shared_hashes(payload))


def write(folder: pathlib.Path, pack: pathlib.Path, variants: list[dict], payload: dict, *, audit: dict,
          roster_details: roster.Details, hash_details: hashes.Details, ledger: dict, avatars: dict,
          hand: manual.Manual, records: dict, icon: dict, missing: dict[str, str], automatic_keys: set, overrides: Overrides) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "blocked.md").unlink(missing_ok=True)
    owners = {entry["variant"] for entry in payload["entries"]}
    pending = [row for row in variants if row["internalName"] not in owners]
    lines = ["# endfield: characters waiting for hashes", "",
             f"{characters(len(pending))} no hashes yet. They still appear in the app.", "",
             "To add hashes, put a file named after the character in `manual/endfield/hashes/`.", ""]
    for row in pending:
        parent = f" (outfit of `{row['baseCharacterId']}`)" if row.get("baseCharacterId") else ""
        lines.append(f"- `{row['internalName']}` — {cell(row['displayName'])}{parent}")
    write_text(folder / "pending-hashes.md", lines)

    left_out = dict(roster_details.left_out)
    for record in ledger["records"]:
        identifier = record["entry"]["variant"]
        if identifier in overrides.exclude and identifier not in left_out:
            left_out[identifier] = (identifier, "Hash-source ID excluded by overrides.exclude; observations remain in the ledger")
    for identifier in audit["unmatchedCatalogCharacters"]:
        left_out.setdefault(identifier, (identifier, "Hash-source ID has no roster entry; observations remain in the ledger"))
    for name in audit["unmatchedFixerNames"]:
        left_out["fixer:" + name] = (name, "Fixer name has no unique roster match; raw source data is retained")
    lines = ["# endfield: what the roster and hash sources have that the pack does not", "",
             f"{len(left_out)} {'entry was' if len(left_out) == 1 else 'entries were'} left out.", "",
             "Remove an `exclude` rule to restore a roster entry. Use `join` or `partOf` for a source's hashes, "
             "or `manual/endfield/characters.json` to add a character missing from Enka.", ""]
    lines += [f"- {f'`{identifier}`' if character_id(identifier) else cell(identifier)} — **{cell(name)}**: {cell(reason)}."
              for identifier, (name, reason) in sorted(left_out.items())]
    write_text(folder / "left-out.md", lines)

    absent = [row for row in variants if not row.get("image") or not (pack / row["image"]).is_file()]
    lines = ["# endfield: characters with no portrait", "",
             f"{characters(len(absent))} no picture. The app shows their initials instead.", "",
             "To add one, put `<internalName>.png`, `.jpg`, `.jpeg` or `.webp` in `manual/endfield/images/`.", ""]
    lines += [f"- `{row['internalName']}` — {cell(row['displayName'])}: "
              f"{cell(missing.get(row['internalName'], 'No portrait file was produced.'))}" for row in absent]
    write_text(folder / "missing-images.md", lines)

    enka_ids = set(roster_details.sources)
    lines = ["# endfield: which character each outfit or source belongs to", "",
             "The parent shown is the one stored in the pack. Source rows describe the accepted hash matching decisions.", "",
             "| Outfit or folder | Belongs to | How it was decided | Confidence |", "|---|---|---|---|"]
    for row in variants:
        identifier = row["internalName"]
        rule = "Enka roster" if identifier in enka_ids else ("manual/endfield/characters.json" if identifier in hand.declared_ids else "manual/endfield/hashes/")
        # Parent overrides can also explicitly make a character independent.
        if identifier in overrides.parents:
            rule = "overrides.parents"
        elif row.get("baseCharacterId") and identifier in hand.declared_ids:
            rule += " (outfitOf)"
        parent = row.get("baseCharacterId")
        lines.append(f"| `{identifier}` — {cell(row['displayName'])} | "
                     f"{f'`{parent}`' if parent else '— (a character of its own)'} | {rule} | explicit |")
    lines += [f"| {cell(name)} | `{target}` | {cell(rule)} | {'explicit' if rule.startswith('overrides.') else 'unique match'} |"
              for name, target, rule in sorted(hash_details.matches)]
    write_text(folder / "inference-report.md", lines)

    shared = audit["sharedHashes"]
    lines = ["# endfield: hashes more than one character has", "",
             f"{len(shared)} {'hash is' if len(shared) == 1 else 'hashes are'} shared. Ignored hashes are left out; each owner is counted once across kinds and components.", "",
             "| Hash | Characters |", "|---|---|"]
    lines += [f"| `{row['hash']}` | {len(row['characters'])}: " + ", ".join(f"`{name}`" for name in row["characters"]) + " |" for row in shared]
    write_text(folder / "collisions.md", lines)

    current = {hashes.key(entry) for entry in payload["entries"]} & hash_details.current_keys
    counts = collections.defaultdict(collections.Counter)
    for entry in payload["entries"]:
        key = hashes.key(entry)
        kind = "current" if key in current else "historical" if key in automatic_keys else "manual"
        counts[entry["variant"]][kind] += 1
    lines = ["# endfield: hashes from older versions of upstream's files", "",
             f"{audit['currentEntries']} current, {audit['historicalOnlyEntries']} historical-only and {audit['manualEntries']} manual-only hash associations in the pack.", "",
             "Current associations appear in the latest catalog. Historical-only associations come from saved catalog observations "
             "or the fixer's old and replacement values, outside that current set. Manual-only associations have no automatic match.", "",
             "Source observations stay in `ledger/endfield.json`; checksum-verified snapshots stay in `upstream/endfield/`. "
             "Snapshot dates are upstream commit dates, not inferred game versions.", "",
             "| Character | Current | Historical-only | Manual-only |", "|---|---|---|---|"]
    lines += [f"| `{row['internalName']}` — {cell(row['displayName'])} | {counts[row['internalName']]['current']} | "
              f"{counts[row['internalName']]['historical']} | {counts[row['internalName']]['manual']} |" for row in variants]
    write_text(folder / "history.md", lines)

    lines = ["# endfield: what manual/ added", ""]
    actions = list(hand.notes)
    if hand.characters:
        actions.insert(0, f"manual/endfield/characters.json: {len(hand.characters)} character or outfit declarations read.")
    for identifier, record in sorted(records.items()):
        if record.get("manual"):
            actions.append(f"{record['source']}: portrait for {identifier}; takes priority over cached and downloaded images.")
    if icon.get("manual"):
        actions.append(f"{icon['source']}: game icon; takes priority over cached and downloaded images.")
    lines += ["- " + cell(action) for action in actions] or ["No manual characters, hashes or images were applied."]
    write_text(folder / "manual.md", lines)


def write_blocked(folder: pathlib.Path, error: Exception) -> None:
    write_text(folder / "blocked.md", ["# endfield: build or publication stopped", "", cell(error), "",
                                      "The other reports describe the last successful local build."])
