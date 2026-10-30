/**
 * The pixel snap grid follows the window's density: moving to a monitor of
 * another density (or zooming) re-reads devicePixelRatio, and keeps doing so
 * for every later change, not just the first.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

test('devicePixelRatioNow follows every density change, not only the first', async () => {
	const listeners = [];
	const queries = [];
	const win = {
		devicePixelRatio: 1,
		matchMedia(query) {
			queries.push(query);
			return { addEventListener: (type, fn, opts) => listeners.push({ type, fn, once: opts?.once === true }) };
		}
	};
	const prior = globalThis.window;
	globalThis.window = win;
	try {
		const { devicePixelRatioNow, startDevicePixelRatioWatch } = await loadTypeScriptModule('src/lib/ui/device-pixel-ratio.svelte.ts');
		assert.equal(devicePixelRatioNow(), 1);
		assert.deepEqual(queries, [], 'a read alone arms nothing: it is a pure read');
		startDevicePixelRatioWatch();
		assert.deepEqual(queries, ['(resolution: 1dppx)']);
		startDevicePixelRatioWatch();
		assert.equal(listeners.length, 1, 'a second start must not add another listener');

		win.devicePixelRatio = 2;
		listeners.at(-1).fn();
		assert.equal(devicePixelRatioNow(), 2);
		assert.equal(queries.at(-1), '(resolution: 2dppx)', 're-armed on the new density');

		win.devicePixelRatio = 1.5;
		listeners.at(-1).fn();
		assert.equal(devicePixelRatioNow(), 1.5);
		assert.ok(listeners.every((l) => l.type === 'change' && l.once), 'each listener fires once, then re-arms');
	} finally {
		globalThis.window = prior;
	}
});

test('a zero, NaN or missing browser ratio throws instead of passing for 1x', async () => {
	const { readDevicePixelRatio } = await loadTypeScriptModule('src/lib/ui/device-pixel-ratio.svelte.ts');
	for (const ratio of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, undefined]) {
		assert.throws(() => readDevicePixelRatio({ devicePixelRatio: ratio }), RangeError, `ratio ${ratio}`);
	}
	// Control: real densities pass through unchanged, fractional ones included.
	for (const ratio of [1, 1.25, 2, 3]) assert.equal(readDevicePixelRatio({ devicePixelRatio: ratio }), ratio);
});

test('server rendering reports the SSR ratio without touching a window', async () => {
	const prior = globalThis.window;
	delete globalThis.window;
	try {
		const mod = await loadTypeScriptModule('src/lib/ui/device-pixel-ratio.svelte.ts?ssr');
		mod.startDevicePixelRatioWatch();
		assert.equal(mod.devicePixelRatioNow(), mod.SSR_DEVICE_PIXEL_RATIO);
	} finally {
		globalThis.window = prior;
	}
});

test('a browser with no matchMedia throws on every start instead of silently never tracking', async () => {
	const prior = globalThis.window;
	const win = { devicePixelRatio: 2 };
	globalThis.window = win;
	try {
		const { devicePixelRatioNow, startDevicePixelRatioWatch } = await loadTypeScriptModule('src/lib/ui/device-pixel-ratio.svelte.ts?no-match-media');
		assert.throws(() => startDevicePixelRatioWatch(), /matchMedia is unavailable/);
		assert.throws(() => startDevicePixelRatioWatch(), /matchMedia is unavailable/, 'a second start must not find the watcher armed');
		// Control: once matchMedia exists, the same module arms and reads normally.
		const listeners = [];
		win.matchMedia = () => ({ addEventListener: (_type, fn) => listeners.push(fn) });
		startDevicePixelRatioWatch();
		assert.equal(devicePixelRatioNow(), 2);
		assert.equal(listeners.length, 1);
	} finally {
		globalThis.window = prior;
	}
});

for (const [file, binding] of [
	['src/lib/components/rb/deck/StripWaveform.svelte', 'const posPct'],
	['src/lib/components/rb/deck/JogDial.svelte', 'const tickAngle']
]) {
	test(`${file.split('/').pop()} starts the density watch at init, before ${binding} reads it`, () => {
		const src = readFileSync(new URL(`../../${file}`, import.meta.url), 'utf8');
		const start = src.search(/^\tstartDevicePixelRatioWatch\(\);$/m);
		const read = src.indexOf(binding);
		assert.ok(start >= 0, 'startDevicePixelRatioWatch() is not called at the top level of the component script');
		assert.ok(read > start, `the watch must start before ${binding} is declared`);
	});
}

// [if] the density changes between module load and the first watch start
//   [then] the start reads the live ratio, publishes it and arms the query on
//   it - broken otherwise (the query is armed on the stale load-time ratio, never
//   matches, and the first playhead uses the old grid; Sol P1 on #8046)
test('a density change before the watch starts is read live at start, and the query is armed on it', async () => {
	const queries = [];
	const listeners = [];
	const win = {
		devicePixelRatio: 1,
		matchMedia(query) {
			queries.push(query);
			return { addEventListener: (_type, fn) => listeners.push(fn) };
		}
	};
	const prior = globalThis.window;
	globalThis.window = win;
	try {
		const { devicePixelRatioNow, startDevicePixelRatioWatch } = await loadTypeScriptModule('src/lib/ui/device-pixel-ratio.svelte.ts?late-start');
		assert.equal(devicePixelRatioNow(), 1, 'precondition: the load-time ratio');
		win.devicePixelRatio = 2;
		startDevicePixelRatioWatch();
		assert.equal(devicePixelRatioNow(), 2, 'the start kept the stale load-time ratio');
		assert.deepEqual(queries, ['(resolution: 2dppx)'], 'the query was armed on a ratio the window had left');
		// Every re-arm takes the same live path: the value read is the value armed.
		win.devicePixelRatio = 1.5;
		listeners.at(-1)();
		assert.equal(devicePixelRatioNow(), 1.5);
		assert.equal(queries.at(-1), '(resolution: 1.5dppx)');
	} finally {
		globalThis.window = prior;
	}
});

test('an invalid live ratio at start throws and leaves the watcher unarmed for the next start', async () => {
	const queries = [];
	const win = {
		devicePixelRatio: 2,
		matchMedia(query) {
			queries.push(query);
			return { addEventListener: () => {} };
		}
	};
	const prior = globalThis.window;
	globalThis.window = win;
	try {
		const { devicePixelRatioNow, startDevicePixelRatioWatch } = await loadTypeScriptModule('src/lib/ui/device-pixel-ratio.svelte.ts?bad-start');
		win.devicePixelRatio = 0;
		assert.throws(() => startDevicePixelRatioWatch(), RangeError);
		assert.deepEqual(queries, [], 'a query was armed on an invalid ratio');
		win.devicePixelRatio = 3;
		startDevicePixelRatioWatch();
		assert.equal(devicePixelRatioNow(), 3, 'the throw left the watcher marked armed, so the retry did nothing');
		assert.deepEqual(queries, ['(resolution: 3dppx)']);
	} finally {
		globalThis.window = prior;
	}
});
