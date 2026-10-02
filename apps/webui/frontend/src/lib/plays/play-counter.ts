/**
 * Open DJ's own play counter (PLAYS-01): one play per deck load that the room
 * actually heard for PLAY_THRESHOLD_S.
 *
 * The library's play count used to be rekordbox `DJPlayCount` alone, so a
 * track played only in Open DJ read 0 forever. The set recorder already
 * measures what was heard (`lib/sets/deck-audibility.ts`), but it only banks
 * plays while REC runs, so everyday plays left no trace. This counter is the
 * always-on half: it samples deck state on a timer, credits heard seconds per
 * deck load, and posts ONE play to `POST /api/v1/tracks/{id}/plays` when a
 * load crosses the threshold. The server adds those rows to the imported count
 * (`apps/shared/state/play_log.py`).
 *
 * "Heard" is `deckWasHeard`, the same gate the set recorder uses, so a deck
 * pre-cued in the headphones with its fader down never counts.
 *
 * A load is identified by a client-minted `play_id`. A post that fails stays
 * pending and is retried on later ticks; the server ignores a `play_id` it has
 * already logged, so a retry of a post that did land never counts twice.
 *
 * Pure factory, no DOM: the sampler, the clock and the POST are injectable so
 * a unit test drives it directly. `installPlayCounter` owns the timer.
 */

import { api } from '$lib/api/client';
import { queryPerformanceState, type PerformanceState } from '$lib/rb/performance-ipc.svelte';
import { deckWasHeard, externallyRoutedDecks } from '$lib/sets/deck-audibility';

/** Derived from the state type rather than imported from `rb/deck-slots`,
 *  which is already the most-imported module in the frontend. */
type DeckId = NonNullable<PerformanceState['master_deck']>;

/** Heard seconds after which a load counts as a play. Mirrors the server's
 *  `play_log.PLAY_THRESHOLD_S` and the set analytics' 60 s default
 *  (docs/product/set-dwell-threshold-analysis.md). */
export const PLAY_THRESHOLD_S = 60;
export const PLAY_SAMPLE_INTERVAL_MS = 1000;
/** Most a single gap between samples may credit, so a throttled background
 *  tab cannot bank minutes it never observed (same bound as the recorder). */
export const MAX_SAMPLE_GAP_MS = 5000;
/** Posts tried per load before it is given up and reported in `status`. */
export const MAX_POST_ATTEMPTS = 5;

const DECK_IDS: readonly DeckId[] = [1, 2, 3, 4];

export interface PlayPost {
	stableId: string;
	playId: string;
	audibleS: number;
	deck: DeckId;
	durationMs: number | null;
}

interface DeckLoad {
	stableId: string;
	playId: string;
	heardMs: number;
	lastSampleMs: number | null;
	durationMs: number | null;
	/** 'counting' until the threshold, then 'pending' until a post lands. */
	phase: 'counting' | 'pending' | 'posted' | 'failed';
	attempts: number;
}

export interface PlayCounterStatus {
	decks: Partial<
		Record<DeckId, { stable_id: string; heard_s: number; phase: DeckLoad['phase'] }>
	>;
	posted: number;
	failed: number;
}

export interface PlayCounterOptions {
	sample?: () => PerformanceState;
	heard?: (state: PerformanceState, deck: DeckId) => boolean;
	post?: (play: PlayPost) => Promise<void>;
	now?: () => number;
	newPlayId?: () => string;
}

export interface PlayCounter {
	tick(): void;
	status(): PlayCounterStatus;
}

function _defaultHeard(): (state: PerformanceState, deck: DeckId) => boolean {
	const routed = externallyRoutedDecks(typeof window === 'undefined' ? '' : window.location.search);
	return (state, deck) => deckWasHeard(state, deck, routed);
}

async function _defaultPost(play: PlayPost): Promise<void> {
	await api.POST('/api/v1/tracks/{stable_id}/plays', {
		params: { path: { stable_id: play.stableId } },
		body: {
			play_id: play.playId,
			audible_s: play.audibleS,
			deck: play.deck,
			duration_ms: play.durationMs
		}
	});
}

function _newPlayId(): string {
	return globalThis.crypto.randomUUID();
}

export function createPlayCounter(options: PlayCounterOptions = {}): PlayCounter {
	const sample = options.sample ?? queryPerformanceState;
	const heard = options.heard ?? _defaultHeard();
	const post = options.post ?? _defaultPost;
	const now = options.now ?? (() => Date.now());
	const newPlayId = options.newPlayId ?? _newPlayId;
	const loads = new Map<DeckId, DeckLoad>();
	let posted = 0;
	let failed = 0;

	function _send(deck: DeckId, load: DeckLoad): void {
		load.attempts += 1;
		load.phase = 'posted';
		post({
			stableId: load.stableId,
			playId: load.playId,
			audibleS: load.heardMs / 1000,
			deck,
			durationMs: load.durationMs
		}).then(
			() => {
				posted += 1;
			},
			(err: unknown) => {
				if (load.attempts >= MAX_POST_ATTEMPTS) {
					load.phase = 'failed';
					failed += 1;
					console.error(`play counter: giving up on ${load.stableId} deck ${deck}`, err);
				} else {
					load.phase = 'pending';
				}
			}
		);
	}

	function _observe(state: PerformanceState, deck: DeckId, at: number): void {
		const deckState = state.decks[deck];
		const stableId = deckState?.stable_id ?? null;
		let load = loads.get(deck);
		if (stableId === null) {
			loads.delete(deck);
			return;
		}
		if (load === undefined || load.stableId !== stableId) {
			load = {
				stableId,
				playId: newPlayId(),
				heardMs: 0,
				lastSampleMs: null,
				// A decoded duration is fractional (seconds * 1000); the server's
				// duration_ms is an int and rejects a float with 422.
				durationMs: deckState.duration_ms == null ? null : Math.round(deckState.duration_ms),
				phase: 'counting',
				attempts: 0
			};
			loads.set(deck, load);
		}
		const isHeard = heard(state, deck);
		if (isHeard && load.lastSampleMs !== null) {
			load.heardMs += Math.min(Math.max(at - load.lastSampleMs, 0), MAX_SAMPLE_GAP_MS);
		}
		// A deck that goes quiet stops the clock: the next heard sample starts
		// a fresh gap rather than crediting the silence in between.
		load.lastSampleMs = isHeard ? at : null;
		if (load.phase === 'counting' && load.heardMs >= PLAY_THRESHOLD_S * 1000) {
			load.phase = 'pending';
		}
		if (load.phase === 'pending') _send(deck, load);
	}

	return {
		tick(): void {
			const state = sample();
			const at = now();
			for (const deck of DECK_IDS) _observe(state, deck, at);
		},
		status(): PlayCounterStatus {
			const decks: PlayCounterStatus['decks'] = {};
			for (const [deck, load] of loads) {
				decks[deck] = {
					stable_id: load.stableId,
					heard_s: Math.round(load.heardMs / 100) / 10,
					phase: load.phase
				};
			}
			return { decks, posted, failed };
		}
	};
}

/** Window global an agent reads and drives the counter through. */
export const PLAY_COUNTER_GLOBAL = '__mdtPlayCounter';

/**
 * Count plays for the lifetime of /performance, where the deck engine lives.
 * Returns the uninstall. Sampling is a local read with no network, so it
 * starts at mount; a post happens at most once per deck load.
 */
export function installPlayCounter(options: PlayCounterOptions = {}): () => void {
	if (typeof window === 'undefined') return () => {};
	const counter = createPlayCounter(options);
	const timer = setInterval(() => counter.tick(), PLAY_SAMPLE_INTERVAL_MS);
	Object.defineProperty(window, PLAY_COUNTER_GLOBAL, {
		value: { status: counter.status, tick: counter.tick },
		configurable: true,
		writable: true,
		enumerable: false
	});
	return () => {
		clearInterval(timer);
		Reflect.deleteProperty(window, PLAY_COUNTER_GLOBAL);
	};
}
