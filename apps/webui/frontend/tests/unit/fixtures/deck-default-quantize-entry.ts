/**
 * Shared-graph entry for deck-default-quantize.test.mjs: one bundle, so the
 * prefs singleton the setter writes is the one the deck store seeds from.
 */
export { _emptyDeckState, deckStates } from '$lib/player/state.svelte';
export { setDeckDefaultQuantize, uiPrefs } from '$lib/rb/prefs.svelte';
export { readSettingValue, applySettingChange } from '$lib/settings/apply';
