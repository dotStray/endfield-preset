"""Pinned GitHub snapshots, including every available commit that changed each hash path.

Raw blobs are checked against GitHub's git object IDs and kept in upstream/endfield/blobs.
Saved provenance survives history rewrites. Only new blobs need downloading on weekly runs.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import urllib.parse

from packbuilder.files import BuildError
from packbuilder.http import FetchError

SHA = re.compile(r"[0-9a-f]{40}")


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob(folder: pathlib.Path, sha: str) -> bytes:
    if not SHA.fullmatch(sha):
        raise BuildError("Invalid saved git blob ID")
    try:
        data = (folder / "blobs" / sha).read_bytes()
    except OSError as error:
        raise BuildError(f"Saved upstream blob {sha} is missing") from error
    if blob_sha(data) != sha:
        raise BuildError(f"Saved upstream blob {sha} does not match its checksum")
    return data


def refresh(config: dict, previous: dict, folder: pathlib.Path, fetcher, *, catalog: bool) -> dict:
    repo, path = config["repo"], config["path"]
    api = f"https://api.github.com/repos/{repo}"
    info = fetcher.json(api + "/commits/HEAD")
    head = info.get("sha", "")
    if not SHA.fullmatch(head):
        raise FetchError(f"{repo}: no commit in GitHub's answer")
    date = info["commit"]["committer"]["date"]
    commits = [{"commit": head, "date": date}]
    for page in range(1, 1001):
        query = urllib.parse.urlencode({"path": path, "sha": head, "per_page": 100, "page": page})
        rows = fetcher.json(api + "/commits?" + query, immutable=True)
        if not isinstance(rows, list):
            raise FetchError(f"{repo}: incomplete history response")
        for row in rows:
            commit = row.get("sha", "")
            if not SHA.fullmatch(commit):
                raise FetchError(f"{repo}: invalid commit in history")
            commits.append({"commit": commit, "date": row["commit"]["committer"]["date"]})
        if len(rows) < 100:
            break
    else:
        raise FetchError(f"{repo}: history exceeded the collection limit; saved data was kept")

    saved = {row["commit"]: row for row in previous.get("snapshots", [])}
    fresh = []
    for record in {row["commit"]: row for row in commits}.values():
        sha = record["commit"]
        snapshot = saved.get(sha)
        if snapshot is None:
            tree = fetcher.json(api + f"/git/trees/{sha}?recursive=1", immutable=True)
            if not isinstance(tree, dict) or tree.get("truncated"):
                raise FetchError(f"{repo}: incomplete file tree at {sha}")
            files = [{"path": entry["path"], "sha": entry["sha"]} for entry in tree.get("tree", [])
                     if entry.get("type") == "blob" and (entry.get("path", "").startswith(path + "/") and entry["path"].endswith(".json") if catalog else entry.get("path") == path)]
            snapshot = {**record, "files": sorted(files, key=lambda row: row["path"])}
        if sha == head and not snapshot["files"]:
            raise FetchError(f"{repo}: {path} is missing at the current commit")
        for entry in snapshot["files"]:
            digest = entry["sha"]
            if not SHA.fullmatch(digest):
                raise FetchError(f"{repo}: invalid file checksum")
            destination = folder / "blobs" / digest
            url = f"https://raw.githubusercontent.com/{repo}/{sha}/{entry['path']}"
            if destination.exists() and blob_sha(destination.read_bytes()) == digest:
                data = destination.read_bytes()
            else:
                data = fetcher.get(url, immutable=not destination.exists())
            if blob_sha(data) != digest:
                fetcher.invalidate(url)
                raise FetchError(f"{repo}/{entry['path']}: git blob checksum mismatch")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        fresh.append(snapshot)
    # Old snapshots remain readable even if upstream deletes or rewrites its git history.
    merged = {row["commit"]: row for row in [*previous.get("snapshots", []), *fresh]}
    return {"repo": repo, "path": path, "head": head, "headDate": date,
            "snapshots": sorted(merged.values(), key=lambda row: (row["date"], row["commit"]))}
