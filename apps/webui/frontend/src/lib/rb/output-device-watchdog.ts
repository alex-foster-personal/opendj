/**
 * The device-level counterpart to `silence-watchdog.ts`'s P0 (issue #1642):
 * "the mixer is producing signal" and "the room can hear it" are two
 * different claims, and neither shipped instrument compares them. Both
 * `master-silence-report.ts` and `meter-tap.ts` (the #1575 master meter) tap
 * `_masterGain`, upstream of the output device, so a dead device reads as a
 * healthy signal for the whole outage.
 *
 * THE GATE IS `outputLatencyDead` ALONE, corroborated only by the master bus
 * still producing signal - not, as an earlier revision of this module
 * required, a presentation-clock stall too. Requiring the stall
 * (`presentation.ts`'s `observePresentedTransportTimeline` detecting
 * `getOutputTimestamp()`'s `contextTime` frozen) was wrong in both
 * directions across two rounds of review: filtering the stall to exclude the
 * render-thread sample-clock fallback discarded exactly the stalls a dead
 * device produces (P1 review thread on PR #1693), and even an UNfiltered
 * stall requirement missed the real production incident outright -
 * `audio-output-liveness.ts`'s docstring records Wed 2 Sep 2026 as
 * `outputLatency === 0` with the HAL position (`contextTime`) still
 * ADVANCING, i.e. `deviceClockStalled: false` the whole time. A signal absent
 * from the one incident this module exists to catch cannot be a required
 * co-signal, so it was dropped from the fold entirely (P1 review thread on
 * PR #1693, `output-device-watchdog.ts:139`).
 *
 * `outputLatencyDead` - `audio-output-liveness.ts`'s own debounced verdict
 * that `AudioContext.outputLatency` read 0 for `LIVENESS_DEAD_POLLS`
 * consecutive polls while a deck played - is sufficient on its own: it is the
 * same measurement that caught the Wed 2 Sep 2026 incident in the first
 * place, and its `LIVENESS_DEAD_POLLS`-poll debounce is what already keeps a
 * momentary reading from firing this verdict.
 *
 * WHAT KEEPS THIS DISJOINT FROM `silent-while-playing`. Gating on
 * `masterRms >= SILENCE_RMS_FLOOR` is what keeps the two claims apart: a dead
 * device with the mixer ALSO quiet is already `silent-while-playing`'s
 * claim, and this verdict fires ONLY in the disjoint case - signal present,
 * device gone - which is the entire reason #1642 was filed.
 *
 * `sinkId`/`enumerateDevices()` reconciliation lost the evaluation for the
 * same reason it always did: no production evidence for this pipeline as a
 * liveness signal. CUEOUT-09 does call `AudioContext.setSinkId` to pin master,
 * but a sink id still cannot tell a dead device from a healthy one, so this
 * fold does not use it.
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
	/**
	 * `audio-output-liveness.ts`'s own debounced verdict: `outputLatency` has
	 * read 0 for `LIVENESS_DEAD_POLLS` consecutive polls while a deck played.
	 * The sole device-loss signal this fold requires (module docstring: a
	 * presentation-clock stall was tried and dropped as a required co-signal
	 * because it missed the real Wed 2 Sep 2026 incident).
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
	const { playing, masterRms, outputLatencyDead, tMs } = sample;
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
	// The RUN is bounded by outputLatencyDead and playback continuing, not by
	// a momentary dip in signal: a quiet break in the track must not read as
	// the outage ending and a fresh one starting (P2 review thread on PR
	// #1693 - re-arming here would toast-storm a single outage that happens
	// to straddle a quiet passage).
	const outageActive = playing && outputLatencyDead;
	if (!outageActive) {
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
