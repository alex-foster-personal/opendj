/**
 * Warm the shared /anlz cache for the track a deck's overview strip shows
 * (LIBUX-20), from StripWaveform's `$effect`.
 *
 * `ensureAnlzPrefetch` READS the track's cache entry and, on a hit, WRITES it
 * back (`retouchAnlzReadyEntry` swaps in a copy carrying a fresh LRU stamp).
 * Called tracked from inside an effect, that read subscribes the effect to the
 * very entry the call then replaces, so every run schedules the next one:
 * Svelte aborts with `effect_update_depth_exceeded`, and every effect waiting
 * behind it in that flush is dropped. PR #4011's e2e gate showed exactly that
 * - the deck strip looped the moment a deck held an already-cached track, the
 * wavestack's vocal-area control and the AutoPlay stall banner never
 * rendered, and the ui-mirror publish stalled 7-11 s at a time.
 *
 * Untracked, the caller's effect depends only on the stable_id it passes, so
 * it warms the cache once per track change. The strip still repaints when the
 * entry lands, because it reads the entry through its own `$derived`.
 */
import { untrack } from 'svelte';

import { ensureAnlzPrefetch } from '$lib/components/rb/wave/anlz-cache.svelte';

export function prefetchDeckStripAnlz(stableId: string | null): void {
	if (stableId === null) return;
	untrack(() => ensureAnlzPrefetch(stableId));
}
