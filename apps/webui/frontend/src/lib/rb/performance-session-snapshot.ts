/**
 * Versioned localStorage snapshot for /performance lv2 timestamps and lv3
 * mixer/deck config. Pure serialize/parse - no DOM or audio imports.
 */

import type { DeckId } from '$lib/rb/deck-id';
import type { CrossfaderAssign } from '$lib/rb/mixer-types';
import type { PitchRange } from '$lib/player/constants';
import { STEM_CONTROL_IDS } from '$lib/rb/stem-types';

export const PERFORMANCE_SESSION_STORAGE_KEY = 'mdt.rb.performance-session.v1';

const SNAPSHOT_VERSION = 1;
const DECK_IDS: DeckId[] = [1, 2, 3, 4];
const STEM_CONTROLS = STEM_CONTROL_IDS;
const PITCH_RANGES = new Set<PitchRange>([8, 16, 100]);
const ASSIGNS = new Set<CrossfaderAssign>(['A', 'B', 'THRU']);

export interface PerformanceSessionDeckSnapshot {
	stable_id: string | null;
	position_ms: number;
	pitch: number;
	pitch_range: PitchRange;
	quantize_enabled: boolean;
	beat_sync_enabled: boolean;
	master_tempo_enabled: boolean;
	key_sync_enabled: boolean;
}

export interface PerformanceSessionMixerChannelSnapshot {
	trim: number;
	eq_high: number;
	eq_mid: number;
	eq_low: number;
	filter: number;
	fader: number;
	assign: CrossfaderAssign;
	stem_eq_mode?: boolean;
}

export interface PerformanceSessionStemControlSnapshot {
	muted: boolean;
	solo: boolean;
	gain?: number;
}

/** V1 snapshots predate independent bass/harmonics. Absent child controls
 * preserve the loader's neutral defaults; malformed present controls fail. */
type SessionStemControls = Record<'vocal' | 'instrumental' | 'drums', PerformanceSessionStemControlSnapshot>
	& Partial<Record<'bass' | 'other', PerformanceSessionStemControlSnapshot>>;

export interface PerformanceSessionSnapshot {
	version: 1;
	captured_at_ms: number;
	playlist_id: string | null;
	decks: Record<DeckId, PerformanceSessionDeckSnapshot>;
	mixer: {
		crossfader: number;
		master: number;
		/** True only when `master` came from an operator's own master_volume
		 * command. A 0 without it is a safety mute (route teardown, a failed
		 * preset) and restore must not replay it. Absent in older snapshots. */
		master_set_by_operator?: boolean;
		channels: Record<DeckId, PerformanceSessionMixerChannelSnapshot>;
	};
	stems: Record<DeckId, SessionStemControls>;
}

export interface PerformanceSessionSnapshotInput {
	captured_at_ms: number;
	playlist_id: string | null;
	decks: Record<
		DeckId,
		{
			stable_id: string | null;
			position_ms: number;
			pitch: number;
			pitch_range: PitchRange;
			quantize_enabled: boolean;
			beat_sync_enabled: boolean;
			master_tempo_enabled: boolean;
			key_sync_enabled: boolean;
		}
	>;
	mixer: {
		crossfader: number;
		master: number;
		/** True only when `master` came from an operator's own master_volume
		 * command. A 0 without it is a safety mute (route teardown, a failed
		 * preset) and restore must not replay it. Absent in older snapshots. */
		master_set_by_operator?: boolean;
		channels: Record<DeckId, PerformanceSessionMixerChannelSnapshot>;
	};
	stems: Record<DeckId, SessionStemControls>;
}

function _assertUnit(value: number, label: string): void {
	if (!Number.isFinite(value) || value < 0 || value > 1) {
		throw new Error(`performance session snapshot: ${label} must be a finite unit value 0..1`);
	}
}

function _parseDeckSnapshot(raw: unknown): PerformanceSessionDeckSnapshot | null {
	if (raw === null || typeof raw !== 'object') return null;
	const deck = raw as Record<string, unknown>;
	const stable_id = typeof deck.stable_id === 'string' ? deck.stable_id : null;
	if (deck.stable_id !== null && typeof deck.stable_id !== 'string') return null;
	const position_ms = deck.position_ms;
	if (typeof position_ms !== 'number' || !Number.isFinite(position_ms) || position_ms < 0) {
		return null;
	}
	const pitch = deck.pitch;
	if (typeof pitch !== 'number' || !Number.isFinite(pitch)) return null;
	const pitch_range = deck.pitch_range;
	if (typeof pitch_range !== 'number' || !PITCH_RANGES.has(pitch_range as PitchRange)) return null;
	for (const key of [
		'quantize_enabled',
		'beat_sync_enabled',
		'master_tempo_enabled',
		'key_sync_enabled'
	] as const) {
		if (typeof deck[key] !== 'boolean') return null;
	}
	return {
		stable_id,
		position_ms: Math.round(position_ms),
		pitch,
		pitch_range: pitch_range as PitchRange,
		quantize_enabled: deck.quantize_enabled as boolean,
		beat_sync_enabled: deck.beat_sync_enabled as boolean,
		master_tempo_enabled: deck.master_tempo_enabled as boolean,
		key_sync_enabled: deck.key_sync_enabled as boolean
	};
}

function _parseChannelSnapshot(raw: unknown): PerformanceSessionMixerChannelSnapshot | null {
	if (raw === null || typeof raw !== 'object') return null;
	const channel = raw as Record<string, unknown>;
	for (const key of ['trim', 'eq_high', 'eq_mid', 'eq_low', 'filter', 'fader'] as const) {
		const value = channel[key];
		if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
			return null;
		}
	}
	const assign = channel.assign;
	if (typeof assign !== 'string' || !ASSIGNS.has(assign as CrossfaderAssign)) return null;
	let stem_eq_mode = false;
	if (channel.stem_eq_mode !== undefined) {
		if (typeof channel.stem_eq_mode !== 'boolean') return null;
		stem_eq_mode = channel.stem_eq_mode;
	}
	return {
		trim: channel.trim as number,
		eq_high: channel.eq_high as number,
		eq_mid: channel.eq_mid as number,
		eq_low: channel.eq_low as number,
		filter: channel.filter as number,
		fader: channel.fader as number,
		assign: assign as CrossfaderAssign,
		stem_eq_mode
	};
}

function _parseStemControl(raw: unknown): PerformanceSessionStemControlSnapshot | null {
	if (raw === null || typeof raw !== 'object') return null;
	const control = raw as Record<string, unknown>;
	if (typeof control.muted !== 'boolean' || typeof control.solo !== 'boolean') return null;
	let gain = 0.5;
	if (control.gain !== undefined) {
		if (typeof control.gain !== 'number' || !Number.isFinite(control.gain) || control.gain < 0 || control.gain > 1) {
			return null;
		}
		gain = control.gain;
	}
	return { muted: control.muted, solo: control.solo, gain };
}

export function serializePerformanceSession(input: PerformanceSessionSnapshotInput): string {
	for (const deckId of DECK_IDS) {
		const deck = input.decks[deckId];
		if (!Number.isFinite(deck.position_ms) || deck.position_ms < 0) {
			throw new Error(`performance session snapshot: deck ${deckId} position_ms must be >= 0`);
		}
		if (!Number.isFinite(deck.pitch)) {
			throw new Error(`performance session snapshot: deck ${deckId} pitch must be finite`);
		}
		if (!PITCH_RANGES.has(deck.pitch_range)) {
			throw new Error(`performance session snapshot: deck ${deckId} pitch_range invalid`);
		}
	}
	_assertUnit(input.mixer.crossfader, 'mixer.crossfader');
	_assertUnit(input.mixer.master, 'mixer.master');
	for (const deckId of DECK_IDS) {
		const channel = input.mixer.channels[deckId];
		_assertUnit(channel.trim, `mixer.channels.${deckId}.trim`);
		_assertUnit(channel.eq_high, `mixer.channels.${deckId}.eq_high`);
		_assertUnit(channel.eq_mid, `mixer.channels.${deckId}.eq_mid`);
		_assertUnit(channel.eq_low, `mixer.channels.${deckId}.eq_low`);
		_assertUnit(channel.filter, `mixer.channels.${deckId}.filter`);
		_assertUnit(channel.fader, `mixer.channels.${deckId}.fader`);
		if (!ASSIGNS.has(channel.assign)) {
			throw new Error(`performance session snapshot: mixer.channels.${deckId}.assign invalid`);
		}
	}

	const payload: PerformanceSessionSnapshot = {
		version: SNAPSHOT_VERSION,
		captured_at_ms: input.captured_at_ms,
		playlist_id: input.playlist_id,
		decks: {} as Record<DeckId, PerformanceSessionDeckSnapshot>,
		mixer: {
			crossfader: input.mixer.crossfader,
			master: input.mixer.master,
			...(input.mixer.master_set_by_operator === undefined
				? {}
				: { master_set_by_operator: input.mixer.master_set_by_operator }),
			channels: {} as Record<DeckId, PerformanceSessionMixerChannelSnapshot>
		},
		stems: {} as Record<DeckId, SessionStemControls>
	};

	for (const deckId of DECK_IDS) {
		const deck = input.decks[deckId];
		payload.decks[deckId] = {
			stable_id: deck.stable_id,
			position_ms: Math.round(deck.position_ms),
			pitch: deck.pitch,
			pitch_range: deck.pitch_range,
			quantize_enabled: deck.quantize_enabled,
			beat_sync_enabled: deck.beat_sync_enabled,
			master_tempo_enabled: deck.master_tempo_enabled,
			key_sync_enabled: deck.key_sync_enabled
		};
		payload.mixer.channels[deckId] = { ...input.mixer.channels[deckId] };
		payload.stems[deckId] = {
			vocal: { ...input.stems[deckId].vocal },
			instrumental: { ...input.stems[deckId].instrumental },
			drums: { ...input.stems[deckId].drums },
			...(input.stems[deckId].bass === undefined ? {} : { bass: { ...input.stems[deckId].bass } }),
			...(input.stems[deckId].other === undefined ? {} : { other: { ...input.stems[deckId].other } })
		};
	}

	return JSON.stringify(payload);
}

export function parsePerformanceSession(raw: string | null | undefined): PerformanceSessionSnapshot | null {
	if (raw === null || raw === undefined || raw.length === 0) return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return null;
	}
	if (parsed === null || typeof parsed !== 'object') return null;
	const blob = parsed as Record<string, unknown>;
	if (blob.version !== SNAPSHOT_VERSION) return null;
	if (typeof blob.captured_at_ms !== 'number' || !Number.isFinite(blob.captured_at_ms)) return null;
	if (blob.playlist_id !== null && typeof blob.playlist_id !== 'string') return null;
	if (blob.decks === null || typeof blob.decks !== 'object') return null;
	if (blob.mixer === null || typeof blob.mixer !== 'object') return null;
	if (blob.stems === null || typeof blob.stems !== 'object') return null;

	const decks: Record<DeckId, PerformanceSessionDeckSnapshot> = {} as Record<
		DeckId,
		PerformanceSessionDeckSnapshot
	>;
	for (const deckId of DECK_IDS) {
		const deckRaw = (blob.decks as Record<string, unknown>)[String(deckId)];
		const deck = _parseDeckSnapshot(deckRaw);
		if (deck === null) return null;
		decks[deckId] = deck;
	}

	const mixerRaw = blob.mixer as Record<string, unknown>;
	if (
		typeof mixerRaw.crossfader !== 'number' ||
		typeof mixerRaw.master !== 'number' ||
		mixerRaw.channels === null ||
		typeof mixerRaw.channels !== 'object'
	) {
		return null;
	}
	if (
		!Number.isFinite(mixerRaw.crossfader) ||
		mixerRaw.crossfader < 0 ||
		mixerRaw.crossfader > 1 ||
		!Number.isFinite(mixerRaw.master) ||
		mixerRaw.master < 0 ||
		mixerRaw.master > 1
	) {
		return null;
	}
	const masterSetByOperator = mixerRaw.master_set_by_operator;
	if (masterSetByOperator !== undefined && typeof masterSetByOperator !== 'boolean') return null;

	const channels: Record<DeckId, PerformanceSessionMixerChannelSnapshot> = {} as Record<
		DeckId,
		PerformanceSessionMixerChannelSnapshot
	>;
	for (const deckId of DECK_IDS) {
		const channel = _parseChannelSnapshot(
			(mixerRaw.channels as Record<string, unknown>)[String(deckId)]
		);
		if (channel === null) return null;
		channels[deckId] = channel;
	}

	const stems = {} as Record<DeckId, SessionStemControls>;
	for (const deckId of DECK_IDS) {
		const deckStems = (blob.stems as Record<string, unknown>)[String(deckId)];
		if (deckStems === null || typeof deckStems !== 'object') return null;
		const stemRecord = deckStems as Record<string, unknown>;
		const deckStemSnapshot = {} as SessionStemControls;
		for (const stem of STEM_CONTROLS) {
			if ((stem === 'bass' || stem === 'other') && stemRecord[stem] === undefined) continue;
			const control = _parseStemControl(stemRecord[stem]);
			if (control === null) return null;
			deckStemSnapshot[stem] = control;
		}
		stems[deckId] = deckStemSnapshot;
	}

	return {
		version: SNAPSHOT_VERSION,
		captured_at_ms: blob.captured_at_ms,
		playlist_id: blob.playlist_id as string | null,
		decks,
		mixer: {
			crossfader: mixerRaw.crossfader,
			master: mixerRaw.master,
			...(masterSetByOperator === undefined ? {} : { master_set_by_operator: masterSetByOperator }),
			channels
		},
		stems
	};
}
