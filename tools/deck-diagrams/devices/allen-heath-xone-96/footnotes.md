# Allen & Heath Xone:96 diagram footnotes

## Source

- Allen & Heath help centre article "Xone:96 MIDI Implementation":
  https://support.allen-heath.com/hc/en-gb/articles/43084536824849-Xone-96-MIDI-Implementation
- Codes come from the article's MIDI CONTROL table (hex), converted to decimal. Article text kept local only.
- `source/plate-top.png` is the article's colour coded panel image, downscaled.

## Channel

- Default MIDI channel 16; changeable at power on (MIDI 1 + MIDI 2 held). All rows carry ch=16.

## Controls

- 31 MIDI controls: 19 switches (Note, Note On 7F / Off 00), 5 faders (CC 00 to 03 and crossfader CC 05),
  7 rotary source switches.
- Rotary source switches send one Note per position; every position is a midi.json row sharing the
  rotary's fig (P1 to P7). The layout hotspot carries the first position.
- Faders, source rotaries and PHONES 2 are OUT only; cue, filter and ON/OFF switches are OUT and IN
  (LED feedback, not modelled).
