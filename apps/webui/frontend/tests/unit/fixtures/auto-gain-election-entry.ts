/**
 * One bundle for the auto gain election check in auto-gain.test.mjs: master
 * election and the auto gain store must share one `deckAutoGain`.
 */
export { onAirGain } from '$lib/rb/master-election';
export { publishDeckAutoGain } from '$lib/rb/auto-gain.svelte';
