/**
 * Live-transport watcher for the telemetry gates (OBS-02, OBS-06).
 *
 * `anyDeckPlaying()` reads rune state (`deckStates[*].playing|audible`), so
 * inside an effect it re-runs the moment a deck flips, in the microtask that
 * follows the write and before any timer can fire. That ordering is the
 * whole point: the session replay must stop BEFORE a flush timer can send a
 * segment during a live set, and a poll cannot promise that. Module-level
 * `$effect.root`, the shape auto-play.svelte.ts uses, with an explicit
 * teardown for the owner (app-init's deferred consent task).
 */

import { untrack } from 'svelte';

import { anyDeckPlaying } from './playing-gate';

/** Call `onChange(live)` on every change of the live read; returns the stop. */
export function watchLiveTransport(onChange: (live: boolean) => void): () => void {
	let last: boolean | null = null;
	return $effect.root(() => {
		$effect(() => {
			const live = anyDeckPlaying();
			if (live === last) return;
			last = live;
			untrack(() => onChange(live));
		});
	});
}
