/**
 * In-memory retry queue for AutoPlay Next's "remember this transition for the pair" save (issue #9674).
 * Pure: the clock, the save call and the toast are injected, so it is unit-tested without a page.
 * Not persisted across a reload (out of scope).
 */

export const SAVE_RETRY_DELAYS_MS = [1000, 4000, 15000] as const;
export const SAVE_FAILED_TOAST = 'Could not save this transition for the pair';

export interface SaveQueueDeps<T> {
	save: (item: T) => Promise<void>;
	schedule: (fn: () => void, ms: number) => unknown;
	onGiveUp: (message: string, retry: () => void) => void;
	onChange?: (pending: number, lastError: string | null) => void;
}

/** True when the error is a 4xx that says the request itself is invalid (not 408 or 429). */
export function isInvalidRequestError(error: unknown): boolean {
	const match = /answered (4\d\d)/.exec(error instanceof Error ? error.message : String(error));
	if (match === null) return false;
	const status = Number(match[1]);
	return status !== 408 && status !== 429;
}

function errorText(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

/** Build a save queue: enqueue() runs the save now and retries with backoff, then hands the failure to onGiveUp. */
export function createSaveQueue<T>(deps: SaveQueueDeps<T>) {
	let pending = 0;
	let lastError: string | null = null;
	const changed = () => deps.onChange?.(pending, lastError);

	async function attempt(item: T, tryIndex: number): Promise<void> {
		try {
			await deps.save(item);
			pending -= 1;
			changed();
		} catch (error: unknown) {
			lastError = errorText(error);
			const invalid = isInvalidRequestError(error);
			if (!invalid && tryIndex < SAVE_RETRY_DELAYS_MS.length) {
				changed();
				deps.schedule(() => void attempt(item, tryIndex + 1), SAVE_RETRY_DELAYS_MS[tryIndex]);
				return;
			}
			changed();
			let retried = false;
			deps.onGiveUp(invalid ? `${SAVE_FAILED_TOAST}: ${lastError}` : SAVE_FAILED_TOAST, () => {
				// One Retry click sends one save; a second click on the same toast is ignored (no duplicate).
				if (retried) return;
				retried = true;
				void attempt(item, SAVE_RETRY_DELAYS_MS.length);
			});
		}
	}

	return {
		/** Queue one save and start its first attempt. */
		enqueue(item: T): Promise<void> {
			pending += 1;
			changed();
			return attempt(item, 0);
		},
		/** Pending count and last error, for the state agents read. */
		state(): { pendingSaves: number; lastError: string | null } {
			return { pendingSaves: pending, lastError };
		}
	};
}
