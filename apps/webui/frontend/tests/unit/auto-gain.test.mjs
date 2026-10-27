/**
 * Auto gain on the deck (AUTOGAIN-01..06): the server's answer is checked,
 * shown and applied at the trim stage, under the TRIM knob.
 *
 * Regression lines:
 * - if a malformed server answer is accepted instead of thrown then broken
 * - if a deck that is not `ready` applies anything but unity gain then broken
 * - if an unanalysed track does not read `AG N/A` then the user cannot tell it was not matched: broken
 * - if the user's own value is not marked as theirs then broken
 * - if publishing a deck's auto gain does not re-apply that deck's trim then a change never reaches the audio: broken
 * - if a failed read blocks the load instead of settling to the visible error state at 0 dB then broken
 * - if the trim stage stops multiplying in auto gain (graph build or knob move) then broken
 * - if the TS constants drift from apps/analysis_loudness/auto_gain.py then the slider can offer values the server refuses: broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://auto-gain.example.test';

let pure;
let live;

function answer(overrides = {}) {
	// An analysed answer applies its analysed offset; keep the two together
	// unless a test sets both on purpose.
	if ('effective_offset_db' in overrides && !('analysed_offset_db' in overrides) && (overrides.status ?? 'analysed') === 'analysed') {
		overrides = { ...overrides, analysed_offset_db: overrides.effective_offset_db };
	}
	return {
		stable_id: 'a'.repeat(40),
		status: 'analysed',
		enabled: true,
		target_lufs: -12,
		integrated_lufs: -6,
		true_peak_dbtp: 1.5,
		analysed_offset_db: -6,
		user_offset_db: null,
		effective_offset_db: -6,
		limited_by: null,
		loudness_reason: null,
		offset_min_db: -12,
		offset_max_db: 12,
		true_peak_ceiling_dbtp: -1,
		...overrides
	};
}

before(async () => {
	pure = await loadTypeScriptModule('src/lib/rb/auto-gain.ts', { viteApiBase: API_BASE });
	live = await loadTypeScriptModule('src/lib/rb/auto-gain.svelte.ts', { viteApiBase: API_BASE });
});

test('a well-formed answer parses and a malformed one throws', () => {
	assert.deepEqual(pure.parseAutoGain(answer()), answer());
	assert.throws(() => pure.parseAutoGain(answer({ status: 'guessed' })), /unknown status/);
	assert.throws(() => pure.parseAutoGain(answer({ effective_offset_db: null, analysed_offset_db: -6 })), /effective_offset_db/);
	assert.throws(() => pure.parseAutoGain(answer({ limited_by: 'vibes' })), /limited_by/);
	assert.throws(() => pure.parseAutoGain(null), /object/);
});

test('an answer outside the resolver contract is refused, never applied', () => {
	assert.throws(() => pure.parseAutoGain(answer({ effective_offset_db: 100, analysed_offset_db: 100 })), /outside -12\.\.12 dB/);
	assert.throws(() => pure.parseAutoGain(answer({ target_lufs: 3 })), /target_lufs 3 is outside/);
	assert.throws(() => pure.parseAutoGain(answer({ effective_offset_db: -5, analysed_offset_db: -6 })), /must apply -6 dB/);
	assert.throws(() => pure.parseAutoGain(answer({ status: 'off' })), /disagrees with enabled/);
	assert.throws(() => pure.parseAutoGain(answer({ status: 'not_analysed' })), /must apply 0 dB/);
	assert.equal(pure.parseAutoGain(answer({ status: 'user', user_offset_db: 2, effective_offset_db: 2 })).effective_offset_db, 2);
	assert.equal(pure.parseAutoGain(answer({ status: 'off', enabled: false, effective_offset_db: 0 })).effective_offset_db, 0);
});

test('a pref read that failed is shown as unavailable and blocks a write', async () => {
	const realFetch = globalThis.fetch;
	const realError = console.error;
	console.error = () => {};
	const methods = [];
	globalThis.fetch = async (_url, init) => {
		methods.push(init?.method ?? 'GET');
		return new Response('down', { status: 503 });
	};
	try {
		await live.hydrateAutoGainPrefs();
		assert.equal(live.autoGainPrefs.loaded, false);
		assert.match(live.autoGainPrefsUnavailable(), /could not be read/);
		await live.saveAutoGainPrefs({ enabled: false });
		assert.ok(!methods.includes('PUT'), 'nothing written against unread values');
	} finally {
		globalThis.fetch = realFetch;
		console.error = realError;
	}
});

test('only a ready deck applies gain, and it is 10^(dB/20)', () => {
	assert.equal(pure.deckAutoGainLinear({ state: 'empty' }), 1);
	assert.equal(pure.deckAutoGainLinear({ state: 'error', stable_id: 'x', message: 'boom' }), 1);
	const linear = pure.deckAutoGainLinear({ state: 'ready', value: answer() });
	assert.ok(Math.abs(linear - 10 ** (-6 / 20)) < 1e-12);
	assert.throws(() => pure.linearFromDb(Number.NaN), /not finite/);
});

test('the readout says what the deck is doing', () => {
	const ready = (o) => pure.autoGainReadout({ state: 'ready', value: answer(o) });
	assert.equal(ready().label, 'AG -6.0 dB');
	assert.equal(ready({ status: 'user', user_offset_db: 2, effective_offset_db: 2 }).label, 'AG +2.0 dB*');
	assert.equal(ready({ status: 'user', user_offset_db: 2, effective_offset_db: 2 }).tone, 'user');
	const na = ready({ status: 'not_analysed', integrated_lufs: null, analysed_offset_db: null, effective_offset_db: 0, loudness_reason: 'no own analysis record' });
	assert.equal(na.label, 'AG N/A');
	assert.match(na.title, /not analysed, playing at 0 dB/);
	assert.equal(ready({ status: 'off', effective_offset_db: 0 }).label, 'AG OFF');
	assert.match(ready({ effective_offset_db: 1, limited_by: 'true_peak' }).title, /true peak stays at -1 dBTP/);
	assert.equal(pure.autoGainReadout({ state: 'error', stable_id: 'x', message: 'HTTP 500' }).label, 'AG ERR');
});

test('publishing re-applies that deck through the engine hook', () => {
	const applied = [];
	live.registerAutoGainApplier((deck) => applied.push([deck, live.autoGainLinear(deck)]));
	live.publishDeckAutoGain(2, { state: 'ready', value: answer({ effective_offset_db: -6 }) });
	assert.equal(applied.length, 1);
	assert.equal(applied[0][0], 2);
	assert.ok(Math.abs(applied[0][1] - 10 ** (-6 / 20)) < 1e-12);
	assert.equal(live.autoGainLinear(1), 1, 'other decks untouched');
	live.publishDeckAutoGain(2, { state: 'empty' });
	assert.deepEqual(applied.at(-1), [2, 1], 'unload goes back to unity');
});

test('a failed read settles to the error state instead of rejecting the load', async () => {
	const realFetch = globalThis.fetch;
	globalThis.fetch = async () => new Response('nope', { status: 500 });
	try {
		const settled = await live.settleTrackAutoGain('b'.repeat(40));
		assert.equal(settled.state, 'error');
		assert.match(settled.message, /HTTP 500/);
	} finally {
		globalThis.fetch = realFetch;
	}
});

test('a user offset is written and its answer published to every deck holding that track', async () => {
	const sid = 'c'.repeat(40);
	const calls = [];
	const realFetch = globalThis.fetch;
	globalThis.fetch = async (url, init) => {
		calls.push([String(url), init?.method ?? 'GET', init?.body ?? null]);
		const user = answer({ stable_id: sid, status: 'user', user_offset_db: -2.5, effective_offset_db: -2.5 });
		return new Response(JSON.stringify(user), { status: 200, headers: { 'Content-Type': 'application/json' } });
	};
	try {
		live.publishDeckAutoGain(3, { state: 'ready', value: answer({ stable_id: sid }) });
		await live.setDeckAutoGainUserOffset(3, -2.5, sid);
		assert.deepEqual(calls[0], [`${API_BASE}/api/v1/auto-gain/tracks/${sid}/user-offset`, 'PUT', '{"offset_db":-2.5}']);
		assert.equal(calls.length, 1, 'the PUT answer is published as is, not read back');
		assert.equal(live.deckAutoGain[3].value.effective_offset_db, -2.5);
		await assert.rejects(live.setDeckAutoGainUserOffset(4, 1, sid), /deck 4 has no track loaded/);
		await assert.rejects(live.setDeckAutoGainUserOffset(3, 13, sid), /-12\.\.12 dB/);
		// A write meant for another track (a load landed while it queued) is refused unwritten.
		calls.length = 0;
		await assert.rejects(live.setDeckAutoGainUserOffset(3, 1, 'd'.repeat(40)), /not d{40}; offset not written/);
		assert.equal(calls.length, 0);
	} finally {
		globalThis.fetch = realFetch;
		live.publishDeckAutoGain(3, { state: 'empty' });
	}
});

test('the trim stage multiplies in auto gain at graph build and on every knob move', () => {
	const graph = readFrontendSource('src/lib/rb/deck-channel-graph.ts');
	assert.match(graph, /return trimKnob \* TRIM_MAX_GAIN \* autoGainLinear\(deck\);/);
	assert.match(graph, /trim\.gain\.value = trimStageGain\(ch\.trim, deck\);/);
	assert.match(graph, /if \(nodes !== null\) setParam\(nodes\.trim\.gain, trimStageGain\(trimKnob, deck\)\);/);
	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	assert.match(engine, /applyTrimStage\(_rt\[deck\]\.nodes, mixerState\.channels\[deck\]\.trim, deck, _setParam\)/);
	assert.match(engine, /registerAutoGainApplier\(_applyTrimGain\);/);
	assert.match(engine, /mixerState\.channels\[deck\]\.trim = value;\n\t\t_applyTrimGain\(deck\);/);
	assert.match(engine, /st\.stable_id = stable_id;\n\t\t\tpublishDeckAutoGain\(deck, candidateAutoGain\);/);
	assert.match(engine, /publishDeckAutoGain\(deck, \{ state: 'empty' \}\);/);
});

test('the TS constants match the Python resolver', () => {
	const py = readFileSync(new URL('../../../../analysis_loudness/auto_gain.py', import.meta.url), 'utf8');
	const pyConst = (name) => Number(new RegExp(`^${name}: Final\\[float\\] = (-?[0-9.]+)`, 'm').exec(py)[1]);
	assert.equal(pure.AUTO_GAIN_DEFAULT_TARGET_LUFS, pyConst('DEFAULT_TARGET_LUFS'));
	assert.equal(pure.AUTO_GAIN_TARGET_MIN_LUFS, pyConst('TARGET_MIN_LUFS'));
	assert.equal(pure.AUTO_GAIN_TARGET_MAX_LUFS, pyConst('TARGET_MAX_LUFS'));
	assert.equal(pure.AUTO_GAIN_OFFSET_MIN_DB, pyConst('OFFSET_MIN_DB'));
	assert.equal(pure.AUTO_GAIN_OFFSET_MAX_DB, pyConst('OFFSET_MAX_DB'));
	assert.match(py, /^DEFAULT_ENABLED: Final\[bool\] = True$/m);
	assert.equal(pure.AUTO_GAIN_DEFAULT_ENABLED, true);
});

test('the user offset is a typed dispatcher command, so a preset lock refuses it like any control', async () => {
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', { viteApiBase: API_BASE });
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({ type: 'auto_gain_offset', deck: 2, offset_db: -2.5, stable_id: 'x' }),
		{ type: 'auto_gain_offset', deck: 2, offset_db: -2.5, stable_id: 'x' }
	);
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({ type: 'auto_gain_offset', deck: 1, offset_db: null, stable_id: 'x' }),
		{ type: 'auto_gain_offset', deck: 1, offset_db: null, stable_id: 'x' }
	);
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'auto_gain_offset', deck: 1, offset_db: 'loud', stable_id: 'x' }), /offset_db/);
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'auto_gain_offset', deck: 1, offset_db: -2 }), /stable_id/);
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'auto_gain_offset', deck: 1, offset_db: -2, stable_id: 'x', extra: 1 }));
	// It holds its deck's queue scope across the awaited write, so a preset's all-scope barrier waits for it.
	assert.deepEqual([...ipc.performanceCommandQueueScopes({ type: 'auto_gain_offset', deck: 3, offset_db: -1, stable_id: 'x' })], [3]);
	const readout = readFrontendSource('src/lib/components/rb/mixer/AutoGainReadout.svelte');
	assert.doesNotMatch(readout, /setDeckAutoGainUserOffset|putTrackAutoGainUserOffset/, 'the readout must not write around the dispatcher');
	const mixer = readFrontendSource('src/lib/components/rb/Mixer.svelte');
	assert.match(mixer, /runPerformanceCommandFromUi\(\{ type: 'auto_gain_offset', deck, offset_db: offsetDb, stable_id: stableId \}\)/);
	assert.match(readFrontendSource('src/lib/rb/performance-ipc.svelte.ts'), /await engine\.setAutoGainOffset\(command\.deck, command\.offset_db, command\.stable_id\);/);
});

test('an older refresh answer never overwrites a newer one', async () => {
	const sid = 'd'.repeat(40);
	const realFetch = globalThis.fetch;
	const pending = [];
	globalThis.fetch = () => new Promise((resolve) => pending.push(resolve));
	const reply = (offset) =>
		new Response(JSON.stringify(answer({ stable_id: sid, effective_offset_db: offset })), { status: 200 });
	try {
		live.publishDeckAutoGain(4, { state: 'ready', value: answer({ stable_id: sid }) });
		const older = live.refreshLoadedDecks([sid]);
		const newer = live.refreshLoadedDecks([sid]);
		while (pending.length < 2) await new Promise((resolve) => setImmediate(resolve));
		pending[1](reply(0));
		await newer;
		pending[0](reply(-6));
		await older;
		assert.equal(live.deckAutoGain[4].value.effective_offset_db, 0, 'the stale -6 dB answer was discarded');
	} finally {
		globalThis.fetch = realFetch;
		live.publishDeckAutoGain(4, { state: 'empty' });
	}
});

test('while a preset owns the lock, Settings cannot change auto gain and refreshes are held', async () => {
	const sid = 'e'.repeat(40);
	const calls = [];
	const realFetch = globalThis.fetch;
	const realError = console.error;
	console.error = () => {};
	globalThis.fetch = async (url, init) => {
		calls.push([String(url), init?.method ?? 'GET']);
		const body = String(url).endsWith('/ui-prefs')
			? { auto_gain: { enabled: true, target_lufs: -12 } }
			: answer({ stable_id: sid, effective_offset_db: -1 });
		return new Response(JSON.stringify(body), { status: 200 });
	};
	try {
		live.publishDeckAutoGain(1, { state: 'ready', value: answer({ stable_id: sid }) });
		live.setAutoGainLiveGuard(() => 'performance preset preload1 owns controls');
		await live.saveAutoGainPrefs({ enabled: false });
		assert.ok(!calls.some(([, method]) => method === 'PUT'), 'no pref write while locked');
		assert.match(live.autoGainPrefs.error, /locked: performance preset preload1/);
		calls.length = 0;
		await live.refreshLoadedDecks();
		assert.equal(calls.length, 0, 'no live gain change while locked');
		const release = live.setAutoGainLiveGuard(() => null);
		release();
		await new Promise((resolve) => setImmediate(resolve));
		assert.ok(calls.some(([url]) => url.endsWith(`/auto-gain/tracks/${sid}`)), 'the held refresh runs when the lock is released');
		calls.length = 0;
		await live.refreshLoadedDecks([]);
		assert.ok(calls.some(([url]) => url.endsWith(`/auto-gain/tracks/${sid}`)), 'an empty id list refreshes every loaded deck');
	} finally {
		live.setAutoGainLiveGuard(() => null);
		globalThis.fetch = realFetch;
		console.error = realError;
		live.publishDeckAutoGain(1, { state: 'empty' });
	}
});

test('a refresh that lands after a preset takes the lock is held, then re-read on release', async () => {
	const sid = 'f'.repeat(40);
	const pending = [];
	const realFetch = globalThis.fetch;
	globalThis.fetch = (url) =>
		new Promise((resolve) => pending.push(() => resolve(new Response(JSON.stringify(answer({ stable_id: sid, effective_offset_db: -3 })), { status: 200 }))));
	try {
		live.publishDeckAutoGain(2, { state: 'ready', value: answer({ stable_id: sid, effective_offset_db: 0 }) });
		const inFlight = live.refreshLoadedDecks([sid]);
		while (pending.length < 1) await new Promise((resolve) => setImmediate(resolve));
		live.setAutoGainLiveGuard(() => 'performance preset preload1 owns controls');
		pending.shift()();
		await inFlight;
		assert.equal(live.deckAutoGain[2].value.effective_offset_db, 0, 'no gain change lands inside the preset');
		const release = live.setAutoGainLiveGuard(() => null);
		release();
		while (pending.length < 1) await new Promise((resolve) => setImmediate(resolve));
		pending.shift()();
		for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
		assert.equal(live.deckAutoGain[2].value.effective_offset_db, -3, 'the held read is applied after release');
	} finally {
		live.setAutoGainLiveGuard(() => null);
		globalThis.fetch = realFetch;
		live.publishDeckAutoGain(2, { state: 'empty' });
	}
});

test('a load whose answer was read before its track changed elsewhere re-reads before playing it', async () => {
	const sid = '1'.repeat(40);
	let offset = -6;
	const realFetch = globalThis.fetch;
	globalThis.fetch = async () => new Response(JSON.stringify(answer({ stable_id: sid, effective_offset_db: offset })), { status: 200 });
	try {
		const candidate = await live.settleTrackAutoGain(sid);
		offset = 2;
		live.invalidateAutoGainReads([sid]);
		live.publishDeckAutoGain(3, candidate);
		for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
		assert.equal(live.deckAutoGain[3].value.effective_offset_db, 2, 'the superseded candidate was re-read');
	} finally {
		globalThis.fetch = realFetch;
		live.publishDeckAutoGain(3, { state: 'empty' });
	}
});

test('master election judges the trim stage as heard, knob times auto gain', async () => {
	const m = await loadTypeScriptModule('tests/unit/fixtures/auto-gain-election-entry.ts', { viteApiBase: API_BASE });
	const deck = { id: 1, loaded: true, playing: true, beat_sync_enabled: false, fader: 1, trim: 0.5, assign: 'THRU' };
	const flat = m.onAirGain(deck, 0.5, 1);
	m.publishDeckAutoGain(1, { state: 'ready', value: answer({ effective_offset_db: -12 }) });
	assert.ok(Math.abs(m.onAirGain(deck, 0.5, 1) / flat - 10 ** (-12 / 20)) < 1e-9, 'a -12 dB auto gain cut is seen by election');
	m.publishDeckAutoGain(1, { state: 'empty' });
});

test('a user-offset answer superseded by a later write is re-read, never applied', async () => {
	const sid = '2'.repeat(40);
	const pending = [];
	let stored = 0;
	const realFetch = globalThis.fetch;
	globalThis.fetch = (url, init) =>
		new Promise((resolve) => {
			const body = init?.body ? JSON.parse(init.body) : null;
			pending.push(() => {
				const offset = body === null ? stored : body.offset_db;
				resolve(new Response(JSON.stringify(answer({ stable_id: sid, status: 'user', user_offset_db: offset, effective_offset_db: offset })), { status: 200 }));
			});
		});
	try {
		live.publishDeckAutoGain(1, { state: 'ready', value: answer({ stable_id: sid }) });
		live.publishDeckAutoGain(2, { state: 'ready', value: answer({ stable_id: sid }) });
		const first = live.setDeckAutoGainUserOffset(1, -3, sid);
		const second = live.setDeckAutoGainUserOffset(2, -1, sid);
		while (pending.length < 2) await new Promise((resolve) => setImmediate(resolve));
		stored = -1;
		pending[1]();
		await second;
		pending[0]();
		await first;
		while (pending.length < 3) await new Promise((resolve) => setImmediate(resolve));
		pending.slice(2).forEach((go) => go());
		for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
		assert.equal(live.deckAutoGain[1].value.effective_offset_db, -1, 'the late -3 dB answer did not win');
		assert.equal(live.deckAutoGain[2].value.effective_offset_db, -1);
	} finally {
		globalThis.fetch = realFetch;
		live.publishDeckAutoGain(1, { state: 'empty' });
		live.publishDeckAutoGain(2, { state: 'empty' });
	}
});

test('a load answer superseded while decoding is re-read before it is committed', async () => {
	const sid = '3'.repeat(40);
	let offset = -6;
	const realFetch = globalThis.fetch;
	globalThis.fetch = async () => new Response(JSON.stringify(answer({ stable_id: sid, effective_offset_db: offset })), { status: 200 });
	try {
		const candidate = await live.settleTrackAutoGain(sid);
		assert.equal(await live.freshTrackAutoGain(candidate), candidate, 'an answer nothing superseded is used as is');
		offset = 1;
		live.invalidateAutoGainReads([sid]);
		assert.equal((await live.freshTrackAutoGain(candidate)).value.effective_offset_db, 1);
	} finally {
		globalThis.fetch = realFetch;
	}
});

test('a save that failed stays visible in Settings after the values are read back', async () => {
	const realFetch = globalThis.fetch;
	const realError = console.error;
	console.error = () => {};
	globalThis.fetch = async (_url, init) =>
		(init?.method ?? 'GET') === 'PUT'
			? new Response('nope', { status: 500 })
			: new Response(JSON.stringify({ auto_gain: { enabled: true, target_lufs: -12 } }), { status: 200 });
	try {
		await live.hydrateAutoGainPrefs();
		assert.equal(live.autoGainPrefsUnavailable(), null);
		await live.saveAutoGainPrefs({ enabled: false });
		assert.equal(live.autoGainPrefs.loaded, true);
		assert.match(live.autoGainPrefsUnavailable(), /were not saved/);
	} finally {
		globalThis.fetch = realFetch;
		console.error = realError;
	}
});

test('a load answer superseded on every read keeps re-reading until one is current', async () => {
	const sid = '4'.repeat(40);
	let reads = 0;
	const realFetch = globalThis.fetch;
	globalThis.fetch = async () => {
		reads += 1;
		// The first four reads are each superseded while in flight.
		if (reads <= 4) live.invalidateAutoGainReads([sid]);
		return new Response(JSON.stringify(answer({ stable_id: sid, effective_offset_db: -reads })), { status: 200 });
	};
	try {
		const candidate = await live.settleTrackAutoGain(sid);
		const fresh = await live.freshTrackAutoGain(candidate);
		assert.equal(fresh.value.effective_offset_db, -5, 'only the first read nothing superseded is committed');
	} finally {
		globalThis.fetch = realFetch;
	}
});
