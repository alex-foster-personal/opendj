/**
 * Test entry: the split-row painter bundled TOGETHER with the section
 * painter's lazy loader, so a test can await `loadSectionsApi()` on the same
 * module instance drawWaveRow reads. Requirements: STRUCT-05.
 */
export { paintSplitRow } from '../../../src/lib/components/rb/wave/split-row';
export { loadSectionsApi } from '../../../src/lib/components/rb/wave/sections-lazy.svelte';
