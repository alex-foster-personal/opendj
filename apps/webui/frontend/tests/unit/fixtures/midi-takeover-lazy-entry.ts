/**
 * One bundle for midi-lazy-engine.test.mjs: the takeover display leaf and the
 * WebMIDI runtime load with the bundle, while the takeover POLICY owner is
 * reached only through `loadTakeoverState()`, the same way the app reaches it
 * (on demand, with the rest of the MIDI engine). esbuild evaluates a module
 * that is only dynamically imported when that import runs, so a mode set
 * before the call is set before the policy exists. No stubs.
 */
export * as ui from '$lib/rb/midi/takeover-ui.svelte';
export * as webmidi from '$lib/rb/midi/webmidi.svelte';

export function loadTakeoverState(): Promise<typeof import('$lib/rb/midi/takeover-state.svelte')> {
	return import('$lib/rb/midi/takeover-state.svelte');
}
