"""Reuse verified portraits; new portraits and round icons come only from Enka.

An initial image can have another source credit, but its address is never downloaded here.
Initial images and round icons are kept until Enka's full portrait becomes available.
The game icon comes from the official Google Play listing, as in the other preset builders.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import re
import warnings

from PIL import Image

from packbuilder.files import BuildError
from packbuilder.files import reject_links
from packbuilder.http import FetchError

MAX_BYTES = 80 * 1024
PLAY_PAGE = "https://play.google.com/store/apps/details?id={app}&hl=en&gl=US"
PLAY_ICON = re.compile(r'<meta property="og:image" content="(https://play-lh\.googleusercontent\.com/[^"=]+)[^"]*"')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(data: bytes, *, square: bool = False) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "WEBP"]) as opened:
                image = opened.convert("RGBA")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        if square:
            side = max(image.size)
            padded = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            padded.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
            image = padded
        for quality in [85, 75, 65, 50, 35]:
            output = io.BytesIO()
            image.save(output, "WEBP", quality=quality, method=6)
            if len(output.getvalue()) <= MAX_BYTES:
                return output.getvalue()
        raise BuildError("Image cannot fit the 80 KiB image limit")
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise BuildError(f"Unreadable image: {error}") from error


def manual_image(name: str, folder: pathlib.Path | None, *, square: bool = False) -> tuple[bytes, dict] | None:
    """A deliberate local image takes priority over cached images and every download source."""
    if folder is None or not folder.exists():
        return None
    reject_links(folder)
    candidates = [path for path in folder.iterdir() if path.stem == name and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
    if len(candidates) > 1:
        raise BuildError(f"Multiple manual images for {name}; keep one")
    if not candidates:
        return None
    path = candidates[0]
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise BuildError(f"Unreadable or oversized manual image for {name}")
    original = path.read_bytes()
    # Keep already-valid WebP bytes so a chosen portrait is not recompressed each week.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(original), formats=["PNG", "JPEG", "WEBP"]) as image:
                image.load()
                ready = image.format == "WEBP" and max(image.size) <= 512 and len(original) <= MAX_BYTES and (not square or image.width == image.height)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise BuildError(f"Invalid manual image for {name}: {error}") from error
    data = original if ready else normalize(original, square=square)
    return data, {"source": f"manual/endfield/images/{path.name}", "sha256": digest(original), "manual": True,
                  "file": f"images/{name}.webp", "packedSha256": digest(data)}


def game_icon(config: dict, previous_pack: pathlib.Path, output: pathlib.Path, record: dict, fetcher, manual_folder: pathlib.Path | None = None) -> tuple[dict, list[str]]:
    """Keep a verified icon when its store URL is unchanged or the store is unavailable."""
    manual = manual_image("_game", manual_folder, square=True)
    if manual is not None:
        data, credit = manual
        output.mkdir(parents=True, exist_ok=True)
        (output / "_game.webp").write_bytes(data)
        return credit, []
    app = config.get("googlePlay")
    previous = previous_pack / "images/_game.webp"
    if not app and not record and not previous.exists():
        return {}, []
    notes = []
    existing = previous.read_bytes() if previous.is_file() else None
    if existing is not None:
        try:
            if digest(existing) != record.get("packedSha256"):
                raise BuildError("checksum mismatch")
            with Image.open(io.BytesIO(existing), formats=["WEBP"]) as image:
                image.load()
                if image.width != image.height or image.width > 512 or len(existing) > MAX_BYTES:
                    raise BuildError("invalid size")
        except (OSError, ValueError, BuildError) as error:
            notes.append(f"Saved game icon failed validation ({error}); trying the official store")
            existing = None
    data, credit = existing, dict(record)
    if fetcher is not None and app:
        try:
            if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", app):
                raise BuildError("Invalid Google Play application ID")
            page = PLAY_PAGE.format(app=app)
            found = PLAY_ICON.search(fetcher.get(page).decode("utf-8", errors="replace"))
            if found is None:
                raise FetchError("Official store listing did not provide a game icon")
            source = found.group(1) + "=s512"
            if existing is None or source != record.get("source"):
                original = fetcher.get(source)
                data = normalize(original, square=True)
                credit = {"source": source, "page": page, "sha256": digest(original)}
        except (FetchError, BuildError) as error:
            notes.append(f"Game icon not refreshed ({error}); keeping the bundled icon")
    if data is None:
        raise BuildError("No valid bundled game icon; fetch the official store icon before an offline build")
    output.mkdir(parents=True, exist_ok=True)
    (output / "_game.webp").write_bytes(data)
    return {**credit, "file": "images/_game.webp", "packedSha256": digest(data)}, notes


def build(variants: list[dict], previous_pack: pathlib.Path, output: pathlib.Path, records: dict, fetcher, manual_folder: pathlib.Path | None = None, *, missing: dict[str, str] | None = None, sources: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    missing = missing if missing is not None else {}
    missing.clear()
    output.mkdir(parents=True, exist_ok=True)
    result, notes = {}, []
    for variant in variants:
        name = variant["internalName"]
        source_id = (sources or {}).get(name, name)
        manual = manual_image(name, manual_folder)
        if manual is None and source_id != name:
            manual = manual_image(source_id, manual_folder)
        if manual is not None:
            data, credit = manual
            credit = {**credit, "file": f"images/{name}.webp"}
            (output / f"{name}.webp").write_bytes(data)
            variant["image"] = credit["file"]
            result[name] = {**credit, "roundIcon": False, "initialSeed": False}
            continue
        old = records.get(name, records.get(source_id, {}))
        relative = old.get("file", f"images/{name}.webp")
        if relative not in {f"images/{stem}.{suffix}" for stem in [name, source_id] for suffix in ["png", "webp"]}:
            raise BuildError(f"Unfamiliar saved portrait path for {name}")
        path = previous_pack / relative
        existing = path.read_bytes() if path.is_file() else None
        problem = "No saved portrait; network downloads are disabled."
        expected = old.get("packedSha256") or old.get("sha256")
        if existing is not None and (not expected or digest(existing) != expected):
            notes.append(f"{name}: saved portrait checksum failed; trying Enka")
            problem = "Saved portrait failed its checksum; network downloads are disabled."
            existing = None
        primary = f"https://enka.network/ui/ef/charremoteicon/icon_{source_id}.png"
        round_url = f"https://enka.network/ui/ef/charroundicon/icon_round_{source_id}.png"
        source = old.get("source", "")
        needs_primary = existing is None or source != primary
        data, credit = existing, dict(old)
        if fetcher is not None and needs_primary:
            try:
                fresh = fetcher.get(primary)
                data = normalize(fresh)
                credit = {"source": primary, "sha256": digest(fresh), "roundIcon": False, "initialSeed": False}
            except (FetchError, BuildError) as error:
                if existing is not None:
                    notes.append(f"{name}: kept bundled portrait ({error})")
                else:
                    try:
                        fresh = fetcher.get(round_url)
                        data = normalize(fresh)
                        credit = {"source": round_url, "sha256": digest(fresh), "roundIcon": True, "initialSeed": False}
                        notes.append(f"{name}: using Enka's round icon while its portrait is unavailable")
                    except (FetchError, BuildError) as fallback:
                        notes.append(f"{name}: no usable Enka image ({fallback})")
                        problem = f"Enka portrait unavailable ({error}); round icon unavailable ({fallback})."
        if data is None:
            missing[name] = problem
            notes.append(f"{name}: no bundled portrait; will retry Enka next run")
            continue
        if relative.endswith(".png") and data == existing:
            data = normalize(data)
        # Decode reused images too; a matching checksum does not make corrupt pixels valid.
        try:
            with Image.open(io.BytesIO(data), formats=["WEBP"]) as image:
                image.load()
                if max(image.size) > 512 or len(data) > MAX_BYTES:
                    raise BuildError(f"Saved portrait for {name} exceeds the pack limits")
        except OSError as error:
            raise BuildError(f"Saved portrait for {name} cannot be decoded") from error
        destination = output / f"{name}.webp"
        destination.write_bytes(data)
        variant["image"] = "images/" + destination.name
        result[name] = {**credit, "file": variant["image"], "packedSha256": digest(data)}
    return dict(sorted(result.items())), notes
