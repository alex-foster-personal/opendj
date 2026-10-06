/**
 * Bug #58 / IOPIN-12 refined (ADR 0NNN, Tue 6 Oct 2026): a failed audio graph
 * rebuild retries before it unloads anything.
 *
 * The soak unload: an output stall triggered a graph rebuild, Signalsmith
 * addModule timed out after 15 s on a loaded machine, and IOPIN-12 unloaded both
 * decks on that single failure. The decoded buffers and track identity were
 * perfectly good; only the new context's worklet module had not arrived yet.
 *
 * So the first failure keeps every deck's buffer and identity, marks it
 * "reattaching", and retries the whole rebuild after about 15 s, 30 s and 60 s,
 * each retry with a longer addModule timeout. Only when every retry has failed
 * does the IOPIN-12 unload run, with a visible deck error.
 *
 * Pure: every effect is injected, so node:test drives the whole schedule.
 */

export const GRAPH_REBUILD_RETRY_DELAYS_MS: readonly number[] = [15_000, 30_000, 60_000];

/** addModule timeout per attempt: the first is the normal 15 s, retries allow longer under load. */
export const GRAPH_REBUILD_ADD_MODULE_TIMEOUTS_MS: readonly number[] = [15_000, 30_000, 45_000, 60_000];

export const GRAPH_REBUILD_EXHAUSTED_MESSAGE = 'audio engine could not restart, reload the track';

export interface GraphRebuildRetryEffects {
	/** One full rebuild attempt with the given addModule timeout. Rejects on failure. */
	rebuildOnce(addModuleTimeoutMs: number): Promise<void>;
	/** A rebuild failed and a retry is scheduled: keep the decks, mark them reattaching. */
	onRetryScheduled(input: { retry: number; of: number; delay_ms: number; error: unknown }): void;
	/** An attempt succeeded. `attempt` 0 is the first try, 1..N are retries. */
	onRecovered(input: { attempt: number }): void;
	/** Every retry failed: run the IOPIN-12 unload with GRAPH_REBUILD_EXHAUSTED_MESSAGE. */
	onExhausted(error: unknown): void;
	sleep(ms: number): Promise<void>;
	delaysMs?: readonly number[];
}

/**
 * Run a rebuild, retrying on failure. Resolves once the graph is rebuilt (first
 * try or a retry); rejects with the last error after onExhausted has run.
 */
export async function rebuildGraphWithRetries(effects: GraphRebuildRetryEffects): Promise<void> {
	const delays = effects.delaysMs ?? GRAPH_REBUILD_RETRY_DELAYS_MS;
	let lastError: unknown = null;
	for (let attempt = 0; attempt <= delays.length; attempt++) {
		if (attempt > 0) await effects.sleep(delays[attempt - 1]);
		try {
			await effects.rebuildOnce(_addModuleTimeoutFor(attempt));
			effects.onRecovered({ attempt });
			return;
		} catch (error: unknown) {
			lastError = error;
			if (attempt < delays.length) {
				effects.onRetryScheduled({ retry: attempt + 1, of: delays.length, delay_ms: delays[attempt], error });
			}
		}
	}
	effects.onExhausted(lastError);
	throw lastError;
}

function _addModuleTimeoutFor(attempt: number): number {
	const table = GRAPH_REBUILD_ADD_MODULE_TIMEOUTS_MS;
	return table[Math.min(attempt, table.length - 1)];
}

export function reattachingDeckMessage(input: { retry: number; of: number; delay_ms: number }): string {
	return (
		`audio engine restarting, reattaching the track ` +
		`(retry ${input.retry} of ${input.of} in ${Math.round(input.delay_ms / 1000)} s)`
	);
}

/** Shown on a deck that was playing before the stall when AutoPlay is off. */
export const ENGINE_RECOVERED_PRESS_PLAY = 'audio engine recovered, press play';

/**
 * CORE decision, Tue 6 Oct 2026 (for the maintainer's review): after a successful retry,
 * an unattended set (AutoPlay ON) resumes the decks that were playing, because
 * silence is the worst outcome; a DJ driving (AutoPlay OFF) gets them back
 * paused with ENGINE_RECOVERED_PRESS_PLAY on each one. Decks that were already
 * paused are left alone either way.
 */
export function planResumeAfterRebuild<D>(input: {
	auto_play_enabled: boolean;
	was_playing: readonly D[];
}): { resume: D[]; prompt: D[] } {
	if (input.auto_play_enabled) return { resume: [...input.was_playing], prompt: [] };
	return { resume: [], prompt: [...input.was_playing] };
}
