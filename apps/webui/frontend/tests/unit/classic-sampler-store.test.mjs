// requirement: SAMPLER-14633
// requirement: SAMPLER-14677
// requirement: SAMPLER-14733
/**
 * Issue #9987: the classic sampler store loads and saves the banks and fires one-shots through
 * the edge sampler's engine seam (one engine, two surfaces).
 *
 * Regression lines:
 * - if the banks load then the unit holds the stored slots
 * - if a slot is loaded then the banks are PUT and the server answer becomes the unit
 * - if a sounding slot is retriggered then the engine stops it and starts it again
 * - if an empty slot fires then the reason comes back and the engine is not called
 * - if no engine is registered then a fire rejects instead of playing nothing silently
 * - if the edge overlay registers the engine then a classic press plays through it, no second registration
 * - if a sounding loop pad is pressed then the engine stops it and does not restart it
 * - if a gate is pressed twice and released once then the engine stops it, no voice hangs
 * - if modes and gains are edited then one debounced PUT carries them and the voice gets the level
 *
 * The fetch replays the documented GET/PUT answer shapes (PR #14057); the engine is a recorder
 * implementing the SamplerEngine seam.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = "export * from '$lib/player/sampler/classic-sampler.svelte';";
const emptyView = () => ({
	v: 1,
	active_bank: 0,
	banks: Array.from({ length: 4 }, () => Array(16).fill(null))
});

/** A fetch that answers GET with `stored` and PUT by echoing the body as a view. */
function banksServer(stored) {
	const calls = [];
	const fetchFn = async (url, init) => {
		calls.push({ url, method: init.method, body: init.body });
		if (init.method === 'PUT') {
			const doc = JSON.parse(init.body);
			stored = {
				...doc,
				banks: doc.banks.map((b) => b.map((s) => (s === null ? null : { ...s, missing: false })))
			};
		}
		return new Response(JSON.stringify(stored), { status: 200 });
	};
	return { calls, fetchFn };
}

function engineRecorder(sounding = new Set()) {
	const calls = [];
	return {
		calls,
		engine: {
			toggle: async (slot) => void calls.push(['toggle', slot.slot_id, slot.settings.mode]),
			stop: (id) => void calls.push(['stop', id]),
			preview: async () => undefined,
			stopPreview: () => undefined,
			warm: () => undefined,
			phase: (id) => (sounding.has(id) ? 'playing' : 'free'),
			playhead: () => null
		}
	};
}

test('banks load into the unit and a slot load writes through', async () => {
	/** [if] the banks load and a track is loaded into A1 [then] one GET, one PUT holding it, unit updated, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	const stored = emptyView();
	stored.banks[1][0] = { path: '/b.wav', mode: 'loop', gain_db: -6, title: 'b', missing: false };
	const server = banksServer(stored);
	S.setClassicSamplerPorts({ fetchFn: server.fetchFn });
	await S.loadClassicBanks();
	assert.equal(S.classicSampler.loaded, true);
	assert.equal(S.classicSampler.unit.banks[1][0].title, 'b');
	await S.loadClassicSlot(
		{ bank: 0, slot: 0 },
		{ stable_id: 't1', title: 'One', duration_ms: 2000 }
	);
	assert.deepEqual(
		server.calls.map((c) => c.method),
		['GET', 'PUT']
	);
	const put = JSON.parse(server.calls[1].body);
	assert.deepEqual(put.banks[0][0], {
		stable_id: 't1',
		mode: 'one_shot',
		gain_db: 0,
		title: 'One'
	});
	assert.deepEqual(put.banks[1][0], { path: '/b.wav', mode: 'loop', gain_db: -6, title: 'b' });
	assert.equal(S.classicSampler.unit.banks[0][0].duration_ms, 2000);
});

test('a retrigger restarts the slot through the shared engine; an empty slot is refused', async () => {
	/** [if] a sounding slot fires and an empty slot fires [then] stop then one-shot toggle, and a reason only, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	const server = banksServer(emptyView());
	const id = `classic:A1:${encodeURIComponent('track:t1')}`;
	const rec = engineRecorder(new Set([id]));
	S.setClassicSamplerPorts({ fetchFn: server.fetchFn, engine: rec.engine });
	await S.loadClassicSlot(
		{ bank: 0, slot: 0 },
		{ stable_id: 't1', title: 'One', duration_ms: 2000 }
	);
	assert.deepEqual(await S.pressClassicSlot({ bank: 0, slot: 0 }), { ok: true, action: 'restart' });
	assert.deepEqual(rec.calls, [
		['stop', id],
		['toggle', id, 'one_shot']
	]);
	assert.deepEqual(await S.pressClassicSlot({ bank: 0, slot: 1 }), {
		ok: false,
		reason: 'bank A slot 2 is empty'
	});
	assert.equal(rec.calls.length, 2);
	S.stopClassicSlots();
	assert.deepEqual(rec.calls.at(-1), ['stop', id]);
});

test('with no engine registered a fire rejects loudly', async () => {
	/** [if] a loaded slot fires before any engine registers [then] it rejects with the no-engine error, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	S.setClassicSamplerPorts({ fetchFn: banksServer(emptyView()).fetchFn });
	await S.loadClassicSlot({ bank: 0, slot: 0 }, { path: '/x.wav', duration_ms: 500 });
	await assert.rejects(S.pressClassicSlot({ bank: 0, slot: 0 }), /has no audio engine yet/);
});

const SHARED_ENTRY = `${ENTRY}
export { registerSamplerEngine } from '$lib/player/sampler/sampler-store.svelte';
export { NO_ENGINE } from '$lib/player/sampler/sampler-engine-seam';`;

test('a classic press plays through the engine the edge store registered', async () => {
	/** [if] the engine registers through registerSamplerEngine [then] a classic press toggles it; NO_ENGINE rejects again, [else stop]. */
	const S = await loadRuneModule(SHARED_ENTRY);
	const rec = engineRecorder();
	S.setClassicSamplerPorts({ fetchFn: banksServer(emptyView()).fetchFn });
	await S.loadClassicSlot({ bank: 0, slot: 0 }, { path: '/x.wav', duration_ms: 500 });
	S.registerSamplerEngine(rec.engine);
	const id = `classic:A1:${encodeURIComponent('file:/x.wav')}`;
	assert.deepEqual(await S.pressClassicSlot({ bank: 0, slot: 0 }), { ok: true, action: 'start' });
	assert.deepEqual(rec.calls, [['toggle', id, 'one_shot']]);
	S.registerSamplerEngine(S.NO_ENGINE);
	await assert.rejects(S.pressClassicSlot({ bank: 0, slot: 0 }), /has no audio engine yet/);
	assert.equal(rec.calls.length, 1);
});

test('loop toggles off on a press; a retriggered gate stops on its one release', async () => {
	/** [if] a sounding loop is pressed and a gate pressed twice then released [then] stop only; toggles; final stop, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	const id = `classic:A1:${encodeURIComponent('file:/l.wav')}`;
	const sounding = new Set();
	const rec = engineRecorder(sounding);
	S.setClassicSamplerPorts({ fetchFn: banksServer(emptyView()).fetchFn, engine: rec.engine });
	await S.loadClassicSlot({ bank: 0, slot: 0 }, { path: '/l.wav', duration_ms: 1000 });
	const a = { bank: 0, slot: 0 };
	S.setClassicSlotMode(a, 'loop');
	assert.deepEqual(await S.pressClassicSlot(a), { ok: true, action: 'start' });
	sounding.add(id);
	assert.deepEqual(await S.pressClassicSlot(a), { ok: true, action: 'stop' });
	S.releaseClassicSlot(a);
	assert.deepEqual(rec.calls, [
		['toggle', id, 'loop'],
		['stop', id]
	]);
	S.setClassicSlotMode(a, 'gate');
	rec.calls.length = 0;
	assert.deepEqual(await S.pressClassicSlot(a), { ok: true, action: 'restart' });
	await S.pressClassicSlot(a);
	S.releaseClassicSlot(a);
	assert.deepEqual(rec.calls, [
		['stop', id],
		['toggle', id, 'gate'],
		['stop', id],
		['toggle', id, 'gate'],
		['stop', id]
	]);
	await S.flushClassicBanks();
});

test('mode and gain edits save once, debounced, and the voice plays at the combined level', async () => {
	/** [if] mode and two gains are edited then 600 ms pass [then] one PUT holding them, voice level at slot+master, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	const server = banksServer(emptyView());
	const levels = [];
	const rec = engineRecorder();
	rec.engine.toggle = async (slot) => void levels.push(slot.settings.level);
	S.setClassicSamplerPorts({ fetchFn: server.fetchFn, engine: rec.engine });
	const a = { bank: 1, slot: 4 };
	await S.loadClassicSlot(a, { stable_id: 't2', duration_ms: 1500 });
	S.setClassicSlotMode(a, 'loop');
	S.setClassicSlotGain(a, -3);
	S.setClassicSlotGain(a, -6);
	S.setClassicMasterGain(-6);
	assert.throws(() => S.setClassicMasterGain(3), /master gain 3 dB/);
	assert.equal(server.calls.length, 1);
	await new Promise((r) => setTimeout(r, S.BANKS_SAVE_DEBOUNCE_MS + 100));
	assert.deepEqual(
		server.calls.map((c) => c.method),
		['PUT', 'PUT']
	);
	const saved = JSON.parse(server.calls[1].body).banks[1][4];
	assert.deepEqual([saved.mode, saved.gain_db], ['loop', -6]);
	assert.equal(S.classicSampler.save_error, null);
	await S.pressClassicSlot(a);
	assert.ok(Math.abs(levels[0] - 100 * 10 ** (-12 / 20)) < 1e-9);
});
