# XDJ-RX3 and XDJ-AZ MIDI message lists

Vendor PDFs, not committed. Fetched Fri 30 Oct 2026.

| Unit | URL | sha256 |
| --- | --- | --- |
| XDJ-RX3 | https://cdn.rekordbox.com/files/20211108182410/XDJ-RX3_Hardware_Diagram_en.pdf | `bd9c747ec65c3910d139d33cbbf08033a9ffd57b548978bf5dd51f2c2cdf9c36` |
| XDJ-AZ | https://web.archive.org/web/20250720035554/https://www.pioneerdj.com/-/media/pioneerdj/software-info/system/xdj-az/xdj-az_midi_message_list_e1.pdf | `2a56db4cc52c5bbf2371c33cbb0188a15ed28daf841f712cac343f4025f54216` |

Mixxx ships no mapping for either unit. The closest is the unmerged XDJ-RX mapping, mixxxdj/mixxx#3008. Its transport, mixer, browser and loop wires agree with these lists; its tempo (CC 29/61) and jog (CC 48) wires do not.

Neither map has been run on hardware. The AZ list prints CH FADER as "Upper Side=0", the opposite of the RX3 list; the map assumes lower = 0.

Display feasibility (ch 14 jog chrome vs full screens): [(private path)](../../research/midi/2026-11-xdj-az-displays.md) (issue #10308).

## Cross-check against rekordbox's bundled maps

Every binding was compared by wire against the maps rekordbox 7 ships for these units (`XDJ-RX3.midi.csv`, `XDJ-AZ MIDI.midi.csv`).

- RX3: 138 of 138 bindings found, each with the matching function.
- AZ: 126 of 154 found. The other 28 are mixer wires on channel 5, which the AZ's rekordbox map omits, so only the vendor list backs them.
- Jog: both maps read CC 34 as the platter and CC 33 as the wheel side. The rekordbox AZ channel fader row matches the RX3 one, with no invert option.

## XDJ-RX3 practice replica trace (Sun 8 Nov 2026, epic #9366)

- Positions in `tools/deck-diagrams/devices/xdj-rx3/panel.reference.json` were traced from the top-panel photo in the RX3 list above: page 1, embedded image 0, 1769 x 1181 px, sha256 `c0a5cb1f8df45f39992ec5e63e6fba5f85cc62174a1e0b9e7ead774ec883f539`. Neither the PDF nor the image is committed; only the traced numbers are.
- Panel size: XDJ-RX3 product page, Specifications (https://www.pioneerdj.com/en/product/all-in-one-dj-systems/xdj-rx3/, read Sun 8 Nov 2026): Width 728.1 mm, Height 118.4 mm, Depth 469.5 mm, Weight 9.3 kg.
