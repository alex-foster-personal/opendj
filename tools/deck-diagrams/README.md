# Deck diagrams

Interactive recreation of DJ controller plate diagrams, joined to official MIDI
message lists. Used for mapping coverage (double-entry), agent/user visual
feedback ("which button?"), and later teaching / how-to UI.

**Issue:** [#371](https://github.com/private_owner/music-dj-tools/issues/371)

**Skill:** [`.agents/skills/deck-diagram-recreate/SKILL.md`](../../.agents/skills/deck-diagram-recreate/SKILL.md)

**Agents:** see [AGENTS.md](AGENTS.md).

## First device: DDJ-FLX10

| File | Purpose |
|---|---|
| `devices/ddj-flx10/source/plate-top.png` | Cropped official plate photo |
| `devices/ddj-flx10/midi.json` | Expected MIDI map (from PDF) |
| `devices/ddj-flx10/layout.json` | Hit targets + layers + MIDI join |
| `devices/ddj-flx10/overlay.html` | Red recreation over reference |
| `devices/ddj-flx10/deck.html` | Usable interactive deck (SHIFT, dials) |
| `devices/ddj-flx10/footnotes.md` | How to read fig codes + out-of-scope |

```sh
uv run python tools/deck-diagrams/scripts/checksum.py tools/deck-diagrams/devices/ddj-flx10
open tools/deck-diagrams/devices/ddj-flx10/deck.html
```

## Related

- #166 FLX10 hardware MIDI runtime (different deliverable)
- #281 parked `af--controller-probe` / `af--controller-p0` lanes
