/**
 * Stem mute/solo/gain apply extracted from audio-engine.svelte.ts (MIXUX-04).
 * Keeps the hotspot file from growing while stem EQ mode lands.
 */

import type { DeckState } from '$lib/rb/deck-state-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { MixerChannelState } from '$lib/rb/mixer-types';
import { AlignedStemDeckProcessor, STEM_CONTROLS } from '$lib/rb/stem-graph';
import type { StemControl } from '$lib/rb/stem-types';

export interface StemEngineRuntime {
	processor: unknown;
}

export interface StemEngineControlDeps {
	requireLoaded: (deck: DeckId, op: string) => { st: DeckState; rt: StemEngineRuntime };
	getChannel: (deck: DeckId) => MixerChannelState;
}

export function applyStemControl(
	deck: DeckId,
	stem: StemControl,
	field: 'muted' | 'solo' | 'gain',
	value: boolean | number,
	deps: StemEngineControlDeps
): void {
	if (!STEM_CONTROLS.includes(stem)) {
		throw new TypeError(`stem must be vocal, instrumental, or drums; got ${String(stem)}`);
	}
	const op =
		field === 'muted' ? 'setStemMute' : field === 'solo' ? 'setStemSolo' : 'setStemGain';
	const { st, rt } = deps.requireLoaded(deck, op);
	if (st.stems.status === 'ready' && !st.stems.available_controls.includes(stem)) {
		throw new Error(
			`deck ${deck} stem layout ${String(st.stems.layout)} has no ${stem} control; ` +
				`available: ${st.stems.available_controls.join(', ')}`
		);
	}
	if (st.stems.status === 'unavailable') {
		// A settled "this track has no stem bundle" - not a failure the user
		// caused or can fix by retrying. The controls that could reach here
		// are already disabled in the UI; anything else that calls this
		// (agent orders, replayed commands) gets a silent no-op rather than
		// a throw that would light up the deck's red error banner for the
		// normal, permanent state of any track without stems.
		return;
	}
	if (st.stems.status !== 'ready' || !(rt.processor instanceof AlignedStemDeckProcessor)) {
		throw new Error(
			`deck ${deck} stems are ${st.stems.status}: ${st.stems.error ?? 'no aligned artifact'}`
		);
	}
	const controls = {
		vocal: { ...st.stems.controls.vocal },
		instrumental: { ...st.stems.controls.instrumental },
		drums: { ...st.stems.controls.drums }
	};
	if (field === 'gain') {
		if (typeof value !== 'number') {
			throw new TypeError('setStemGain: value must be a number');
		}
		controls[stem].gain = value;
	} else {
		if (typeof value !== 'boolean') {
			throw new TypeError(`setStem${field === 'muted' ? 'Mute' : 'Solo'}: ${field} must be boolean`);
		}
		controls[stem][field] = value;
	}
	st.stems.controls = controls;
	rt.processor.setControls(controls);
}

export function applyStemEqMode(
	deck: DeckId,
	enabled: boolean,
	getChannel: (deck: DeckId) => MixerChannelState
): void {
	if (typeof enabled !== 'boolean') {
		throw new TypeError('setStemEqMode: enabled must be boolean');
	}
	getChannel(deck).stem_eq_mode = enabled;
}
