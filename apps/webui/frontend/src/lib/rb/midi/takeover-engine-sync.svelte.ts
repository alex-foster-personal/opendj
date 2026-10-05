/** IOPIN-06 bridge between engine-published scalar state and absolute MIDI pickup. */

import { deckStates, mixerState, pitchRanges } from '$lib/rb/audio-engine.svelte';
import type { MidiAction, MidiInputValue } from './midi-types';
import {
	noteAbsoluteMidiApplied,
	noteTakeoverSoftwareValue,
	observeAbsoluteMidi
} from './takeover-state.svelte';

function continuous01(value: MidiInputValue): number {
	if (value.kind !== 'continuous' && value.kind !== 'continuous14') {
		throw new Error(`continuous action received non-continuous input ${value.kind}`);
	}
	return value.value01;
}

/** Current engine scalar in the hardware's 0..1 domain. The policy remains
 * engine-independent while its action boundary stays honest about UI, IPC,
 * preset, and other-controller edits. */
function softwareScalar(action: MidiAction): number | null {
	switch (action.type) {
		case 'mixer_channel':
			if (action.target === 'trim') return mixerState.channels[action.deck].trim;
			if (action.target === 'fader') return mixerState.channels[action.deck].fader;
			if (action.target === 'filter') return mixerState.channels[action.deck].filter;
			if (action.target === 'eq') {
				if (action.band === undefined) throw new Error('mixer_channel eq action requires band (device map bug)');
				return mixerState.channels[action.deck][`eq_${action.band}`];
			}
			return null;
		case 'mixer_global':
			return action.target === 'master' ? mixerState.master : mixerState.crossfader;
		case 'headphone_mix':
			return mixerState.headphones.mix;
		case 'headphone_level':
			return mixerState.headphones.level;
		case 'deck_pitch': {
			const range = pitchRanges[action.deck] / 100;
			return range === 0 ? 0.5 : (deckStates[action.deck].pitch - (1 - range)) / (2 * range);
		}
		default:
			return null;
	}
}

/** Gate only physically identified absolute values. Direct programmatic
 * action-glue calls have no hardware position and intentionally stay immediate. */
export function takeoverContinuous(
	action: MidiAction,
	value: MidiInputValue,
	deviceId: string | undefined,
	controlId: string | undefined
): number | null {
	const hardwareValue = continuous01(value);
	const currentSoftwareValue = softwareScalar(action);
	if (currentSoftwareValue === null) return hardwareValue;
	const decision = observeAbsoluteMidi(
		action,
		hardwareValue,
		value.kind === 'continuous14' ? 1 / 16383 : 1 / 127,
		deviceId,
		controlId,
		currentSoftwareValue
	);
	if (decision !== null && !decision.apply) return null;
	noteAbsoluteMidiApplied(action, hardwareValue, deviceId, controlId);
	return hardwareValue;
}

/** Keep pickup targets synchronized with every engine-visible scalar change. */
export function startTakeoverEngineSync(): () => void {
	return $effect.root(() => {
		$effect(() => {
			for (const deck of [1, 2, 3, 4] as const) {
				const channel = mixerState.channels[deck];
				noteTakeoverSoftwareValue(`mixer:${deck}:trim`, channel.trim);
				noteTakeoverSoftwareValue(`mixer:${deck}:eq:high`, channel.eq_high);
				noteTakeoverSoftwareValue(`mixer:${deck}:eq:mid`, channel.eq_mid);
				noteTakeoverSoftwareValue(`mixer:${deck}:eq:low`, channel.eq_low);
				noteTakeoverSoftwareValue(`mixer:${deck}:filter`, channel.filter);
				noteTakeoverSoftwareValue(`mixer:${deck}:fader`, channel.fader);
				const range = pitchRanges[deck] / 100;
				const value = range === 0 ? 0.5 : (deckStates[deck].pitch - (1 - range)) / (2 * range);
				noteTakeoverSoftwareValue(`deck:${deck}:pitch`, Math.min(1, Math.max(0, value)));
			}
			noteTakeoverSoftwareValue('mixer:global:crossfader', mixerState.crossfader);
			noteTakeoverSoftwareValue('mixer:global:master', mixerState.master);
			noteTakeoverSoftwareValue('headphones:mix', mixerState.headphones.mix);
			noteTakeoverSoftwareValue('headphones:level', mixerState.headphones.level);
		});
	});
}
