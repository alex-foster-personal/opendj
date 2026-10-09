/**
 * Test entry: the main waveform painter bundled TOGETHER with the section
 * painter's lazy loader, so a test can await `loadSectionsApi()` on the same
 * module instance render.ts reads (each loadTypeScriptModule call is its own
 * bundle with its own loader state). Requirements: STRUCT-05.
 */
export * from '../../../src/lib/components/rb/wave/render';
export { loadSectionsApi, sectionsApiFor, sectionsLoadError } from '../../../src/lib/components/rb/wave/sections-lazy.svelte';
