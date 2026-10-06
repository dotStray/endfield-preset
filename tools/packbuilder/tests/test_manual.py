"""Exercise hand inputs through the real staged builder, including reversions and failures."""

import pathlib
import tempfile
import unittest

from packbuilder import build
from packbuilder.files import BuildError, read_json, write_json
from test_builder import ID, TODAY, fixture, png


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        fixture(self.root)
        self.run_build()
        self.original = self.payload()
        self.ledger = read_json(self.root / "ledger/endfield.json")

    def run_build(self):
        return build.build(self.root, no_network=True, today=TODAY)

    def payload(self):
        return read_json(self.root / "packs/endfield/hashes.json")

    def variants(self):
        return {row["internalName"]: row for row in read_json(self.root / "packs/endfield/variants.json")}

    def text(self, relative, content):
        path = self.root / "manual/endfield" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def definitions(self, value):
        write_json(self.root / "manual/endfield/characters.json", value)

    def test_add_pasted_ini_bare_hashes_and_shader_exclusion_without_changing_ledger(self):
        self.text(f"hashes/{ID}.txt", "ib 11111111\n12345678\n[TextureOverrideExtraPosition]\nhash = F0000001\n[ShaderOverrideGlobal]\nhash = 0123456789abcdef\n")
        first = self.run_build()
        entries = {(row["kind"], row["hash"]) for row in self.payload()["entries"]}
        self.assertTrue({("position_vb", "f0000001"), ("unknown", "12345678"), ("root_vs", "0123456789abcdef")} <= entries)
        self.assertEqual(1, sum(row["hash"] == "11111111" for row in self.payload()["entries"]))
        self.assertIn("0123456789abcdef", self.payload()["ignoredHashes"])
        self.assertEqual(self.ledger, read_json(self.root / "ledger/endfield.json"))
        self.assertEqual(first["packVersion"], self.run_build()["packVersion"])
        self.assertFalse(self.run_build()["changed"])

    def test_display_name_and_localized_alias_files_find_existing_id(self):
        self.text("hashes/ar de lia.txt", "ib f0000001")
        self.text("hashes/阿黛莉娅.ini", "[TextureOverrideExtraIB]\nhash = f0000002")
        self.run_build()
        self.assertEqual({ID}, set(self.variants()))
        self.assertTrue({"f0000001", "f0000002"} <= {entry["hash"] for entry in self.payload()["entries"]})

    def test_component_hash_json_includes_texture_and_buffer_kinds(self):
        write_json(self.root / f"manual/endfield/hashes/{ID}.json", [
            {"component_name": "Body", "ib": "f0000001", "blend_vb": "f0000002", "root_vs": "0123456789abcdef",
             "texture_hashes": [[["Diffuse", ".dds", "f0000003"]]]}])
        self.run_build()
        entries = self.payload()["entries"]
        self.assertEqual("texture", next(row["kind"] for row in entries if row["hash"] == "f0000003"))
        self.assertEqual("Diffuse", next(row["textureKind"] for row in entries if row["hash"] == "f0000003"))
        self.assertEqual("blend_vb", next(row["kind"] for row in entries if row["hash"] == "f0000002"))

    def test_replacement_and_additions_then_deletion_restores_automatic_hashes(self):
        replacement = self.text(f"hashes/replace/{ID}.txt", "ib f0000001")
        added = self.text(f"hashes/{ID}.txt", "position_vb f0000002")
        self.run_build()
        self.assertEqual({"f0000001", "f0000002"}, {row["hash"] for row in self.payload()["entries"]})
        self.assertEqual(self.ledger, read_json(self.root / "ledger/endfield.json"))
        added.unlink()
        replacement.unlink()
        self.run_build()
        self.assertEqual(self.original, self.payload())
        self.assertEqual(self.ledger, read_json(self.root / "ledger/endfield.json"))

    def test_unknown_addition_makes_a_character_with_stable_id_and_portrait(self):
        self.text("hashes/New Operator.txt", "ib f0000001")
        name = "NewOperator"
        path = self.root / f"manual/endfield/images/{name}.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(png())
        self.run_build()
        variant = self.variants()[name]
        self.assertFalse(variant["hashesPending"])
        self.assertTrue((self.root / "packs/endfield" / variant["image"]).is_file())
        self.assertEqual(name, variant["modFilesName"])
        self.assertIn("matched no character", (self.root / "reports/endfield/manual.md").read_text())
        self.assertFalse(self.run_build()["changed"])

    def test_manual_characters_outfits_and_out_of_order_parent_declarations(self):
        base, outfit = "New", "NewSummer"
        self.definitions([{"name": "Summer Night", "id": outfit, "outfitOf": "New Operator"},
                          {"name": "New Operator", "id": base, "displayName": "New Operator Display"}])
        self.text("hashes/Summer Night.txt", "ib f0000001")
        self.run_build()
        variants = self.variants()
        self.assertEqual(base, variants[outfit]["baseCharacterId"])
        self.assertFalse(variants[outfit]["isDefaultVariant"])
        self.assertTrue(variants[base]["hashesPending"])
        self.assertEqual({}, variants[base]["attributes"])
        self.assertFalse(self.run_build()["changed"])

    def test_outfit_inherits_source_parent_attributes(self):
        self.definitions([{"name": "Spring Outfit", "id": "Spring", "outfitOf": "Ardelia"}])
        self.run_build()
        variants = self.variants()
        self.assertEqual(variants[ID]["attributes"], variants["Spring"]["attributes"])

    def test_overrides_can_correct_manual_character_names_and_aliases(self):
        identifier = "Test"
        self.definitions([{"name": "My Character", "id": identifier}])
        write_json(self.root / "overrides/endfield.json", {"displayNames": {identifier: "Corrected"}, "aliases": {identifier: ["Extra Name"]}})
        self.text("hashes/Extra Name.txt", "ib f0000001")
        self.run_build()
        self.assertEqual("Corrected", self.variants()[identifier]["displayName"])
        self.assertEqual(identifier, next(row["variant"] for row in self.payload()["entries"] if row["hash"] == "f0000001"))

    def test_removing_published_manual_character_requires_retirement(self):
        self.definitions([{"name": "New", "id": "New"}])
        self.run_build()
        self.definitions([])
        with self.assertRaises(BuildError):
            self.run_build()
        write_json(self.root / "overrides/endfield.json", {"retired": ["New"]})
        self.run_build()
        self.assertEqual({ID}, set(self.variants()))

    def assert_blocked(self, action):
        before = {str(path.relative_to(self.root)): path.read_bytes() for folder in ("packs", "ledger", "upstream")
                  for path in (self.root / folder).rglob("*") if path.is_file()}
        action()
        with self.assertRaises(BuildError):
            self.run_build()
        after = {str(path.relative_to(self.root)): path.read_bytes() for folder in ("packs", "ledger", "upstream")
                 for path in (self.root / folder).rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue((self.root / "reports/endfield/blocked.md").exists())

    def test_unknown_replacement_blocks_instead_of_inventing_a_character(self):
        self.assert_blocked(lambda: self.text("hashes/replace/Typo.txt", "ib f0000001"))

    def test_malformed_json_blocks_instead_of_being_ignored(self):
        self.assert_blocked(lambda: self.text(f"hashes/{ID}.json", "[broken"))

    def test_empty_hash_file_blocks(self):
        self.assert_blocked(lambda: self.text(f"hashes/{ID}.txt", "no hashes here"))

    def test_missing_parent_and_cyclic_outfits_block(self):
        self.assert_blocked(lambda: self.definitions([{"name": "Summer", "outfitOf": "Nobody"}]))
        self.assert_blocked(lambda: self.definitions([{"name": "A", "outfitOf": "B"}, {"name": "B", "outfitOf": "A"}]))

    def test_duplicate_ids_and_ambiguous_names_block(self):
        self.assert_blocked(lambda: self.definitions([{"name": "One", "id": "Same"}, {"name": "Two", "id": "Same"}]))
        self.definitions([{"name": "Same", "id": "One"}, {"name": "Same", "id": "Two"}])
        self.assert_blocked(lambda: self.text("hashes/Same.txt", "ib f0000001"))

    def test_manual_symlinks_are_not_followed(self):
        external = self.root / "external.txt"
        external.write_text("ib f0000001")
        def add_link():
            path = self.root / f"manual/endfield/hashes/{ID}.txt"
            path.parent.mkdir(parents=True)
            path.symlink_to(external)
        self.assert_blocked(add_link)
