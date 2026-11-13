# XDJ-XZ footnotes

Source: Pioneer DJ `XDJ-XZ_MIDI_Message_List_E1.pdf` (E1, (c) 2019), sha256
`c0e23435447a5c3f444f55e777e4d7e1b02a0abd6238e8dd0ff1a6a00df867b9`, fetched from
`downloads.support.alphatheta.com/software_info/all-in-one-dj-systems/XDJ-XZ/` on Fri 13 Nov 2026
(same file as `docs/controller/research/XDJ-XZ-MIDI-Message-List.md`). The PDF, its text
extraction and full-page renders are local only (gitignored). `source/plate-top.png` is the
page-1 top-panel photo (embedded image, no fig labels), downscaled to 1400 x 1074.
Runtime map compared: `XDJ_XZ_MAP` in
`apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj-xz.ts` (not changed here). Rows whose
text extraction interleaves two lines (CUE/LOOP CALL, TRACK SEARCH, SEARCH, BEAT LEFT/RIGHT,
FILTER/FLANGER, INPUT SELECT) were read from word positions on the PDF page and agree with it.

Not hardware confirmed: nothing in this directory has been checked against a live unit.

## Geometry

Box positions are the measured image-px boxes from `panel.reference.json` (trace of the same
page-1 photo, 1850 x 1419 px) converted to percent of plate. Three touch display GUI targets
are not in that trace and are placed by eye on the display: NEEDLE POSITION (fig 1-27, one
strip per deck half) and DECK SELECT (fig 4-8, SHORT CUT).

## Channel assignment

- ch 1: browser (shares the channel with deck 1; no note/CC collisions found).
- ch 1-4: deck 1-4 (status 9n/Bn, n = deck - 1). The left deck sends deck 1 or 3, the right
  deck 2 or 4, by DECK SELECT (fig 4-8). Layout boxes carry ch 1 (left) or ch 2 (right) and
  label the pair, e.g. `[deck 1/3]`.
- ch 5: effect and mixer.
- ch 6-9: performance pads of deck 1-4. Pad note = 16 x mode index + pad index (modes HOT CUE,
  BEAT LOOP, SLIP LOOP, BEAT JUMP, EXTENSION1-4); +SHIFT adds 8. PAD MODE buttons (5-1..5-4)
  send notes 34-37 on the deck channel, +SHIFT 38-41 selects EXTENSION1-4.
- ch 12: MIDI OUT only (LOAD button illumination, jog display position bar and cue marker).

## SHIFT encoding

SHIFT (fig 1-13) has no wire: it is hardware-encoded. The list prints +SHIFT rows for JOG
platter rotate/touch, JOG wheel side, ROTARY SELECTOR rotate/press, BACK, TAG TRACK, TRACK
FILTER, PAD MODE buttons and every pad mode; those are the `_shift` twins joined in layout.json.
Long-press rows (BEAT SYNC 71, 4 BEAT 68, LOOP IN 69, TIME MODE 15) are separate base-layer rows.

## Multi-wire controls

- Rotary switches send one CC per position: EFFECT SELECT (2-9, 14 effects) and FX CH SELECT
  (2-10, 11 positions); the box joins the first row and every row is in midi.json.
- INPUT SELECT (3-4) and AUX SELECT (3-31) slide switches send one wire per position.
- TEMPO (1-2, CC 0/32) and TIME (2-11, CC 13/45) are MSB/LSB pairs; the box joins the MSB.
- X-PAD (2-1) sends CC 116 (slider mode) or CC 117 (beat mode); press is CC 114, the same wire
  as BEAT FX ON/OFF (2-13).
- PC Control mode (CC 100 on the deck channel) has no plate fig and no box.

## Out of scope (hardware control, no wire)

1-8 JOG ADJUST, 1-13 SHIFT, 1-25 JOG RING, 3-18..3-20 meters and clip, 3-21 MIC switches,
3-22..3-24 MIC indicators and LEVEL, 3-30 TALKOVER, 4-5 SHORT CUT, 4-9..4-16 source and
display buttons, 6-1..6-6 USB STOP, indicators, MASTER REC and TRACK MARK. 7-1 and 7-2 are jog
display MIDI OUT (ch 12) and are not hit targets.
