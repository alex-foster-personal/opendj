# DDJ-RR diagram notes

## Provenance

Derived from the [official AlphaTheta MIDI list](https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-RR/DDJ-RR_MIDI_Message_List_E.pdf),
`DDJ-RR_MIDI_Message_List_E.pdf`, retrieved Fri 13 Nov 2026. SHA-256:
`aae9c77364e453907c4bc30cd35b1dd49a321a05e472cf62005a212113f612e9`.
The seven-page PDF, text extraction and full-page renders stay local and ignored.
`source/plate-top.png` is the crop (40, 215, 1275, 1170) of page one at 72 dpi:
top plate plus front panel with callouts. Targets are centred on the printed callout tags.

## Fig identifiers

Vendor figs (D1-L, M2-1) are written lowercase with an underscore (`d1_l`,
`m2_1`) so the checksum tag pattern accepts them. Rows listing several figs
are expanded into one row set per fig.

## Channels

Status bytes are authoritative. `9n`/`Bn` resolve to channel 1 for -L and -1
figs and channel 2 for -R and -2 figs (decks 3/4 send on channels 3/4). `9p`
resolves to pad channels 8/9 (10/11 for decks 3/4). `94/95/96` and `B4/B5/B6`
are channels 5/6/7. Where the printed channel disagrees with the status (B1
press, B2 print 1/2/3/4 with status 96 or 9n) the status wins; each row keeps
the printed values in its notes.

## Encoding

14-bit controls have MSB and LSB CC rows sharing a fig; layout joins the MSB.
SHIFT rows use layer `shift`. Rows with a printed condition (pad modes, LOAD
deck selection, vinyl on/off, input selector positions, long press) use
`mode`, or `shift_mode` when also under SHIFT. Selector rows printing two
alternative Data1 values (for example 56/57) become `_alt1`/`_alt2` rows.

## Waived figs

Channel 3/4 mixer figs are documented but not on the two-channel plate.
F10-2 has no separate callout (the plate prints one F10 tag, used for F10-1).

WAIVE_FIG: m2_3
WAIVE_FIG: m2_4
WAIVE_FIG: m3_3
WAIVE_FIG: m4_3
WAIVE_FIG: m5_3
WAIVE_FIG: m6_3
WAIVE_FIG: f10_2

## Exclusions

D29-L/R JOG INDICATOR is MIDI-OUT only. M12, M13-1 and M13-2 are on the
plate but have no rows in the list. This is a transcription of the
publication, not a hardware capture.
