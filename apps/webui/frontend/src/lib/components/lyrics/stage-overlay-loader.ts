/**
 * Lazy loading for the STAGE lyric overlay, kept beside the component so the
 * root layout's wiring stays one import (the quality ratchet's fan-out gate).
 *
 * The overlay is a separate chunk (the library surface went over its gzip
 * budget after #4039). It draws nothing while closed and every Stage button
 * lives off the library page (the /performance top bar and the track page), so
 * keeping it off the library first paint changes nothing observable there.
 * /performance imports the component eagerly, so on that route the chunk
 * arrives with the page. Everywhere else it is warmed once the boot window
 * closes (prefetchStageOverlay, from the layout's onMount), so the first Stage
 * press draws without waiting on a fetch.
 *
 * A chunk that cannot be fetched is handed to the caller's reporter (the layout
 * raises an error toast, the pin shell's way) and the stage is closed, so the
 * next Stage press is a real open that imports and reports again instead of
 * leaving an invisible "open" stage that swallows every later press.
 */
import { bootScheduler } from '$lib/rb/boot-scheduler';
import { closeStage, stageState } from '$lib/lyrics/stage-store.svelte';

type StageOverlayModule = typeof import('./StageOverlay.svelte');
let stageOverlayModule: Promise<StageOverlayModule> | null = null;

/** Whether the root layout should mount the overlay at all. */
export function isStageOverlayOpen(): boolean {
	return stageState.open;
}

export function loadStageOverlay(
	reportLoadFailure: (error: unknown) => void
): Promise<StageOverlayModule> {
	if (stageOverlayModule === null) {
		const attempt = import('./StageOverlay.svelte');
		stageOverlayModule = attempt;
		attempt.catch((error: unknown) => {
			// Forget the failed attempt so the next open imports again and
			// reports again, rather than awaiting this rejection in silence.
			// Chromium keeps a failed module fetch in its module map (see the
			// setup overlay note in +layout.svelte), so there the re-import
			// rejects without a request and only a reload recovers.
			if (stageOverlayModule === attempt) stageOverlayModule = null;
			closeStage();
			reportLoadFailure(error);
		});
	}
	return stageOverlayModule;
}

export function prefetchStageOverlay(reportLoadFailure: (error: unknown) => void): void {
	bootScheduler.defer('stage-overlay:prefetch', () => void loadStageOverlay(reportLoadFailure));
}
