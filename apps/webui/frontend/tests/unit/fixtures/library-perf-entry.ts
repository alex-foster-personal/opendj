/**
 * Shared-graph entry point for the library browse instrumentation tests.
 *
 * loadTypeScriptModule bundles each entry independently, so loading
 * library-perf.ts and perf-event-log.ts as two separate entries yields two
 * separate ring logs - a test that emits through one and reads the other
 * silently proves nothing.
 *
 * Re-exporting both from a single entry puts them in one bundle, so
 * readPerfEvents here reads the exact ring the emitters below write to.
 * No stubs: both modules are the real thing.
 */
export * from '$lib/rb/library-perf';
export { readPerfEvents } from '$lib/rb/perf-event-log';
