/**
 * One on-demand module loader for the waveform painters that stay off the "/"
 * first paint (bundle budget `library`, scripts/bundle-budget.mjs): the
 * section overlay (sections-lazy, STRUCT-05) and the Gothic block snapper
 * (block-layout-lazy, DECKUX-42) share this so the library chunk carries the
 * retry logic once.
 *
 * Contract:
 * - `apiFor(true)` starts the fetch and returns null until the module lands;
 *   `loaded` is rune state, so an effect or derived that asked re-runs then.
 * - A failed fetch is never cached: it rejects loudly, sets `loadError()`
 *   (rune state, for the UI) and retries with backoff doubling from 1 s to
 *   60 s, so a deck redrawing every frame does not re-request a dead chunk.
 * - The retry is a timer, not the next redraw, so a surface that only
 *   repaints on events (a stopped deck, the preview strip) still recovers.
 *   It runs only for a module some surface has wanted.
 */

const RETRY_BASE_MS = 1_000;
const RETRY_MAX_MS = 60_000;

export interface LazyModule<T> {
	/** Fetch (once) and cache the module; a failed fetch clears itself so a later call retries. */
	load(): Promise<T>;
	/** The module when `want` and loaded, else null; see the contract above. */
	apiFor(want: boolean): T | null;
	/** Why the last fetch failed, or null; reactive. */
	loadError(): string | null;
}

export function lazyModule<T>(importer: () => Promise<T>, failureLog: string): LazyModule<T> {
	let api: T | null = null;
	let loaded = $state(false);
	let pending: Promise<T> | null = null;
	let failures = 0;
	let retryAt = 0;
	let retryTimer: ReturnType<typeof setTimeout> | null = null;
	let wanted = false;
	let error = $state<string | null>(null);

	const logFailure = (err: unknown): void => console.error(failureLog, err);

	function retryNow(): void {
		retryTimer = null;
		if (loaded || pending !== null || !wanted) return;
		void load().catch(logFailure);
	}

	function load(): Promise<T> {
		pending ??= importer().then(
			(mod) => {
				api = mod;
				failures = 0;
				retryAt = 0;
				error = null;
				loaded = true;
				return mod;
			},
			(err: unknown) => {
				pending = null;
				const delay = Math.min(RETRY_BASE_MS * 2 ** failures, RETRY_MAX_MS);
				retryAt = Date.now() + delay;
				failures += 1;
				if (wanted && retryTimer === null) retryTimer = setTimeout(retryNow, delay);
				error = err instanceof Error ? err.message : String(err);
				throw err;
			}
		);
		return pending;
	}

	function apiFor(want: boolean): T | null {
		if (!want) return null;
		wanted = true;
		if (!loaded) {
			// In flight: the call that started the fetch already holds the one
			// logger, so later frames add no handler and no promise.
			if (pending !== null) return null;
			if (Date.now() < retryAt) {
				if (retryTimer === null) retryTimer = setTimeout(retryNow, retryAt - Date.now());
				return null;
			}
			void load().catch(logFailure);
			return null;
		}
		return api;
	}

	return { load, apiFor, loadError: () => error };
}
