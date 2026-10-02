/**
 * Shared-graph entry for the falling-edge tests: the reporter, the perf log it
 * writes to and the toast store it pushes into, in one bundle so a subscriber
 * sees the rows the reporter records.
 */
export {
	notePlayingFallingEdge,
	readPauseOrigin,
	withPauseOrigin
} from '$lib/rb/unexpected-pause-report';
export { subscribePerfEvents } from '$lib/rb/perf-event-log';
export { toasts } from '$lib/stores.svelte';
