/**
 * STEMPERF-11663 entry: the stem controls, the stem processor class and the
 * performance command parser in one bundle, so `instanceof` holds across them.
 */
export { applyStemControl } from '$lib/rb/stem-engine-controls';
export {
	AlignedStemDeckProcessor,
	createDefaultStemControls,
	createStemPartBus,
	stemPartGains,
	stemPartSends
} from '$lib/rb/stem-graph';
export { _parseCommand } from '$lib/rb/performance-ipc-guards';
