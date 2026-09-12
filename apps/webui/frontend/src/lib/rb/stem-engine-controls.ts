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
	rt.processor.setControls(controls);
	st.stems.controls = controls;
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
