"""Stable JSON and reversible replacement of a validated build's files."""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import tempfile


class BuildError(Exception):
    """An invalid source, saved file or pack that must not replace the last working pack."""


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def read_json(path: pathlib.Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        raise BuildError(f"Cannot read {path.name}: {error}") from error


def write_bytes(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def write_json(path: pathlib.Path, value: object) -> None:
    write_bytes(path, dumps(value).encode("utf-8"))


def reject_links(path: pathlib.Path) -> None:
    if path.is_symlink():
        raise BuildError(f"{path} is a link; saved builder inputs must be real files.")
    if path.is_dir():
        for entry in path.rglob("*"):
            if entry.is_symlink():
                raise BuildError(f"{entry} is a link; saved builder inputs must be real files.")


def remove(path: pathlib.Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def commit_files(staged: pathlib.Path, root: pathlib.Path, names: list[str]) -> None:
    """Replace only generated paths, restoring every old path if any replacement fails.

    A build lock prevents concurrent builders/publishers from promoting another staging tree.
    Network caches are not authoritative inputs and need no rollback.
    """
    backups = staged / ".backups"
    backups.mkdir()
    moved: list[tuple[pathlib.Path, pathlib.Path, bool]] = []
    try:
        for index, name in enumerate(names):
            target, source = root / name, staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            reject_links(target)
            backup = backups / str(index)
            had_old = target.exists()
            if had_old:
                os.replace(target, backup)
            moved.append((target, backup, had_old))
            os.replace(source, target)
    except BaseException:
        for target, backup, had_old in reversed(moved):
            remove(target)
            if had_old:
                os.replace(backup, target)
        raise


@contextlib.contextmanager
def build_lock(root: pathlib.Path):
    """A portable lock; a killed runner leaves an explicit lock rather than racing a build."""
    path = root / ".build.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise BuildError("Another build holds .build.lock. If it was interrupted, remove that lock before retrying.") from error
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(str(os.getpid()))
        yield
    finally:
        path.unlink(missing_ok=True)
