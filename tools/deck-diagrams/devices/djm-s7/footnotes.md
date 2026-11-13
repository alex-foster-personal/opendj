# DJM-S7 diagram notes

## Provenance

Derived from the [official AlphaTheta MIDI list](https://downloads.support.alphatheta.com/software_info/dj-mixers/DJM-S7/DJM-S7_MIDI_Message_v100_E.pdf),
`DJM-S7_MIDI_Message_v100_E.pdf`, retrieved Fri 13 Nov 2026. SHA-256:
`f345cf74d993288f7a2a6552a9d9af8c4d2bb03bac6440981281530cd85cd028`.
The six-page PDF, text extraction and full-page renders stay local and ignored.
`source/plate-top.png` is the crop (260, 150, 945, 1430) of the page-one render
at 108 dpi: top plate plus front panel with callouts. Layout percentages use
that 685 by 1280 crop; targets are centred on the printed callout tags.

## Fig identifiers

The vendor uses hyphenated figs (D1-1, M2-1). They are written lowercase with
an underscore (`d1_1`, `m2_1`) so the checksum tag pattern accepts them; plain
figs are lowercased too (`m7`, `e1`). Vendor rows listing two figs (for example
`M2-1 M2-2`, `D1-13 D2-13`, `M9 M10`) are expanded into one row set per fig.

## Channels

Status bytes are authoritative. `9n`/`Bn` resolve n=0/1 to channel 1 for deck 1
figs (D1-x, Mx-1, M9) and channel 2 for deck 2 figs. `9p` resolves to pad
channels 8/9. `94/95/96` and `B4/B5/B6` are channels 5/6/7. Where the printed
decimal channel disagrees with the status (BROWSE, BACK print channel 7 with
status `Bn`/`9n`), the status wins and each row records the printed values.

## Encoding

14-bit controls have MSB and LSB CC rows sharing a fig; layout joins the MSB.
SHIFT rows use layer `shift`. Rows printed with a condition (FX selected, fader
start transitions, FX-button-held BROWSE) use `mode`, or `shift_mode` when also
under SHIFT. Layout joins the first row of each layer present for the fig.

## Publication inconsistencies

- M18 BLUETOOTH LEVEL prints decimal 4/37 but hex Data1 05/25; hex is used.
- E12 base condition prints decimal 2/34 but hex 0A/2A; hex is used. Its
  SERATO ECHO row prints decimal 4/36 with hex 02/22; hex is used.
- English pitch labels are ignored where they disagree with hex Data1.

## Exclusions

M16 MIC LEVEL and M20 CROSSFADER FEELING ADJUST are hardware-only and have no
MIDI rows or targets. Meters, input selectors and sockets are not interactive.
This is a transcription of the publication, not a hardware capture.
