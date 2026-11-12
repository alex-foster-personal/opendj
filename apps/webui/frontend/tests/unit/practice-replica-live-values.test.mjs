// requirement: SKIN-9662
// The FLX4 practice replica shows what the engine publishes, never a fixed 0.5.
//
//   if CH 1 FADER at engine 0.8 shows anything but 0.8 then broken
//   if a continuous control's action has no engine scalar and it still renders a range input then broken
//   if HI 1 with a takeover ghost at 0.85 shows no ghost marker then broken
//   if deck 1 PLAY shows lit while the LED snapshot says off, or unlit while it says on, then broken
//   if deck 1 BEAT SYNC shows lit while the LED snapshot says off, or unlit while it says on, then broken
//   if an LED address the map declares is missing from the snapshot and that passes silently then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-flx4/panel.json');
const layout = JSON.parse(readFileSync(PANEL, 'utf8'));

let vite;
let bindings;
let live;
let FLX4_MAP;
let ssr;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: { alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') }, conditions: ['browser'] }
	});
	bindings = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-bindings.ts');
	live = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-live-values.ts');
	({ FLX4_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx4.ts'));
	ssr = await loadSvelteSsrModule(
		[
			"export { default as DeviceReplica } from '$lib/components/rb/practice/DeviceReplica.svelte';",
			"export { FLX4_MAP } from '$lib/rb/midi/maps/ddj-flx4';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

after(async () => {
	await vite.close();
	delete globalThis.window;
});

const control = (id) => {
	const c = layout.controls.find((x) => x.id === id);
	assert.ok(c, `panel has ${id}`);
	return c;
};
const resolved = (id) => bindings.resolveControl(FLX4_MAP, control(id));

/** Recorded engine state: every LED address the map declares, off unless named. */
function readers({ scalars = {}, ghosts = {}, ledsOn = [] } = {}) {
	const leds = [];
	const seen = new Set();
	for (const rule of FLX4_MAP.leds) {
		const key = `${rule.out.ch}:${rule.out.note}`;
		if (seen.has(key)) continue;
		seen.add(key);
		leds.push({
			ch: rule.out.ch,
			note: rule.out.note,
			velocity: ledsOn.includes(key) ? rule.out.velocityOn : rule.out.velocityOff
		});
	}
	return {
		scalar: (action) => {
			const k = JSON.stringify(action);
			return k in scalars ? scalars[k] : null;
		},
		ghost: (fid) => ghosts[fid] ?? null,
		leds
	};
}
const actionOf = (id) => JSON.stringify(resolved(id).binding.action);
const wireKey = (id) => {
	const r = resolved(id);
	return `${r.wire.ch}:${r.wire.id}`;
};

test('a bound fader shows the engine scalar, clamped to 0..1', () => {
	const read = readers({
		scalars: { [actionOf('ch1-ch-fader')]: 0.8, [actionOf('crossfader')]: 1.4 }
	});
	assert.deepEqual(live.controlLive(resolved('ch1-ch-fader'), FLX4_MAP, read), {
		kind: 'value',
		value: 0.8,
		ghost: null
	});
	assert.equal(live.controlLive(resolved('crossfader'), FLX4_MAP, read).value, 1);
});

test('a continuous control with no engine scalar is no-source, not a guessed value', () => {
	assert.deepEqual(live.controlLive(resolved('ch1-ch-fader'), FLX4_MAP, readers()), {
		kind: 'no-source'
	});
});

test('a takeover ghost carries the last hardware position', () => {
	const read = readers({
		scalars: { [actionOf('ch1-hi')]: 0.5 },
		ghosts: { 'mixer:1:eq:high': { value: 0.85, target: 0.5 } }
	});
	assert.deepEqual(live.controlLive(resolved('ch1-hi'), FLX4_MAP, read), {
		kind: 'value',
		value: 0.5,
		ghost: 0.85
	});
});

test('PLAY and BEAT SYNC mirror the LED snapshot both ways', () => {
	const on = readers({ ledsOn: [wireKey('d1-play-pause'), wireKey('d1-beat-sync')] });
	assert.deepEqual(live.controlLive(resolved('d1-play-pause'), FLX4_MAP, on), {
		kind: 'lit',
		on: true
	});
	assert.deepEqual(live.controlLive(resolved('d1-play-pause'), FLX4_MAP, readers()), {
		kind: 'lit',
		on: false
	});
	assert.deepEqual(live.controlLive(resolved('d1-beat-sync'), FLX4_MAP, on), {
		kind: 'lit',
		on: true
	});
	assert.deepEqual(live.controlLive(resolved('d1-beat-sync'), FLX4_MAP, readers()), {
		kind: 'lit',
		on: false
	});
});

test('a declared LED address missing from the snapshot throws', () => {
	const read = { ...readers(), leds: [] };
	assert.throws(
		() => live.controlLive(resolved('d1-play-pause'), FLX4_MAP, read),
		/LED snapshot has no entry/
	);
});

test('every LED rule in FLX4_MAP lands on a panel control (pads, PLAY, CUE, CH CUE)', () => {
	const all = bindings.resolveLayout(FLX4_MAP, layout);
	const shown = live.liveByControl(all, FLX4_MAP, readers());
	const litCapable = [...shown.entries()].filter(([, s]) => s.kind === 'lit').map(([id]) => id);
	for (const id of ['d1-play-pause', 'd1-cue', 'ch1-ch-cue', 'd1-pad-1', 'd2-pad-8'])
		assert.ok(litCapable.includes(id), id);
	assert.ok(litCapable.length >= 22, `lit-capable ${litCapable.length}`);
});

//----- the component, rendered (svelte/server)

const strip = (html) => html.replace(/<!--[\s\S]*?-->/g, '');
function renderReplica(state) {
	const read = readers(state);
	return strip(
		ssr.render(ssr.DeviceReplica, { props: { layout, map: ssr.FLX4_MAP, readLive: () => read } })
			.body
	);
}
const cell = (html, id) => {
	const m = html.match(
		new RegExp(`<div[^>]*data-control-id="${id}"[\\s\\S]*?(?=<div[^>]*data-control-id=|$)`)
	);
	assert.ok(m, `rendered ${id}`);
	return m[0];
};

test('rendered: fader input carries the engine value, never 0.5', () => {
	const html = renderReplica({
		scalars: { [actionOf('ch1-ch-fader')]: 0.8, [actionOf('ch2-ch-fader')]: 0.25 }
	});
	assert.match(cell(html, 'ch1-ch-fader'), /value="0\.8"/);
	assert.match(cell(html, 'ch2-ch-fader'), /value="0\.25"/);
});

test('rendered: no-source control is inert with the PARITY-TODO title and no input', () => {
	const c = cell(renderReplica(), 'ch1-ch-fader');
	assert.match(c, /class="[^"]*\binert\b/);
	assert.match(c, /not implemented - see PARITY-TODO \(no engine value to show\)/);
	assert.doesNotMatch(c, /<input/);
});

test('rendered: ghost marker appears only when the hardware differs', () => {
	const scalars = { [actionOf('ch1-hi')]: 0.5 };
	assert.match(
		cell(
			renderReplica({ scalars, ghosts: { 'mixer:1:eq:high': { value: 0.85, target: 0.5 } } }),
			'ch1-hi'
		),
		/class="ghost[^"]*"[^>]*--g:0\.85/
	);
	assert.doesNotMatch(cell(renderReplica({ scalars }), 'ch1-hi'), /class="ghost/);
});

test('rendered: PLAY and BEAT SYNC lit from the snapshot', () => {
	const on = renderReplica({ ledsOn: [wireKey('d1-play-pause'), wireKey('d1-beat-sync')] });
	const off = renderReplica();
	for (const id of ['d1-play-pause', 'd1-beat-sync']) {
		assert.match(cell(on, id), /data-lit="true"/);
		assert.match(cell(off, id), /data-lit="false"/);
	}
});

test('perf: one full-panel pass over 77 controls', () => {
	const all = bindings.resolveLayout(FLX4_MAP, layout);
	const read = readers({ scalars: { [actionOf('ch1-ch-fader')]: 0.8 } });
	for (let i = 0; i < 200; i++) live.liveByControl(all, FLX4_MAP, read);
	const n = 2000;
	const t0 = performance.now();
	for (let i = 0; i < n; i++) live.liveByControl(all, FLX4_MAP, read);
	const us = ((performance.now() - t0) / n) * 1000;
	console.log(`[perf] liveByControl 77 controls: ${us.toFixed(1)} us per pass`);
	assert.ok(us < 1000, `${us} us per pass`);
});
