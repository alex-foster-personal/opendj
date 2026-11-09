# XDJ-XZ MIDI message list and panel size

Vendor documents, not committed. Fetched Sun 8 Nov 2026 for the practice replica (epic #9366).

| Document | URL | sha256 |
| --- | --- | --- |
| XDJ-XZ MIDI Message List E1 (5 pages; page 1 carries the top-panel photo with figure numbers) | https://downloads.support.alphatheta.com/software_info/all-in-one-dj-systems/XDJ-XZ/XDJ-XZ_MIDI_Message_List_E1.pdf | `c0e23435447a5c3f444f55e777e4d7e1b02a0abd6238e8dd0ff1a6a00df867b9` |
| XDJ-XZ Operating Instructions DRI1625B (136 pages, English) | https://downloads.support.alphatheta.com/manuals/all-in-one-dj-systems/XDJ-XZ/XDJ-XZ_DRI1625B_manual.pdf | `c36e71e6f907d7c49ce9e38764030535fff17038186523adab223410ca2109c5` |

Page 1 of the MIDI list embeds one top-panel image (1850 x 1419 px, JPEG, `pdfimages` object 6;
sha256 `a8d6fe8f79020f374bd035c0c615f263c872af6e053812736710a5a590f08513`). It is the source of
the measured trace in `tools/deck-diagrams/devices/xdj-xz/panel.reference.json`; the image itself
stays out of the repo.

## Panel size

The operating instructions above carry no Specifications section (the printed Quick Start Guide
does, and it is not hosted). The published size comes from the Pioneer DJ product specifications
page, `https://www.pioneerdj.com/en/product/all-in-one-system/xdj-xz/black/specifications/`,
read from its Wayback Machine capture of 11 Dec 2023 (the live URL now redirects to the product
overview): Width 878 mm, Depth 466.1 mm, Height 118.4 mm, Weight 13 kg, Jog Wheel 206 mm.
Retail listings repeat the same numbers.

## Channel layout

Browser 1; deck n on channel n; mixer and effects 5; performance pads on channel 5 + n; "others and
jog display" on 12 (MIDI-OUT only). SHIFT (fig 1-13) has no wire: it is hardware-encoded, like the
XDJ-RX3. The list is wire-for-wire the RX3 list for every control `XDJ_RX3_MAP` binds or hints,
which is why `apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj-xz.ts` extends that map.

Rows marked "Hardware Control" (JOG ADJUST, JOG RING, MIC LEVEL and MIC switches, TALKOVER, level
meters, USB STOP, MASTER REC, TRACK MARK, SHORT CUT, display tabs) send nothing and are not placed.

Not run on hardware.
