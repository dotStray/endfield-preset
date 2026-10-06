"""Source changes, failed updates and publication must preserve usable packs and provenance."""

import copy
import datetime
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from PIL import Image

from packbuilder import build, hashes, images, release, roster, upstream
from packbuilder.files import BuildError, commit_files, read_json, write_json
from packbuilder.http import FetchError
from packbuilder.settings import Overrides

ID = "Ardelia"
SOURCE_ID = "chr_0025_ardelia"
TODAY = datetime.date(2026, 10, 6)
CONFIG = {"repository": "dotStray/endfield-preset", "gameId": "endfield", "displayName": "Arknights: Endfield",
          "shortName": "EF", "importer": "EFMI", "minAppVersion": "0.1.0", "disabledPrefix": "DISABLED_",
          "roster": {"url": "https://raw.githubusercontent.com/EnkaNetwork/API-docs/master/store/ef/"},
          "hashes": {"catalog": {"repo": "test/catalog", "path": "catalog", "license": "AGPL-3.0"},
                     "fixer": {"repo": "test/fixer", "path": "fixer.py"}}}
AVATARS = {"1": {"StrId": SOURCE_ID, "NameHash": "1", "Element": "Natural", "Profession": "SUPPORTER", "WeaponType": "Wand", "Rarity": 6}}
LOCS = {"en": {"1": "Ardelia"}, "zh": {"1": "阿黛莉娅"}}
CATALOG = {"kind": "bem-character-catalog", "identity_policy": "efmi-dx11-region-texture0-fullchain-v1",
           "character_id": SOURCE_ID, "entries": {"abcdef12": 0}, "textures": {"aa000001": {"name": "Diffuse", "native_crc": "87654321"}},
           "entry_identity_evidence": {"0": {"source_hash": "abcdef12", "payload_sha256": "01234567" * 8}}, "preserved_globals": {"eeeeeeee": {}}}
FIXER = b'''CHARACTERS = [{"name": "Ardelia", "components": [{"index": 0,
"ib": ("11111111", "22222222"), "vb0": ("33333333", "44444444"),
"lod_ib": (None, "55555555"), "lod_vb0": (None, "66666666")}], "textures": [("aa000001", "bb000001")]}]
BUILTIN_WET_FIX_TEMPLATES = [{"name": "Ardelia", "overrides": [("Wet", "77777777", 42, "resource")]}]
SPECIAL_CASES = [SpecialCase("Ardelia", "22222222", "88888888", 42, "_Patched")]
OLD_SHADER_HASHES = {"123456789abcdef0"}
HASH_RULES = {"global": ("eeeeeeee", "ffffffff")}
'''


def png():
    output = io.BytesIO()
    Image.new("RGBA", (96, 64), (90, 120, 180, 255)).save(output, "PNG")
    return output.getvalue()


def snapshot(folder, repo, path, data, commit="a" * 40):
    sha = upstream.blob_sha(data)
    (folder / "blobs").mkdir(parents=True, exist_ok=True)
    (folder / "blobs" / sha).write_bytes(data)
    return {"repo": repo, "path": path, "head": commit, "headDate": "2026-10-01T00:00:00Z",
            "snapshots": [{"commit": commit, "date": "2026-10-01T00:00:00Z", "files": [{"path": path, "sha": sha}]}]}


def fixture(root):
    write_json(root / "config/endfield.json", CONFIG)
    folder = root / "upstream/endfield"
    write_json(folder / "avatars.json", AVATARS)
    write_json(folder / "locs.json", LOCS)
    sources = {"catalog": snapshot(folder, "test/catalog", "catalog/ardelia.json", json.dumps(CATALOG).encode()),
               "fixer": snapshot(folder, "test/fixer", "fixer.py", FIXER)}
    write_json(folder / "sources.json", sources)
    path = root / f"packs/endfield/images/{ID}.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(png())
    write_json(folder / "images.json", {ID: {"source": f"https://enka.network/ui/ef/charremoteicon/icon_{SOURCE_ID}.png",
                                          "sha256": images.digest(png()), "file": f"images/{ID}.png"}})
    return sources


class FailedSources:
    def __init__(self):
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(url)
        raise FetchError("source temporarily unavailable", 503)

    def json(self, url, **kwargs):
        return json.loads(self.get(url, **kwargs))


class HashTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.sources = fixture(self.root)
        self.folder = self.root / "upstream/endfield"
        self.variants = roster.assemble(AVATARS, LOCS, [], CONFIG, Overrides())[1]

    def tearDown(self):
        self.directory.cleanup()

    def collect(self, previous=None):
        return hashes.collect(self.folder, self.sources, self.variants, Overrides(), previous or {}, identities={SOURCE_ID: ID})

    def test_both_buffer_pair_members_lod_wet_texture_and_special_cases(self):
        payload, ledger, report = self.collect()
        values = {row["hash"] for row in payload["entries"]}
        self.assertTrue({"11111111", "22222222", "33333333", "44444444", "55555555", "66666666", "77777777", "88888888", "aa000001", "bb000001"} <= values)
        self.assertNotIn("87654321", values)
        self.assertNotIn("eeeeeeee", values)
        self.assertIn("123456789abcdef0", ledger["ignoredHashes"])
        patched = next(row for row in ledger["records"] if row["entry"]["hash"] == "88888888")
        self.assertEqual("SPECIAL_CASES/Ardelia/new_hash", patched["origins"][0]["field"])

    def test_upstream_rewrite_preserves_old_observations_without_duplicate_weight(self):
        payload, previous, report = self.collect()
        changed = copy.deepcopy(CATALOG)
        changed["entries"] = {"abcdef34": 0}
        changed["entry_identity_evidence"]["0"]["source_hash"] = "abcdef34"
        self.sources["catalog"] = snapshot(self.folder, "test/catalog", "catalog/ardelia.json", json.dumps(changed).encode(), "b" * 40)
        fresh, ledger, report = self.collect(previous)
        self.assertTrue({hashes.key(row) for row in payload["entries"]} <= {hashes.key(row) for row in fresh["entries"]})
        self.assertEqual(len(fresh["entries"]), len({hashes.key(row) for row in fresh["entries"]}))
        repeated = self.collect(ledger)
        self.assertEqual((fresh, ledger), repeated[:2])

    def test_catalog_alias_is_historical_and_native_ids_are_not_collected(self):
        data = copy.deepcopy(CATALOG)
        data["entries"]["12345678"] = 0
        data["legacy_source_aliases"] = {"12345678": "abcdef12"}
        self.sources["catalog"] = snapshot(self.folder, "test/catalog", "catalog/a.json", json.dumps(data).encode())
        payload, ledger, report = self.collect()
        self.assertEqual(2, report["currentEntries"])
        record = next(row for row in ledger["records"] if row["entry"]["hash"] == "12345678")
        self.assertTrue(record["origins"][0]["legacyAlias"])

    def test_unrostered_catalog_identity_stays_in_ledger(self):
        other = copy.deepcopy(CATALOG)
        other["character_id"] = "chr_0038_purrche"
        extra = snapshot(self.folder, "test/catalog", "catalog/other.json", json.dumps(other).encode())
        self.sources["catalog"]["snapshots"][0]["files"] += extra["snapshots"][0]["files"]
        payload, ledger, report = self.collect()
        self.assertFalse(any(row["variant"] == other["character_id"] for row in payload["entries"]))
        self.assertTrue(any(row["entry"]["variant"] == other["character_id"] for row in ledger["records"]))

    def test_fixer_module_and_special_case_constructor_are_never_executed(self):
        text = FIXER + b'\ndef SpecialCase(*args): raise RuntimeError("executed")\nraise RuntimeError("module executed")\n'
        self.assertEqual("88888888", hashes.literals(text)["SPECIAL_CASES"][0]["new_hash"])
        with self.assertRaises(BuildError):
            hashes.literals(b'CHARACTERS = __import__("os").system("false")')

    def test_unfamiliar_identity_policy_is_refused(self):
        data = copy.deepcopy(CATALOG)
        data["identity_policy"] = "unknown-native-crc-policy"
        self.sources["catalog"] = snapshot(self.folder, "test/catalog", "catalog/a.json", json.dumps(data).encode())
        with self.assertRaises(BuildError):
            self.collect()

    def test_corrupt_saved_blob_is_refused(self):
        sha = self.sources["catalog"]["snapshots"][0]["files"][0]["sha"]
        (self.folder / "blobs" / sha).write_bytes(b"broken")
        with self.assertRaises(BuildError):
            self.collect()


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        fixture(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_identical_build_keeps_version_and_archive_bytes(self):
        first = build.build(self.root, no_network=True, today=TODAY)
        archive = (self.root / "dist" / first["archive"]["url"]).read_bytes()
        second = build.build(self.root, no_network=True, today=TODAY + datetime.timedelta(days=7))
        self.assertFalse(second["changed"])
        self.assertEqual(first["packVersion"], second["packVersion"])
        self.assertEqual(archive, (self.root / "dist" / second["archive"]["url"]).read_bytes())

    def test_failed_refresh_uses_saved_sources_and_does_not_redownload_portrait(self):
        first = build.build(self.root, no_network=True, today=TODAY)
        failed = FailedSources()
        second = build.build(self.root, fetcher=failed, today=TODAY)
        self.assertFalse(second["changed"])
        self.assertEqual(first["archive"]["sha256"], second["archive"]["sha256"])
        self.assertTrue(second["warnings"])
        self.assertEqual(0, second["imageRequests"])

    def test_bad_saved_source_leaves_pack_and_ledger_intact(self):
        build.build(self.root, no_network=True, today=TODAY)
        before = build.fingerprint(self.root / "packs/endfield")
        ledger = (self.root / "ledger/endfield.json").read_bytes()
        source = read_json(self.root / "upstream/endfield/sources.json")["fixer"]
        (self.root / "upstream/endfield/blobs" / source["snapshots"][0]["files"][0]["sha"]).write_bytes(b"broken")
        with self.assertRaises(BuildError):
            build.build(self.root, no_network=True, today=TODAY)
        self.assertEqual(before, build.fingerprint(self.root / "packs/endfield"))
        self.assertEqual(ledger, (self.root / "ledger/endfield.json").read_bytes())
        self.assertTrue((self.root / "reports/endfield/blocked.md").is_file())

    def test_corrupt_portrait_cannot_remove_existing_image_in_offline_build(self):
        build.build(self.root, no_network=True, today=TODAY)
        (self.root / f"packs/endfield/images/{ID}.webp").write_bytes(b"broken")
        with self.assertRaises(BuildError):
            build.build(self.root, no_network=True, today=TODAY)

    def test_game_icon_is_bundled_and_retained_on_store_failure_but_corruption_blocks(self):
        config = copy.deepcopy(CONFIG)
        config["icon"] = {"googlePlay": "com.gryphline.endfield.gp"}
        write_json(self.root / "config/endfield.json", config)
        fetcher = mock.Mock(requests=[])
        fetcher.json.side_effect = FetchError("unavailable", 503)
        fetcher.get.side_effect = [b'<meta property="og:image" content="https://play-lh.googleusercontent.com/test=s180">', png()]
        first = build.build(self.root, fetcher=fetcher, today=TODAY)
        self.assertEqual("images/_game.webp", read_json(self.root / "packs/endfield/game.json")["icon"])
        icon = self.root / "packs/endfield/images/_game.webp"
        before = icon.read_bytes()
        second = build.build(self.root, fetcher=FailedSources(), today=TODAY)
        self.assertFalse(second["changed"])
        self.assertEqual(first["archive"]["sha256"], second["archive"]["sha256"])
        self.assertEqual(before, icon.read_bytes())
        icon.write_bytes(b"corrupt")
        with self.assertRaises(BuildError):
            build.build(self.root, no_network=True, today=TODAY)

    def test_published_folder_name_and_old_alias_survive_enka_rename(self):
        build.build(self.root, no_network=True, today=TODAY)
        locs = copy.deepcopy(LOCS)
        locs["en"]["1"] = "New English Name"
        write_json(self.root / "upstream/endfield/locs.json", locs)
        result = build.build(self.root, no_network=True, today=TODAY)
        row = read_json(self.root / "packs/endfield/variants.json")[0]
        self.assertEqual("Ardelia", row["modFilesName"])
        self.assertIn("Ardelia", row["aliases"])
        self.assertEqual("2026.10.06.01", result["packVersion"])

    def test_partial_promotion_rolls_back_every_generated_path(self):
        root, stage = self.root / "target", self.root / "stage"
        for folder in [root, stage]:
            folder.mkdir()
        for name in ["pack", "ledger"]:
            (root / name).write_text("old " + name)
            (stage / name).write_text("new " + name)
        import os
        original = os.replace
        def fail(source, target):
            if pathlib.Path(source) == stage / "ledger":
                raise OSError("simulated disk failure")
            return original(source, target)
        with mock.patch("packbuilder.files.os.replace", side_effect=fail), self.assertRaises(OSError):
            commit_files(stage, root, ["pack", "ledger"])
        self.assertEqual("old pack", (root / "pack").read_text())
        self.assertEqual("old ledger", (root / "ledger").read_text())


class UpstreamTests(unittest.TestCase):
    def test_corrupt_local_blob_is_repaired_from_verified_download(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            saved = snapshot(folder, "test/fixer", "fixer.py", FIXER)
            sha = saved["snapshots"][0]["files"][0]["sha"]
            (folder / "blobs" / sha).write_bytes(b"corrupt local cache")
            fetcher = mock.Mock()
            fetcher.json.side_effect = [{"sha": saved["head"], "commit": {"committer": {"date": saved["headDate"]}}}, []]
            fetcher.get.return_value = FIXER
            fresh = upstream.refresh({"repo": "test/fixer", "path": "fixer.py"}, saved, folder, fetcher, catalog=False)
            self.assertEqual(FIXER, upstream.blob(folder, sha))
            self.assertEqual(saved["head"], fresh["head"])
            self.assertFalse(fetcher.get.call_args.kwargs["immutable"])

    def test_download_checksum_mismatch_does_not_write_blob_and_invalidates_response(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            saved = snapshot(folder, "test/fixer", "fixer.py", FIXER)
            sha = saved["snapshots"][0]["files"][0]["sha"]
            (folder / "blobs" / sha).unlink()
            fetcher = mock.Mock()
            fetcher.json.side_effect = [{"sha": saved["head"], "commit": {"committer": {"date": saved["headDate"]}}}, []]
            fetcher.get.return_value = b"incomplete download"
            with self.assertRaises(FetchError):
                upstream.refresh({"repo": "test/fixer", "path": "fixer.py"}, saved, folder, fetcher, catalog=False)
            self.assertFalse((folder / "blobs" / sha).exists())
            fetcher.invalidate.assert_called_once()


class PortraitTests(unittest.TestCase):
    def test_seed_is_reused_without_ever_downloading_its_external_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / f"old/images/{ID}.png"
            path.parent.mkdir(parents=True);path.write_bytes(png())
            records = {ID: {"file": f"images/{ID}.png", "source": "https://external.example/seed.png", "sha256": images.digest(png()), "initialSeed": True}}
            failed = FailedSources()
            variants = [{"internalName": ID}]
            result, notes = images.build(variants, root / "old", root / "new/images", records, failed, sources={ID: SOURCE_ID})
            self.assertTrue(result[ID]["initialSeed"])
            self.assertEqual(1, len(failed.requests))
            self.assertTrue(all(url.startswith("https://enka.network/") for url in failed.requests))

    def test_seed_upgrades_when_enka_portrait_becomes_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory);path = root / f"old/images/{ID}.png"
            path.parent.mkdir(parents=True);path.write_bytes(png())
            records = {ID: {"file": f"images/{ID}.png", "source": "https://external.example/seed.png", "sha256": images.digest(png()), "initialSeed": True}}
            fetcher = mock.Mock();fetcher.get.return_value = png()
            result, notes = images.build([{"internalName": ID}], root / "old", root / "new/images", records, fetcher, sources={ID: SOURCE_ID})
            self.assertFalse(result[ID]["initialSeed"])
            self.assertTrue(result[ID]["source"].startswith("https://enka.network/"))

    def test_round_icon_is_reused_when_main_portrait_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / f"old/images/{ID}.webp"
            path.parent.mkdir(parents=True)
            data = images.normalize(png())
            path.write_bytes(data)
            records = {ID: {"file": f"images/{ID}.webp", "source": f"https://enka.network/ui/ef/charroundicon/icon_round_{SOURCE_ID}.png", "packedSha256": images.digest(data), "roundIcon": True, "initialSeed": False}}
            failed = FailedSources()
            result, notes = images.build([{"internalName": ID}], root / "old", root / "new/images", records, failed, sources={ID: SOURCE_ID})
            self.assertEqual([f"https://enka.network/ui/ef/charremoteicon/icon_{SOURCE_ID}.png"], failed.requests)
            self.assertEqual(data, (root / f"new/images/{ID}.webp").read_bytes())
            self.assertTrue(result[ID]["roundIcon"])


class ManualImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        fixture(self.root)
        self.folder = self.root / "manual/endfield/images"
        self.folder.mkdir(parents=True)
        self.portrait = self.folder / f"{ID}.webp"
        self.portrait.write_bytes(images.normalize(png()))

    def tearDown(self):
        self.directory.cleanup()

    def test_manual_portrait_and_icon_win_even_when_online_and_rebuild_stays_identical(self):
        config = copy.deepcopy(CONFIG)
        config["icon"] = {"googlePlay": "com.gryphline.endfield.gp"}
        write_json(self.root / "config/endfield.json", config)
        (self.folder / "_game.png").write_bytes(png())
        online = mock.Mock(requests=[])
        online.json.side_effect = FetchError("hash source unavailable", 503)
        online.get.return_value = png()
        first = build.build(self.root, fetcher=online, today=TODAY)
        online.get.assert_not_called()
        self.assertEqual(self.portrait.read_bytes(), (self.root / f"packs/endfield/images/{ID}.webp").read_bytes())
        record = read_json(self.root / "upstream/endfield/images.json")[ID]
        self.assertTrue(record["manual"])
        self.assertEqual(f"manual/endfield/images/{ID}.webp", record["source"])
        self.assertTrue(read_json(self.root / "upstream/endfield/icon.json")["manual"])
        second = build.build(self.root, fetcher=online, today=TODAY + datetime.timedelta(days=7))
        online.get.assert_not_called()
        self.assertFalse(second["changed"])
        self.assertEqual(first["archive"], second["archive"])

    def test_invalid_manual_image_blocks_without_overwriting_existing_pack(self):
        build.build(self.root, no_network=True, today=TODAY)
        before = build.fingerprint(self.root / "packs/endfield")
        self.portrait.write_bytes(b"not a picture")
        with self.assertRaises(BuildError):
            build.build(self.root, fetcher=FailedSources(), today=TODAY)
        self.assertEqual(before, build.fingerprint(self.root / "packs/endfield"))

    def test_removing_override_returns_to_automatic_enka_image(self):
        build.build(self.root, no_network=True, today=TODAY)
        self.portrait.unlink()
        online = mock.Mock(requests=[])
        online.json.side_effect = FetchError("hash source unavailable", 503)
        online.get.return_value = png()
        build.build(self.root, fetcher=online, today=TODAY)
        online.get.assert_called_once_with(f"https://enka.network/ui/ef/charremoteicon/icon_{SOURCE_ID}.png")
        record = read_json(self.root / "upstream/endfield/images.json")[ID]
        self.assertNotIn("manual", record)
        self.assertEqual(f"https://enka.network/ui/ef/charremoteicon/icon_{SOURCE_ID}.png", record["source"])


class GameIconTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.config = {"googlePlay": "com.gryphline.endfield.gp"}
        self.source = "https://play-lh.googleusercontent.com/test=s512"
        self.page = b'<meta property="og:image" content="https://play-lh.googleusercontent.com/test=s180">'

    def tearDown(self):
        self.directory.cleanup()

    def seed(self):
        fetcher = mock.Mock()
        fetcher.get.side_effect = [self.page, png()]
        return images.game_icon(self.config, self.root / "missing", self.root / "old/images", {}, fetcher)[0]

    def test_square_padding_preserves_picture_and_transparent_margins(self):
        with Image.open(io.BytesIO(images.normalize(png(), square=True))) as icon:
            self.assertEqual((96, 96), icon.size)
            self.assertEqual(0, icon.getpixel((48, 0))[3])
            self.assertEqual(255, icon.getpixel((0, 16))[3])
            self.assertEqual(255, icon.getpixel((95, 79))[3])
            self.assertEqual(0, icon.getpixel((48, 95))[3])

    def test_same_store_url_does_not_download_icon_again(self):
        record = self.seed()
        fetcher = mock.Mock()
        fetcher.get.return_value = self.page
        repeated, notes = images.game_icon(self.config, self.root / "old", self.root / "new/images", record, fetcher)
        self.assertEqual(record, repeated)
        fetcher.get.assert_called_once_with(images.PLAY_PAGE.format(app=self.config["googlePlay"]))

    def test_changed_store_url_downloads_new_icon(self):
        record = self.seed()
        fetcher = mock.Mock()
        fetcher.get.side_effect = [self.page.replace(b"/test=", b"/updated="), png()]
        updated, notes = images.game_icon(self.config, self.root / "old", self.root / "new/images", record, fetcher)
        self.assertEqual("https://play-lh.googleusercontent.com/updated=s512", updated["source"])
        self.assertEqual(2, fetcher.get.call_count)

    def test_first_offline_build_cannot_omit_configured_icon(self):
        with self.assertRaises(BuildError):
            images.game_icon(self.config, self.root / "missing", self.root / "new/images", {}, None)


class FakeReleases:
    def __init__(self):
        self.public, self.drafts, self.data = {}, {}, {}
        self.corrupt = False
        self.created = 0

    def list(self, *, drafts=False):
        return copy.deepcopy(self.drafts if drafts else self.public)

    def create_draft(self, tag, asset, notes):
        self.created += 1
        self.drafts[tag] = {asset.name}
        self.data[(tag, asset.name)] = asset.read_bytes()

    def upload(self, tag, asset):
        self.drafts[tag].add(asset.name)
        self.data[(tag, asset.name)] = asset.read_bytes()

    def download(self, tag, name, destination):
        return b"corrupt upload" if self.corrupt else self.data[(tag, name)]

    def publish_draft(self, tag):
        self.public[tag] = self.drafts.pop(tag)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        fixture(self.root)
        build.build(self.root, no_network=True, today=TODAY)

    def tearDown(self):
        self.directory.cleanup()

    def test_checksum_failure_keeps_index_and_retry_resumes_same_draft(self):
        write_json(self.root / "index.json", {"schemaVersion": 1, "packs": []})
        before = (self.root / "index.json").read_bytes()
        remote = FakeReleases();remote.corrupt = True
        with self.assertRaises(BuildError):
            release.publish(self.root, CONFIG, remote)
        self.assertEqual(before, (self.root / "index.json").read_bytes())
        self.assertFalse(remote.public)
        remote.corrupt = False
        first = release.publish(self.root, CONFIG, remote)
        repeated = release.publish(self.root, CONFIG, remote)
        self.assertEqual(first, repeated)
        self.assertEqual(1, remote.created)
        version = first["packs"][0]["versions"][0]
        self.assertEqual(hashlib.sha256(next(iter(remote.data.values()))).hexdigest(), version["sha256"])

    def test_zip_members_match_pack_and_published_version_cannot_be_overwritten(self):
        remote = FakeReleases()
        release.publish(self.root, CONFIG, remote)
        data = next(iter(remote.data.values()))
        self.assertTrue(release.archive_matches(data, self.root / "packs/endfield"))
        (self.root / "packs/endfield/game.json").write_text('{}')
        before = (self.root / "index.json").read_bytes()
        with self.assertRaises(BuildError):
            release.publish(self.root, CONFIG, remote)
        self.assertEqual(before, (self.root / "index.json").read_bytes())

    def test_future_version_prevents_clock_regression(self):
        with self.assertRaises(BuildError):
            release.next_version(TODAY, {"2026.10.07"})


if __name__ == "__main__":
    unittest.main()
