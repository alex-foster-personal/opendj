/**
 * Deterministic four-deck preset orchestration for /performance/* routes.
 *
 * A preset is applied in strict phases. Every decoded/analyzed track must be
 * ready while transport is stopped before any deck configuration or playback
 * command is emitted. Commands use the same typed dispatcher as the UI and
 * browser IPC, so a route preload cannot create a private control path.
 */

import type {
	PerformanceCommand,
	PerformanceState
} from '$lib/rb/performance-ipc.svelte';
import type { DeckId, SyncMode } from '$lib/rb/types';

const PRESET_DECK_IDS = [1, 2, 3, 4] as const;

export interface PerformanceDeckPreset {
	deck: DeckId;
	stable_id: string;
	position_ms: number;
	loop: {
		start_ms: number;
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
	decks: readonly PerformanceDeckPreset[];
}

export interface PerformancePresetDriver {
	dispatch(command: PerformanceCommand): Promise<PerformanceState>;
	query(): PerformanceState;
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

function _validateDeckPreset(deck: PerformanceDeckPreset): void {
	if (!_isDeckId(deck.deck)) throw new RangeError(`preset deck must be one of 1, 2, 3, 4`);
	if (typeof deck.stable_id !== 'string' || deck.stable_id.trim() === '') {
		throw new TypeError(`deck ${deck.deck} stable_id must be a non-empty string`);
	}
	_assertFiniteNonNegative(`deck ${deck.deck} position_ms`, deck.position_ms);
	_assertFiniteNonNegative(`deck ${deck.deck} loop.start_ms`, deck.loop.start_ms);
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

export function validatePerformancePreset(preset: PerformancePreset): void {
	if (preset.version !== 1) throw new RangeError(`performance preset version must be 1`);
	if (typeof preset.id !== 'string' || preset.id.trim() === '') {
		throw new TypeError(`performance preset id must be a non-empty string`);
	}
	if (!_isDeckId(preset.master_deck)) {
		throw new RangeError(`performance preset master_deck must be one of 1, 2, 3, 4`);
	}
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

// --------------------------------------------------------------- readiness

function _orderedDecks(preset: PerformancePreset): PerformanceDeckPreset[] {
	return PRESET_DECK_IDS.map((deckId) => {
		const definition = preset.decks.find((deck) => deck.deck === deckId);
		if (definition === undefined) {
			throw new Error(`validated preset lost deck ${deckId}`);
		}
		return definition;
	});
}

function _nearestBeatIndex(beatgridMs: readonly number[], targetMs: number): number {
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

export function assertPresetTracksReady(
	preset: PerformancePreset,
	state: PerformanceState
): void {
	for (const definition of _orderedDecks(preset)) {
		const deck = state.decks[definition.deck];
		const prefix = `deck ${definition.deck} is not preset-ready`;
		if (deck.stable_id !== definition.stable_id) {
			throw new Error(`${prefix}: expected ${definition.stable_id}, got ${String(deck.stable_id)}`);
		} else if (deck.duration_ms === null || !Number.isFinite(deck.duration_ms) || deck.duration_ms <= 0) {
			throw new Error(`${prefix}: decoded duration is unavailable`);
		} else if (deck.beatgrid_ms.length === 0) {
			throw new Error(`${prefix}: real PQTZ beat grid is empty`);
		} else if (deck.processor_error !== null) {
			throw new Error(`${prefix}: processor failed: ${deck.processor_error}`);
		} else if (deck.command_error !== null) {
			throw new Error(`${prefix}: command failed: ${deck.command_error}`);
		} else if (deck.playing || deck.audible || deck.transport_pending) {
			throw new Error(`${prefix}: transport must be fully stopped`);
		}
		if (definition.position_ms > deck.duration_ms) {
			throw new Error(
				`${prefix}: position_ms ${definition.position_ms} exceeds decoded duration ${deck.duration_ms}`
			);
		}
		if (definition.loop.start_ms > deck.duration_ms) {
			throw new Error(
				`${prefix}: loop.start_ms ${definition.loop.start_ms} exceeds decoded duration ${deck.duration_ms}`
			);
		}
		let startIndex: number;
		try {
			startIndex = _nearestBeatIndex(deck.beatgrid_ms, definition.loop.start_ms);
		} catch (error) {
			throw new Error(`${prefix}: ${error instanceof Error ? error.message : String(error)}`, {
				cause: error
			});
		}
		const remainingBeats = deck.beatgrid_ms.length - startIndex - 1;
		if (definition.loop.beats > remainingBeats) {
			throw new Error(
				`${prefix}: loop requires ${definition.loop.beats} beats from PQTZ index ` +
					`${startIndex}, but only ${remainingBeats} remain`
			);
		}
		const loopOutMs = deck.beatgrid_ms[startIndex + definition.loop.beats];
		if (loopOutMs > deck.duration_ms) {
			throw new Error(
				`${prefix}: loop out ${loopOutMs} exceeds decoded duration ${deck.duration_ms}`
			);
		}
	}
}

// ------------------------------------------------------------- application

async function _configureDeck(
	definition: PerformanceDeckPreset,
	driver: PerformancePresetDriver
): Promise<void> {
	const deck = definition.deck;
	await driver.dispatch({ type: 'pitch_range', deck, range: definition.pitch_range });
	await driver.dispatch({ type: 'quantize', deck, enabled: definition.quantize_enabled });
	await driver.dispatch({ type: 'sync_mode', deck, mode: definition.sync_mode });
	await driver.dispatch({ type: 'beat_sync', deck, enabled: definition.beat_sync_enabled });
	await driver.dispatch({ type: 'master_tempo', deck, enabled: definition.master_tempo_enabled });
	await driver.dispatch({ type: 'tempo', deck, ratio: definition.tempo_ratio });
	await driver.dispatch({ type: 'seek', deck, position_ms: definition.position_ms });
	await driver.dispatch({
		type: 'beat_loop',
		deck,
		beats: definition.loop.beats,
		start_ms: definition.loop.start_ms
	});
}

export async function applyPerformancePreset(
	preset: PerformancePreset,
	driver: PerformancePresetDriver
): Promise<PerformanceState> {
	validatePerformancePreset(preset);
	const decks = _orderedDecks(preset);

	for (const definition of decks) {
		await driver.dispatch({
			type: 'load',
			deck: definition.deck,
			stable_id: definition.stable_id
		});
	}
	assertPresetTracksReady(preset, driver.query());

	for (const definition of decks) await _configureDeck(definition, driver);
	await driver.dispatch({ type: 'master', deck: preset.master_deck });

	const playbackOrder = [
		decks.find((deck) => deck.deck === preset.master_deck),
		...decks.filter((deck) => deck.deck !== preset.master_deck)
	];
	let finalState: PerformanceState | null = null;
	for (const definition of playbackOrder) {
		if (definition === undefined) throw new Error(`validated preset has no master deck definition`);
		finalState = await driver.dispatch({ type: 'play', deck: definition.deck, playing: true });
	}
	if (finalState === null) throw new Error(`performance preset emitted no playback commands`);
	return finalState;
}
