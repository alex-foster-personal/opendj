/** LIBM-181: a failed library cover image is retried, never remembered as "no artwork".
 *
 * Only the server's verdict (`artwork_available`, remembered through
 * optional-resource-availability.ts) may say a track has no cover. An image
 * error is a transport event (a backend restart answers ECONNREFUSED for a
 * second), so it hides the cover only until the next retry: 2 s, 8 s and
 * 30 s after the 1st, 2nd and 3rd failure, each with a cache-buster. After the
 * 3rd failed retry the id is given up, until the events bus reconnects
 * (`subscribeResync`), which clears every given-up id so covers come back once
 * the backend does. Before this, one error wrote `artwork:false` for the rest
 * of the session (#4096). */

import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { shouldFetchArtwork } from './optional-resource-availability';

export const ARTWORK_RETRY_DELAYS_MS: readonly number[] = [2000, 8000, 30000];

export interface ArtworkRetryTimers {
	setTimeout(fn: () => void, ms: number): unknown;
	clearTimeout(handle: unknown): void;
}

type Entry = { failures: number; due: boolean; handle: unknown };

/** LIBM-185: the library says tracks changed (enrichment fetched a cover): ids
 * that failed before get a fresh retry budget and a new cache-buster. */
export const ARTWORK_RESET_TOPIC_KIND = 'tracks' as const;

export class ArtworkRetry {
	#entries = new Map<string, Entry>();
	#resets = new Map<string, number>();
	#listeners = new Set<() => void>();
	#timers: ArtworkRetryTimers;

	constructor(timers?: ArtworkRetryTimers) {
		this.#timers = timers ?? {
			setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
			clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>)
		};
	}

	/** The cover image for `stableId` failed to load. */
	noteError(stableId: string): void {
		const prev = this.#entries.get(stableId);
		if (prev !== undefined && prev.handle !== null) this.#timers.clearTimeout(prev.handle);
		const failures = (prev?.failures ?? 0) + 1;
		const entry: Entry = { failures, due: false, handle: null };
		const delay = ARTWORK_RETRY_DELAYS_MS[failures - 1];
		if (delay !== undefined) {
			entry.handle = this.#timers.setTimeout(() => {
				entry.handle = null;
				entry.due = true;
				this.#notify();
			}, delay);
		}
		this.#entries.set(stableId, entry);
		this.#notify();
	}

	/** The cover loaded: forget its failures. */
	noteLoad(stableId: string): void {
		const prev = this.#entries.get(stableId);
		if (prev === undefined) return;
		if (prev.handle !== null) this.#timers.clearTimeout(prev.handle);
		this.#entries.delete(stableId);
	}

	/** True while the cover waits for its next retry, or after it was given up. */
	hidden(stableId: string): boolean {
		const entry = this.#entries.get(stableId);
		return entry !== undefined && !entry.due;
	}

	givenUp(stableId: string): boolean {
		return (this.#entries.get(stableId)?.failures ?? 0) > ARTWORK_RETRY_DELAYS_MS.length;
	}

	/** Failures so far; a nonzero count goes on the URL as a cache-buster. */
	failures(stableId: string): number {
		return this.#entries.get(stableId)?.failures ?? 0;
	}

	/** A cover that failed may exist now (the server announced a track change):
	 * forget its failures so it loads at once, under a new cache-buster. Ids
	 * that never failed are left alone. */
	reset(stableId: string): void {
		const prev = this.#entries.get(stableId);
		if (prev === undefined) return;
		if (prev.handle !== null) this.#timers.clearTimeout(prev.handle);
		this.#entries.delete(stableId);
		this.#resets.set(stableId, (this.#resets.get(stableId) ?? 0) + 1);
		this.#notify();
	}

	/** Cache-buster for the URL: failures, plus `.N` after N resets; '' when clean. */
	version(stableId: string): string {
		const failures = this.failures(stableId);
		const resets = this.#resets.get(stableId) ?? 0;
		if (resets === 0) return failures === 0 ? '' : String(failures);
		return `${failures}.${resets}`;
	}

	/** Backend reconnected: given-up covers get a fresh set of retries. */
	clearGivenUp(): void {
		let changed = false;
		for (const [id, entry] of this.#entries) {
			if (entry.failures > ARTWORK_RETRY_DELAYS_MS.length) {
				this.#entries.delete(id);
				changed = true;
			}
		}
		if (changed) this.#notify();
	}

	subscribe(listener: () => void): () => void {
		this.#listeners.add(listener);
		return () => {
			this.#listeners.delete(listener);
		};
	}

	#notify(): void {
		for (const listener of this.#listeners) listener();
	}
}

let _shared: ArtworkRetry | null = null;

/** The page-wide retry state, cleared of given-up ids on every events-bus resync. */
export function artworkRetry(): ArtworkRetry {
	if (_shared === null) {
		const shared = new ArtworkRetry();
		subscribeResync(() => shared.clearGivenUp());
		subscribeKind(ARTWORK_RESET_TOPIC_KIND, (ids) => {
			for (const id of ids) shared.reset(id);
		});
		_shared = shared;
	}
	return _shared;
}

/** Whether a library row should put its cover `<img>` up now: the server has
 * not said "no artwork", and the image is not waiting out a retry delay. */
export function artworkFetchAllowed(stableId: string, retry: ArtworkRetry = artworkRetry()): boolean {
	return shouldFetchArtwork(stableId) && !retry.hidden(stableId);
}

/** Append the retry cache-buster to a cover URL after a failure. */
export function artworkRetrySrc(url: string, stableId: string, retry: ArtworkRetry = artworkRetry()): string {
	const version = retry.version(stableId);
	return version === '' ? url : `${url}${url.includes('?') ? '&' : '?'}r=${version}`;
}
