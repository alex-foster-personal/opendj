// One bundle for the Track information state and the click selection it reads,
// so a test's click reaches the same mixerSelection the state consults.
export * from '../../../src/lib/rb/track-info-state.svelte';
export { clickSelect, mixerSelection, onDeckLoadStart } from '../../../src/lib/rb/mixer-selection.svelte';
