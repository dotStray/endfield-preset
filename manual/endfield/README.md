# Manual inputs

Put one PNG, JPEG or WebP per character in `images/`, named with its internal character ID. Use `_game.*` for the game icon. Manual files take priority over every automatic image source. Remove a file to return to automatic sourcing.

| File | Effect |
|---|---|
| `hashes/<Name>.txt` or `.ini` | Add hashes alongside automatic hashes; duplicates are omitted. Paste one hash per line, `ib 1575ec63`, or complete override sections from a mod. |
| `hashes/<Name>.json` | Add hashes from a component-list `hash.json`, including buffer and texture hashes. |
| `hashes/replace/<Name>.txt`, `.ini` or `.json` | Replace that character's automatic hashes while this file exists. Delete it to restore them. Additional files in `hashes/` are still added. |
| `characters.json` | Add characters or outfits missing from Enka. |

Hash filenames match internal IDs, displayed names or aliases, ignoring case and spaces. A new name in `hashes/` creates a character; check `reports/endfield/manual.md` for typos. A replacement must match an existing character. Shader hashes are excluded from ownership matching.

```json
[
  {"name": "New Operator", "id": "NewOperator"},
  {"name": "Summer Outfit", "id": "SummerOutfit", "outfitOf": "New Operator"}
]
```

`displayName` is optional. Without an explicit `id`, words join into a readable name: `New Operator` becomes `NewOperator`. IDs use letters, digits, hyphens or underscores. Published IDs and mod-folder names stay stable. Outfits inherit their parent's attributes.

Files stay until you remove them. Removing a published manual character requires `retired` in `overrides/endfield.json`; unexplained hash losses require `allowShrink`. Invalid files, missing parents and ambiguous names stop the build without replacing the working pack or source history.
