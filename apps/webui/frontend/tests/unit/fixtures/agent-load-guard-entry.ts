/** AGENT-8332/54: the production agent-order executor, the dispatcher it runs
 * through and the engine deck state, in one bundled module instance. */
export { executeAgentOrder } from '$lib/rb/agent-orders';
export { installPerformanceBrowserIpc, queryPerformanceState } from '$lib/rb/performance-ipc.svelte';
export { deckStates } from '$lib/rb/audio-engine.svelte';
export { DeckReplacementBlockedError } from '$lib/rb/audio-engine-guards';
export { deckPlayingRefusal, playingDeckReplaced } from '$lib/rb/agent-load-refusal';
