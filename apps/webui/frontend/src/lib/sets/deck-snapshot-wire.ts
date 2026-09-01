/**
 * The wire contract: one PerformanceState, projected to one snapshot.
 *
 * Split out of `deck-observer-emitter.ts` at the 600-line review threshold.
 * The seam is the same one `deck-audibility.ts` uses: everything here is a
 * pure function of engine state, with no timers, no buffer and no HTTP, and
 * the emitter is all three of those.
 *
 * WHY IT VALIDATES BEFORE ANYTHING IS BUFFERED
 * `submit_many` on the server validates snapshot by snapshot and raises on
 * the first bad one, which 422s the request AFTER the earlier snapshots in
 * that batch were already enqueued. One malformed deck projection would
 * therefore reject a whole batch of good observations. So the same rules
 * `apps/sets/sources/opendj_wire.py:parse_snapshot` applies are applied here,
 * at projection time, and a snapshot that fails them never reaches the buffer.
 */

import type { DeckId } from '$lib/rb/deck-slots';
import type { PerformanceState } from '$lib/rb/performance-ipc.svelte';
import { deckWasHeard } from './deck-audibility';
import { isMasterMuted } from '$lib/player/master-mute.svelte';

export interface DeckObservationWire {
	stable_id: string | null;
	playing: boolean;
	audible: boolean;
	position_ms: number;
	duration_ms: number | null;
	title: string | null;
	artist: string | null;
}

export interface DeckSnapshotWire {
	/** UTC ISO 8601 with a real offset, from `Date.prototype.toISOString`.
	 *  The zone is produced by the clock and never typed by hand. */
	observed_at: string;
	decks: Record<string, DeckObservationWire>;
}

/** A deck state the server contract says cannot exist. Never coerced. */
export class DeckProjectionError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'DeckProjectionError';
	}
}

const WIRE_DECK_IDS = [1, 2, 3, 4] as const;

function _requireBool(value: unknown, field: string, deck: number): boolean {
	if (typeof value !== 'boolean') {
		throw new DeckProjectionError(`deck ${deck}: ${field} must be a bool, got ${String(value)}`);
	}
	return value;
}

function _requirePositionMs(value: unknown, deck: number): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new DeckProjectionError(
			`deck ${deck}: position_ms must be a finite number, got ${String(value)}`
		);
	}
	if (value < 0) {
		throw new DeckProjectionError(`deck ${deck}: position_ms must not be negative, got ${value}`);
	}
	return value;
}

/** `duration_ms` is null for a deck with nothing loaded, and the server
 *  rejects a non-positive duration outright. A zero-length deck is the
 *  "length not known yet" state, so it travels as null rather than as a
 *  number the far side would refuse. */
function _requireDurationMs(value: unknown, deck: number): number | null {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new DeckProjectionError(
			`deck ${deck}: duration_ms must be a finite number or null, got ${String(value)}`
		);
	}
	return value > 0 ? value : null;
}

function _optionalString(value: unknown, field: string, deck: number): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'string') {
		throw new DeckProjectionError(`deck ${deck}: ${field} must be a string or null`);
	}
	return value;
}

function _requireStableId(value: unknown, deck: number): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'string') {
		throw new DeckProjectionError(`deck ${deck}: stable_id must be a string or null`);
	}
	if (value === '') {
		throw new DeckProjectionError(
			`deck ${deck}: stable_id must not be empty; an empty deck sends null`
		);
	}
	return value;
}

/**
 * Project one engine state onto the wire contract, or throw.
 *
 * Exported because this is the half worth testing exhaustively: everything
 * the server refuses has to be refused here first, or one bad deck loses a
 * whole batch of good observations.
 */
export function toWireSnapshot(
	state: PerformanceState,
	observedAt: Date,
	routedDecks: ReadonlySet<DeckId> = new Set(),
	masterMuted: boolean = isMasterMuted()
): DeckSnapshotWire {
	const decks: Record<string, DeckObservationWire> = {};
	for (const deckId of WIRE_DECK_IDS) {
		const deck = state.decks[deckId];
		if (deck === undefined) {
			throw new DeckProjectionError(`deck ${deckId}: missing from the engine state`);
		}
		const stableId = _requireStableId(deck.stable_id, deckId);
		// Validate the engine's own flag first, so a non-bool is still refused
		// rather than being swallowed by the `&&` below.
		_requireBool(deck.audible, 'audible', deckId);
		const audible = deckWasHeard(state, deckId, routedDecks, masterMuted);
		if (audible && stableId === null) {
			throw new DeckProjectionError(
				`deck ${deckId}: audible with no track loaded is not a state a deck can be in`
			);
		}
		decks[String(deckId)] = {
			stable_id: stableId,
			playing: _requireBool(deck.playing, 'playing', deckId),
			audible,
			position_ms: _requirePositionMs(deck.position_ms, deckId),
			duration_ms: _requireDurationMs(deck.duration_ms, deckId),
			title: _optionalString(deck.title, 'title', deckId),
			artist: _optionalString(deck.artist, 'artist', deckId)
		};
	}
	return { observed_at: observedAt.toISOString(), decks };
}
