"""Reports must describe the accepted pack after overrides, manual inputs and source failures."""

import copy
import pathlib
import tempfile
import unittest
from unittest import mock

from packbuilder import build
from packbuilder.files import BuildError, read_json, write_json
from test_builder import AVATARS, CATALOG, FIXER, ID, SOURCE_ID, LOCS, TODAY, FailedSources, fixture, png, snapshot

OTHER = "OtherCharacter"
SOURCE_OTHER = "chr_9999_other"
PART = "chr_9001_part"
COMMON = {"pending-hashes.md", "missing-images.md", "collisions.md", "left-out.md",
          "inference-report.md", "manual.md", "history.md"}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.sources = fixture(self.root)
        self.folder = self.root / "upstream/endfield"

    def tearDown(self):
        self.directory.cleanup()

    def run_build(self, **kwargs):
        return build.build(self.root, no_network=not kwargs, today=TODAY, **kwargs)

    def report(self, name):
        return (self.root / "reports/endfield" / name).read_text()

    def hand_file(self, relative, text):
        path = self.root / "manual/endfield" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def add_enka_character(self):
        avatars, locs = copy.deepcopy(AVATARS), copy.deepcopy(LOCS)
        avatars["2"] = {**avatars["1"], "StrId": SOURCE_OTHER, "NameHash": "2"}
        locs["en"]["2"] = "Other Character"
        write_json(self.folder / "avatars.json", avatars)
        write_json(self.folder / "locs.json", locs)

    def add_catalog_part(self):
        import json
        data = {**CATALOG, "character_id": PART}
        extra = snapshot(self.folder, "test/catalog", "catalog/part.json", json.dumps(data).encode())
        self.sources["catalog"]["snapshots"][0]["files"] += extra["snapshots"][0]["files"]
        write_json(self.folder / "sources.json", self.sources)

    def test_all_common_reports_exist_even_when_empty_and_remain_stable(self):
        first = self.run_build()
        folder = self.root / "reports/endfield"
        self.assertTrue(COMMON <= {path.name for path in folder.iterdir()})
        for name in ["pending-hashes.md", "missing-images.md", "collisions.md", "left-out.md"]:
            self.assertIn("\n0 ", self.report(name))
        self.assertIn("No manual characters, hashes or images were applied.", self.report("manual.md"))
        before = {name: self.report(name) for name in COMMON}
        archive = (self.root / "dist" / first["archive"]["url"]).read_bytes()
        second = self.run_build()
        self.assertFalse(second["changed"])
        self.assertEqual(first["packVersion"], second["packVersion"])
        self.assertEqual(archive, (self.root / "dist" / second["archive"]["url"]).read_bytes())
        self.assertEqual(before, {name: self.report(name) for name in COMMON})

    def test_pending_hashes_and_missing_portraits_are_independent(self):
        self.add_enka_character()
        write_json(self.root / "manual/endfield/characters.json", [{"name": "Manual Outfit", "outfitOf": "Ardelia"}])
        self.hand_file("hashes/Manual Outfit.txt", "ib 99887766\n")
        self.run_build()
        pending, missing = self.report("pending-hashes.md"), self.report("missing-images.md")
        self.assertIn(OTHER, pending)
        self.assertNotIn("ManualOutfit", pending)
        self.assertIn(OTHER, missing)
        self.assertIn("ManualOutfit", missing)
        self.assertNotIn(ID, missing)
        self.assertIn("network downloads are disabled", missing)
        self.assertIn(f"`{ID}`", self.report("inference-report.md"))
        self.assertIn("characters.json (outfitOf)", self.report("inference-report.md"))

    def test_manual_portrait_fills_missing_image_without_filling_pending_hashes(self):
        self.add_enka_character()
        path = self.root / f"manual/endfield/images/{OTHER}.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(png())
        self.run_build()
        self.assertIn(OTHER, self.report("pending-hashes.md"))
        self.assertIn("\n0 characters have no picture", self.report("missing-images.md"))
        self.assertIn(path.name.replace("_", r"\_"), self.report("manual.md"))

    def test_failed_enka_portrait_and_round_icon_have_a_reason(self):
        self.add_enka_character()
        result = self.run_build(fetcher=FailedSources())
        missing = self.report("missing-images.md")
        self.assertIn(OTHER, missing)
        self.assertIn("Enka portrait unavailable", missing)
        self.assertIn("round icon unavailable", missing)
        self.assertIn("source temporarily unavailable", missing)
        self.assertEqual(2, result["imageRequests"])

    def test_retained_round_icon_or_initial_image_is_not_reported_missing(self):
        for credit in [{"source": "https://enka.network/ui/ef/charroundicon/icon_round_" + SOURCE_ID + ".png", "roundIcon": True},
                       {"source": "https://credits.invalid/seed.png", "initialSeed": True}]:
            with self.subTest(credit=credit):
                records = read_json(self.folder / "images.json")
                records[ID].update(credit)
                write_json(self.folder / "images.json", records)
                self.run_build(fetcher=FailedSources())
                self.assertIn("\n0 characters have no picture", self.report("missing-images.md"))
                self.assertTrue(read_json(self.root / "packs/endfield/variants.json")[0]["image"])

    def test_excluded_roster_and_unmatched_hash_sources_are_named_with_reasons(self):
        self.add_enka_character()
        self.add_catalog_part()
        self.sources["fixer"] = snapshot(self.folder, "test/fixer", "fixer.py", FIXER.replace(b"Ardelia", b"Unknown Fixer"))
        write_json(self.folder / "sources.json", self.sources)
        write_json(self.root / "overrides/endfield.json", {"exclude": [OTHER]})
        self.run_build()
        left = self.report("left-out.md")
        self.assertIn("\n3 entries were left out", left)
        self.assertIn("Other Character", left)
        self.assertIn("Enka entry excluded by overrides.exclude", left)
        self.assertIn(PART, left)
        self.assertIn("observations remain in the ledger", left)
        self.assertIn("Unknown Fixer", left)
        self.assertIn("no unique roster match", left)
        self.assertNotIn(OTHER, self.report("pending-hashes.md"))
        self.assertNotIn(OTHER, self.report("missing-images.md"))

    def test_excluded_manual_character_is_reported(self):
        write_json(self.root / "manual/endfield/characters.json", [{"name": "Excluded Manual", "id": OTHER}])
        write_json(self.root / "overrides/endfield.json", {"exclude": [OTHER]})
        self.run_build()
        self.assertIn("Excluded Manual", self.report("left-out.md"))
        self.assertIn("Manual entry excluded by overrides.exclude", self.report("left-out.md"))
        self.assertNotIn("Added manual character", self.report("manual.md"))
        self.assertIn("excluded by overrides.exclude", self.report("manual.md"))

    def test_retired_entry_has_its_previous_name_and_removal_reason(self):
        self.add_enka_character()
        self.run_build()
        write_json(self.folder / "avatars.json", AVATARS)
        write_json(self.root / "overrides/endfield.json", {"retired": [OTHER]})
        self.run_build()
        self.assertIn("Other Character", self.report("left-out.md"))
        self.assertIn("overrides.retired", self.report("left-out.md"))

    def test_outfit_join_and_part_rules_describe_the_final_decisions(self):
        self.add_enka_character()
        self.add_catalog_part()
        self.sources["fixer"] = snapshot(self.folder, "test/fixer", "fixer.py", FIXER.replace(b"Ardelia", b"Custom Fixer"))
        write_json(self.folder / "sources.json", self.sources)
        write_json(self.root / "overrides/endfield.json", {"join": {"Custom Fixer": OTHER},
                   "parents": {OTHER: ID}, "partOf": {PART: ID}})
        self.run_build()
        inference = self.report("inference-report.md")
        self.assertIn(f"`{OTHER}` — Other Character | `{ID}` | overrides.parents", inference)
        self.assertIn(f"Custom Fixer | `{OTHER}` | overrides.join", inference)
        escaped_part = PART.replace("_", r"\_")
        self.assertIn(f"{escaped_part} | `{ID}` | overrides.partOf", inference)
        self.assertIn("\n0 entries were left out", self.report("left-out.md"))

    def test_shared_hashes_are_recomputed_after_manual_replacement_and_ignore_duplicates(self):
        self.hand_file("hashes/Other Character.txt", "ib abcdef12\n")
        self.run_build()
        self.assertIn("`abcdef12`", self.report("collisions.md"))
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        self.hand_file("hashes/replace/Ardelia.txt", "ib 99999991\nposition_vb 99999991\nib eeeeeeee\nroot_vs 123456789abcdef0\n")
        self.hand_file("hashes/Other Character.txt", "ib abcdef12\nib 99999991\ntexture 99999991\nib eeeeeeee\nroot_vs 123456789abcdef0\n")
        result = self.run_build()
        collision = self.report("collisions.md")
        self.assertIn("\n1 hash is shared", collision)
        self.assertIn("| `99999991` | 2:", collision)
        for ignored in ["abcdef12", "eeeeeeee", "123456789abcdef0"]:
            self.assertNotIn(ignored, collision)
        audit = result["hashes"]
        self.assertEqual([{"hash": "99999991", "characters": [ID, "OtherCharacter"]}], audit["sharedHashes"])
        self.assertEqual(0, audit["currentEntries"])
        self.assertEqual(0, audit["historicalOnlyEntries"])
        self.assertEqual(len(read_json(self.root / "packs/endfield/hashes.json")["entries"]), audit["manualEntries"])
        self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())
        self.assertIn("Replacement Ardelia", self.report("manual.md"))
        repeated = self.run_build()
        self.assertFalse(repeated["changed"])
        self.assertEqual(collision, self.report("collisions.md"))

    def test_ignored_override_applies_to_hashes_added_only_by_manual_files(self):
        self.hand_file("hashes/Ardelia.txt", "ib f1000001\nib f1000002\n")
        self.hand_file("hashes/Other Character.txt", "texture f1000001\ntexture f1000002\n")
        write_json(self.root / "overrides/endfield.json", {"ignoredHashes": ["f1000002"]})
        self.run_build()
        payload = read_json(self.root / "packs/endfield/hashes.json")
        self.assertIn("f1000002", payload["ignoredHashes"])
        self.assertIn("f1000001", self.report("collisions.md"))
        self.assertNotIn("f1000002", self.report("collisions.md"))
        self.assertTrue(any(row["hash"] == "f1000002" for row in payload["entries"]))

    def test_history_counts_use_only_retained_associations_after_manual_replacement(self):
        import json
        first = self.run_build()
        self.assertEqual(2, first["hashes"]["currentEntries"])
        self.assertEqual(first["hashes"]["totalEntries"] - 2, first["hashes"]["historicalOnlyEntries"])
        changed = copy.deepcopy(CATALOG)
        changed["entries"] = {"abcdef34": 0}
        changed["entry_identity_evidence"]["0"]["source_hash"] = "abcdef34"
        self.sources["catalog"] = snapshot(self.folder, "test/catalog", "catalog/ardelia.json", json.dumps(changed).encode(), "b" * 40)
        write_json(self.folder / "sources.json", self.sources)
        self.hand_file("hashes/replace/Ardelia.txt", "ib abcdef12\nib 99887766\n")
        result = self.run_build()
        self.assertEqual((0, 1, 1, 2), tuple(result["hashes"][key] for key in ["currentEntries", "historicalOnlyEntries", "manualEntries", "totalEntries"]))
        self.assertIn("0 current, 1 historical-only and 1 manual-only", self.report("history.md"))
        self.assertIn(f"`{ID}` — Ardelia | 0 | 1 | 1 |", self.report("history.md"))

    def test_join_and_part_chains_show_the_final_hash_owner(self):
        self.add_enka_character()
        self.add_catalog_part()
        self.sources["fixer"] = snapshot(self.folder, "test/fixer", "fixer.py", FIXER.replace(b"Ardelia", b"Custom Fixer"))
        write_json(self.folder / "sources.json", self.sources)
        write_json(self.root / "overrides/endfield.json", {"join": {"Custom Fixer": OTHER}, "partOf": {PART: OTHER, OTHER: ID}})
        self.run_build()
        inference = self.report("inference-report.md")
        self.assertIn(f"Custom Fixer | `{ID}` | overrides.join + overrides.partOf", inference)
        escaped_part = PART.replace("_", r"\_")
        self.assertIn(f"{escaped_part} | `{ID}` | overrides.partOf", inference)
        self.assertIn("\n0 entries were left out", self.report("left-out.md"))

    def test_manual_report_includes_portrait_and_game_icon_priority(self):
        directory = self.root / "manual/endfield/images"
        directory.mkdir(parents=True)
        (directory / f"{ID}.png").write_bytes(png())
        (directory / "_game.png").write_bytes(png())
        self.run_build()
        report = self.report("manual.md")
        escaped_id = ID.replace("_", r"\_")
        self.assertIn(f"{escaped_id}.png", report)
        self.assertIn("game.png", report)
        self.assertIn("portrait for", report)
        self.assertIn("game icon; takes priority", report)

    def test_reports_do_not_resolve_declarations_again_after_alias_corrections(self):
        write_json(self.root / "manual/endfield/characters.json", [{"name": "Ardelia"}, {"name": "Other", "id": OTHER}])
        write_json(self.root / "overrides/endfield.json", {"aliases": {OTHER: ["Ardelia"]}})
        self.run_build()
        self.assertIn("2 character or outfit declarations read", self.report("manual.md"))
        self.assertIn(f"`{OTHER}` — Other", self.report("inference-report.md"))

    def test_markdown_names_do_not_break_tables_or_create_links(self):
        display = "Special | [name](https://example.invalid)\n## Heading"
        write_json(self.root / "manual/endfield/characters.json", [{"name": "Manual", "id": OTHER, "displayName": display}])
        self.run_build()
        for name in ["history.md", "inference-report.md"]:
            row = next(line for line in self.report(name).splitlines() if line.startswith(f"| `{OTHER}`"))
            self.assertEqual(5, row.count("|"))
            self.assertIn("&#124;", row)
            self.assertIn(r"\[name\]", row)
            self.assertNotIn("\n## Heading", self.report(name))

    def test_failed_build_preserves_last_reports_and_success_clears_blocked(self):
        first = self.run_build()
        folder = self.root / "reports/endfield"
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        pack = build.fingerprint(self.root / "packs/endfield")
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        write_json(self.root / "overrides/endfield.json", {"parents": {ID: ID}})
        with self.assertRaises(BuildError):
            self.run_build()
        self.assertTrue((folder / "blocked.md").is_file())
        self.assertEqual(before, {name: (folder / name).read_bytes() for name in before})
        self.assertEqual(pack, build.fingerprint(self.root / "packs/endfield"))
        self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())
        write_json(self.root / "overrides/endfield.json", {})
        second = self.run_build()
        self.assertFalse(second["changed"])
        self.assertEqual(first["packVersion"], second["packVersion"])
        self.assertFalse((folder / "blocked.md").exists())

    def test_failed_new_hash_source_does_not_leak_decisions_into_saved_source_report(self):
        changed = snapshot(self.folder, "test/fixer", "fixer.py", FIXER.replace(b"Ardelia", b"Extra Source"), "b" * 40)
        broken = snapshot(self.folder, "test/fixer", "broken.py", b"CHARACTERS = []", "b" * 40)
        changed["snapshots"][0]["files"] += broken["snapshots"][0]["files"]
        write_json(self.root / "overrides/endfield.json", {"join": {"Extra Source": ID}})
        with mock.patch("packbuilder.upstream.refresh", side_effect=[self.sources["catalog"], changed]):
            result = self.run_build(fetcher=FailedSources())
        self.assertTrue(any("New hash source data failed validation" in note for note in result["warnings"]))
        self.assertNotIn("Extra Source", self.report("inference-report.md"))
        self.assertIn("Ardelia", self.report("inference-report.md"))
