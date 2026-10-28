/**
 * The seam between the typed performance dispatcher and the "Separate this
 * track" store (STEM-77), kept tiny on purpose.
 *
 * performance-ipc.svelte.ts is in the initial load of "/", and the separation
 * store pulls in the jobs store and the stems job client (about 4.4 KB gzip,
 * measured on #6819). Nothing on "/" separates stems, so the dispatcher does
 * not import the store, and neither does the deck (on /performance the same
 * bytes count against that route's budget): this port loads it on demand
 * when a deck first shows NO STEMS or a `stem_separate` command arrives, and
 * keeps it once loaded. The store does not import this port, so the two do
 * not form an import cycle.
 */
import { rustCommandUnsupported } from '$lib/audio-engine/rust-mode.svelte';
import type { StemSeparationSnapshot, stemSeparate } from './stem-separate.svelte';
import { stemStatusView } from './stem-status';
import type { StemDeckState } from './stem-types';

export type { StemSeparationSnapshot };

/** The separation store's public face (its class is module-private). */
export type StemSeparationPort = typeof stemSeparate;

let registered: StemSeparationPort | null = null;
let loadError: string | null = null;

/** The separation store if it has been loaded, else null (nothing is loaded by asking). */
export function loadedStemSeparation(): StemSeparationPort | null {
	return registered;
}

/** Why the last load of the store's chunk failed, or null: the deck's
 * refusal and the `stem_separation` query both read this one text. */
export function stemSeparationLoadError(): string | null {
	return loadError;
}

/** The separation store, loading its chunk first if no deck has yet. A failed
 * load is recorded (stemSeparationLoadError) and rejects with that text; the
 * next call tries again. */
export async function stemSeparation(): Promise<StemSeparationPort> {
	if (registered !== null) return registered;
	try {
		const store = (await import('./stem-separate.svelte')).stemSeparate;
		registered = store;
		loadError = null;
		return store;
	} catch (exc) {
		loadError = `the stem separation code could not be loaded (${exc instanceof Error ? exc.message : String(exc)})`;
		throw new Error(loadError);
	}
}

/** A deck's separation state for the `stem_separation` query, read from the
 * store only once something has loaded it: a query never loads it. A load
 * that FAILED is reported as the refusal the deck's button shows. */
export function stemSeparationSnapshot(deck: number, stableId: string | null): StemSeparationSnapshot | null {
	if (stableId === null) return null;
	if (rustCommandUnsupported('stem_separate')) {
		// The Rust engine refuses `stem_separate` and the deck's button is
		// inert there, so the query reports that refusal too (Codex on #6819).
		const base = registered?.snapshot(stableId, deck);
		return {
			stable_id: stableId,
			job: base?.job ?? null,
			starting: false,
			enqueue_error: null,
			refusal: RUST_SEPARATION_REFUSAL,
			checking: false,
			bundle_checked: base?.bundle_checked ?? null
		};
	}
	if (registered !== null) return registered.snapshot(stableId, deck);
	if (loadError === null) return null;
	return { stable_id: stableId, job: null, starting: false, enqueue_error: null, refusal: loadError, checking: false, bundle_checked: null };
}

/** The refusal the `stem_separation` query reports in Rust engine mode. */
export const RUST_SEPARATION_REFUSAL = 'stem separation is not available with the Rust engine yet - see PARITY-TODO';

/** The `stem_separate` command: the checks and their messages live with the
 * store, out of the first load of "/". ``readDeck`` is read only once the
 * store has loaded, which can take a chunk fetch: a load of another track
 * that lands meanwhile must refuse the command rather than buy a separation
 * of the wrong track (Codex on #6819). ``expected`` is the track the command
 * is for, null when the deck held none when it was issued. */
export async function separateLoadedDeck(
	deck: number,
	expected: string | null,
	readDeck: () => { stable_id: string | null; stems: StemDeckState }
): Promise<void> {
	const store = await stemSeparation();
	const live = readDeck();
	if (expected !== null && live.stable_id !== expected) {
		throw new Error(`deck ${deck} no longer holds ${expected}; nothing was separated`);
	}
	await store.separateDeck(deck, expected, stemStatusView(live.stems).name);
}

/** The ended run a `stem_load` starting now on a deck holding `stableId` can
 * look for, or null (none ended, or the store is not loaded). Read BEFORE
 * the reload starts: a look that began while the job ran says nothing about
 * what it wrote after (Codex P1 on #6819). */
export function stemLoadRun(stableId: string | null): string | null {
	return stableId === null ? null : (registered?.endedRun(stableId) ?? null);
}

/**
 * The dispatcher's `stem_load` look, recorded as the deck button records its
 * own (stem-separate-probe.ts lookForBundle): `retryStems` returns once the
 * reload has STARTED, so the look is noted only once the deck's stems leave
 * `loading`, and only if the deck still holds `stableId`. Noting it when
 * `retryStems` returned let an agent's `stem_separate` pass the load-first
 * check while an existing bundle was still loading, and buy a duplicate run
 * (Sol P1 on #6819). It is credited to `run` (stemLoadRun, read before the
 * reload began). Polls every `everyMs`: the dispatcher has no component
 * effect to wake it, and does not await this, so the deck queue never waits
 * out a decode. Resolves whether the deck still held the track.
 */
export function noteStemsLoadedOnceSettled(
	deck: number,
	stableId: string | null,
	run: string | null,
	live: () => { stable_id: string | null; stems: { status: string } },
	everyMs = 100
): Promise<boolean> {
	return new Promise((resolve) => {
		const look = (): void => {
			const now = live();
			if (now.stems.status === 'loading') {
				setTimeout(look, everyMs);
				return;
			}
			const holds = now.stable_id === stableId;
			if (holds && stableId !== null) registered?.noteStemsLoaded(deck, stableId, run);
			resolve(holds);
		};
		look();
	});
}
