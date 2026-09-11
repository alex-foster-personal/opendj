/**
 * Shared-graph entry point for the health-snapshot-vs-library-change tests.
 *
 * loadTypeScriptModule bundles each entry independently, so loading api.ts and
 * events-bus.ts as two separate entries would give api.ts's module-level bus
 * subscription a DIFFERENT events-bus singleton than the one the test drives
 * with a fake socket -- the wiring would look untestable when it is really
 * just split across two copies of the module.
 *
 * Re-exporting both from one entry puts them in one bundle, so the
 * `_kindListeners`/`_resyncListeners` registries here are the exact ones
 * api.ts's subscription writes into. No stubs: both modules are the real
 * thing, and the only seam the tests use is globalThis.fetch plus the bus's
 * own injected socketFactory/scheduler, which is what a real page load and
 * a real WS frame go through too.
 */
export { getHealth } from '$lib/api';
export * as eventsBus from '$lib/api/events-bus';
