// requirement: SKIN-11741
// The practice drill is a pure state machine, and a real-controller press on the drilled
// device feeds it through the REAL webmidi dispatch (fake MIDIAccess, nothing in src stubbed).
//
//   if a target hit 1200 ms after start, after two other presses, is not reach 1200 misses 2 then broken
//   if a press after the hit changes the drill state then broken
//   if restart does not zero reach and misses and restart timing then broken
//   if a drill with no target or not started counts a press then broken
//   if an FLX4 note on d1-beat-sync's wire is not a hit while drilling d1-beat-sync then broken
//   if the same note is not a miss while drilling d1-play-pause then broken
//   if the same bytes from another device change the drill then broken
//   if a note-off or velocity-0 note counts as a press then broken
//   if one fader gesture counts as more than one miss then broken
//   if a press after unsubscribe changes the drill then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PANEL = resolve(FRONTEND_ROOT, '../../../tools/deck-diagrams/devices/ddj-flx4/panel.json');
const layout = JSON.parse(readFileSync(PANEL, 'utf8'));

let vite;
let drillMod;
let session;
let webmidi;
let FLX4_MAP;

function _fakeInput(id, name, manufacturer) {
	return { id, name, manufacturer, state: 'connected', onmidimessage: null };
}
const flx4In = _fakeInput('flx4-in', 'DDJ-FLX4', 'Pioneer DJ');
const mixIn = _fakeInput('mix-in', 'Mixtour', 'Reloop');
const fakeAccess = {
	inputs: new Map([
		[flx4In.id, flx4In],
		[mixIn.id, mixIn]
	]),
	outputs: new Map(),
	onstatechange: null
};

function wire(input, status, d1, d2) {
	assert.notEqual(input.onmidimessage, null, `${input.name} got no onmidimessage listener`);
	input.onmidimessage({ data: Uint8Array.of(status, d1, d2) });
}

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
	drillMod = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-drill.ts');
	session = await vite.ssrLoadModule('/src/lib/components/rb/practice/practice-session.ts');
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	({ FLX4_MAP } = await vite.ssrLoadModule('/src/lib/rb/midi/maps/ddj-flx4.ts'));
	const mixtour = await vite.ssrLoadModule('/src/lib/rb/midi/maps/reloop-mixtour.ts');
	const request = async () => fakeAccess;
	try {
		globalThis.navigator.requestMIDIAccess = request;
	} catch {
		Object.defineProperty(globalThis, 'navigator', {
			value: { requestMIDIAccess: request },
			configurable: true
		});
	}
	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(FLX4_MAP);
	webmidi.registerDeviceMap(mixtour.RELOOP_MIXTOUR_MAP);
	webmidi.registerActionHandler(() => {});
	await webmidi.initMidi();
});

after(async () => {
	webmidi?._resetMidiForTests();
	await vite?.close();
});

test('pure drill: reach, misses, ignored presses after the hit, restart', () => {
	const drill = drillMod.createDrill('t');
	drill.start(1000);
	drill.press('a', 1100);
	drill.press('b', 1500);
	const hit = drill.press('t', 2200);
	assert.equal(hit.reachMs, 1200);
	assert.equal(hit.misses, 2);
	assert.equal(drill.press('a', 3000), hit);
	assert.equal(drill.press('t', 3100), hit);
	assert.deepEqual(drill.state, hit);
	const restarted = drill.restart(5000);
	assert.deepEqual(restarted, { target: 't', reachMs: null, misses: 0, startedMs: 5000 });
	assert.equal(drill.press('t', 5400).reachMs, 400);
});

test('negative controls: no target or not started ignores presses', () => {
	const none = drillMod.createDrill(null);
	none.start(0);
	assert.deepEqual(none.press('x', 10), { target: null, reachMs: null, misses: 0, startedMs: 0 });
	const idle = drillMod.createDrill('t');
	assert.equal(idle.press('t', 10).reachMs, null);
	assert.equal(idle.state.misses, 0);
});

function _hardwareDrill(target) {
	const drill = drillMod.createDrill(target);
	drill.start(performance.now());
	const stop = session.watchDrillHardwareHits(layout, FLX4_MAP, webmidi.subscribeMidiInput, (id) =>
		drill.press(id, performance.now())
	);
	return { drill, stop };
}

test('hardware note on d1-beat-sync is a hit while drilling it', () => {
	const { drill, stop } = _hardwareDrill('d1-beat-sync');
	try {
		wire(flx4In, 0x80, 88, 0);
		wire(flx4In, 0x90, 88, 0);
		assert.equal(drill.state.reachMs, null, 'note-off / velocity-0 is not a press');
		wire(flx4In, 0x90, 88, 0x7f);
		assert.notEqual(drill.state.reachMs, null);
		assert.equal(drill.state.misses, 0);
	} finally {
		stop();
	}
});

test('hardware note on d1-beat-sync is a miss while drilling d1-play-pause; other devices and unsubscribe change nothing', () => {
	const { drill, stop } = _hardwareDrill('d1-play-pause');
	wire(mixIn, 0x90, 88, 0x7f);
	assert.equal(drill.state.misses, 0, 'another device changes nothing');
	wire(flx4In, 0x90, 88, 0x7f);
	assert.equal(drill.state.misses, 1);
	assert.equal(drill.state.reachMs, null);
	stop();
	wire(flx4In, 0x90, 88, 0x7f);
	assert.equal(drill.state.misses, 1, 'after unsubscribe nothing counts');
});

test('one fader gesture is one miss', () => {
	const fader = layout.controls.find((c) => c.midi.kind === 'cc' && c.type === 'fader');
	assert.ok(fader, 'FLX4 panel has a cc fader');
	const { drill, stop } = _hardwareDrill('d1-play-pause');
	try {
		const status = 0xb0 | (fader.midi.ch - 1);
		for (const v of [10, 20, 30, 40]) wire(flx4In, status, fader.midi.id, v);
		assert.equal(drill.state.misses, 1);
	} finally {
		stop();
	}
});
