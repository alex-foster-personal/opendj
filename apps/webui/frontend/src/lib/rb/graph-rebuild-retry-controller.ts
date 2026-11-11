/**
 * Bug #58 / issue #5522: ports-injected orchestration around
 * `rebuildGraphWithRetries` (mark-reattaching, recover/resume, exhausted unload).
 * The live engine still owns the chain; this module is the extraction target.
 */

import type { DeckId } from '$lib/rb/deck-slots';
import type { PauseOrigin } from '$lib/rb/unexpected-pause';
import {
	ENGINE_RECOVERED_PRESS_PLAY,
	GRAPH_REBUILD_EXHAUSTED_MESSAGE,
	GRAPH_REBUILD_RETRY_DELAYS_MS,
	planResumeAfterRebuild,
	rebuildGraphWithRetries,
	reattachingDeckMessage
} from '$lib/rb/graph-rebuild-retry';
import type { EngineRecoveryStopReason } from '$lib/rb/engine-recovery-stop';

export interface GraphRebuildRetryDeckRuntime {
	audioBuffer: unknown | null;
	processor: unknown | null;
	desiredActive: boolean;
	controlActive: boolean;
	pending: unknown[];
	presentation: unknown;
	nextScheduleRevision: number;
	metadataDurationMs: number | null;
}

export interface GraphRebuildRetryDeckState {
	deck_id: DeckId;
	position_ms: number;
	stable_id: string | null;
	playing: boolean;
	audible: boolean;
	transport_pending: boolean;
	processor_error: string | null;
	duration_ms: number | null;
}

export interface GraphRebuildRetryControllerPorts {
	readonly deckIds: readonly DeckId[];
	runtime(deck: DeckId): GraphRebuildRetryDeckRuntime;
	state(deck: DeckId): GraphRebuildRetryDeckState;
	contextState(): string;
	autoPlayEnabled(): boolean;
	setStretchAddModuleTimeoutMs(ms: number): void;
	resetStretchAddModuleTimeoutMs(): void;
	recreateGraphKeepingDecks(): Promise<void>;
	resumeContext(): Promise<void>;
	createPresentedTransportTimeline(positionSec: number): unknown;
	recordUnexpectedPause(input: {
		cause: 'worklet-error';
		deck: DeckId;
		position_ms: number;
		context_state?: string;
		decoded_duration_ms?: number | null;
		metadata_duration_ms?: number | null;
		cause_error?: unknown;
	}): void;
	withPauseOrigin<T>(origin: PauseOrigin, fn: () => T): T;
	recordDeckStopOf(
		st: { deck_id: DeckId; position_ms: number; stable_id: string | null },
		cause?: 'engine'
	): void;
	recordPerfEvent(
		kind: string,
		message: string,
		deck: DeckId | null,
		level: 'info' | 'error'
	): void;
	recordProcessorFailure(deck: DeckId, error: Error): void;
	markEngineRecoveryStop(reason: EngineRecoveryStopReason): void;
	play(deck: DeckId): Promise<void>;
	/** Injected by unit tests so retries do not wait on real timeouts. */
	retryDelaysMs?: readonly number[];
	retrySleep?: (ms: number) => Promise<void>;
}

export interface GraphRebuildRetryController {
	rebuildAudioGraphKeepingDecks(): Promise<void>;
	reattachingDecks(): DeckId[];
}

/** Issue #2155 / bug #58: one rebuild chain at a time; stalls join the chain in flight. */
export function createGraphRebuildRetryController(
	ports: GraphRebuildRetryControllerPorts
): GraphRebuildRetryController {
	const reattachingDecksSet = new Set<DeckId>();
	const reattachWasPlaying = new Set<DeckId>();
	let rebuildChain: Promise<void> | null = null;
	const retryDelaysMs = ports.retryDelaysMs ?? GRAPH_REBUILD_RETRY_DELAYS_MS;
	const retrySleep =
		ports.retrySleep ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));

	function decksWithoutProcessor(): DeckId[] {
		return ports.deckIds.filter((deck) => {
			const rt = ports.runtime(deck);
			return rt.audioBuffer !== null && rt.processor === null;
		});
	}

	async function rebuildOnce(addModuleTimeoutMs: number): Promise<void> {
		ports.setStretchAddModuleTimeoutMs(addModuleTimeoutMs);
		try {
			await ports.recreateGraphKeepingDecks();
		} finally {
			ports.resetStretchAddModuleTimeoutMs();
		}
		await ports.resumeContext();
	}

	function markDecksReattaching(input: {
		retry: number;
		of: number;
		delay_ms: number;
		error: unknown;
	}): void {
		const message = reattachingDeckMessage(input);
		const cause = input.error instanceof Error ? input.error.message : String(input.error);
		let stoppedPlayingDeck = false;
		for (const deck of decksWithoutProcessor()) {
			const st = ports.state(deck);
			const rt = ports.runtime(deck);
			if (!reattachingDecksSet.has(deck)) {
				if (rt.desiredActive || st.playing) {
					stoppedPlayingDeck = true;
					reattachWasPlaying.add(deck);
					ports.recordDeckStopOf(st, 'engine');
				}
				ports.recordUnexpectedPause({
					cause: 'worklet-error',
					deck,
					position_ms: st.position_ms,
					context_state: ports.contextState(),
					decoded_duration_ms: st.duration_ms,
					metadata_duration_ms: rt.metadataDurationMs,
					cause_error: input.error
				});
			}
			reattachingDecksSet.add(deck);
			rt.pending = [];
			rt.desiredActive = false;
			rt.controlActive = false;
			rt.presentation = ports.createPresentedTransportTimeline(st.position_ms / 1000);
			rt.nextScheduleRevision = 0;
			ports.withPauseOrigin('worklet', () => {
				st.playing = false;
				st.audible = false;
				st.transport_pending = false;
				st.processor_error = message;
			});
		}
		if (stoppedPlayingDeck) ports.markEngineRecoveryStop('graph-rebuild-failed');
		ports.recordPerfEvent(
			'audio-graph-rebuild-retry',
			`audio graph rebuild failed (${cause}); decks kept loaded, ${message}`,
			null,
			'error'
		);
	}

	function finishReattach(attempt: number): void {
		for (const deck of [...reattachingDecksSet]) {
			const st = ports.state(deck);
			if (
				ports.runtime(deck).processor !== null &&
				st.processor_error?.startsWith('audio engine restarting') === true
			) {
				st.processor_error = null;
			}
		}
		const wasPlaying = ports.deckIds.filter(
			(deck) =>
				reattachWasPlaying.has(deck) &&
				ports.runtime(deck).processor !== null &&
				ports.state(deck).stable_id !== null
		);
		reattachingDecksSet.clear();
		reattachWasPlaying.clear();
		if (attempt > 0) {
			ports.recordPerfEvent(
				'audio-graph-rebuild-recovered',
				`audio graph rebuilt on retry ${attempt}; decks kept loaded`,
				null,
				'info'
			);
		}
		const plan = planResumeAfterRebuild({
			auto_play_enabled: ports.autoPlayEnabled(),
			was_playing: wasPlaying
		});
		for (const deck of plan.prompt) ports.state(deck).processor_error = ENGINE_RECOVERED_PRESS_PLAY;
		for (const deck of plan.resume) {
			void ports.play(deck).catch((error: unknown) => {
				ports.state(deck).processor_error = ENGINE_RECOVERED_PRESS_PLAY;
				ports.recordPerfEvent(
					'audio-graph-rebuild-resume-failed',
					`deck ${deck} did not resume after the rebuild: ${String(error)}`,
					null,
					'error'
				);
			});
		}
	}

	function unloadDecksOrphanedByFailedRebuild(error: unknown): void {
		const cause = error instanceof Error ? error.message : String(error);
		const orphaned = decksWithoutProcessor();
		if (
			orphaned.some((deck) => {
				const rt = ports.runtime(deck);
				const st = ports.state(deck);
				return reattachingDecksSet.has(deck) || rt.desiredActive || st.playing;
			})
		) {
			ports.markEngineRecoveryStop('graph-rebuild-failed');
		}
		for (const deck of orphaned) {
			ports.recordProcessorFailure(deck, new Error(`${GRAPH_REBUILD_EXHAUSTED_MESSAGE}: ${cause}`));
		}
		reattachingDecksSet.clear();
		reattachWasPlaying.clear();
	}

	function rebuildAudioGraphKeepingDecks(): Promise<void> {
		if (rebuildChain !== null) return rebuildChain;
		const chain = rebuildGraphWithRetries({
			rebuildOnce,
			onRetryScheduled: markDecksReattaching,
			onRecovered: ({ attempt }) => finishReattach(attempt),
			onExhausted: unloadDecksOrphanedByFailedRebuild,
			sleep: (ms) => retrySleep(ms),
			delaysMs: retryDelaysMs
		}).finally(() => {
			if (rebuildChain === chain) rebuildChain = null;
		});
		rebuildChain = chain;
		return chain;
	}

	return {
		rebuildAudioGraphKeepingDecks,
		reattachingDecks: () => ports.deckIds.filter((deck) => reattachingDecksSet.has(deck))
	};
}
