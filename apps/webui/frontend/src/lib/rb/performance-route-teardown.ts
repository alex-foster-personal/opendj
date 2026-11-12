/**
 * Fail-closed teardown coordinator for a performance route.
 *
 * The graceful transport stop is diagnostic cleanup. It must never delay the
 * hard safety boundary: mute first, then begin audio-engine disposal in the
 * same JavaScript turn. Every failure is reported, while repeated teardown
 * requests share one disposal completion and cannot dispose twice.
 *
 * `createPerformanceRouteTeardown` is the shared wiring /performance, its
 * preload wrapper and /practice use on unmount.
 */

import { engine, getDeckState, mixerState } from '$lib/rb/audio-engine.svelte';
import { readMasterWriteRevision } from '$lib/player/state.svelte';
import { createGigTeardownActions } from '$lib/rb/library-mode-runtime';

export type PerformanceTeardownFailurePhase = 'hard-mute' | 'graceful-stop' | 'dispose';

export interface PerformanceRouteTeardownActions {
	hardMute(): void;
	gracefulStop(): Promise<void>;
	dispose(): Promise<void>;
	reportError(phase: PerformanceTeardownFailurePhase, error: unknown): void;
}

function _reportTeardownError(
	actions: PerformanceRouteTeardownActions,
	phase: PerformanceTeardownFailurePhase,
	error: unknown
): void {
	try {
		actions.reportError(phase, error);
	} catch (reportError) {
		console.error(
			`performance route teardown could not report ${phase} failure`,
			error,
			reportError
		);
	}
}

export function createFailClosedPerformanceTeardown(
	actions: PerformanceRouteTeardownActions
): () => Promise<void> {
	let teardownPromise: Promise<void> | null = null;

	return (): Promise<void> => {
		if (teardownPromise !== null) return teardownPromise;

		try {
			actions.hardMute();
		} catch (error) {
			_reportTeardownError(actions, 'hard-mute', error);
		}

		try {
			void actions
				.gracefulStop()
				.catch((error: unknown) => _reportTeardownError(actions, 'graceful-stop', error));
		} catch (error) {
			_reportTeardownError(actions, 'graceful-stop', error);
		}

		let disposal: Promise<void>;
		try {
			disposal = actions.dispose();
		} catch (error) {
			_reportTeardownError(actions, 'dispose', error);
			disposal = Promise.resolve();
		}
		teardownPromise = disposal.catch((error: unknown) => {
			_reportTeardownError(actions, 'dispose', error);
		});
		return teardownPromise;
	};
}

/** Pause every playing deck: the graceful stop the performance route has always run. */
export async function pausePlayingDecks(): Promise<void> {
	for (const deckId of [1, 2, 3, 4] as const) {
		if (getDeckState(deckId).playing) await engine.pause(deckId);
	}
}

export function reportPerformanceUnmountError(
	phase: PerformanceTeardownFailurePhase,
	error: unknown
): void {
	console.error(`performance unmount ${phase} failed`, error);
}

/**
 * The fail-closed teardown every route that owns the gig engine runs on unmount: hard-mute
 * (capturing the master for restoration), graceful stop, then engine dispose through
 * releaseGigRuntime. /performance, its preload wrapper and /practice all build it here.
 */
export function createPerformanceRouteTeardown(
	options: {
		gracefulStop?: () => Promise<void>;
		reportError?: (phase: PerformanceTeardownFailurePhase, error: unknown) => void;
	} = {}
): () => Promise<void> {
	return createFailClosedPerformanceTeardown({
		...createGigTeardownActions({
			readMaster: () => mixerState.master,
			readMasterWriteRevision,
			setMaster: (value) => engine.setMaster(value),
			disposeEngine: () => engine.dispose()
		}),
		gracefulStop: options.gracefulStop ?? pausePlayingDecks,
		reportError: options.reportError ?? reportPerformanceUnmountError
	});
}
