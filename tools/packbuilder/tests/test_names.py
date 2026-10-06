"""Pack names are user folders, independent of source IDs and future display-name changes."""

import copy
import pathlib
import tempfile
import unittest

from packbuilder import build, roster
from packbuilder.files import BuildError, read_json, write_json
from packbuilder.names import pascal
from packbuilder.settings import Overrides
from test_builder import AVATARS, CONFIG, ID, SOURCE_ID, LOCS, TODAY, fixture, png


class NameTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        fixture(self.root)

    def run_build(self):
        return build.build(self.root, no_network=True, today=TODAY)

    def test_display_names_become_readable_names_with_the_other_builders_rules(self):
        for display, expected in [("Chen Qianyu", "ChenQianyu"), ("Zhuang Fangyi", "ZhuangFangyi"),
                                  ("Endministrator (Male)", "EndministratorMale"), ("Ch'en", "Chen"),
                                  ("Héloïse", "Heloise"), ("Last Rite", "LastRite")]:
            with self.subTest(display=display):
                locs = copy.deepcopy(LOCS)
                locs["en"]["1"] = display
                row = roster.assemble(AVATARS, locs, [], CONFIG, Overrides())[1][0]
                self.assertEqual(expected, row["internalName"])
                self.assertEqual(expected, row["modFilesName"])
                self.assertEqual(expected, pascal(display))

    def test_names_stay_pinned_after_enka_rename_and_display_override(self):
        self.run_build()
        ledger = read_json(self.root / "ledger/endfield.json")
        before = (self.root / f"packs/endfield/images/{ID}.webp").read_bytes()
        locs = copy.deepcopy(LOCS)
        locs["en"]["1"] = "New Official Name"
        write_json(self.root / "upstream/endfield/locs.json", locs)
        write_json(self.root / "overrides/endfield.json", {"displayNames": {ID: "Chosen Display Name"}})
        self.run_build()
        row = read_json(self.root / "packs/endfield/variants.json")[0]
        self.assertEqual((ID, ID, "Chosen Display Name"), (row["internalName"], row["modFilesName"], row["displayName"]))
        self.assertEqual(ledger["names"], read_json(self.root / "ledger/endfield.json")["names"])
        self.assertEqual(before, (self.root / row["image"].replace("images/", "packs/endfield/images/")).read_bytes())
        self.assertEqual({ID}, {entry["variant"] for entry in read_json(self.root / "packs/endfield/hashes.json")["entries"]})

    def test_source_join_distinguishes_two_roster_entries_with_the_same_display_name(self):
        avatars = copy.deepcopy(AVATARS)
        avatars["2"] = {**avatars["1"], "StrId": "chr_9999_other"}
        overrides = Overrides(join={SOURCE_ID: "ArdeliaBase", "chr_9999_other": "ArdeliaOutfit"},
                              parents={"ArdeliaOutfit": "ArdeliaBase"})
        details = roster.Details()
        variants = roster.assemble(avatars, LOCS, [], CONFIG, overrides, details=details)[1]
        self.assertEqual({"ArdeliaBase", "ArdeliaOutfit"}, {row["internalName"] for row in variants})
        self.assertEqual("ArdeliaBase", next(row["baseCharacterId"] for row in variants if row["internalName"] == "ArdeliaOutfit"))
        self.assertEqual({SOURCE_ID: "ArdeliaBase", "chr_9999_other": "ArdeliaOutfit"}, details.names)

    def test_legacy_pack_migration_preserves_hashes_portrait_bytes_and_source_observations(self):
        self.run_build()
        pack = self.root / "packs/endfield"
        entries = read_json(pack / "hashes.json")
        ledger = read_json(self.root / "ledger/endfield.json")
        image = pack / f"images/{ID}.webp"
        portrait = image.read_bytes()
        image.rename(pack / f"images/{SOURCE_ID}.webp")
        variants = read_json(pack / "variants.json")
        variants[0].update(internalName=SOURCE_ID, image=f"images/{SOURCE_ID}.webp")
        write_json(pack / "variants.json", variants)
        write_json(pack / "hashes.json", {**entries, "entries": [{**entry, "variant": SOURCE_ID} for entry in entries["entries"]]})
        records = read_json(self.root / "upstream/endfield/images.json")
        write_json(self.root / "upstream/endfield/images.json", {SOURCE_ID: {**records[ID], "file": f"images/{SOURCE_ID}.webp"}})
        write_json(self.root / "ledger/endfield.json", {key: value for key, value in ledger.items() if key != "names"})
        result = self.run_build()
        self.assertTrue(result["changed"])
        self.assertEqual(entries, read_json(pack / "hashes.json"))
        self.assertEqual(ledger["records"], read_json(self.root / "ledger/endfield.json")["records"])
        self.assertEqual(portrait, (pack / f"images/{ID}.webp").read_bytes())
        self.assertFalse((pack / f"images/{SOURCE_ID}.webp").exists())
        self.assertEqual(ID, read_json(pack / "variants.json")[0]["modFilesName"])
        self.assertFalse(self.run_build()["changed"])

    def test_manual_image_using_a_source_id_keeps_priority_and_exports_under_readable_name(self):
        path = self.root / f"manual/endfield/images/{SOURCE_ID}.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(png())
        self.run_build()
        row = read_json(self.root / "packs/endfield/variants.json")[0]
        self.assertEqual(f"images/{ID}.webp", row["image"])
        self.assertTrue((self.root / "packs/endfield" / row["image"]).is_file())
        self.assertTrue(read_json(self.root / "upstream/endfield/images.json")[ID]["manual"])

    def test_invalid_pinned_name_blocks_without_replacing_the_pack(self):
        self.run_build()
        before = build.fingerprint(self.root / "packs/endfield")
        ledger = read_json(self.root / "ledger/endfield.json")
        ledger["names"][SOURCE_ID] = "../invalid"
        write_json(self.root / "ledger/endfield.json", ledger)
        with self.assertRaises(BuildError):
            self.run_build()
        self.assertEqual(before, build.fingerprint(self.root / "packs/endfield"))
