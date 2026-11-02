/**
 * DECKGAP-7691 (pin 251822d6104b): which mixer values return to neutral when a track loads
 * onto a deck.
 *
 * The choice is per item and shared by every deck (one policy, not four).
 * Stems start on, because a new track already publishes unity stem controls.
 * Level, trim, EQ, and filter start off, so a mix you set stays until you
 * ask for the reset.
 *
 * A reset writes Open DJ only. The controller knob is not moved. Pickup
 * already keeps Open DJ's value until that knob crosses it.
 */

import type { EqBand } from './mixer-types';
import type { StemControls } from './stem-graph';

export const TRACK_LOAD_RESET_IDS = ['level', 'stems', 'eq', 'trim', 'filter'] as const;

export type TrackLoadResetId = (typeof TRACK_LOAD_RESET_IDS)[number];

export interface TrackLoadResetItem {
	id: TrackLoadResetId;
	label: string;
	detail: string;
}

export const TRACK_LOAD_RESET_ITEMS: readonly TrackLoadResetItem[] = [
	{
		id: 'level',
		label: 'Level',
		detail: 'Channel fader back to full.'
	},
	{
		id: 'stems',
		label: 'Stems',
		detail:
			'Mute, solo, and stem level back to open and unity. On by default, because a new track already starts there. Off keeps the previous track\'s stem mix.'
	},
	{
		id: 'eq',
		label: 'EQ',
		detail: 'HI, MID, and LOW back to flat.'
	},
	{
		id: 'trim',
		label: 'Trim',
		detail: 'Trim back to center (unity).'
	},
	{
		id: 'filter',
		label: 'Filter',
		detail: 'Filter back to center (bypass).'
	}
];

export const TRACK_LOAD_RESET_DEFAULTS: Record<TrackLoadResetId, boolean> = {
	level: false,
	stems: true,
	eq: false,
	trim: false,
	filter: false
};

/** Neutral mixer positions. Same numbers as a fresh channel strip. */
export const TRACK_LOAD_RESET_NEUTRAL = {
	level: 1,
	trim: 0.5,
	eq: 0.5,
	filter: 0.5
} as const;

export const TRACK_LOAD_RESET_WARNING =
	'WARNING: this resets the value in Open DJ, not on your controller. The knob or fader on your deck stays where it is. When they disagree, Open DJ keeps its value until you move that control onto it.';

const STORAGE_KEY = 'mdt.rb.track-load-reset';
const VALUE_EPSILON = 1e-6;

export type TrackLoadResetPrefs = Record<TrackLoadResetId, boolean>;

export interface TrackLoadChannelNow {
	fader: number;
	trim: number;
	eqHigh: number;
	eqMid: number;
	eqLow: number;
	filter: number;
}

export type ChannelResetAction =
	| { kind: 'fader'; value: number }
	| { kind: 'trim'; value: number }
	| { kind: 'filter'; value: number }
	| { kind: 'eq'; band: EqBand; value: number };

export function parseTrackLoadReset(raw: unknown): TrackLoadResetPrefs {
	const out: TrackLoadResetPrefs = { ...TRACK_LOAD_RESET_DEFAULTS };
	if (typeof raw !== 'object' || raw === null) return out;
	const record = raw as Record<string, unknown>;
	for (const id of TRACK_LOAD_RESET_IDS) {
		if (typeof record[id] === 'boolean') out[id] = record[id];
	}
	return out;
}

function loadStoredPrefs(): TrackLoadResetPrefs {
	try {
		if (typeof localStorage === 'undefined') return { ...TRACK_LOAD_RESET_DEFAULTS };
		const raw = localStorage.getItem(STORAGE_KEY);
		if (raw === null || raw === '') return { ...TRACK_LOAD_RESET_DEFAULTS };
		return parseTrackLoadReset(JSON.parse(raw));
	} catch {
		return { ...TRACK_LOAD_RESET_DEFAULTS };
	}
}

let prefs: TrackLoadResetPrefs | null = null;
const listeners = new Set<() => void>();

export function readTrackLoadReset(): TrackLoadResetPrefs {
	if (prefs === null) prefs = loadStoredPrefs();
	return prefs;
}

export function writeTrackLoadReset(next: TrackLoadResetPrefs): void {
	prefs = { ...next };
	try {
		if (typeof localStorage !== 'undefined') {
			localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
		}
	} catch {
		/* private mode: the choice still applies until reload */
	}
	for (const fn of listeners) fn();
}

export function subscribeTrackLoadReset(fn: () => void): () => void {
	listeners.add(fn);
	return () => listeners.delete(fn);
}

function differs(current: number, neutral: number): boolean {
	return Math.abs(current - neutral) > VALUE_EPSILON;
}

/** Mixer writes for one published load. Already-neutral values are skipped. */
export function channelResetActions(
	enabled: TrackLoadResetPrefs,
	current: TrackLoadChannelNow
): ChannelResetAction[] {
	const actions: ChannelResetAction[] = [];
	if (enabled.level && differs(current.fader, TRACK_LOAD_RESET_NEUTRAL.level)) {
		actions.push({ kind: 'fader', value: TRACK_LOAD_RESET_NEUTRAL.level });
	}
	if (enabled.trim && differs(current.trim, TRACK_LOAD_RESET_NEUTRAL.trim)) {
		actions.push({ kind: 'trim', value: TRACK_LOAD_RESET_NEUTRAL.trim });
	}
	if (enabled.eq) {
		const flat = TRACK_LOAD_RESET_NEUTRAL.eq;
		if (differs(current.eqHigh, flat)) actions.push({ kind: 'eq', band: 'high', value: flat });
		if (differs(current.eqMid, flat)) actions.push({ kind: 'eq', band: 'mid', value: flat });
		if (differs(current.eqLow, flat)) actions.push({ kind: 'eq', band: 'low', value: flat });
	}
	if (enabled.filter && differs(current.filter, TRACK_LOAD_RESET_NEUTRAL.filter)) {
		actions.push({ kind: 'filter', value: TRACK_LOAD_RESET_NEUTRAL.filter });
	}
	return actions;
}

export function cloneStemControls(controls: StemControls): StemControls {
	return {
		vocal: { ...controls.vocal },
		instrumental: { ...controls.instrumental },
		drums: { ...controls.drums },
		bass: { ...controls.bass },
		other: { ...controls.other }
	};
}

/**
 * Controls to keep onto the new track. Null means "use the engine default"
 * (the reset-stems box is on).
 */
export function stemCarryControls(
	enabled: TrackLoadResetPrefs,
	controls: StemControls
): StemControls | null {
	if (enabled.stems) return null;
	return cloneStemControls(controls);
}

interface StemCarryRow {
	token: number;
	controls: StemControls | null;
}

const stemCarry = new Map<number, StemCarryRow>();

/** Snapshot the outgoing track just before this load replaces it. */
export function captureStemCarry(deck: number, token: number, controls: StemControls): void {
	stemCarry.set(deck, {
		token,
		controls: stemCarryControls(readTrackLoadReset(), controls)
	});
}

/** The snapshot for this load, or null when stems should publish at unity. */
export function peekStemCarry(deck: number, token: number): StemControls | null {
	const row = stemCarry.get(deck);
	if (row === undefined || row.token !== token || row.controls === null) return null;
	return cloneStemControls(row.controls);
}

/** The mixer channel fields a reset reads, as `mixerState.channels[deck]` holds them. */
export interface TrackLoadChannelState {
	fader: number;
	trim: number;
	eq_high: number;
	eq_mid: number;
	eq_low: number;
	filter: number;
}

/** The engine's own mixer setters, so a reset is an ordinary mixer write. */
export interface TrackLoadResetWriters {
	setFader(value: number): void;
	setTrim(value: number): void;
	setFilter(value: number): void;
	setEq(band: EqBand, value: number): void;
}

/** Apply the saved level / EQ / trim / filter reset to one channel after a load that won. */
export function applyTrackLoadChannelReset(channel: TrackLoadChannelState, write: TrackLoadResetWriters): void {
	const now: TrackLoadChannelNow = {
		fader: channel.fader,
		trim: channel.trim,
		eqHigh: channel.eq_high,
		eqMid: channel.eq_mid,
		eqLow: channel.eq_low,
		filter: channel.filter
	};
	for (const action of channelResetActions(readTrackLoadReset(), now)) {
		if (action.kind === 'fader') write.setFader(action.value);
		else if (action.kind === 'trim') write.setTrim(action.value);
		else if (action.kind === 'filter') write.setFilter(action.value);
		else write.setEq(action.band, action.value);
	}
}

/** On stem upgrade: when "reset stems" is off, the new bundle takes the carried mix. The carry is
 * spent on first use, so a later graph rebuild or stem reload on the same load keeps the live mix. */
export function applyStemCarry(
	deck: number,
	token: number,
	state: { controls: StemControls },
	processor: { setControls(controls: StemControls): void }
): void {
	const carried = peekStemCarry(deck, token);
	if (carried === null) return;
	stemCarry.delete(deck);
	state.controls = carried;
	processor.setControls(carried);
}
