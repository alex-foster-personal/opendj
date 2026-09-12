/**
 * Single-bundle entry for resumeAudioContextOrReportDead behavioural tests.
 * instrumentation, perf-event-log, and stores must share one module graph.
 */
export { resumeAudioContextOrReportDead } from '$lib/rb/audio-context-instrumentation';
export { readPerfEvents, resetPerfEventLog } from '$lib/rb/perf-event-log';
export { toasts } from '$lib/stores.svelte';
