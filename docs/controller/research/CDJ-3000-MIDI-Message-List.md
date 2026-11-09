# CDJ-3000 MIDI message list and panel size

Vendor documents, not committed. Fetched Sun 8 Nov 2026 for the practice replica (epic #9366).

| Document | URL | sha256 |
| --- | --- | --- |
| CDJ-3000 MIDI Message List E108 (5 pages, A4, Feb 2021; page 1 carries the top-panel photo with part numbers 1 to 40) | https://www.pioneerdj.com/-/media/pioneerdj/software-info/player/cdj-3000/cdj3000_midi_message_list_e108.pdf (the live URL answers an HTML interstitial to a plain fetch; the bytes below are the Wayback Machine capture of 11 May 2021, `https://web.archive.org/web/20210511194253/` plus that URL) | `44b7d91661fe4b219ab3ee05719ba7ccf4521b78aa7ee09358b4ec53de98c1aa` |
| CDJ-3000 Instruction Manual DRI1586A (89 pages, English) | https://downloads.support.alphatheta.com/manuals/dj-players/CDJ-3000/CDJ-3000_DRI1586A_manual.pdf | `46c3e5ac3eec96993777111f6e886a9ed25b071dfc79c4031cc85f6b8034628d` |

Page 1 of the MIDI list embeds one top-panel photo (762 x 1014 px, JPEG, `pdfimages` image 1,
object 13; sha256 of the extracted JPEG `3d57ec5597930b60cc78e6c86ad1020946fc92dd5c823f94abe5386605a64828`).
It is the source of the measured trace in `tools/deck-diagrams/devices/cdj-3000/panel.reference.json`;
the image itself stays out of the repo. The part numbers are PDF text drawn over the photo, so the
image carries no numbers.

## Panel size

Instruction Manual DRI1586A, Specifications, General: "Max. external dimensions 329 mm (W) x 453 mm
(D) x 118 mm (H)", weight 5.5 kg. The Pioneer DJ product specifications page
(`https://www.pioneerdj.com/en/product/player/cdj-3000/black/specifications/`) prints the same
numbers. The jog platter size is not published on either; the photo's outer jog rim reads 220 mm.

## Channel layout

One channel per player, n = the player number (1 to 4). Status `9n` for every button (note on, 7F
press, 00 release) and `Bn` for the continuous rows: JOG (TOUCH) CC 16 and JOG RING CC 48 (speed,
64 = stopped), Needle Position CC 28, TEMPO SLIDER CC 29 (7-bit, 0 at the minus end), TOUCH/BRAKE
CC 30, ENCODER CC 79 (relative, 1 to 30 and 98 to 127). The list has no MIDI-OUT rows, so the map
carries no LED rules. The 4 BEAT LOOP and 8 BEAT LOOP buttons send the BEAT LOOP 4 and 8 rows
(notes 44 and 45); the other beat-loop and beat-jump rows are touch-screen buttons.

Not run on hardware. A CDJ-3000X port also matches `CDJ-3000`; its own list was not checked.
