/**
 * Issue #9654 / STEM-9934: a deck on NO STEMS re-runs its stem bundle lookup
 * once when a library_jobs or tracks event names its track (or none), and on
 * resync. Driven with a fake event bus and fake deck state.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://deck-stems-refresh.example.test';
const { subscribeLoadedDeckStemsRefresh } = await loadTypeScriptModule(
	'src/lib/rb/loaded-deck-stems-refresh.ts'
);
const availability = await loadTypeScriptModule('src/lib/rb/optional-resource-availability.ts');
const apiRb = await loadTypeScriptModule('src/lib/rb/api-rb.ts', { viteApiBase: API_BASE });
const ROUTE = JSON.parse(
	readFileSync(new URL('../fixtures/stem-route-answers.json', import.meta.url), 'utf8')
);

let originalFetch;

before(() => {
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

function stems(status, error = null, load = null) {
	return {
		status,
		source: null,
		model: null,
		layout: null,
		available_controls: [],
		alignment: null,
		controls: {},
		error,
		load
	};
}

const NONE = () => stems('unavailable', 'no bundle');
const OTHER_STATES = {
	ready: () => stems('ready'),
	loading: () => stems('loading', null, { phase: 'probing', progress: null, reason: null }),
	error: () => stems('error', 'x'),
	empty: () => stems('unavailable', null)
};

function fakeBus() {
	const kinds = new Map([
		['library_jobs', new Set()],
		['tracks', new Set()]
	]);
	const resyncs = new Set();
	return {
		subscribeKind(kind, listener) {
			kinds.get(kind).add(listener);
			return () => kinds.get(kind).delete(listener);
		},
		subscribeResync(listener) {
			resyncs.add(listener);
			return () => resyncs.delete(listener);
		},
		emit(kind, ids) {
			for (const listener of kinds.get(kind)) listener(ids);
		},
		resync() {
			for (const listener of resyncs) listener();
		},
		listenerCount() {
			return [...kinds.values()].reduce((n, set) => n + set.size, 0) + resyncs.size;
		}
	};
}

function setup({
	stable_id = 'trk-a',
	deckStems = NONE(),
	canLoad = true,
	loadStems: loadStemsOverride
} = {}) {
	const bus = fakeBus();
	const deck = { stable_id, stems: deckStems };
	const calls = [];
	const pending = [];
	const defaultLoadStems = (id) => {
		calls.push(id);
		let resolve;
		let reject;
		const promise = new Promise((res, rej) => {
			resolve = res;
			reject = rej;
		});
		pending.push({ resolve, reject });
		return promise;
	};
	const unsubscribe = subscribeLoadedDeckStemsRefresh({
		deck: 'A',
		state: () => deck,
		subscribeKind: bus.subscribeKind,
		subscribeResync: bus.subscribeResync,
		canLoadStems: () => canLoad,
		loadStems: loadStemsOverride ?? defaultLoadStems
	});
	return { bus, deck, calls, pending, unsubscribe };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test('none + library_jobs naming the track looks up once', () => {
	const { bus, calls } = setup();
	bus.emit('library_jobs', ['trk-a']);
	assert.deepEqual(calls, ['trk-a'], 'if a NO STEMS deck ignores its stems job then broken');
});

test('none + library_jobs naming another track does nothing', () => {
	const { bus, calls } = setup();
	bus.emit('library_jobs', ['trk-b']);
	assert.equal(calls.length, 0, 'if an unrelated job triggers a lookup then broken');
});

test('none + tracks [] and none + resync each look up once', async () => {
	const a = setup();
	a.bus.emit('tracks', []);
	assert.deepEqual(a.calls, ['trk-a'], 'if tracks [] does not trigger a lookup then broken');
	const b = setup();
	b.bus.resync();
	assert.deepEqual(b.calls, ['trk-a'], 'if resync does not trigger a lookup then broken');
});

test('ready / loading / error / empty decks are never touched', () => {
	for (const [name, make] of Object.entries(OTHER_STATES)) {
		const { bus, calls } = setup({ deckStems: make() });
		bus.emit('library_jobs', ['trk-a']);
		bus.emit('tracks', []);
		bus.resync();
		assert.equal(calls.length, 0, `if a ${name} deck gets a stem lookup then broken`);
	}
});

test('an empty deck does nothing', () => {
	const { bus, calls } = setup({ stable_id: null });
	bus.emit('tracks', []);
	bus.resync();
	assert.equal(calls.length, 0, 'if an empty deck sends stem_load then broken');
});

test('a burst of events coalesces into one lookup; the flag clears after', async () => {
	const { bus, calls, pending } = setup();
	for (let i = 0; i < 5; i += 1) bus.emit('library_jobs', ['trk-a']);
	assert.equal(calls.length, 1, 'if a burst of 5 events sends more than one lookup then broken');
	pending[0].resolve({ ok: true });
	await tick();
	bus.emit('library_jobs', ['trk-a']);
	assert.equal(calls.length, 2, 'if the in-flight flag never clears then broken');
});

test('Rust engine mode (stem_load unsupported) sends nothing', () => {
	const { bus, calls } = setup({ canLoad: false });
	bus.emit('library_jobs', ['trk-a']);
	bus.resync();
	assert.equal(calls.length, 0, 'if stem_load is sent where it is refused then broken');
});

test('unsubscribe removes all three listeners', () => {
	const { bus, unsubscribe } = setup();
	assert.equal(bus.listenerCount(), 3);
	unsubscribe();
	assert.equal(bus.listenerCount(), 0, 'if unsubscribe leaks a listener then broken');
});

test('previously stemless track picks up its new bundle through the real probe', async () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('trk-a', { stems: false });
	const blocked = await apiRb.probeStemArtifact('trk-a');
	assert.equal(blocked.status, 'unavailable');
	assert.equal(blocked.error, 'no stem bundle advertised');

	const fetchUrls = [];
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		fetchUrls.push(url);
		return Response.json({ ...ROUTE.ready.body, stable_id: 'trk-a' });
	};

	const probes = [];
	const { bus } = setup({
		loadStems: async (id) => {
			probes.push(await apiRb.probeStemArtifact(id));
		}
	});
	bus.emit('library_jobs', ['trk-a']);
	await tick();

	assert.equal(
		fetchUrls.length,
		1,
		'if a cached no-stems flag from load time blocks the refresh lookup then broken'
	);
	assert.ok(fetchUrls[0].endsWith('/api/v1/tracks/trk-a/stems'));
	assert.equal(probes[0].status, 'ready');
	assert.equal(availability.optionalResources('trk-a').stems, 'unknown');
});

test('still-stemless track settles as unavailable from the server, no 404', async () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('trk-a', { stems: false });

	const fetchUrls = [];
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		fetchUrls.push(url);
		return Response.json(ROUTE.not_found.body);
	};

	const probes = [];
	const { bus } = setup({
		loadStems: async (id) => {
			probes.push(await apiRb.probeStemArtifact(id));
		}
	});
	bus.emit('library_jobs', ['trk-a']);
	await tick();

	assert.equal(fetchUrls.length, 1);
	assert.equal(probes[0].status, 'unavailable');
	assert.match(String(probes[0].error ?? ''), /STEM_BUNDLE_NOT_FOUND|no stem bundle/);
	assert.notEqual(probes[0].error, 'no stem bundle advertised');
});

test('a deck that is not looked up keeps its cached stems absence', async () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('trk-a', { stems: false });

	let fetchCalls = 0;
	globalThis.fetch = async () => {
		fetchCalls += 1;
		return Response.json(ROUTE.ready.body);
	};

	const { bus, calls } = setup({ deckStems: OTHER_STATES.ready() });
	bus.emit('library_jobs', ['trk-a']);
	bus.emit('tracks', []);
	await tick();

	assert.equal(calls.length, 0);
	assert.equal(fetchCalls, 0);
	assert.equal(availability.optionalResources('trk-a').stems, false);
});

test('forgetStemsAbsence leaves stems true and other caps alone', () => {
	availability.resetOptionalResourcesForTests();
	availability.rememberOptionalResources('t', { stems: true, lyrics: false });
	availability.forgetStemsAbsence('t');
	const caps = availability.optionalResources('t');
	assert.equal(caps.stems, true);
	assert.equal(caps.lyrics, false);
});
