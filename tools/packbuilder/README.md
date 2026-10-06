# Builder

Python 3.11+, [uv](https://docs.astral.sh/uv/) and GitHub CLI for publishing. Run from the preset folder:

```sh
uv run --locked --project tools/packbuilder packbuilder build
uv run --locked --project tools/packbuilder packbuilder build --no-network
uv run --locked --project tools/packbuilder python -m unittest discover -s tools/packbuilder/tests -v
```

`build` writes the checked pack to `packs/endfield/`, a ZIP and local registry to `dist/`, and its reports to `reports/endfield/`. It keeps the version when pack contents do not change. `--root` selects a different preset folder.

Reports are regenerated from the checked pack on every build:

| Report | What it shows |
|---|---|
| `pending-hashes.md` | Characters with no hashes. |
| `missing-images.md` | Characters with no portrait, and why. |
| `collisions.md` | Hashes shared by characters, excluding ignored hashes. |
| `left-out.md` | Excluded or unmatched source entries, and why. |
| `inference-report.md` | Outfit parents and source matching decisions. |
| `manual.md` | Manual characters, hashes, portraits and game icon used. |
| `history.md` | Current, historical-only and manual-only hash associations. |

`build.md`, `build.json` and `hash-audit.json` keep the build summary and source audit. Manual hash corrections are included in the totals. A failed build keeps the last successful reports and adds `blocked.md`; a successful build removes it. Report changes alone do not change the pack version.

Hand corrections live in `overrides/endfield.json`, like the other presets. The builder reads this file; it never generates or rewrites it. `config/endfield.json` selects the sources.

| Rule | What it changes |
|---|---|
| `join` | Pins an Enka source ID to a pack name, or matches a fixer's source name to a character. |
| `displayNames`, `aliases` | Corrects a displayed name or adds matching names, keyed by permanent pack name. Existing mod-folder names stay stable. |
| `partOf` | Assigns a catalog model ID or fixer name's hashes to a character. Saved observations remain in the ledger. |
| `parents` | Groups existing characters as outfits. Use `null` for an independent character. |
| `exclude` | Leaves a source ID or pack name out of the pack. |
| `ignoredHashes` | Prevents listed hashes from deciding a mod's owner. |
| `retired`, `allowShrink` | Explicitly permits a published character's removal or loss of its hash associations. Neither rule deletes saved hash history. |

Unknown keys, invalid values, conflicting source matches and missing correction targets block the build. To remove an already published character, use `retired` along with `exclude` or the actual source removal. A hash reassignment that takes associations away from a character needs its name in `allowShrink`.

Enka supplies the roster, attributes and downloaded portraits. Display names become readable permanent names (`Chen Qianyu` → `ChenQianyu`), used for internal IDs, mod folders and portrait files. `ledger/endfield.json` remembers the source-ID-to-name mapping so later name changes leave folders stable. Enka IDs are source keys and download identifiers; the fixer only supplies hashes. Manual images take priority: put `<internalName>.png`, `.jpg`, `.jpeg` or `.webp` in `manual/endfield/images/`, or `_game.*` for the game icon. Keep one file per name. These images are used before cached images or network requests, including when Enka has a normal portrait. An invalid manual image blocks the build; it is never silently replaced. Remove the manual file to return to automatic sourcing.

Manual hashes go in `manual/endfield/hashes/<Name>.txt`, `.ini` or `.json`. Names match internal IDs, displayed names or aliases, ignoring case and spaces. A file in `hashes/replace/` replaces that character's automatic hashes while present; removing it restores them. The upstream ledger remains intact. `characters.json` adds named characters and outfits, with optional `id`, `displayName` and `outfitOf`. See [manual inputs](../../manual/endfield/README.md). Invalid or ambiguous inputs block the build and preserve the previous pack.

Verified portraits are reused, resized without cropping to at most 512 pixels, and stored as WebP under 80 KiB. Initial bundled images can stay; their external source URLs are never downloaded. Automatic Enka portraits can replace automatic initial images or round icons when available. Liino uses the original Enka round icon while her normal portrait is unavailable; she has no manual override.

Without a manual override, the game icon uses the official Google Play listing in `config/endfield.json`, like the other preset builders. It is stored as `images/_game.webp`, padded square without cropping, and reused while its source URL is unchanged. A failed store request keeps the verified bundled icon.

The current catalog and all available snapshots supply EFMI entry and texture hashes. The fixer is read through literal AST data: both members of IB, VB0, LOD and texture pairs, wet-effect entries and special-case patched IBs. Native CRCs and shared shaders are excluded. `upstream/endfield/` saves checksum-verified raw blobs and snapshot pins; `ledger/endfield.json` preserves source observations. Unknown catalog IDs remain in the ledger until Enka names them.

An unreachable or malformed upstream uses the last valid saved copy. Characters, hashes and existing portraits cannot silently disappear. A blocked build preserves the previous pack and ledger and writes `reports/endfield/blocked.md`.

Publishing is explicit:

```sh
uv run --locked --project tools/packbuilder packbuilder build --publish
```

This creates a draft release, verifies its uploaded ZIP, publishes it, then updates `index.json`. It requires write access to the repository in `config/endfield.json`. It never creates a repository or commits/pushes files; the workflow saves its generated files afterward. A failed upload leaves the previous registry intact and its draft available for retry.

The workflow runs weekly on Monday at 13:23 Asia/Saigon (`23 6 * * 1` UTC), on builder/configuration/overrides/manual changes and on demand. Human commits use `dotStray <156100749+dotStray@users.noreply.github.com>`; workflow commits use `github-actions[bot]` for both author and committer.
