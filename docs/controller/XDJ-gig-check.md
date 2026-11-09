# XDJ-RX3 / XDJ-AZ soundcheck

The XDJ-AZ map ran on the unit on Fri 6 Nov 2026 (set build da92dbd1, issue #10288); the RX3 map has not run on hardware. Do this in order, before doors.

1. Connect USB. RX3: UTILITY > MIXER MODE. AZ: SOURCE > SOFTWARE CONTROL, or BROWSE > MIDI as the unit offers.
2. Open Performance. The footer must show a mapped device, not just a MIDI port. If it is unmapped, the port name does not contain `XDJ-RX3` or `XDJ-AZ`: read the name from the learn log.
3. Move a channel fader up. AZ: confirmed normal on Fri 6 Nov 2026 (the learn log rose as the faders went up; the vendor list's "Upper Side=0" is the error). RX3: if the app fader falls, the map needs `invert: true` on the fader row.
4. Press PLAY, CUE, a hot cue pad and CH CUE on each side. AZ: also pads 5 to 8, which use separate channels.
5. Move TEMPO, crossfader, EQ, COLOR and browse + LOAD.
6. Confirm audio reaches the booth and the headphones through the unit.

AZ, seen Fri 6 Nov 2026:

- Audio routing: Open DJ's USB output channels 1 and 2 arrive at the unit's mixer channel 1, so channel 1's fader, EQ, trim and filter act on the whole mix, and one deck cannot be pre-listened through the unit. Ride the mix on channel 1 until the routing work in epic #10241 lands. Headphone cue on the unit works.
- The unit's screens stay blank in software control. That is expected: they are not driven over MIDI.
- Jogs do nothing while SHIFT is held.
- The LED test lit nothing on the unit (tracked separately in epic #10241).

Jog: paused it scrubs, playing it nudges the tempo while it turns. There is no scratch or vinyl mode.

Jog HITL (review-followup issue #7973, still open): the constants in `apps/webui/frontend/src/lib/rb/midi/jog.ts` are Mixxx FLX10 estimates, not wheel measurements. A Linux CI host cannot set them. On a real XDJ-RX3, XDJ-AZ, or DDJ-FLX10:

1. Paused, turn one full revolution. Record MIDI tick count and the seek delta in ms. `JOG_SCRUB_MS_PER_TICK` is seek_delta_ms / ticks (shipped estimate: 2.5).
2. Playing, turn at a typical nudge. Record ticks and the tempo-ratio change. `JOG_BEND_PER_TICK` is ratio_delta / ticks (shipped estimate: 0.002). Cap a hard spin with `JOG_BEND_MAX` (shipped estimate: 0.04).
3. Do not derive replacements from Mixxx 1500/5760 ticks-per-turn.
4. After measuring, update `jog.ts` and the pins in `apps/webui/frontend/tests/unit/xdj-maps.test.mjs` (today: 4 ticks -> +10 ms scrub; 5 ticks -> 0.002 bend), then tick the remaining #7973 box.

Until that run, keep the estimates. Soundcheck still starts here: if scrub overshoots or a nudge is unusable, stop and play from USB.

Not mapped: jog touch, needle search, slip, reverse, Beat FX.

If any step fails and cannot be fixed in minutes, play from USB on the unit itself. Have the set exported to USB before leaving.
