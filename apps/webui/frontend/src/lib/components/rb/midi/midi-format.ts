/**
 * Pure formatting + status-derivation helpers for the MIDI panel UI
 * (build unit: midi panel). NO runes here so the module is unit-testable
 * without a component harness; all reactive state stays in
 * webmidi.svelte.ts / midi-ui-state.svelte.ts.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 hexByte/formatBytes: raw wire bytes -> uppercase hex for the
 *     learn-log console.
 *     [if] hexByte(0x90) isn't '90' [then] broken
 *     [if] hexByte(300) doesn't throw [then ⛔️] broken (fail-fast on
 *       non-byte input, never render garbage)
 *   ✔︎ 🎯 describeSource: decoded MidiSource -> human string; null (an
 *     undecoded status family) says so explicitly.
 *     [if] describeSource(null) is empty or '-' [then] broken (silent gap)
 *   ✔︎ 🎯 midiLabelStatus: single source of truth for the TopBar label
 *     colour. green = granted AND >=1 mapped device; amber = permission
 *     request in flight; grey = everything else.
 *     [if] granted with only UNMAPPED devices shows green [then ⛔️] broken
 */

import type { DeviceMap, MidiAction, MidiSource } from '$lib/rb/midi/midi-types';
import type { MidiPermission } from '$lib/rb/midi/webmidi.svelte';

export type MidiLabelStatus = 'green' | 'amber' | 'grey' | 'red';

// ---------------------------------------------------------------- _helpers

/** One wire byte -> two uppercase hex chars. Throws on non-byte input. */
export function hexByte(n: number): string {
	if (!Number.isInteger(n) || n < 0 || n > 255) {
		throw new RangeError(`hexByte: expected an integer 0..255, got ${n}`);
	}
	return n.toString(16).toUpperCase().padStart(2, '0');
}

/** Raw message triple -> '90 3C 7F' for the learn-log console. */
export function formatBytes(status: number, data1: number, data2: number): string {
	return `${hexByte(status)} ${hexByte(data1)} ${hexByte(data2)}`;
}

/** Decoded source -> 'ch 2 note 60' / 'ch 4 cc 31' / 'ch 1 pitchbend'.
 * null decode (status family outside P0 scope) is named explicitly -
 * the learn log must never show a silent gap. */
export function describeSource(src: MidiSource | null): string {
	if (src === null) return 'undecoded';
	if (src.kind === 'pitchbend') return `ch ${src.ch} pitchbend`;
	return `ch ${src.ch} ${src.kind} ${src.id}`;
}

/** performance.now() capture time -> '12.4s' since page load. */
export function formatLogTs(tsMs: number): string {
	if (!Number.isFinite(tsMs) || tsMs < 0) {
		throw new RangeError(`formatLogTs: expected a non-negative ms value, got ${tsMs}`);
	}
	return `${(tsMs / 1000).toFixed(1)}s`;
}

// ------------------------------------------------------- label status logic

/** TopBar MIDI label colour + glyph (unit spec):
 * grey  = unsupported / denied / idle-prompt (MIDI not enabled yet)
 * amber = a permission request is currently in flight (prompt pending)
 * green = granted AND at least one connected device matched a DeviceMap (tick)
 * red   = granted but NO mapped device bound - access was granted and the
 *         controller then disconnected (or nothing recognised is plugged in);
 *         the actionable "MIDI is on but nothing is driving the app" state (X) */
export function midiLabelStatus(
	permission: MidiPermission,
	requestPending: boolean,
	hasMappedDevice: boolean
): MidiLabelStatus {
	if (requestPending) return 'amber';
	if (permission === 'granted') return hasMappedDevice ? 'green' : 'red';
	return 'grey';
}

/** The glyph shown next to the TopBar MIDI label for a given status: a tick
 * when a controller is bound, an X when access is granted but disconnected,
 * nothing otherwise (amber pulses; grey is idle). */
export function midiLabelGlyph(status: MidiLabelStatus): string {
	if (status === 'green') return '✓'; // check mark
	if (status === 'red') return '✗'; // ballot X
	return '';
}

/** Tooltip for the TopBar MIDI label - states WHY the colour is what it is. */
export function midiLabelTitle(
	permission: MidiPermission,
	requestPending: boolean,
	mappedDeviceCount: number,
	deviceCount: number
): string {
	if (requestPending) return 'MIDI: permission request pending in the browser';
	if (permission === 'unsupported') return 'MIDI: WebMIDI not supported in this browser (use Chrome or Edge)';
	if (permission === 'denied') return 'MIDI: permission denied - re-enable in browser site settings';
	if (permission === 'prompt') return 'MIDI: click to open the panel and request access';
	if (permission === 'granted') {
		if (mappedDeviceCount === 0) {
			return 'MIDI: access granted but no mapped controller connected - reconnect your device';
		}
		return `MIDI: ${mappedDeviceCount} mapped / ${deviceCount} connected device(s) - click for panel`;
	}
	const _exhaustive: never = permission;
	throw new Error(`Unhandled MidiPermission: ${_exhaustive}`);
}

// ----------------------------------------------------- decoded trace labels

/** A mapped action -> a friendly, jargon-free label for the learn log, e.g.
 * deck_play_toggle deck 1 -> 'Play (deck 1)'. Exhaustive over MidiAction so
 * a new action type is a compile error here, not a bare type string leaking
 * into the UI. */
export function friendlyLabel(action: MidiAction): string {
	switch (action.type) {
		case 'deck_play_toggle':
			return `Play (deck ${action.deck})`;
		case 'deck_cue':
			return `Cue (deck ${action.deck})`;
		case 'deck_hot_cue':
			return `Hot cue ${action.slot} (deck ${action.deck})`;
		case 'deck_beat_loop':
			return `Beat loop ${action.beats} (deck ${action.deck})`;
		case 'deck_loop_exit':
			return `Loop exit (deck ${action.deck})`;
		case 'deck_sync_toggle':
			return `Beat sync (deck ${action.deck})`;
		case 'deck_manual_loop_cycle':
			return `Loop in / out / exit (deck ${action.deck})`;
		case 'deck_loop_scale':
			return `Loop ${action.factor === 0.5 ? 'half' : 'double'} (deck ${action.deck})`;
		case 'deck_key_sync_toggle':
			return `Key sync (deck ${action.deck})`;
		case 'deck_stem_eq_toggle':
			return `Stem EQ (deck ${action.deck})`;
		case 'deck_key_nudge':
			return `Key ${action.semitones < 0 ? 'down' : 'up'} (deck ${action.deck})`;
		case 'deck_tempo_nudge':
			return `Tempo ${action.direction < 0 ? '-0.1' : '+0.1'} BPM (deck ${action.deck})`;
		case 'controller_pad_mode':
			return `${action.mode.replaceAll('_', ' ')} mode (deck ${action.deck})`;
		case 'controller_pad':
			return `${action.shifted ? 'Shift ' : ''}pad ${action.pad} (deck ${action.deck})`;
		case 'mixer_channel': {
			if (action.target === 'trim') return `Trim (deck ${action.deck})`;
			if (action.target === 'fader') return `Channel fader (deck ${action.deck})`;
			if (action.target === 'eq') {
				return action.band === undefined
					? `EQ (deck ${action.deck})`
					: `EQ ${action.band} (deck ${action.deck})`;
			}
			if (action.target === 'filter') return `Filter (deck ${action.deck})`;
			const _exhaustiveTarget: never = action.target;
			throw new Error(`Unhandled mixer_channel target: ${_exhaustiveTarget}`);
		}
		case 'mixer_global': {
			if (action.target === 'crossfader') return 'Crossfader';
			if (action.target === 'master') return 'Master level';
			const _exhaustiveTarget: never = action.target;
			throw new Error(`Unhandled mixer_global target: ${_exhaustiveTarget}`);
		}
		case 'deck_pitch':
			return `Tempo (deck ${action.deck})`;
		case 'browse_encoder':
			return 'Browse';
		case 'browse_load':
			return `Load (deck ${action.deck})`;
		case 'shift_modifier':
			return 'Shift';
		case 'channel_cue':
			return `Cue / PFL (deck ${action.deck})`;
		case 'headphone_mix':
			return 'Headphones mix';
		case 'headphone_level':
			return 'Headphones level';
		case 'master_cue':
			return 'Master cue';
		default: {
			const _exhaustive: never = action;
			throw new Error(`Unhandled MidiAction: ${JSON.stringify(_exhaustive)}`);
		}
	}
}

/** Best-guess hint for an UNMAPPED source: if the device map documents this
 * control (as an out-of-scope hint), name it. Returns null when the map has
 * no hint for the source (so the caller falls back to the raw dispatch note).
 * Pure lookup - no fabricated names, hints come straight from the map. */
export function bestGuessHint(map: DeviceMap | null, source: MidiSource | null): string | null {
	if (map === null || source === null || map.hints === undefined) return null;
	for (const hint of map.hints) {
		if (
			hint.source.ch === source.ch &&
			hint.source.kind === source.kind &&
			hint.source.id === source.id
		) {
			return hint.label;
		}
	}
	return null;
}

/** The label a learn-log row should DISPLAY: a friendly action label for
 * mapped traffic, a documented best-guess for unmapped-but-known controls,
 * else the raw dispatch note (never empty - unmapped traffic stays a signal).
 * `action` and `hint` are resolved by the caller (the component knows the
 * device's map); this keeps the decision in one tested place. */
export function traceLabel(
	note: string,
	action: MidiAction | null | undefined,
	hint: string | null
): string {
	if (action !== null && action !== undefined) return friendlyLabel(action);
	if (hint !== null) return `likely: ${hint}`;
	return note;
}
