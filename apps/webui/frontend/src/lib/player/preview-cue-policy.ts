/**
 * Whether a library preview can actually be heard, and where it would come
 * out. A pure leaf: no imports, no graph, no state, so the decision can be
 * unit tested without a browser and cannot drift into the player's own
 * bookkeeping.
 *
 * Mini-PRD
 * --------
 * R1 (CUEOUT-15) A preview never plays into a path the operator cannot hear,
 *    and never claims to be playing when the gain on its route is zero.
 * R2 (CUEOUT-15) The refusal names the specific cause, so "I clicked it and
 *    nothing happened" cannot be the operator's experience.
 * R3 (CUEOUT-15) The route is reported, because in `practice` the preview is
 *    audible on the ROOM output and the operator must know that before a set.
 *
 *   [if] the audio graph has not been built [then] refused, naming the page ⛔️
 *   [if] no output device has been enumerated yet (`supported` false, the state
 *        of any page where the I/O menu has never been opened) [then] NOT
 *        refused: practice and split-cable need no device selection at all ⛔️
 *   [if] `navigator.mediaDevices` is unavailable (CUEOUT-14 packaged app)
 *        [then] refused, naming the packaged app rather than the operator ⛔️
 *   [if] a cue device is selected but its sink never started
 *        [then] refused, naming the dead sink, NOT played into silence ⛔️
 *   [if] the gain on the chosen route is exactly 0
 *        [then] refused, naming MIX or GAIN ⛔️
 *   [if] the gain is above 0 but below AUDIBLE_FLOOR
 *        [then] played, with a warning naming the knob ⛔️
 *   [if] output mode is `practice` [then] route is `main_practice` and the
 *        warning says the room can hear it ⛔️
 *
 * Status: ✔︎ ✅ 🎯
 */

/** Where a preview connected to the cue bus physically comes out. */
export type PreviewRoute = 'cue' | 'main_practice' | 'split_right';

/** Below this linear gain a preview is present but effectively inaudible.
 * 0.02 is about -34 dB, far under any usable monitoring level and well above
 * the denormal noise a smoothed AudioParam settles at. */
export const AUDIBLE_FLOOR = 0.02;

export interface PreviewCueInputs {
	/** The engine has built its graph, so a cue bus node exists to join. */
	graph_built: boolean;
	/** `mixerState.headphones.active`: the monitor element accepted its sink. */
	active: boolean;
	output_mode: 'practice' | 'two_outputs' | 'split_cable';
	/** Label of the selected cue device, for the refusal text. May be empty. */
	cue_device_label: string | null;
	/** The LINEAR gain the preview reaches the ear at on its route, computed by
	 * the caller from `headphones.ts`'s own mix maths. Passed in rather than
	 * recomputed here so there is exactly one copy of that arithmetic. */
	cue_path_gain: number;
}

export type PreviewCueVerdict =
	| { audible: true; route: PreviewRoute; warning: string | null }
	| { audible: false; refusal: string };

function _routeFor(mode: PreviewCueInputs['output_mode']): PreviewRoute {
	switch (mode) {
		case 'two_outputs':
			return 'cue';
		case 'split_cable':
			return 'split_right';
		case 'practice':
			return 'main_practice';
		default: {
			const _exhaustive: never = mode;
			throw new Error(`unhandled headphone output mode: ${String(_exhaustive)}`);
		}
	}
}

/** Human name for the knob that would be at fault for a quiet route. */
function _quietKnob(route: PreviewRoute): string {
	return route === 'cue' ? 'MIX (turn it toward CUE) or GAIN' : 'MIX (turn it toward CUE)';
}

export function previewCueVerdict(inputs: PreviewCueInputs): PreviewCueVerdict {
	const { cue_path_gain: gain } = inputs;
	if (!Number.isFinite(gain) || gain < 0) {
		throw new RangeError(`preview cue path gain must be a finite gain >= 0, got ${gain}`);
	}
	if (!inputs.graph_built) {
		return {
			audible: false,
			refusal: 'preview: the audio engine is not running - load a deck once to start it'
		};
	}
	const route = _routeFor(inputs.output_mode);
	if (inputs.output_mode === 'two_outputs' && !inputs.active) {
		const which =
			inputs.cue_device_label === null || inputs.cue_device_label === ''
				? 'the selected cue device'
				: inputs.cue_device_label;
		return {
			audible: false,
			refusal: `preview: ${which} is selected as HEADPHONE CUE but is not playing - re-select it in I/O`
		};
	}
	if (gain === 0) {
		return { audible: false, refusal: `preview: the cue path is silent - raise ${_quietKnob(route)}` };
	}
	if (gain < AUDIBLE_FLOOR) {
		return {
			audible: true,
			route,
			warning: `preview is very quiet - raise ${_quietKnob(route)}`
		};
	}
	if (route === 'main_practice') {
		return {
			audible: true,
			route,
			warning: 'preview is playing on the MAIN output (practice mode, no cue device selected)'
		};
	}
	return { audible: true, route, warning: null };
}
