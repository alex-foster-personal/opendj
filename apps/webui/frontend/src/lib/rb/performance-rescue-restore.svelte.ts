/**
 * RESCUE-02/03 orchestration: decode wait, simultaneous resume, toast Undo.
 */

import type { DeckId } from '$lib/rb/deck-id';
import {
	computeResumeTarget,
	deckDecodedForRescue,
	formatElapsedAgo,
	gigPlaybackEligible,
	RESCUE_DECODE_CEILING_MS,
	type RescueResumeException,
	type RescueResumeTarget
} from '$lib/rb/performance-rescue-math';
import type { RescueSnapshot } from '$lib/rb/rescue-snapshot';
import type {
	PerformanceCommand,
	PerformanceState
} from '$lib/rb/performance-ipc.svelte';
import { pushToast } from '$lib/stores.svelte';
import type { ReloadResumeOffer } from '$lib/rb/reload-resume';

const DECK_IDS: DeckId[] = [1, 2, 3, 4];

export type RescueDeckDecodeStatus = 'pending' | 'decoded' | 'failed';

export interface RescueRestoreStatus {
	phase: 'idle' | 'restoring' | 'resuming' | 'done' | 'failed';
	playing_deck_ids: DeckId[];
	per_deck: Record<DeckId, RescueDeckDecodeStatus>;
	started_at_ms: number;
}

export const rescueRestoreStatus = $state<RescueRestoreStatus>({
	phase: 'idle',
	playing_deck_ids: [],
	per_deck: { 1: 'pending', 2: 'pending', 3: 'pending', 4: 'pending' },
	started_at_ms: 0
});

/**
 * RESCUE-07: the reload "Resume N decks" offer. reload-resume.svelte.ts owns its
 * behaviour; the state lives beside the RESCUE-02 status so the performance IPC
 * reads both through one import.
 */
export const reloadResume = $state<{
	offer: ReloadResumeOffer | null;
	error: string | null;
	auto_play_on: boolean;
}>({ offer: null, error: null, auto_play_on: false });

export interface RescueRestoreDeps {
	now?: () => number;
	dispatch: (command: PerformanceCommand) => Promise<PerformanceState>;
	query: () => PerformanceState;
	setTimeout?: typeof globalThis.setTimeout;
	clearTimeout?: typeof globalThis.clearTimeout;
	requestAnimationFrame?: typeof globalThis.requestAnimationFrame;
	pushToast?: typeof pushToast;
}

function _resetStatus(): void {
	rescueRestoreStatus.phase = 'idle';
	rescueRestoreStatus.playing_deck_ids = [];
	for (const deckId of DECK_IDS) rescueRestoreStatus.per_deck[deckId] = 'pending';
	rescueRestoreStatus.started_at_ms = 0;
}

function _playingDeckIds(snapshot: RescueSnapshot): DeckId[] {
	return DECK_IDS.filter((deckId) => {
		const deck = snapshot.decks[deckId];
		return deck.playing && deck.stable_id !== null && deck.stable_id.length > 0;
	});
}

async function _waitForDecode(
	snapshot: RescueSnapshot,
	playingDeckIds: DeckId[],
	deps: RescueRestoreDeps
): Promise<DeckId[]> {
	const setTimeoutFn = deps.setTimeout ?? globalThis.setTimeout;
	const clearTimeoutFn = deps.clearTimeout ?? globalThis.clearTimeout;
	const raf = deps.requestAnimationFrame ?? globalThis.requestAnimationFrame.bind(globalThis);
	const startedAt = deps.now?.() ?? Date.now();

	return await new Promise<DeckId[]>((resolve) => {
		let settled = false;
		let ceilingTimer: ReturnType<typeof setTimeoutFn> | undefined;
		const finish = (decoded: DeckId[]): void => {
			if (settled) return;
			settled = true;
			if (ceilingTimer !== undefined) clearTimeoutFn(ceilingTimer);
			resolve(decoded);
		};

		const poll = (): void => {
			const state = deps.query();
			const decoded: DeckId[] = [];
			for (const deckId of playingDeckIds) {
				const expected = snapshot.decks[deckId].stable_id;
				if (expected === null) {
					rescueRestoreStatus.per_deck[deckId] = 'failed';
					continue;
				}
				const deck = state.decks[deckId];
				if (
					deckDecodedForRescue({
						stable_id: deck.stable_id,
						expected_stable_id: expected,
						duration_ms: deck.duration_ms,
						processor_error: deck.processor_error
					})
				) {
					rescueRestoreStatus.per_deck[deckId] = 'decoded';
					decoded.push(deckId);
				} else {
					rescueRestoreStatus.per_deck[deckId] = 'pending';
				}
			}
			if (decoded.length === playingDeckIds.length) {
				finish(decoded);
				return;
			}
			raf(() => poll());
		};

		ceilingTimer = setTimeoutFn(() => {
			const state = deps.query();
			const decoded: DeckId[] = [];
			for (const deckId of playingDeckIds) {
				const expected = snapshot.decks[deckId].stable_id;
				if (expected === null) {
					rescueRestoreStatus.per_deck[deckId] = 'failed';
					continue;
				}
				const deck = state.decks[deckId];
				if (
					deckDecodedForRescue({
						stable_id: deck.stable_id,
						expected_stable_id: expected,
						duration_ms: deck.duration_ms,
						processor_error: deck.processor_error
					})
				) {
					rescueRestoreStatus.per_deck[deckId] = 'decoded';
					decoded.push(deckId);
				} else {
					rescueRestoreStatus.per_deck[deckId] = 'failed';
				}
			}
			finish(decoded);
		}, RESCUE_DECODE_CEILING_MS);

		rescueRestoreStatus.phase = 'restoring';
		rescueRestoreStatus.started_at_ms = startedAt;
		rescueRestoreStatus.playing_deck_ids = [...playingDeckIds];
		poll();
	});
}

function _resumePlans(
	snapshot: RescueSnapshot,
	decodedDeckIds: DeckId[],
	elapsedWallMs: number,
	state: PerformanceState
): { targets: RescueResumeTarget[]; exceptions: RescueResumeException[] } {
	const targets: RescueResumeTarget[] = [];
	const exceptions: RescueResumeException[] = [];

	for (const deckId of decodedDeckIds) {
		const snapDeck = snapshot.decks[deckId];
		const stamp = snapDeck.beat_stamp;
		const result = computeResumeTarget(
			deckId,
			stamp,
			state.decks[deckId].beatgrid_ms,
			snapDeck.pitch,
			elapsedWallMs
		);
		if ('reason' in result) exceptions.push(result);
		else targets.push(result);
	}

	return { targets, exceptions };
}

export async function runRescuePlaybackRestore(
	snapshot: RescueSnapshot,
	deps: RescueRestoreDeps
): Promise<void> {
	const now = deps.now ?? (() => Date.now());
	const toast = deps.pushToast ?? pushToast;

	if (!gigPlaybackEligible(snapshot, now())) {
		_resetStatus();
		return;
	}

	const playingDeckIds = _playingDeckIds(snapshot);
	if (playingDeckIds.length === 0) {
		_resetStatus();
		return;
	}

	const elapsedWallMs = now() - snapshot.captured_at_ms;
	const decodedDeckIds = await _waitForDecode(snapshot, playingDeckIds, deps);
	if (decodedDeckIds.length === 0) {
		rescueRestoreStatus.phase = 'failed';
		toast('Rescue restore: no decks decoded in time', 'error');
		return;
	}

	rescueRestoreStatus.phase = 'resuming';
	const state = deps.query();
	const { targets, exceptions } = _resumePlans(snapshot, decodedDeckIds, elapsedWallMs, state);
	const undecoded = playingDeckIds.filter((deckId) => !decodedDeckIds.includes(deckId));

	if (targets.length === 0) {
		rescueRestoreStatus.phase = 'failed';
		const names = [...exceptions, ...undecoded.map((deck) => ({ deck, reason: 'not decoded' }))]
			.map((item) => `deck ${item.deck}: ${item.reason}`)
			.join('; ');
		toast(`Rescue restore failed: ${names}`, 'error');
		return;
	}

	await deps.dispatch({
		type: 'rescue_resume',
		decks: targets.map((target) => ({
			deck: target.deck,
			position_ms: target.target_position_ms
		}))
	});

	const ago = formatElapsedAgo(elapsedWallMs);
	let message = `Restored ${targets.length} playing deck${targets.length === 1 ? '' : 's'} from ${ago} ago`;
	const exceptionParts = [
		...exceptions.map((item) => `deck ${item.deck}: ${item.reason}`),
		...undecoded.map((deckId) => `deck ${deckId}: not decoded`)
	];
	if (exceptionParts.length > 0) message += `; ${exceptionParts.join('; ')}`;

	toast(message, 'info', 10_000, undefined, {}, undefined, {
		label: 'Undo',
		handler: () => {
			void deps.dispatch({ type: 'rescue_stop_all' });
		}
	});

	rescueRestoreStatus.phase = 'done';
}
