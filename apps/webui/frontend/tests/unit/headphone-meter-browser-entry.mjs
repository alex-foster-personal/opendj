// Browser entry for headphone-signal-meter-gate.test.mjs: the production
// headphone module and the mixer state it publishes into, bundled TOGETHER so
// the page reads the very `mixerState` instance the meters write. Not a test
// file (no `.test.` in the name), and nothing here replaces production code.
export {
	acquireHeadphoneSignalMeters,
	disposeHeadphoneMonitor,
	ensureHeadphoneGraph,
	refreshHeadphoneSignals,
	SIGNAL_METER_INTERVAL_MS
} from '$lib/player/headphones';
export { mixerState } from '$lib/player/state.svelte';
