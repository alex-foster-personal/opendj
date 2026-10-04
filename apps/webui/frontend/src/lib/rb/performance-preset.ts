/**
 * Deterministic four-deck preset orchestration for /performance/* routes.
 *
 * A preset is applied in strict phases. Every decoded/analyzed track must be
 * ready while transport is stopped before any deck configuration or playback
 * command is emitted. Commands use the same typed dispatcher as the UI and
 * browser IPC, so a route preload cannot create a private control path.
 *
 * The output stays hard-muted while individual transports are staged. One
 * same-ratio master tempo command then makes the engine reschedule the master
 * and every synced follower at a shared horizon. Requested volume is restored
 * only after that common revision reaches the audio output.
 */

import type {
	PerformanceCommand,
	PerformanceState
} from '$lib/rb/performance-ipc.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { SyncMode } from '$lib/rb/deck-state-types';
import type { CrossfaderAssign } from '$lib/rb/mixer-types';
import type { PerformancePresetPhase } from '$lib/rb/performance-preset-constants';

export const PRESET_DECK_IDS = [1, 2, 3, 4] as const;
export const LOOP_BOUNDARY_TOLERANCE_MS = 0.01;
export const DEFAULT_AUTOPLAY_PROBE_TIMEOUT_MS = 750;
export const DEFAULT_PRESENTATION_TIMEOUT_MS = 30_000;
export const DEFAULT_STOP_TIMEOUT_MS = 30_000;
export type PerformanceAudioActivationPolicy = 'auto' | 'require-gesture';

export interface PerformanceMixerChannelPreset {
	trim: number;
	high: number;
	mid: number;
	low: number;
	filter: number;
	fader: number;
	assign: CrossfaderAssign;
}

export interface PerformanceDeckPreset {
	deck: DeckId;
	stable_id: string;
	position_ms: number;
	loop: {
		in_ms: number;
		out_ms: number;
		beats: number;
	};
	pitch_range: 8 | 16 | 100;
	tempo_ratio: number;
	quantize_enabled: boolean;
	beat_sync_enabled: boolean;
	master_tempo_enabled: boolean;
	sync_mode: SyncMode;
}

export interface PerformancePreset {
	version: 1;
	id: string;
	master_deck: DeckId;
	mixer: {
		crossfader: number;
		master: number;
		channels: Record<DeckId, PerformanceMixerChannelPreset>;
	};
	decks: readonly PerformanceDeckPreset[];
}

export interface PerformancePresetDriver {
	dispatch(command: PerformanceCommand): Promise<PerformanceState>;
	query(): PerformanceState;
	setPhase(phase: PerformancePresetPhase): void;
}

export interface PerformancePresetPlan {
	load: readonly PerformanceCommand[];
	configure: readonly PerformanceCommand[];
	start: readonly PerformanceCommand[];
	synchronize: PerformanceCommand;
	unmute: PerformanceCommand;
}

export class PerformancePresetPendingError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'PerformancePresetPendingError';
	}
}

// ------------------------------------------------------------- validation

function _isDeckId(value: unknown): value is DeckId {
	return value === 1 || value === 2 || value === 3 || value === 4;
}

function _assertFiniteNonNegative(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0) {
		throw new RangeError(`${name} must be a finite non-negative number`);
	}
}

function _assertUnit(name: string, value: number): void {
	if (!Number.isFinite(value) || value < 0 || value > 1) {
		throw new RangeError(`${name} must be within 0..1`);
	}
}

function _validateDeckPreset(deck: PerformanceDeckPreset): void {
	if (!_isDeckId(deck.deck)) throw new RangeError(`preset deck must be one of 1, 2, 3, 4`);
	if (typeof deck.stable_id !== 'string' || deck.stable_id.trim() === '') {
		throw new TypeError(`deck ${deck.deck} stable_id must be a non-empty string`);
	}
	_assertFiniteNonNegative(`deck ${deck.deck} position_ms`, deck.position_ms);
	_assertFiniteNonNegative(`deck ${deck.deck} loop.in_ms`, deck.loop.in_ms);
	_assertFiniteNonNegative(`deck ${deck.deck} loop.out_ms`, deck.loop.out_ms);
	if (deck.loop.out_ms <= deck.loop.in_ms) {
		throw new RangeError(`deck ${deck.deck} loop.out_ms must be greater than loop.in_ms`);
	}
	if (!Number.isInteger(deck.loop.beats) || deck.loop.beats <= 0) {
		throw new RangeError(`deck ${deck.deck} loop.beats must be a positive integer`);
	}
	if (deck.pitch_range !== 8 && deck.pitch_range !== 16 && deck.pitch_range !== 100) {
		throw new RangeError(`deck ${deck.deck} pitch_range must be 8, 16, or 100`);
	}
	if (!Number.isFinite(deck.tempo_ratio) || deck.tempo_ratio <= 0) {
		throw new RangeError(`deck ${deck.deck} tempo_ratio must be a finite positive number`);
	}
	if (Math.abs(deck.tempo_ratio - 1) * 100 > deck.pitch_range + 1e-9) {
		throw new RangeError(
			`deck ${deck.deck} tempo_ratio ${deck.tempo_ratio} exceeds +-${deck.pitch_range}% pitch range`
		);
	}
	if (typeof deck.quantize_enabled !== 'boolean') {
		throw new TypeError(`deck ${deck.deck} quantize_enabled must be boolean`);
	}
	if (typeof deck.beat_sync_enabled !== 'boolean') {
		throw new TypeError(`deck ${deck.deck} beat_sync_enabled must be boolean`);
	}
	if (typeof deck.master_tempo_enabled !== 'boolean') {
		throw new TypeError(`deck ${deck.deck} master_tempo_enabled must be boolean`);
	}
	if (deck.sync_mode !== 'beat' && deck.sync_mode !== 'bar') {
		throw new TypeError(`deck ${deck.deck} sync_mode must be "beat" or "bar"`);
	}
}

function _validateMixerChannel(deck: DeckId, channel: PerformanceMixerChannelPreset): void {
	_assertUnit(`mixer deck ${deck} trim`, channel.trim);
	_assertUnit(`mixer deck ${deck} high`, channel.high);
	_assertUnit(`mixer deck ${deck} mid`, channel.mid);
	_assertUnit(`mixer deck ${deck} low`, channel.low);
	_assertUnit(`mixer deck ${deck} filter`, channel.filter);
	_assertUnit(`mixer deck ${deck} fader`, channel.fader);
	if (channel.assign !== 'A' && channel.assign !== 'B' && channel.assign !== 'THRU') {
		throw new TypeError(`mixer deck ${deck} assign must be A, B, or THRU`);
	}
}

export function validatePerformancePreset(preset: PerformancePreset): void {
	if (preset.version !== 1) throw new RangeError(`performance preset version must be 1`);
	if (typeof preset.id !== 'string' || preset.id.trim() === '') {
		throw new TypeError(`performance preset id must be a non-empty string`);
	}
	if (!_isDeckId(preset.master_deck)) {
		throw new RangeError(`performance preset master_deck must be one of 1, 2, 3, 4`);
	}
	_assertUnit(`performance preset mixer.crossfader`, preset.mixer.crossfader);
	_assertUnit(`performance preset mixer.master`, preset.mixer.master);
	for (const deck of PRESET_DECK_IDS) _validateMixerChannel(deck, preset.mixer.channels[deck]);
	if (!Array.isArray(preset.decks)) {
		throw new TypeError(`performance preset decks must be an array`);
	}
	const deckIds = preset.decks.map((deck) => deck.deck).sort();
	if (
		deckIds.length !== PRESET_DECK_IDS.length ||
		!PRESET_DECK_IDS.every((deck, index) => deckIds[index] === deck)
	) {
		throw new Error(`performance preset decks must contain exactly one definition for 1, 2, 3, 4`);
	}
	for (const deck of preset.decks) _validateDeckPreset(deck);
}

export function isPerformanceRoutePath(pathname: string): boolean {
	return pathname === '/performance' || pathname.startsWith('/performance/');
}

export function isTrackifyRoutePath(pathname: string): boolean {
	return pathname === '/music-player' || pathname.startsWith('/music-player/');
}

export function parsePerformanceAudioActivationPolicy(
	value: string | undefined
): PerformanceAudioActivationPolicy {
	if (value === undefined || value === 'auto') return 'auto';
	if (value === 'require-gesture') return 'require-gesture';
	throw new Error(
		`performance audio activation policy must be "auto" or "require-gesture"; got ${value}`
	);
}

// --------------------------------------------------------------- readiness

export function orderedPresetDecks(preset: PerformancePreset): PerformanceDeckPreset[] {
	return PRESET_DECK_IDS.map((deckId) => {
		const definition = preset.decks.find((deck) => deck.deck === deckId);
		if (definition === undefined) {
			throw new Error(`validated preset lost deck ${deckId}`);
		}
		return definition;
	});
}

export function nearestPresetBeatIndex(beatgridMs: readonly number[], targetMs: number): number {
	let nearestIndex = 0;
	let nearestDistance = Number.POSITIVE_INFINITY;
	for (const [index, beatMs] of beatgridMs.entries()) {
		if (!Number.isFinite(beatMs) || beatMs < 0) {
			throw new Error(`PQTZ beat ${index} must be finite and non-negative`);
		}
		if (index > 0 && beatMs <= beatgridMs[index - 1]) {
			throw new Error(`PQTZ beat grid must be strictly increasing at index ${index}`);
		}
		const distance = Math.abs(beatMs - targetMs);
		if (distance < nearestDistance) {
			nearestIndex = index;
			nearestDistance = distance;
		}
	}
	return nearestIndex;
}

// The runner half (track readiness, plan, start/stop and the autoplay probe)
// lives in performance-preset-runner.ts, imported only by /performance/preload1,
// so the library page that needs isPerformanceRoutePath does not download it.
