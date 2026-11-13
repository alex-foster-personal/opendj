/**
 * Beat FX FILTER (issue #9794): the beat-synced LFO filter (`fx/beat-fx-filter-kernel.ts`,
 * FXFILT-08) as a unit effect. The unit's slot is a send and return: the channel dry stays at
 * 1 and the return joins masterGain. The filter is an insert (LEVEL is its dry/wet balance,
 * DJM-A9 manual p. 54, mimicked), so the slot returns the kernel's balance minus the send:
 *
 * ```
 * send -> kernel.input -+-> kernel (dry 1 - L, lowpass L) ---+-> return
 *                       +-> cancel (-1) ---------------------+
 * dry (x) + return (L * (LP(x) - x)) = (1 - L) * x + L * LP(x)
 * ```
 *
 * At level 0 the return is exactly 0. The LFO runs on the kernel's TIME clock with the unit's
 * division time as its period; a new period keeps the current phase (no restart on each beat
 * press). Grid-anchored phase needs the target's grid in the slot contract (follow-up).
 */
import {
	assertBeatFxFilterBeats,
	assertBeatFxFilterTimeMs,
	beatFxFilterPhase,
	buildBeatFxFilterKernel,
	type BeatFxFilterClock
} from '../beat-fx-filter-kernel';
import { beatDivisionToMs } from '../time-fx-catalog';
import { RecordingKernelHost, type BeatFxKernel } from './kernel-host';
import { registerBeatFxEffect } from './registry';

/** Build the LFO FILTER for the unit's slot: the return carries only what the filter changes. */
export function createLfoFilterSlotKernel(ctx: BaseAudioContext): BeatFxKernel {
	const filter = buildBeatFxFilterKernel(ctx);
	const cancel = ctx.createGain();
	const output = ctx.createGain();
	cancel.gain.value = -1;
	filter.input.connect(cancel).connect(output);
	filter.output.connect(output);
	let clock: BeatFxFilterClock | null = null;
	return {
		input: filter.input,
		output,
		setTiming: (seconds, at) => {
			if (clock !== null && clock.periodSec === seconds) return;
			assertBeatFxFilterTimeMs(seconds * 1000);
			const phase = clock === null ? 0 : beatFxFilterPhase(clock, at);
			// TIME clock: `beats` is unread in time mode; the period is the unit's division time.
			clock = {
				mode: 'time',
				beats: 1,
				timeMs: seconds * 1000,
				periodSec: seconds,
				contextTime: at - phase * seconds,
				reason: 'no-tempo'
			};
			filter.setClock(clock, at);
		},
		setDepth: (depth, at) => filter.setLevel(depth, at),
		dispose: () => {
			filter.dispose();
			cancel.disconnect();
			output.disconnect();
		}
	};
}

registerBeatFxEffect({
	name: 'filter',
	levelLaw: 'balance',
	builtOnMain: true,
	usesDivision: true,
	assertDivisionAllowed: (beats, bpm) => {
		assertBeatFxFilterBeats(beats);
		assertBeatFxFilterTimeMs(beatDivisionToMs(beats, bpm));
	},
	createHost: () => new RecordingKernelHost('filter'),
	createKernel: createLfoFilterSlotKernel
});
