# deck-diagrams - agent entry

Tracking: https://github.com/private_owner/music-dj-tools/issues/371

## Skill (main workflow)

**Read and follow:** [`.agents/skills/deck-diagram-recreate/SKILL.md`](../../.agents/skills/deck-diagram-recreate/SKILL.md)

That skill is the functional entry point. This directory holds assets, schemas, HTML decks, and helpers the skill produces.

## Quick use

```sh
# checksum a device against its MIDI expected map
uv run python tools/deck-diagrams/scripts/checksum.py tools/deck-diagrams/devices/ddj-flx10

# rebuild FLX10 layout.json from midi.json + plate positions
uv run python tools/deck-diagrams/scripts/build_flx10_layout.py

# open interactive surfaces (offline, no build)
open tools/deck-diagrams/devices/ddj-flx10/overlay.html
open tools/deck-diagrams/devices/ddj-flx10/deck.html
```

## Layout

| Path | Role |
|---|---|
| `catalog/controllers.json` | All decks + rough popularity + status |
| `schema/control-layout.schema.json` | `layout.json` contract |
| `devices/<id>/` | Per-controller sources, layout, overlay, deck |
| `scripts/` | Checksum + layout builders |
| `../docs/controller/reference/` | Manifest only. The vendor PDFs are NOT in the repo (manufacturer copyright, removed 1 Sep 2026). Read its README.md for where to get them; the derived `midi.json` here is what code consumes. |

## Rules

- Geometry lives here, **not** in runtime `apps/webui/.../midi/maps/*.ts`.
- Diagrams **consume** MIDI expected maps; they do not invent note numbers.
- SHIFT is a first-class layer. Pad modes are additional layers when present.
- HID-only surfaces (jog screens, VU bitmaps) stay out of scope - list them in `footnotes.md`.
- No U+2013 or U+2014 characters in files.

## Scale gate

See skill `reference.md` section "Scale to 100". Do not fan out past 5 until FLX10 checksum + overlay gate passes and the skill backlog items for plate crop / fig parse are noted.

## Agent index / context excludes

`devices/` (~109MB, 100+ controllers) is excluded from default agent indexing/context. `docs/controller/reference/` no longer holds PDFs at all -- they were removed as vendor copyright and are gitignored, so there is nothing there to index but a manifest. Catalog, schema, scripts, and this file stay visible. Broader repo excludes (`.planning/` audits, `blog/`, dumps, `uv.lock`) live in the same root ignore files.

| Harness | File (repo root only) | Effect |
|---|---|---|
| Cursor | `.cursorindexingignore` | Out of codebase index; still `@`-mention / Read |
| Claude Code | `.claudeignore` | Out of Read/Glob/Grep; use Bash for a specific device |
| Codex | `.codexignore` | Best-effort; Codex mainly respects `.gitignore` |
| Gemini | `.geminiignore` | Out of Gemini index context |
| Antigravity | `.antigravityignore` | Out of Antigravity index context |

Do not put these paths in `.cursorignore` (hard block) or `.gitignore` (they stay in git). Ignore files must sit at the repo root, not under `.claude/` / `.agents/` / `.codex/`.
