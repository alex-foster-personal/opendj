/**
 * The typed dispatcher, the master mute and Rust engine mode in one graph
 * (issue #12209): one bundle, so the test flips the mode the dispatcher reads
 * and sees the same mute the TopBar renders.
 */
export {
	installPerformanceBrowserIpc,
	queryPerformanceState,
	runPerformanceCommandFromUi
} from '$lib/rb/performance-ipc.svelte';
export { initRustMode, rustMode } from '$lib/audio-engine/rust-mode.svelte';
export { installRustEngineClientForTest } from '$lib/audio-engine/rust-engine';
export { EngineCommandError } from '$lib/audio-engine/client';
export { isMasterMuted } from '$lib/player/master-mute.svelte';
