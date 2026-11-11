# XDJ-AZ footnotes

Source: AlphaTheta `xdj-az_midi_message_list_e1.pdf` (E1, (c) 2024), sha256
`2a56db4cc52c5bbf2371c33cbb0188a15ed28daf841f712cac343f4025f54216`, fetched from the
Wayback Machine copy of pioneerdj.com on Fri 6 Nov 2026. The PDF, its text extraction
and full-page renders are local only (gitignored). `source/plate-top.png` is a crop of
the page-1 panel figure. The crop is not committed on this head (vendor full-page renders stay local), so deck.html and overlay.html show a visible alert when the image is missing rather than a silent broken underlay. Runtime map compared: `XDJ_AZ_MAP` in
`apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts` (not changed here).

Not hardware confirmed: nothing in this directory has been checked against a live unit.

## RISK: CH FADER direction (fig 3-2)

The AZ list prints every CH FADER row (CC 17/18/19/20, ch 5) as
`Upper Side=0(0x00), Lower Side=127(0x7F)`. Every other Pioneer list, the RX3 included,
reads fader-down = 0. `XDJ_AZ_MAP` binds the faders with no inversion, i.e. it assumes
lower = 0. If the PDF is right, a fader pulled DOWN sends 127 and the app plays that
channel at full volume. Check at soundcheck: pull one fader fully down and confirm the
app channel goes silent before the set. The diagram outlines all four CH FADERs in
dashed yellow and carries a banner for this.

## Channel assignment

- ch 1: browser (shares the channel with deck 1; no note/CC collisions found).
- ch 1-4: deck 1-4 (status 9n/Bn, n = deck - 1). The left deck side sends deck 1 or 3,
  the right side deck 2 or 4, by DECK SELECT (fig 1-10). Layout boxes carry ch 1 (left)
  or ch 2 (right) and label the pair, e.g. `[deck 1/3]`.
- ch 5: effect and mixer.
- ch 6-9: pads 1-4 of deck 1-4; ch 10-13: pads 5-8 of deck 1-4.
- ch 14: MIDI OUT only (LOAD LED blink, jog display position and cue marker).

## SHIFT encoding

SHIFT (fig 1-14) sends its own wire, Note 63 on the deck channel, AND shifted controls
send distinct twin codes (hardware-encoded): jog platter CC 41 (base 34), jog side CC 38
(base 33), jog touch Note 72 (base 32), SLIP REVERSE Note 82, BEAT JUMP FWD/REV
Note 23/25, pad mode buttons Note 38-41, pads base+1 per mode, browser rotate CC 96,
press Note 97, BACK 95, TAG TRACK 98, TRACK FILTER 93.

## Pads

Note = 19 * (pad index mod 4) + mode offset. Offsets: HOT CUE 0 (+SHIFT 1), BEAT LOOP 2
(3), PAD FX BANK1 page 1/2 = 4/5, PAD FX BANK2 page 1/2 = 6/7, +SHIFT 8, BEAT JUMP 9 (10),
GATE CUE 11 (12), BEAT LOOP2 13 (14), KEY SHIFT 15 (16), BEAT JUMP2 17 (18). The
text extraction groups offsets 4-8 under "SLIP LOOP mode" and "PAD FX BANK1/BANK2"
ambiguously; midi.json names them by the printed PAD FX bank/page and should be
re-read against the PDF before any binding is built on them. The PDF table numbers the
pad rows `6[L,R]` while the panel figure labels the pads 5-5..5-12; layout follows the
figure. Pad LED out on the shifted rows is a color number 1-127.

## Hardware only or out of scope (not placed)

- 1-8 JOG ADJUST, 1-26 JOG RING, 3-21 MIC 1/2 LEVEL, 4-5 SHORT CUT: hardware control, no MIDI.
- 7-1 / 7-2 jog display current position bar and cue marker: MIDI OUT only (ch 14).
- 1-27 DECK QUANTIZE: drawn on the left only; the plate figure does not mark a right-side twin.
- 1-28 LOAD: one box over the four LOAD buttons (Note 81 on the target deck channel).
- 2-9 BEAT FX SELECT sends a different CC per detent (DELAY 42 ... MOBIUS 58); the box carries DELAY.
- 2-10 CH SELECT is one box over 8 buttons; the label lists every CC.

WAIVE_FIG: 3-3
WAIVE_FIG: 3-4

3-3 CROSSFADER ASSIGN (CC 65-68, A=0 / THRU=0x40 / B=0x7F) and 3-4 INPUT SELECT
(Note 59-66) are set in Mixer Settings / UTILITY, not on the plate.

## Map vs PDF disagreements (XDJ_AZ_MAP)

1. CH FADER direction: see RISK above. Map assumes lower = 0; PDF prints upper = 0.
2. Jog: map binds both CC 34 (platter) and CC 33 (wheel side) to `deck_jog`
   relativeCentered. PDF agrees on codes and the centre-64 encoding, but the shifted jog
   twins CC 41 / CC 38 are unbound, so jog does nothing in the app while SHIFT is held.
   Jog touch Note 32 (and 72 shifted) is a hint only, so there is no scratch.
3. SHIFT: map binds Note 63 as `shift_modifier` (wire agrees). The unit also sends
   hardware twin codes for shifted controls, so a software shift layer on top of the
   unit's own would double up; today the map binds none of the twin codes, so no
   conflict yet. The file header says "The RX3 list gives SHIFT no wire"; on the AZ it has one.
4. LOOP OUT: map binds Note 6 (LOOP IN) and Note 7 (LOOP OUT) to the same
   `deck_manual_loop_cycle`; the PDF gives them distinct roles.
5. Unbound in the map but present in the PDF: MASTER Note 30, SYNC long press Note 71,
   BEAT JUMP 22-25, DECK QUANTIZE 26, TEMPO RESET 19, browser press Note 51, BACK 50,
   LINK CUE CC 84, all Beat FX and Sound Color FX controls, master EQ, booth, mic.
6. Crossfader: CC 11 ch 5, left 0 / right 127. Map agrees.
7. Tempo: CC 0 MSB + CC 32 LSB, '-' side 0 and '+' side 16383. Map agrees (lsbOffset 32);
   which physical end is '-' is not stated by the PDF.
8. Trim, EQ, COLOR, CH CUE, master, headphones, browse encoder, LOAD, pads (hot cue and
   beat loop): codes agree with the map.
9. X-PAD press and BEAT FX ON/OFF share CC 114 on ch 5 (PDF); not in the map.
