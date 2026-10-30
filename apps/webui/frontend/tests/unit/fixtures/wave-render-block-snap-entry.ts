/**
 * Test entry: the main waveform painter bundled TOGETHER with the Gothic block
 * snapper's lazy loader, so a test drives the same loader instance render.ts
 * reads. Requirements: DECKUX-42.
 */
export * from '../../../src/lib/components/rb/wave/render';
export { loadBlockSnapApi, blockSnapApiFor, blockSnapLoadError } from '../../../src/lib/components/rb/wave/block-layout-lazy.svelte';
