# CDJ-2000NXS2 diagram footnotes

Source: official AlphaTheta / Pioneer DJ operating instructions
`CDJ-2000NXS2_DRI1290A_manual.pdf`, section "List of MIDI Messages" (p.38),
fetched Fri 13 Nov 2026 from
https://downloads.support.alphatheta.com/manuals/dj-players/CDJ-2000NXS2/CDJ-2000NXS2_DRI1290A_manual.pdf
(sha256 `ee3d2e7132103df64562506973f78e8c2d631a55fb3389438be1f23740ed9c66`).
The PDF, its text extraction and page renders stay local only (manufacturer
copyright). `source/plate-top.png` is a crop of the "Control panel" figure (p.13).

No standalone MIDI Message List PDF exists for this model at the AlphaTheta
`software_info` URL pattern (404); the manual's table is the vendor source.

## MIDI channel

The unit sends on ONE channel `n`, set at UTILITY [MIDI CHANNEL] (1-16, p.37).
Every row in `midi.json` carries `ch: 1`; substitute the configured channel.
The table only applies in USB-MIDI control mode (BROWSE/SEARCH, then LINK,
CONTROL MODE, USB-MIDI). HID mode is out of scope.

## SHIFT

None. The manual lists no +SHIFT rows and the unit has no SHIFT button, so
`layout.json` declares only the `base` layer.

## Figs that carry more than one MIDI row

- `hot_cue_a`..`hot_cue_d`: the four HOT CUE buttons send A-D (9n 18-1B) or,
  after HOT CUE BANK, E-H (9n 14-17). BANK itself sends no MIDI.
- `direction_lever`: REV sends REVERSE (9n 21), SLIP REV sends SLIP REVERSE (9n 3B).
- `rotary_selector`: turn = ENCODER (Bn 4F, relative), push = ENCODER PUSH (9n 33).
- `jog_ring`: JOG RING (Bn 30) when the top is not touched.
- `jog_top`: JOG (TOUCH) (Bn 10) while turned with the top touched, plus
  JOG TOUCH switch (9n 20).
- `beat_4_8`: the 4/8BEAT button sends BEAT LOOP 4 (9n 2A, "SW/GUI button").
- `touch_display`: GUI rows on the touch display: NEEDLE (Bn 1C), BEAT LOOP
  1/16..32 except 4 (9n 22-2D), BEAT JUMP FWD/REV (9n 38/39). Their on-screen
  positions are not fixed, so they share one hit-target over the display.

## Out of scope (not in the MIDI list)

- Source buttons rekordbox, LINK, USB, SD, DISC; USB STOP
- BROWSE/SEARCH, TAG LIST, INFO/LINK INFO, MENU/UTILITY, SHORTCUT, TRACK FILTER/EDIT
- HOT CUE BANK, JOG ADJUST knob
- LED feedback, jog display and main display graphics (no MIDI-in table in the manual)

Note numbers 9n 1C, 1D, 2E, 31 and 34-37 are not listed; the gaps are the
vendor's, not omissions.
