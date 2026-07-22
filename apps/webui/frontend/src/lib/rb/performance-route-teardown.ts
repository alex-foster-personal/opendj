/**
 * Fail-closed teardown coordinator for a performance route.
 *
 * The graceful transport stop is diagnostic cleanup. It must never delay the
 * hard safety boundary: mute first, then begin audio-engine disposal in the
 * same JavaScript turn. Every failure is reported, while repeated teardown
 * requests share one disposal completion and cannot dispose twice.
 */

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
		console.error(`performance route teardown could not report ${phase} failure`, error, reportError);
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
