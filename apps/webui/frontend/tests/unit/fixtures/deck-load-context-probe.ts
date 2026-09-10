/**
 * One bundle holding the deck-load context writer AND the ring it writes into.
 *
 * Not a convenience. `loadTypeScriptModule` bundles each entry point
 * separately, so loading `deck-load-context.ts` and `perf-event-log.ts` as two
 * entries would give the test a DIFFERENT copy of the ring from the one the
 * code under test appends to -- `readPerfEvents()` would return an empty array
 * and every assertion about a row would read as "no row was written", which is
 * the false-negative shape .claude/rules/verification.md warns about. One
 * entry point, one module instance each, one ring.
 */
export { beginDeckLoad, recordDeckLoad } from '$lib/rb/deck-load-context';
export { startMachinePressurePolling } from '$lib/rb/machine-pressure';
export { readPerfEvents } from '$lib/rb/perf-event-log';
