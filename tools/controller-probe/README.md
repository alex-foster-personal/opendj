# Controller probe

The instrument that turns a THEORY map into an ADJUDICATED one.

`flx4_expected_map.json` (402 controls) and the runtime map at
`apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts` were both derived from the
vendor MIDI message list PDF. Neither has been confirmed against hardware.
`capture.py` is how they get confirmed, and it is the agent-native equivalent of
the in-app learn wizard in `specs/controller-onboarding.md` section 3.4.

Files here:

| Path | What it is |
|---|---|
| `capture.py` | The CLI. Standalone PEP 723 script, runs under `uv run` with no repo install |
| `flx4_expected_map.json` | The theory-first expected map (CTRL-01) |
| `schema/expected_map.schema.json` | Draft 2020-12 schema the expected map validates against |
| `fixtures/unknown_status_byte.json` | Negative fixture: one status byte the schema must refuse |

## Live capture (T3)

Tier 3 in `../../docs/controller/research/DATA-SOURCES-INDEX.md` is live capture on
real hardware. That is what this is, in four subcommands.

### 1. Find the port

```sh
uv run tools/controller-probe/capture.py ports
```

```
[0] DDJ-FLX4
1 input port(s)
```

Exits nonzero when CoreMIDI reports no input ports at all, rather than printing
an empty list and exiting 0.

### 2. Watch the wire

```sh
uv run tools/controller-probe/capture.py sniff
# any other controller, or a virtual source:
uv run tools/controller-probe/capture.py sniff --port IAC
```

Every non-realtime inbound message prints as one line: UTC time, raw hex, the
decode, and the expected-map control it resolves to.

```
2026-09-18T02:35:56.929+00:00  90 0B 7F      note_on ch1 code 11 value 127  deck1_play_pause (PLAY/PAUSE)
2026-09-18T02:35:57.082+00:00  80 0B 00      note_off ch1 code 11 value 0  UNMAPPED
2026-09-18T02:35:57.235+00:00  B0 22 40      cc ch1 code 34 value 64  deck1_jog_vinyl_on (JOG DIAL vinyl on)
```

`--port` is a case-insensitive substring and must match exactly one port: zero
or two matches exit nonzero listing every candidate, because a probe that
guesses its own subject measures the wrong thing. Realtime bytes (status
`0xF8` and above) are dropped, so the FLX4's clock cannot flood the log.
Ctrl-C exits 0 and prints how many distinct wire pairs were mapped vs unmapped.

### 3. Walk the controls

```sh
uv run tools/controller-probe/capture.py capture \
  --out .tmp/flx4-deck1.json --section deck --deck 1 --no-shift
```

Prompts for one control at a time, in expected-map file order:

```
[1/2] Press: PLAY/PAUSE (deck 1) expecting note ch1 code 11
    -> match: 0x90/11 0x80/11
[2/2] Press: JOG DIAL vinyl on (deck 1) expecting cc ch1 code 34
    -> match: 0xB0/34 0xB0/66
```

Keys while it waits: `Enter` or `s` skips, `b` goes back one, `q` saves and
quits. Filters: `--section` (deck, mixer, browse, performance, effect),
`--deck 1|2`, `--no-shift`, `--ids a,b,c`.

A `note` control ends on its release (note-off, or the note-on-with-zero-velocity
idiom), bounded at 2.0 s. A `cc` control samples a 0.4 s window from its first
message and records each DISTINCT `(status, data1)` pair once, so a 14-bit
control shows both its MSB and LSB CC numbers (the `0xB0/66` above is the LSB
companion of `0xB0/34`).

The JSON is rewritten atomically after every step, so a crash or an unplugged
cable costs at most the control in hand.

### 4. Grade it

```sh
uv run tools/controller-probe/capture.py diff .tmp/flx4-deck1.json
```

```
id                 | expected                     | observed        | verdict
-----------------------------------------------------------------------------
deck1_play_pause   | note ch1 status 0x90 code 11 | 0x90/11 0x80/11 | match
deck1_jog_vinyl_on | cc ch1 status 0xB0 code 34   | 0xB0/34 0xB0/66 | match
EXTRA 0xB0/66 matches no expected control
match 2 / mismatch 0 / skipped 0 / not-attempted 400
```

Exit 1 on any mismatch, 0 otherwise, so it works as a gate and not only a
report.

Read the verdicts precisely:

- `match` means some observed message carried the EXACT expected
  `(status, code)`. Nothing looser counts.
- `mismatch` means the control sent traffic and never sent that pair. This is
  the finding the whole tool exists to produce.
- `skipped` means the step was skipped, or gathered no messages at all. Silence
  is not evidence against the map, so it is never a mismatch.
- `not-attempted` counts expected-map controls with no entry in the capture.
  It is measured against the FULL map, not against your filter, because the
  capture file does not record which filter produced it. A deck-1 walk
  therefore reports a large figure here by construction.
- `EXTRA` lists observed pairs that no expected control explains. A note-off
  whose note-on IS mapped is not listed, because the map holds only note-on
  rows and every button release would otherwise bury the real discoveries.

## Tests

```sh
uv run pytest tests/controller_probe -q
```

`tests/controller_probe/test_capture.py` loads `capture.py` by path and covers
the decode, reverse-lookup, verdict and diff logic, including a hand-written
capture whose single wrong status byte must exit 1 and must exit 0 once
corrected. `python-rtmidi` is in the `dev` extra for that reason: the module
imports it at top level.

The CoreMIDI transport itself is not unit tested. It is exercised against
hardware and against a virtual CoreMIDI source, which is what produced every
output pasted above.
