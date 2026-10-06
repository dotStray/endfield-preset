"""Deterministic local registries and optional GitHub releases.

Normal builds export locally. Only an explicit publish request contacts GitHub to create a
release; index.json is updated after its archive is uploaded and its checksum is verified.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import pathlib
import re
import subprocess
import tempfile
import zipfile

from packbuilder.files import BuildError, read_json, reject_links, write_bytes, write_json

DATED = re.compile(r"\d{4}\.\d{2}\.\d{2}(?:\.\d{2})?")


def next_version(today: datetime.date, taken: set[str]) -> str:
    base = today.strftime("%Y.%m.%d")
    newest = max((version for version in taken if DATED.fullmatch(version)), default="")
    for candidate in [base, *(f"{base}.{number:02d}" for number in range(1, 100))]:
        if candidate not in taken and candidate > newest:
            return candidate
    raise BuildError(f"No later version is available for {base}; check the build date and saved versions")


def zip_bytes(pack: pathlib.Path) -> bytes:
    reject_links(pack)
    manifest = read_json(pack / "manifest.json")
    date = datetime.datetime.strptime(manifest["packVersion"][:10], "%Y.%m.%d")
    stamp = (max(date.year, 1980), date.month, date.day, 0, 0, 0)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(pack.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(pack).as_posix(), stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return buffer.getvalue()


def entry(manifest: dict, url: str, data: bytes) -> dict:
    return {"packVersion": manifest["packVersion"], "packSchemaVersion": 1,
            "minAppVersion": manifest["minAppVersion"], "url": url,
            "sha256": hashlib.sha256(data).hexdigest(), "sizeBytes": len(data),
            "changelog": f"{manifest['counts']['variants']} characters; current and historical hashes."}


def index_for(game: dict, version: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    pack = next((row for row in previous.get("packs", []) if row["gameId"] == game["gameId"]), {})
    versions = [version] + [row for row in pack.get("versions", []) if row["packVersion"] != version["packVersion"]]
    versions.sort(key=lambda row: row["packVersion"], reverse=True)
    result = {key: game[key] for key in ["gameId", "displayName", "shortName", "importer"]}
    result["versions"] = versions[:10]
    date = datetime.datetime.strptime(version["packVersion"][:10], "%Y.%m.%d").date()
    return {"schemaVersion": 1, "updatedAt": date.isoformat() + "T00:00:00Z", "packs": [result]}


def export(pack: pathlib.Path, output: pathlib.Path) -> dict:
    manifest = read_json(pack / "manifest.json")
    game = read_json(pack / "game.json")
    data = zip_bytes(pack)
    name = f"endfield-{manifest['packVersion']}.zip"
    write_bytes(output / "packs" / name, data)
    index = index_for(game, entry(manifest, "packs/" + name, data))
    write_json(output / "index.json", index)
    return index


class GitHubReleases:
    def __init__(self, repository: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise BuildError("Invalid publishing repository name")
        self.repository = repository

    def run(self, arguments: list[str]) -> str:
        try:
            result = subprocess.run(["gh", *arguments], capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BuildError(f"GitHub release command could not finish: {error}") from error
        if result.returncode:
            raise BuildError("GitHub release command failed: " + result.stderr.strip())
        return result.stdout

    def list(self, *, drafts=False) -> dict[str, set[str]]:
        import json
        output = self.run(["api", f"repos/{self.repository}/releases", "--paginate", "--jq", ".[] | {tag: .tag_name, draft: .draft, assets: [.assets[].name]}"])
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        return {row["tag"]: set(row["assets"]) for row in rows if row["draft"] == drafts}

    def upload(self, tag: str, asset: pathlib.Path) -> None:
        # Only a missing file in a draft is uploaded; published assets are never overwritten.
        self.run(["release", "upload", tag, str(asset), "--repo", self.repository])

    def create_draft(self, tag: str, asset: pathlib.Path, notes: pathlib.Path) -> None:
        self.run(["release", "create", tag, str(asset), "--repo", self.repository, "--target", "main",
                  "--draft", "--title", "Endfield " + tag, "--notes-file", str(notes)])

    def download(self, tag: str, name: str, destination: pathlib.Path) -> bytes:
        self.run(["release", "download", tag, "--repo", self.repository, "--pattern", name, "--output", str(destination), "--clobber"])
        return destination.read_bytes()

    def publish_draft(self, tag: str) -> None:
        self.run(["release", "edit", tag, "--repo", self.repository, "--draft=false", "--latest"])


def publish(root: pathlib.Path, config: dict, releases=None) -> dict:
    """Recover a prior upload idempotently; a failed upload never changes the public index.

    A newly created release stays a draft until its uploaded bytes are checked. A failure
    leaves that draft reviewable and the previous index intact; it never deletes a release.
    """
    pack = root / "packs/endfield"
    manifest, game = read_json(pack / "manifest.json"), read_json(pack / "game.json")
    data = zip_bytes(pack)
    version = manifest["packVersion"]
    name = f"endfield-{version}.zip"
    releases = releases or GitHubReleases(config["repository"])
    available = releases.list()
    drafts = releases.list(drafts=True)
    previous = read_json(root / "index.json", {})
    prefix = f"https://github.com/{config['repository']}/releases/download/"
    url = prefix + version + "/" + name
    (root / ".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=root / ".cache") as directory:
        directory = pathlib.Path(directory)
        if version not in available:
            # A retry can resume its own draft, but never overwrites an existing public asset.
            asset, notes = directory / name, directory / "notes.md"
            asset.write_bytes(data)
            notes.write_text(f"{manifest['counts']['variants']} characters with current and historical hashes.\n")
            if version not in drafts:
                releases.create_draft(version, asset, notes)
            elif name not in drafts[version]:
                releases.upload(version, asset)
        elif name not in available[version]:
            raise BuildError(f"Release {version} already exists without {name}; inspect it before retrying")
        uploaded = releases.download(version, name, directory / "verified.zip")
        if hashlib.sha256(uploaded).digest() != hashlib.sha256(data).digest():
            # Compression bytes can differ between zlib versions. A recorded public archive
            # remains authoritative when its checksum and every member match the local pack.
            recorded = next((row for item in previous.get("packs", []) for row in item.get("versions", [])
                             if row.get("url") == url), None)
            if version not in available or not recorded or hashlib.sha256(uploaded).hexdigest() != recorded.get("sha256") or len(uploaded) != recorded.get("sizeBytes") or not archive_matches(uploaded, pack):
                raise BuildError(f"Uploaded archive for {version} differs from the prepared pack; index was not changed")
            data = uploaded
        if version not in available:
            releases.publish_draft(version)
    # Remove references to deleted GitHub assets; local initial-build archives stay valid.
    for row in previous.get("packs", []):
        versions = []
        for record in row.get("versions", []):
            address = record.get("url", "")
            if address.startswith(prefix):
                tag, _, asset = address[len(prefix):].partition("/")
                if asset not in available.get(tag, set()) and address != url:
                    continue
            versions.append(record)
        row["versions"] = versions
    index = index_for(game, entry(manifest, url, data), previous)
    write_json(root / "index.json", index)
    return index


def archive_matches(data: bytes, pack: pathlib.Path) -> bool:
    """Compare bounded ZIP members, not compression bytes, for an unchanged public version."""
    expected = {path.relative_to(pack).as_posix(): path for path in pack.rglob("*") if path.is_file()}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != len(expected) or {member.filename for member in members} != set(expected):
                return False
            for member in members:
                path = expected[member.filename]
                if member.file_size != path.stat().st_size or hashlib.sha256(archive.read(member)).digest() != hashlib.sha256(path.read_bytes()).digest():
                    return False
        return True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False
