// PERF-LEAK-11789: the opt-in live handles probe behind the PERFPROBE-01 switch.
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let flag;
let probe;
let site;
let target;
let win;
let doc;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/live-handles-probe.ts');
	flag = await loadTypeScriptModule('src/lib/rb/frame-probe-flag.ts');
});

function fakeTarget() {
	let next = 1;
	const timeouts = new Map();
	const rafs = new Map();
	return {
		timeouts,
		rafs,
		setInterval: () => next++,
		clearInterval: () => {},
		setTimeout(fn, ms, ...args) {
			const id = next++;
			timeouts.set(id, { fn, args });
			return id;
		},
		clearTimeout: (id) => timeouts.delete(id),
		requestAnimationFrame(cb) {
			const id = next++;
			rafs.set(id, cb);
			return id;
		},
		cancelAnimationFrame: (id) => rafs.delete(id)
	};
}

function fakeEventTarget() {
	const handlers = [];
	return {
		handlers,
		addEventListener(type, l, opts) {
			handlers.push({ type, l, opts });
		},
		removeEventListener(type, l) {
			const i = handlers.findIndex((h) => h.type === type && h.l === l);
			if (i >= 0) handlers.splice(i, 1);
		},
		dispatch(type) {
			for (const h of handlers.filter((x) => x.type === type)) h.l({ type });
		}
	};
}

function fireAllRafs() {
	const pending = [...target.rafs.entries()];
	target.rafs.clear();
	for (const [, cb] of pending) cb(16);
}

beforeEach(() => {
	site = 'src/lib/rb/a.ts:1';
	target = fakeTarget();
	win = fakeEventTarget();
	doc = fakeEventTarget();
	probe = mod.installLiveHandlesProbe({ target, win, doc, captureSite: () => site });
});

afterEach(() => {
	probe.uninstall();
});

test('setInterval then clearInterval leaves intervals at 0', () => {
	const h = target.setInterval(() => {}, 1000);
	assert.equal(probe.totals().intervals, 1);
	target.clearInterval(h);
	assert.equal(probe.totals().intervals, 0);
});

test('two uncleared intervals from one site report by_site[site] = 2', () => {
	target.setInterval(() => {}, 1000);
	target.setInterval(() => {}, 2000);
	const snap = probe.snapshot();
	assert.equal(snap.intervals, 2);
	assert.equal(snap.by_site['src/lib/rb/a.ts:1'], 2);
});

test('clearTimeout also clears an interval handle', () => {
	const h = target.setInterval(() => {}, 1000);
	target.clearTimeout(h);
	assert.equal(probe.totals().intervals, 0);
});

test('only timeouts over 5 s count, and a fired one drops out with args and this kept', () => {
	target.setTimeout(() => {}, 5000);
	assert.equal(probe.totals().long_timeouts, 0);
	let seen = null;
	const id = target.setTimeout(
		function (a, b) {
			seen = [this, a, b];
		},
		5001,
		'x',
		'y'
	);
	assert.equal(probe.totals().long_timeouts, 1);
	const { fn, args } = target.timeouts.get(id);
	fn.apply('ctx', args);
	assert.deepEqual(seen, ['ctx', 'x', 'y']);
	assert.equal(probe.totals().long_timeouts, 0);
});

test('a remove with a different capture flag leaves the listener live (browser rule)', () => {
	const l = () => {};
	win.addEventListener('x', l, true);
	win.addEventListener('x', l, { capture: true });
	assert.equal(probe.totals().window_listeners, 1, 'duplicate add counts once');
	win.removeEventListener('x', l, false);
	assert.equal(probe.totals().window_listeners, 1);
	win.removeEventListener('x', l, { capture: true });
	assert.equal(probe.totals().window_listeners, 0);
	doc.addEventListener('y', l);
	assert.equal(probe.totals().window_listeners, 1, 'document listeners count too');
});

test('once listeners drop after firing; signals are honoured', () => {
	const l = () => {};
	win.addEventListener('x', l, { once: true });
	assert.equal(probe.totals().window_listeners, 1);
	win.dispatch('x');
	assert.equal(probe.totals().window_listeners, 0);

	const aborted = new AbortController();
	aborted.abort();
	win.addEventListener('y', l, { signal: aborted.signal });
	assert.equal(probe.totals().window_listeners, 0);

	const live = new AbortController();
	win.addEventListener('z', l, { signal: live.signal });
	assert.equal(probe.totals().window_listeners, 1);
	live.abort();
	assert.equal(probe.totals().window_listeners, 0);
});

test('a rAF loop shows as one pending entry by callback name', () => {
	let id = 0;
	function tick() {
		id = target.requestAnimationFrame(tick);
	}
	tick();
	for (let i = 0; i < 3; i += 1) {
		fireAllRafs();
		const snap = probe.snapshot();
		assert.equal(snap.rafs, 1);
		assert.equal(snap.by_site['raf:tick'], 1);
	}
	target.cancelAnimationFrame(id);
	assert.equal(probe.totals().rafs, 0);
});

test('window.__mdtLiveHandles returns the snapshot and uninstall restores identity', () => {
	const original = fakeTarget();
	const ow = fakeEventTarget();
	const before = {
		setInterval: original.setInterval,
		setTimeout: original.setTimeout,
		add: ow.addEventListener,
		remove: ow.removeEventListener
	};
	const p = mod.installLiveHandlesProbe({ target: original, win: ow, doc: null });
	assert.notEqual(original.setInterval, before.setInterval);
	original.setInterval(() => {}, 10);
	const snap = ow.__mdtLiveHandles();
	assert.deepEqual(Object.keys(snap).sort(), [
		'by_site',
		'intervals',
		'long_timeouts',
		'rafs',
		'window_listeners'
	]);
	assert.equal(snap.intervals, 1);
	assert.equal(mod.installLiveHandlesProbe({ target: original }), p, 'never wraps twice');
	p.uninstall();
	assert.equal(original.setInterval, before.setInterval);
	assert.equal(original.setTimeout, before.setTimeout);
	assert.equal(ow.addEventListener, before.add);
	assert.equal(ow.removeEventListener, before.remove);
	assert.equal(ow.__mdtLiveHandles, undefined);
});

test('siteFromStack trims to a repo-relative path:line past probe frames', () => {
	const v8 = [
		'Error',
		'    at Object.setInterval (http://localhost:5173/src/lib/rb/live-handles-probe.ts:10:3)',
		'    at start (http://localhost:5173/src/lib/rb/audio-health.svelte.ts?t=1:42:9)'
	].join('\n');
	assert.equal(mod.siteFromStack(v8), 'src/lib/rb/audio-health.svelte.ts:42');
	assert.equal(mod.siteFromStack('tick@http://h/src/lib/x.ts:7:3'), 'src/lib/x.ts:7');
	assert.equal(
		mod.siteFromStack('    at http://h/_app/immutable/chunks/x.js:1:200'),
		'_app/immutable/chunks/x.js:1'
	);
	assert.equal(mod.siteFromStack('garbage'), 'unknown');
	assert.equal(mod.siteFromStack(undefined), 'unknown');
});

test('flag off: nothing loads and no global is wrapped', async () => {
	const before = { setInterval: globalThis.setInterval, setTimeout: globalThis.setTimeout };
	let loads = 0;
	const stop = await flag.armLiveHandlesProbe('', { getItem: () => null }, () => {
		loads += 1;
		return Promise.resolve(mod);
	});
	assert.equal(stop, null);
	assert.equal(loads, 0);
	assert.equal(globalThis.setInterval, before.setInterval);
	assert.equal(globalThis.setTimeout, before.setTimeout);
});

test('flag on: globals are wrapped and stop restores identity', async () => {
	const before = globalThis.setInterval;
	const stop = await flag.armLiveHandlesProbe('?perfprobe=1', null, () => Promise.resolve(mod));
	assert.equal(typeof stop, 'function');
	assert.notEqual(globalThis.setInterval, before);
	const h = setInterval(() => {}, 60_000);
	clearInterval(h);
	stop();
	assert.equal(globalThis.setInterval, before);
});

test('siteFromStack skips probe frames in a hashed production chunk by script URL', () => {
	const probe = 'http://h/_app/immutable/chunks/CxYTUo_f.js';
	const stack = [
		'Error',
		`    at captureSite (${probe}:1:900)`,
		`    at wrapped (${probe}:1:1400)`,
		'    at tick (http://h/_app/immutable/chunks/Ab12Cd.js:3:50)'
	].join('\n');
	assert.equal(mod.siteFromStack(stack, probe), '_app/immutable/chunks/Ab12Cd.js:3');
	assert.equal(mod.siteFromStack(stack, null), '_app/immutable/chunks/CxYTUo_f.js:1');
});
