/**
 * The device-level counterpart to `silence-watchdog.ts`'s P0 (issue #1642):
 * "the mixer is producing signal" and "the room can hear it" are two
 * different claims, and neither shipped instrument compares them. Both
 * `master-silence-report.ts` and `meter-tap.ts` (the #1575 master meter) tap
 * `_masterGain`, upstream of the output device, so a dead device reads as a
 * healthy signal for the whole outage.
 *
 * THE PROBE IS NOT NEW. `presentation.ts`'s `observePresentedTransportTimeline`
 * already detects this exact condition - `AudioContext.getOutputTimestamp()`'s
 * `contextTime` (the HAL output position) frozen while `performanceTime` keeps
 * advancing - and it is proven against two real incidents, not assumed:
 * Wed 2 Sep 2026 (`.planning/hardening-ledger/decisions/presentation-clock-fallback.md`)
 * and the Wed 9 Sep 2026 17:44Z flap (`audio-output-rebind.ts`'s docstring).
 * `getOutputTimestamp().contextTime` vs `performance.now()` and "the
 * presentation-clock reporter already present" are the same mechanism; this
 * module composes the SHIPPED, hardened one rather than re-deriving it, which
 * is also why `sinkId`/`enumerateDevices()` reconciliation lost the
 * evaluation - it has no production evidence behind it for this pipeline, and
 * the app never calls `setSinkId` on the master output, so a sink id alone
 * cannot tell "still on default" from "default device changed under us".
 *
 * WHAT THIS MODULE ADDS: the composition neither existing consumer of the
 * stall makes. `presentation-clock-report.ts` uses the stall to trigger a
 * waveform fallback and a re-bind attempt; it does not gate on whether the
 * mixer is still producing signal, so it cannot be read as "the room is
 * silent" on its own - a stall during a genuinely quiet master bus would just
 * double-report what `silent-while-playing` already says. Gating on
 * `masterRms >= SILENCE_RMS_FLOOR` is what keeps the two claims apart: this
 * verdict fires ONLY in the disjoint case - signal present, device gone -
 * which is the entire reason #1642 was filed.
 *
 * A CLOCK STALL ALONE IS NOT DEVICE DEATH (P1 review thread on PR #1693).
 * `observePresentedTransportTimeline` falls back to the render-thread sample
 * clock whenever `contextTime` freezes and that sample clock is still
 * advancing - which is true both for the Wed 2 Sep 2026 CoreAudio timestamp
 * glitch (device alive, rendering) and for a genuinely dead sink (render
 * thread keeps processing samples with nowhere to send them). `clock_source`
 * cannot tell those apart, so this module does not ask it to. It composes a
 * SECOND shipped, proven signal instead: `audio-output-liveness.ts`'s own
 * debounced verdict, that `AudioContext.outputLatency` has read 0 for
 * `LIVENESS_DEAD_POLLS` consecutive polls while a deck played - the same
 * measurement that caught the Wed 2 Sep 2026 incident in the first place.
 * `outputLatencyDead` corroborates the stall rather than replacing it: a
 * benign glitch leaves `outputLatency` reporting its real, nonzero figure the
 * whole time, so the conjunction stays false and only a genuinely dead
 * device satisfies both.
 *
 * A PURE FOLD, not a class owning a timer, for the same reason
 * `silence-watchdog.ts` is one: the decision is what is worth testing, and a
 * decision observable only by waiting out a real timer is untestable. The
 * caller supplies the clock.
 *
 * EDGE-TRIGGERED, like its sibling: one verdict per outage, not one per frame.
 */

import { SILENCE_RMS_FLOOR } from '$lib/rb/silence-watchdog';

export type DeviceLivenessVerdict = 'ok' | 'device-unreachable';

export interface DeviceLivenessSample {
	/** Whether a deck claims to be playing right now. */
	playing: boolean;
	/** RMS measured at the master bus, upstream of the output device. */
	masterRms: number;
	/** The presentation clock's stall edge: `getOutputTimestamp().contextTime` frozen. */
	deviceClockStalled: boolean;
	/**
	 * `audio-output-liveness.ts`'s own debounced verdict: `outputLatency` has
	 * read 0 for `LIVENESS_DEAD_POLLS` consecutive polls while a deck played.
	 * The corroborator that turns a clock stall into device death rather than
	 * a benign timestamp glitch (P1 review thread on PR #1693).
	 */
	outputLatencyDead: boolean;
	tMs: number;
}

export interface DeviceLivenessState {
	/** Whether the current unreachable run has already fired its one-shot side effect. */
	reported: boolean;
	lastTMs: number | null;
	/**
	 * EDGE-TRIGGERED: `'device-unreachable'` only on the sample that crosses
	 * into an outage, `'ok'` on every other sample - for a consumer that must
	 * fire a side effect (toast, perf event) exactly once per outage.
	 */
	verdict: DeviceLivenessVerdict;
	/**
	 * LEVEL-TRIGGERED: `'device-unreachable'` for every sample of an ongoing
	 * outage, until the presentation clock recovers or playback stops - for a
	 * consumer projecting the CURRENT state of the room (UI mirror `audible`,
	 * the toast list rebuilt every publish tick). Without this, a per-second
	 * poll reads `verdict`'s single crossing sample, then `'ok'` for the rest
	 * of the outage, and reports no ongoing problem once the one-shot toast
	 * auto-dismisses (P1 review thread on PR #1693).
	 */
	live: DeviceLivenessVerdict;
}

const EMPTY: Readonly<DeviceLivenessState> = Object.freeze({
	reported: false,
	lastTMs: null,
	verdict: 'ok' as DeviceLivenessVerdict,
	live: 'ok' as DeviceLivenessVerdict
});

export function foldDeviceLivenessSample(
	state: Readonly<DeviceLivenessState> = EMPTY,
	sample: DeviceLivenessSample
): DeviceLivenessState {
	const { playing, masterRms, deviceClockStalled, outputLatencyDead, tMs } = sample;
	// A broken meter must not read as evidence either way: see
	// silence-watchdog.ts's identical guard for why this throws rather than
	// substituting a default.
	if (!Number.isFinite(masterRms) || masterRms < 0) {
		throw new RangeError(`masterRms must be a finite non-negative number, got ${masterRms}`);
	}
	if (!Number.isFinite(tMs)) {
		throw new RangeError(`tMs must be a finite number, got ${tMs}`);
	}
	if (state.lastTMs !== null && tMs < state.lastTMs) {
		throw new RangeError(
			`sample time went backwards (${tMs} after ${state.lastTMs}); an edge-triggered ` +
				'verdict cannot reason about ordering that moves backwards'
		);
	}
	// The RUN is bounded by the clock stall and playback continuing, not by a
	// momentary dip in signal: a quiet break in the track must not read as
	// the outage ending and a fresh one starting (P2 review thread on PR
	// #1693 - re-arming here would toast-storm a single outage that happens
	// to straddle a quiet passage).
	//
	// `outputLatencyDead` is required alongside the clock stall, not instead
	// of it: a stall is common to both a benign glitch and a dead device, and
	// only the corroborator distinguishes them (P1 review thread on PR
	// #1693, see module docstring).
	const clockStallActive = playing && deviceClockStalled && outputLatencyDead;
	if (!clockStallActive) {
		return { reported: false, lastTMs: tMs, verdict: 'ok', live: 'ok' };
	}
	// The graph producing signal is what keeps this claim DISJOINT from
	// silent-while-playing: a device that is gone while the mixer is ALSO
	// quiet is already covered by that verdict, and firing this one too would
	// blur the two claims #1642 exists to keep apart. Once signal has proven
	// THIS run is the disjoint case, a later quiet frame does not retract the
	// proof - sticky on `state.live` for the same reason as the run boundary
	// above.
	const graphProducingSignal = masterRms >= SILENCE_RMS_FLOOR;
	const unreachable = graphProducingSignal || state.live === 'device-unreachable';
	if (!unreachable) {
		return { reported: false, lastTMs: tMs, verdict: 'ok', live: 'ok' };
	}
	const crossed = !state.reported;
	return {
		reported: true,
		lastTMs: tMs,
		verdict: crossed ? 'device-unreachable' : 'ok',
		live: 'device-unreachable'
	};
}
