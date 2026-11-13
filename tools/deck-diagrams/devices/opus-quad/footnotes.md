# AlphaTheta OPUS-QUAD diagram footnotes

Source: official AlphaTheta `OPUS-QUAD_MIDI_Message_List_E1.pdf` (kept local, gitignored).
MIDI rows in `midi.json` are transcribed from that list. Hotspots in `layout.json` sit
on the controls the page-1 callout leader lines point at, not on the callout labels.

## How to read the plate

- Callouts `1-*` deck, `2-*` effect, `3-*` mixer, `4-*` browser, `7-*` jog illumination.
- The list labels the left deck once for deck channels 1/2/3/4. The left deck side sends
  deck 1 or 3 and the right side deck 2 or 4, by DECK SELECT (fig 1-11). Each deck fig has
  a left hotspot on ch 1 and a right hotspot on ch 2, labelled `[deck 1/3]` / `[deck 2/4]`.
- The mixer reads 3, 1, 2, 4 from left to right; each per-channel fig (TRIM, EQ, COLOR,
  CH CUE, CH FADER) has one hotspot per channel on that channel's row.
- `+SHIFT` rows are distinct messages and are joined as `shift_name` twins.

## MIDI channels

| Channel | Category |
|---|---|
| 1 | Browser |
| 1-4 | Decks 1-4: deck 1 rows keep the plain name; decks 2-4 have their own rows `<name>_deck2/3/4` on ch 2/3/4 |
| 5 | Effect and mixer |
| 12 | Illumination (MIDI OUT) |

## Out of scope

- `7-1` to `7-5` jog illumination: MIDI OUT only (rows kept without fig tags); no input controls.
- Touch screen: not MIDI.
