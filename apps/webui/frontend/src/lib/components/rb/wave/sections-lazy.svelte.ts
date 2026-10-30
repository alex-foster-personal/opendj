/**
 * STRUCT-05: on-demand loader for the section overlay painter (sections.ts).
 *
 * `show_sections` defaults OFF, and the browser preview strip, the main
 * waveform painter (render.ts) and the deck strip all sit in the "/" static
 * import closure, so a static import of sections.ts made every library
 * first paint download the label tables and band painter that most users
 * never switch on (bundle budget `library`, scripts/bundle-budget.mjs). The
 * painter is fetched behind this dynamic import the first time a surface
 * asks for it with the pref on, and the module is cached for every later
 * call. Loading, backoff, retry and the reactive error: lazy-module.svelte.ts.
 */
import type * as SectionsModule from './sections';
import { lazyModule } from './lazy-module.svelte';

export type SectionsApi = typeof SectionsModule;

const _sections = lazyModule<SectionsApi>(() => import('./sections'), 'section overlay: painter module failed to load');

/** Why the section painter failed to load, or null when it has not failed
 * (or has since loaded). Reactive: Show sections must not look like a track
 * with no sections while its painter cannot load. */
export const sectionsLoadError = _sections.loadError;

/** Fetch (once) and cache the section painter module. A failed fetch clears
 * the pending promise so a later call retries, and rejects loudly. */
export const loadSectionsApi = _sections.load;

/** The section painter when `want` is true and the module has loaded; null
 * otherwise. With `want` false nothing is fetched. */
export const sectionsApiFor = _sections.apiFor;
