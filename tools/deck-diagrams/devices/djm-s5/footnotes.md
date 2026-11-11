# DJM-S5 diagram notes

## Provenance

Derived from the [official AlphaTheta MIDI list](https://downloads.support.alphatheta.com/software_info/dj-mixers/DJM-S5/DJM-S5_MIDI_Message_v100_E.pdf),
`DJM-S5_MIDI_Message_v100_E.pdf`, retrieved Wed 11 Nov 2026. SHA-256:
`dbef5bbafbc3cc433171927b4293bb779373fcd70b5e31ba810206908df55dc0`.
The six-page PDF, local text extraction and full-page renders remain ignored.
Only the cropped interface illustration is included. The top crop is the
rectangle (375, 225, 880, 970) in the 1263 by 1786 page-one render at 108 dpi.
It includes the entire top plate and its callouts, excluding the front panel,
heading and page furniture. Pixel rectangles were measured before cropping;
layout percentages use the resulting 505 by 745 image.

## Inventory and channels

The inventory contains 380 valid input rows across 33 vendor figs. Layout has
32 targets, one per top-panel fig, and no fabricated hardware messages. Rows
represent the first channel of each explicitly documented pair: deck 1/2,
pad 8/9, pad 12/13 and pad 14/15. Each such row states the complete expansion.
FX conditions use explicit channels 5 and 6; global controls use channel 7.
Channels are one-based, while the MIDI status low nibble is zero-based.

One vendor fig labels a representative physical control: the right-side LOAD,
EQ, filter, fader and pad counterparts have no additional fig identifiers.
Their documented channel expansions remain in the inventory rather than
creating invented right-side figs or duplicate overlay targets. Pad figures
P1-P4 also represent PAD5-PAD8 when 5-8 is selected. The separate PAD5-PAD8
rows are retained even where their MODE messages repeat PAD1-PAD4.

The table enumerates eight message banks on pad channels 8/9 and four each
on 14/15 and 12/13, without naming software modes for those rows. Names use
one-based bank indices in table order; no guessed mode names are assigned.
Normal PAD1-PAD8 addresses start at 0-7, consecutive banks add 16, and SHIFT
adds 8. These formulas summarize the enumerated table, not an extrapolation
into undocumented banks. All valid listed rows are retained.

## Encoding and conditions

BROWSE has independent rotation, press and six FX/CUT-held rotation messages.
It is relative, not an absolute knob: clockwise 1-30 and counterclockwise
127-98, with the vendor's 30-click/40ms maximum. LOAD is per deck.

Every high-resolution control has separate MSB and LSB CC rows sharing a fig.
The full value is `(MSB << 7) | LSB`, 0-16383. The LSB controller is 32 above
the MSB. A layout target joins the MSB; its notes identify the pair. This
applies to trim, EQ, filter, levels, channel/cross/headphone faders and all six
LEVEL/DEPTH selection conditions. It does not turn the HTML's illustrative
0-127 nudge into a real 14-bit MIDI transmitter.

Channel faders send CC position plus the documented SHIFT fader-start notes:
102 for sync, 81 for current cue, 83 for HOT CUE1 and 82 on return to zero.
Crossfader start rows are also SHIFT rows, with channel 2 when leaving Min
and channel 1 when leaving Max. Reverse swaps the CC endpoints. The layout
joins the sync event as the representative SHIFT twin; the other events are
retained in MIDI. No threshold, velocity or timing is invented.

FX selector base rows specify ON=127 only; release is not documented there.
E1/E4 shifted rows specify both OFF/ON, while E2/E3/E5/E6 specify ON only.
Some shifted selectors also change hardware crossfader settings. The levers
retain their unconditioned messages and every selected FX/CUT condition,
including the same conditional addresses printed for both levers.

SHIFT changes addresses and is joined by `shift_name`; it is not a generic
modifier bit. Pads additionally join single-MODE and SHIFT+single-MODE twins
through `mode_name` and `shift_mode_name`. Double-MODE messages stay in MIDI:
they use 14/15, or 12/13 with SHIFT, rather than the single-MODE deck pair.
The physical SHIFT and MODE rows have no Data2 definition in the publication;
no values are claimed for them. Pad and 5-8 rows explicitly have matching
computer-to-mixer illumination addresses. Other rows have no documented
MIDI-OUT address, so illumination is not inferred.

The generated offline pages provide hover addresses, SHIFT highlighting,
button press feedback and illustrative knob/fader nudges through the existing
generator. MODE twins appear in hover metadata. The pages do not simulate
all pad banks or hardware, send MIDI, or connect to the installed app.

## Publication inconsistencies and limitations

These are source ambiguities, not hardware-verified corrections:

- M6 is BOOTH and M7 MASTER in the MIDI table, but the plate attaches M6
  to the MASTER legend and M7 to BOOTH. The targets follow the table's
  function at the actual printed BOOTH/MASTER positions. Thus these two
  targets intentionally disagree with the plate's colored callout tags.
- E6 base prints decimal channel 6 but status `96`. Its represented channel
  is 7, following the literal status, with a warning on the row. It is not
  silently changed to `95` by symmetry with E4/E5.
- E11 with CUT1 selected prints decimal CC 4/36 but wire Data1 `02/22`.
  The represented pair is 2/34, following the wire columns.
- PAD4 single-MODE and SHIFT+single-MODE print channel reference 8/9 but
  status `9n`; declared n=0/1 resolves to channels 1/2. The rows use that
  status and record the conflicting reference.
- PAD4 banks 1-4 on wire-derived channels 14/15 and 12/13, including
  their SHIFT rows, also print channel reference 8/9. The same conflict
  appears in double-MODE and SHIFT+double-MODE. Status `9D/9E` resolves
  to channels 14/15; `9B/9C` resolves to 12/13. All 18 rows retain those
  wire-derived channels and record the conflicting printed reference and
  status bytes. Hardware verification is required before runtime use.
- PAD7 and PAD8, channels 8/9, message banks 3 and 4, print statuses
  `10p`, `11p`, `12p`, `13p` instead of valid one-byte note statuses.
  These eight variants are explicitly recorded in `midi.json` under
  `unavailable`, with printed Data1, page, channel reference and status.
  They are excluded from addressable controls. No guessed `9p` repairs
  are made. Their other banks and MODE rows remain represented.
- English pitch labels are reference text only. Where those labels disagree
  with Data1 (for example shifted FX CUT5/CUT6), the numeric wire Data1 is
  authoritative for this derived inventory.

A hardware capture is needed to resolve these ambiguities before building a
runtime MIDI map. This artifact claims transcription of documented addresses,
not successful hardware testing.

## Exclusions

M13 MIC LEVEL and M15 CROSSFADER FEELING ADJUST are explicitly hardware-only.
They are front-panel controls outside this crop and have no invented MIDI
rows. Input-source selectors, meters, sockets and other unaddressed surfaces
are not interactive. Neither MIDI bytes nor HID identities are guessed.

M14 AUX LEVEL has a valid CC pair in the inventory, but is physically on the
front panel below the top plate, excluded from the top-only crop. It is the
only MIDI-bearing fig omitted from layout, with this individual waiver:

WAIVE_FIG: M14
