/**
 * Row model and pin state for the top-bar performance readout card.
 *
 * The compact readouts (Hz, prefetch count, waveform stutter, memory) share
 * one container and one hover card instead of one native tooltip each. The
 * card lists every readout as name, live value, a one-line plain-English
 * explainer of what it measures and what good looks like, and the detailed
 * live hover string the readout used to carry on its own `title`.
 *
 * Pure: no runes, no DOM. PerfMeters.svelte feeds it live values, and an
 * agent or test can call it with the same inputs to read the same rows.
 */

/** Live inputs, already formatted the way the compact readouts print them. */
export interface PerfReadoutInputs {
	hzText: string;
	hzDetail: string;
	cacheText: string;
	cacheDetail: string;
	waveformText: string;
	waveformDetail: string;
	memoryText: string;
	memoryDetail: string;
}

export type PerfReadoutKey = 'hz' | 'cache' | 'waveform' | 'memory';

export interface PerfReadoutRow {
	key: PerfReadoutKey;
	name: string;
	value: string;
	explainer: string;
	detail: string;
}

/** Static one-liners. Order matches the compact readouts left to right. */
export const PERF_READOUT_EXPLAINERS: Readonly<Record<PerfReadoutKey, { name: string; explainer: string }>> =
	Object.freeze({
		hz: {
			name: 'Audio publish rate (Hz)',
			explainer:
				'How many times a second the audio clock publishes to the screen while a deck is audible, like game FPS; good is 45 or more (orange under 45, red under 30), and -- means nothing is playing.'
		},
		cache: {
			name: 'Prefetched tracks',
			explainer:
				'Tracks already decoded into memory so they load instantly; any number up to the cap is fine, 0 just means nothing is prefetched yet.'
		},
		waveform: {
			name: 'Waveform stutters',
			explainer:
				'Late waveform paint frames in the last measured window (visual only, not audio glitches); good is W0, and W-- means no waveform has drawn yet so nothing is measured.'
		},
		memory: {
			name: 'Memory (MB)',
			explainer:
				'Approximate memory held by the app (decoded audio plus JS heap where the browser reports it); good stays out of the orange and red bands.'
		}
	});

function _row(key: PerfReadoutKey, value: string, detail: string): PerfReadoutRow {
	const { name, explainer } = PERF_READOUT_EXPLAINERS[key];
	return { key, name, value, explainer, detail };
}

/** One row per compact readout, in display order. */
export function perfReadoutRows(input: PerfReadoutInputs): PerfReadoutRow[] {
	return [
		_row('hz', input.hzText, input.hzDetail),
		_row('cache', input.cacheText, input.cacheDetail),
		_row('waveform', input.waveformText, input.waveformDetail),
		_row('memory', input.memoryText, input.memoryDetail)
	];
}

// -----------------------------------------------------------------------------
// Card open / pin state

export interface PerfCardState {
	hovered: boolean;
	pinned: boolean;
}

export type PerfCardEvent = 'enter' | 'leave' | 'toggle' | 'escape' | 'outside';

export const PERF_CARD_CLOSED: PerfCardState = Object.freeze({ hovered: false, pinned: false });

/**
 * Hover opens, click (or Enter/Space) toggles the pin, Escape and an outside
 * click close. Unpinning by click also drops hover so the card actually
 * closes under a still pointer, rather than looking like the click did nothing.
 */
export function nextPerfCardState(state: PerfCardState, event: PerfCardEvent): PerfCardState {
	switch (event) {
		case 'enter':
			return { ...state, hovered: true };
		case 'leave':
			return { ...state, hovered: false };
		case 'toggle':
			return state.pinned ? { hovered: false, pinned: false } : { hovered: state.hovered, pinned: true };
		case 'escape':
			return { hovered: false, pinned: false };
		case 'outside':
			return state.pinned ? { ...state, pinned: false } : state;
		default: {
			const _exhaustive: never = event;
			throw new Error(`Unhandled perf card event: ${String(_exhaustive)}`);
		}
	}
}

export function isPerfCardOpen(state: PerfCardState): boolean {
	return state.hovered || state.pinned;
}
