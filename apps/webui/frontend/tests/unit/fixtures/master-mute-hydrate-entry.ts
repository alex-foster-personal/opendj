// One bundle for both modules so the hydrator and the test drive the SAME
// master-mute instance. Loaded separately, each bundle carries its own copy
// and a hydrate assertion reads a mute state the hydrator never touched.
export * as mute from '../../../src/lib/player/master-mute.svelte';
export * as prefsHydrate from '../../../src/lib/rb/prefs-hydrate';
