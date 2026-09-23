/**
 * Single-bundle entry for the device output probe's lazy install (issue #923).
 * The instrumentation, the probe control module and the output-health store
 * must share one module graph, or the test would read a different copy of the
 * registration than the one the dynamic import writes.
 */
export {
	armAudioContextWatchdog,
	disarmContextInstrumentation
} from '$lib/rb/audio-context-instrumentation';
export { registeredDeviceOutputProbe } from '$lib/rb/device-output-probe-control';
export { audioOutputHealth } from '$lib/rb/audio-output-health.svelte';
