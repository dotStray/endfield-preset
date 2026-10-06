"""Hand corrections must apply to real builds without changing saved source observations."""

import copy
import json
import pathlib
import tempfile
import unittest

from packbuilder import build
from packbuilder.files import BuildError, read_json, write_json
from test_builder import AVATARS, CATALOG, FIXER, ID, SOURCE_ID, LOCS, TODAY, fixture, snapshot

OTHER = "OtherCharacter"
SOURCE_OTHER = "chr_9999_other"


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.sources = fixture(self.root)
        self.path = self.root / "overrides/endfield.json"

    def tearDown(self):
        self.directory.cleanup()

    def run_build(self):
        return build.build(self.root, no_network=True, today=TODAY)

    def variants(self):
        return {row["internalName"]: row for row in read_json(self.root / "packs/endfield/variants.json")}

    def add_character(self):
        avatars, locs = copy.deepcopy(AVATARS), copy.deepcopy(LOCS)
        avatars["2"] = {**avatars["1"], "StrId": SOURCE_OTHER, "NameHash": "2"}
        locs["en"]["2"] = "Other Character"
        write_json(self.root / "upstream/endfield/avatars.json", avatars)
        write_json(self.root / "upstream/endfield/locs.json", locs)

    def test_display_name_and_alias_corrections_preserve_id_folder_and_portrait(self):
        self.run_build()
        image = (self.root / f"packs/endfield/images/{ID}.webp").read_bytes()
        write_json(self.path, {"displayNames": {ID: "Corrected Name"}, "aliases": {ID: ["Hand Alias"]}})
        source = self.path.read_bytes()
        self.run_build()
        row = self.variants()[ID]
        self.assertEqual("Corrected Name", row["displayName"])
        self.assertEqual("Ardelia", row["modFilesName"])
        self.assertTrue({"Ardelia", "Hand Alias", "阿黛莉娅"} <= set(row["aliases"]))
        self.assertEqual(image, (self.root / "packs/endfield" / row["image"]).read_bytes())
        self.assertEqual(source, self.path.read_bytes(), "the builder must not rewrite hand corrections")

    def test_join_maps_a_fixer_name_to_the_enka_id(self):
        folder = self.root / "upstream/endfield"
        self.sources["fixer"] = snapshot(folder, "test/fixer", "fixer.py", FIXER.replace(b"Ardelia", b"Unmatched Name"))
        write_json(folder / "sources.json", self.sources)
        write_json(self.path, {"join": {"Unmatched Name": ID}})
        self.run_build()
        entries = read_json(self.root / "packs/endfield/hashes.json")["entries"]
        self.assertTrue({"11111111", "44444444", "77777777", "88888888", "bb000001"} <= {row["hash"] for row in entries})
        self.assertEqual({ID}, {row["variant"] for row in entries})
        self.assertEqual([], read_json(self.root / "reports/endfield/hash-audit.json")["unmatchedFixerNames"])

    def test_correcting_a_join_reassigns_saved_fixer_hashes_without_stale_owners(self):
        self.add_character()
        self.run_build()
        old_records = read_json(self.root / "ledger/endfield.json")["records"]
        write_json(self.path, {"join": {"Ardelia": OTHER}, "allowShrink": [ID]})
        first = self.run_build()
        entries = read_json(self.root / "packs/endfield/hashes.json")["entries"]
        self.assertEqual({"abcdef12", "aa000001"}, {row["hash"] for row in entries if row["variant"] == ID})
        self.assertTrue({"11111111", "77777777", "88888888"} <= {row["hash"] for row in entries if row["variant"] == OTHER})
        records = read_json(self.root / "ledger/endfield.json")["records"]
        self.assertTrue(all(record in records for record in old_records))
        repeated = self.run_build()
        self.assertFalse(repeated["changed"])
        self.assertEqual(first["archive"], repeated["archive"])

    def test_display_name_override_can_supply_missing_source_localization(self):
        locs = copy.deepcopy(LOCS)
        del locs["en"]["1"]
        write_json(self.root / "upstream/endfield/locs.json", locs)
        write_json(self.path, {"displayNames": {SOURCE_ID: "Ardelia"}})
        self.run_build()
        self.assertEqual("Ardelia", self.variants()[ID]["displayName"])

    def test_deprecated_config_corrections_are_refused_instead_of_silently_ignored(self):
        self.run_build()
        before = build.fingerprint(self.root / "packs/endfield")
        for group, field, value in [("roster", "excludedIds", []), ("hashes", "fixerAliases", {})]:
            with self.subTest(field=field):
                config = read_json(self.root / "config/endfield.json")
                config[group][field] = value
                write_json(self.root / "config/endfield.json", config)
                with self.assertRaisesRegex(BuildError, "overrides/endfield.json"):
                    self.run_build()
                self.assertEqual(before, build.fingerprint(self.root / "packs/endfield"))
                del config[group][field]
                write_json(self.root / "config/endfield.json", config)

    def test_part_of_assigns_catalog_parts_without_rewriting_the_ledger(self):
        folder = self.root / "upstream/endfield"
        part = copy.deepcopy(CATALOG)
        part["character_id"] = SOURCE_OTHER
        part["entries"] = {"abcdef34": 0}
        part["entry_identity_evidence"]["0"]["source_hash"] = "abcdef34"
        part["textures"] = {}
        extra = snapshot(folder, "test/catalog", "catalog/part.json", json.dumps(part).encode())
        self.sources["catalog"]["snapshots"][0]["files"].extend(extra["snapshots"][0]["files"])
        write_json(folder / "sources.json", self.sources)
        self.run_build()
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        write_json(self.path, {"partOf": {SOURCE_OTHER: ID}})
        self.run_build()
        entry = next(row for row in read_json(self.root / "packs/endfield/hashes.json")["entries"] if row["hash"] == "abcdef34")
        self.assertEqual(ID, entry["variant"])
        self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())
        self.assertEqual({ID}, set(self.variants()), "a hash part must not invent a roster character")

    def test_parents_groups_existing_enka_entries_as_an_outfit(self):
        self.add_character()
        write_json(self.path, {"parents": {OTHER: ID}})
        result = self.run_build()
        rows = self.variants()
        self.assertEqual(ID, rows[OTHER]["baseCharacterId"])
        self.assertFalse(rows[OTHER]["isDefaultVariant"])
        self.assertTrue(rows[ID]["isDefaultVariant"])
        self.assertEqual(1, result["counts"]["skins"])

    def test_parent_cycle_is_refused(self):
        self.add_character()
        write_json(self.path, {"parents": {OTHER: ID, ID: OTHER}})
        with self.assertRaisesRegex(BuildError, "cycle"):
            self.run_build()

    def test_part_ownership_cycle_is_refused(self):
        self.add_character()
        write_json(self.path, {"partOf": {OTHER: ID, ID: OTHER}})
        with self.assertRaisesRegex(BuildError, "cycle"):
            self.run_build()

    def test_exclusion_requires_retirement_before_removing_a_published_character(self):
        self.add_character()
        self.run_build()
        before = build.fingerprint(self.root / "packs/endfield")
        write_json(self.path, {"exclude": [OTHER]})
        with self.assertRaisesRegex(BuildError, "retired"):
            self.run_build()
        self.assertEqual(before, build.fingerprint(self.root / "packs/endfield"))
        write_json(self.path, {"exclude": [OTHER], "retired": [OTHER]})
        self.run_build()
        self.assertEqual({ID}, set(self.variants()))

    def test_retirement_allows_an_actual_enka_removal_and_preserves_old_hash_evidence(self):
        self.add_character()
        self.run_build()
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        write_json(self.root / "upstream/endfield/avatars.json", {"2": {**AVATARS["1"], "StrId": SOURCE_OTHER, "NameHash": "2"}})
        with self.assertRaisesRegex(BuildError, "retired"):
            self.run_build()
        write_json(self.path, {"retired": [ID]})
        self.run_build()
        self.assertEqual({OTHER}, set(self.variants()))
        self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())

    def test_hash_reassignment_requires_allow_shrink_and_keeps_raw_history(self):
        self.add_character()
        self.run_build()
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        write_json(self.path, {"partOf": {ID: OTHER}})
        with self.assertRaisesRegex(BuildError, "allowShrink"):
            self.run_build()
        write_json(self.path, {"partOf": {ID: OTHER}, "allowShrink": [ID]})
        self.run_build()
        self.assertEqual({OTHER}, {row["variant"] for row in read_json(self.root / "packs/endfield/hashes.json")["entries"]})
        self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())

    def test_ignored_hash_correction_can_be_removed_without_polluting_saved_history(self):
        self.run_build()
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        write_json(self.path, {"ignoredHashes": ["ABCDEF12"]})
        self.run_build()
        self.assertIn("abcdef12", read_json(self.root / "packs/endfield/hashes.json")["ignoredHashes"])
        self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())
        write_json(self.path, {})
        self.run_build()
        self.assertNotIn("abcdef12", read_json(self.root / "packs/endfield/hashes.json")["ignoredHashes"])

    def test_invalid_overrides_block_without_replacing_pack_ledger_or_manual_rules(self):
        self.run_build()
        before = build.fingerprint(self.root / "packs/endfield")
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        cases = [[], {"displayName": {}}, {"notes": 123}, {"join": []}, {"join": {"name": None}},
                 {"displayNames": {ID: ""}}, {"aliases": {ID: "wrong"}}, {"aliases": {ID: [123]}},
                 {"exclude": ID}, {"parents": {ID: 123}}, {"ignoredHashes": ["not a hash"]},
                 {"allowShrink": ["not an Enka ID"]}]
        for case in cases:
            with self.subTest(case=case):
                write_json(self.path, case)
                rules = self.path.read_bytes()
                with self.assertRaises(BuildError):
                    self.run_build()
                self.assertEqual(before, build.fingerprint(self.root / "packs/endfield"))
                self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())
                self.assertEqual(rules, self.path.read_bytes())
                self.assertTrue((self.root / "reports/endfield/blocked.md").is_file())

    def test_unknown_targets_are_refused_instead_of_silently_ignoring_corrections(self):
        cases = [{"displayNames": {OTHER: "Name"}}, {"aliases": {OTHER: ["Alias"]}},
                 {"join": {"name": OTHER}}, {"partOf": {"part": OTHER}},
                 {"parents": {ID: OTHER}}, {"allowShrink": [OTHER]}]
        for case in cases:
            with self.subTest(case=case):
                write_json(self.path, case)
                with self.assertRaises(BuildError):
                    self.run_build()

    def test_conflicting_normalized_source_names_and_duplicate_json_keys_are_refused(self):
        self.add_character()
        cases = [{"join": {"Ardelia": ID, "ardelia": OTHER}},
                 {"join": {"Ardelia": ID}, "partOf": {"Ardelia": OTHER}}]
        for case in cases:
            with self.subTest(case=case):
                write_json(self.path, case)
                with self.assertRaises(BuildError):
                    self.run_build()
        self.path.write_text('{"exclude": [], "exclude": ["' + ID + '"]}')
        with self.assertRaisesRegex(BuildError, "Duplicate"):
            self.run_build()


if __name__ == "__main__":
    unittest.main()
