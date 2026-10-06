/**
 * Shared-graph entry point for boot-walk-settle-drain.test.mjs.
 *
 * loadTypeScriptModule bundles each entry independently, so loading the boot
 * hydration and the boot scheduler as two entries would give two schedulers,
 * and a test deferring work on one would prove nothing about the walk held on
 * the other. One entry, one singleton.
 */
export * from '../../../src/lib/rb/library-boot-hydration';
export { bootScheduler, BOOT_QUIET_MS, DECK_LOAD_YIELD_MAX_MS } from '../../../src/lib/rb/boot-scheduler';
