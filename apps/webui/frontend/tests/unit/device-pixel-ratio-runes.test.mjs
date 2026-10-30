/**
 * devicePixelRatioNow() is read inside component `$derived`s (StripWaveform's
 * posPct, JogDial's tickAngle). Svelte forbids writing `$state` while a derived
 * evaluates (`state_unsafe_mutation`), so the first read must not assign the
 * ratio. The plain TypeScript loader stubs `$state` away and cannot see this;
 * this file runs the real Svelte runtime (Codex P1 on #8046).
 *
 * [if] the first read of the ratio happens inside a $derived [then] it returns
 *   the browser ratio and nothing throws - broken otherwise (playheads stop
 *   rendering on track load)
 * [if] the density changes afterwards [then] the derived follows it
 *   (control: a read-only module must still track changes)
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"import { devicePixelRatioNow, startDevicePixelRatioWatch } from '$lib/ui/device-pixel-ratio.svelte';",
	'export { startDevicePixelRatioWatch };',
	"import { flushSync } from 'svelte';",
	'export function watchDerived(): { seen: number[]; stop: () => void } {',
	'	const seen: number[] = [];',
	'	const stop = $effect.root(() => {',
	'		const doubled = $derived(devicePixelRatioNow() * 2);',
	'		$effect(() => {',
	'			seen.push(doubled);',
	'		});',
	'	});',
	'	flushSync();',
	'	return { seen, stop };',
	'}',
	'export { flushSync };'
].join('\n');

test('the first ratio read inside a $derived does not mutate state, and later changes still flow', async () => {
	const listeners = [];
	const prior = globalThis.window;
	globalThis.window = {
		devicePixelRatio: 2,
		matchMedia: () => ({ addEventListener: (_type, fn) => listeners.push(fn) })
	};
	try {
		const mod = await loadRuneModule(ENTRY);
		mod.startDevicePixelRatioWatch();
		const { seen, stop } = mod.watchDerived();
		assert.deepEqual(seen, [4], 'the derived did not read the browser ratio');
		globalThis.window.devicePixelRatio = 3;
		listeners.at(-1)();
		mod.flushSync();
		assert.deepEqual(seen, [4, 6], 'a density change did not reach the derived');
		stop();
	} finally {
		globalThis.window = prior;
	}
});

// [if] the density changes after component init but before any playhead is
//   read (every deck empty) [then] the first read sees the new density - broken
//   otherwise (the first playhead snaps to the old grid; Sol P1 on #8046)
test('a density change before the first read is not missed once the watch has started', async () => {
	const listeners = [];
	const prior = globalThis.window;
	globalThis.window = {
		devicePixelRatio: 1,
		matchMedia: () => ({ addEventListener: (_type, fn) => listeners.push(fn) })
	};
	try {
		const mod = await loadRuneModule(ENTRY);
		mod.startDevicePixelRatioWatch();
		globalThis.window.devicePixelRatio = 2;
		listeners.at(-1)();
		const { seen, stop } = mod.watchDerived();
		assert.deepEqual(seen, [4], 'the first read used the density from before the change');
		stop();
	} finally {
		globalThis.window = prior;
	}
});

// [if] the density changes after module load but before the watch starts [then]
//   the first derived read after start sees the live ratio (Sol P1 on #8046)
test('a density change before the watch starts reaches the first derived read', async () => {
	const prior = globalThis.window;
	globalThis.window = {
		devicePixelRatio: 1,
		matchMedia: () => ({ addEventListener: () => {} })
	};
	try {
		const mod = await loadRuneModule(ENTRY);
		globalThis.window.devicePixelRatio = 2;
		mod.startDevicePixelRatioWatch();
		const { seen, stop } = mod.watchDerived();
		assert.deepEqual(seen, [4], 'the first read used the load-time ratio');
		stop();
	} finally {
		globalThis.window = prior;
	}
});
