/**
 * DECKUX-42: on-demand loader for the Gothic block snapper (block-layout.ts).
 *
 * render.ts sits in the "/" first-paint closure, and the library bundle has
 * no headroom for code only a Gothic deck row uses (scripts/bundle-budget.mjs).
 * The snapper is fetched the first time a 'blocks' row asks for it. Until it
 * lands the row paints the fixed 3 px pitch, then rebuilds snapped. A failed
 * fetch is surfaced (blockSnapLoadError, shown under the Settings waveform
 * preview with a Reload button, since the browser can keep a failed module
 * fetch for the page's life, CHROME-07) and retried with backoff:
 * lazy-module.svelte.ts, shared with the section overlay.
 */
import type * as BlockLayoutModule from './block-layout';
import { lazyModule } from './lazy-module.svelte';

export type BlockSnapApi = typeof BlockLayoutModule;

const _snap = lazyModule<BlockSnapApi>(
	() => import('./block-layout'),
	'Gothic block snapping failed to load'
);

/** Why the snapper failed to load, or null. Reactive. */
export const blockSnapLoadError = _snap.loadError;
/** Fetch (once) and cache the snapper. */
export const loadBlockSnapApi = _snap.load;
/** The snapper when `want` is true and it has loaded, else null. */
export const blockSnapApiFor = _snap.apiFor;
