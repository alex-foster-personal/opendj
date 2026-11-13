# XDJ-RX3 footnotes

Source: Pioneer DJ / AlphaTheta `XDJ-RX3_MIDI_Message_List_E1.pdf` (E1, (c) 2021), sha256
`bd9c747ec65c3910d139d33cbbf08033a9ffd57b548978bf5dd51f2c2cdf9c36`, fetched from the
Wayback Machine copy of
`https://www.pioneerdj.com/-/media/pioneerdj/software-info/system/xdj-rx3/xdj-rx3_midi_message_list_e1.pdf`
(capture of Wed 12 Feb 2025) on Fri 13 Nov 2026. The PDF, its text extraction and full-page
renders are local only (gitignored). `source/plate-top.png` is a crop of the page-1 panel
figure (150 dpi). Runtime map: `XDJ_RX3_MAP` in
`apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts` (not changed here).

Not hardware confirmed: nothing in this directory has been checked against a live unit.

## Channel assignment

- ch 1: browser (shares the channel with deck 1; no note/CC collisions found).
- ch 1-4: deck 1-4 (status 9n/Bn, n = deck - 1). The left deck side sends deck 1 or 3,
  the right side deck 2 or 4. Layout boxes carry ch 1 (left) or ch 2 (right) and label the
  pair, e.g. `[deck 1/3]`.
- ch 5: effect and mixer.
- ch 6-9: performance pads of deck 1-4.
- ch 12: MIDI OUT only (LOAD button illumination, jog display position bar and cue marker).

## SHIFT encoding

SHIFT (fig 1-13) has no MIDI row of its own in the list; shifted controls send distinct twin
codes instead: jog platter CC 41 (base 34), jog wheel side CC 38 (base 33), jog touch
Note 72 (base 32), TAP CC 126 (base 78), BEAT FX ON/OFF CC 52 (base 114), browser rotate
CC 96 (base 79), press Note 97 (51), BACK 95 (50), TAG TRACK/REMOVE 98 (48), TRACK FILTER
93 (49), pad mode buttons Note 38-41 (34-37), pads base + 8.

Long-press rows (not SHIFT, kept on the base layer): SYNC Note 71, LOOP IN Note 69,
TIME MODE Note 15.

## Pads

Pad note = 16 * mode + (pad - 1), +SHIFT = +8. Modes in order: HOT CUE 0, BEAT LOOP 1,
SLIP LOOP 2, BEAT JUMP 3, EXTENSION1-4 = 4-7 (the +SHIFT layer of the four pad mode
buttons). midi.json carries pad rows on ch 6 (deck 1); decks 2-4 use ch 7-9. Pad LED out on
any pad row is a color number 1-126 (0 = off/dim).

## Plate fig notes

- 1-8: the plate labels the button 1-8-1; the list row 8-2 (VINYL SPEED ADJUST volume,
  CC 30, set via SHORT CUT, sent only while VINYL SPEED ADJUST is ON) has no own plate
  control, so both rows carry [1-8] and one box is drawn.
- 1-22: the two LOAD buttons sit under the browser knob; one box per deck side.
- 1-23, 1-24, 2-16, 4-6: touch-screen GUI targets (NEEDLE, TIME MODE, X-PAD, DECK 1/2),
  drawn over their screen area.
- 2-3 BEAT FX SELECT sends a different CC per effect (DELAY 42 ... HELIX 62); the box
  carries DELAY. 2-4 CH SELECT likewise (CF.A 39 ... MASTER 41); the box carries CF.A.
- 2-8 COLOR, 3-2 CH FADER, 3-4 to 3-7 TRIM/EQ and 3-10 CH CUE list deck 3/4 rows
  (4-channel mode); only the two plate channels are drawn.
- 3-17 AUX SELECT sends one CC per position (OFF 119, LINE 120, PORTABLE 121); the box
  carries LINE.
- Mode rows with no plate fig: PC Control mode CC 100 (deck ch), Mixer mode CC 101 (ch 5).

## Hardware only or out of scope (not placed)

- 1-13 SHIFT: no MIDI row (see SHIFT encoding).
- 4-5 SHORT CUT, 6-1 to 6-6 (USB STOP, USB indicators, MASTER REC, TRACK MARKER):
  hardware control, no MIDI.
- 3-16 CH LEVEL METER: MIDI OUT only (CC 2 on the deck channel).
- 7-1 / 7-2 jog display current position bar and cue marker: MIDI OUT only (ch 12).
- LOAD illumination (ch 12 Note 0-3): MIDI OUT only.
