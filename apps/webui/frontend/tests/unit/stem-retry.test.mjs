// requirement STEM-46 / STEM-47: "get this deck's stems now" is one decision,
// shared by the deck button, the `stem_load` command and an agent.
//
// [if] stems are ready [then] nothing is fetched, landed or reloaded
// [if] a bundle is held behind transport commands [then] it is landed
// [if] a decode is held by machine pressure [then] it is released
// [if] a load is already running by itself [then] the call rejects, naming
//   the phase, and does NOT start a second load
// [if] the last load failed [then] the engine's recorded fetch failure is
//   cleared BEFORE the load is run again
// [if] the deck has no decoded mix [then] the call rejects by name
// [if] a second retry arrives for a deck while one is running [then] it joins
//   that retry: one hydrate, one reload
// [if] the deck loads another track while the hydrate request is out [then]
//   the old track is never reloaded onto it
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let graph;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/stem-retry.ts');
	graph = await loadTypeScriptModule('src/lib/rb/stem-graph.ts');
});

function port(overrides = {}) {
	const log = [];
	return {
		log,
		port: {
			landHeld: null,
			reload: () => log.push('reload'),
			current: () => true,
			releaseDecode: () => {
				log.push('releaseDecode');
				return 0;
			},
			requestHydration: async (stableId) => {
				log.push(`hydrate:${stableId}`);
			},
			...overrides
		}
	};
}

const ALIGNMENT = { sample_rate_hz: 44100, frame_count: 100, channel_count: 2, duration_ms: 2 };
const loading = (phase) => graph.loadingStemDeckState({ phase, progress: null, reason: null });

test('ready stems are left alone', async () => {
	const { port: p, log } = port();
	const ready = graph.readyStemDeckState({ source: 'demucs', model: 'm', layout: 'demucs4' }, ALIGNMENT);
	await mod.retryDeckStems(2, 'sid', ready, p);
	assert.deepEqual(log, []);
});

test('a bundle held behind transport commands is landed, not reloaded', async () => {
	const { port: p, log } = port({ landHeld: async () => void log.push('landHeld') });
	await mod.retryDeckStems(2, 'sid', loading('waiting'), p);
	assert.deepEqual(log, ['landHeld']);
});

test('a decode held by pressure is released', async () => {
	const { port: p, log } = port({
		releaseDecode: () => {
			log.push('releaseDecode');
			return 1;
		}
	});
	await mod.retryDeckStems(2, 'sid', loading('waiting'), p);
	assert.deepEqual(log, ['releaseDecode']);
});

test('a load already running by itself rejects by phase and starts no second load', async () => {
	const { port: p, log } = port();
	await assert.rejects(
		mod.retryDeckStems(2, 'sid', loading('decoding'), p),
		/deck 2 stems are already loading \(decoding\)/
	);
	assert.deepEqual(log, ['releaseDecode']);
});

test('a failed load clears the recorded fetch failure before loading again', async () => {
	const { port: p, log } = port();
	const failed = { ...graph.unavailableStemDeckState('hub answered HTTP 503'), status: 'error' };
	await mod.retryDeckStems(2, 'sid', failed, p);
	assert.deepEqual(log, ['hydrate:sid', 'reload']);
});

test('a track loaded while the hydrate request is out never gets the old track\'s stems', async () => {
	let loadedSince = false;
	const { port: p, log } = port({
		requestHydration: async (stableId) => {
			log.push(`hydrate:${stableId}`);
			loadedSince = true;
		},
		current: () => !loadedSince
	});
	const failed = { ...graph.unavailableStemDeckState('hub answered HTTP 503'), status: 'error' };
	await mod.retryDeckStems(2, 'track-a', failed, p);
	assert.deepEqual(log, ['hydrate:track-a'], 'track A was reloaded onto the deck now holding track B');
});

test('a failed hydrate request is reported, and no reload hides it', async () => {
	const { port: p, log } = port({
		requestHydration: async () => {
			throw new Error('engine answered HTTP 500');
		}
	});
	const failed = { ...graph.unavailableStemDeckState('x'), status: 'error' };
	await assert.rejects(mod.retryDeckStems(2, 'sid', failed, p), /HTTP 500/);
	assert.deepEqual(log, []);
});

test('a track reported without stems is probed again, with no hydrate request', async () => {
	const { port: p, log } = port();
	await mod.retryDeckStems(2, 'sid', graph.unavailableStemDeckState('STEM_BUNDLE_NOT_FOUND'), p);
	assert.deepEqual(log, ['reload']);
});

test('a deck with no decoded mix rejects by name', async () => {
	const { port: p, log } = port({ reload: null });
	await assert.rejects(
		mod.retryDeckStems(3, 'sid', graph.unavailableStemDeckState(), p),
		/deck 3 has no decoded mix to align stems to/
	);
	await assert.rejects(
		mod.retryDeckStems(3, null, graph.unavailableStemDeckState(), port().port),
		/no decoded mix/
	);
	assert.deepEqual(log, []);
});

test('a second retry on the same deck joins the running one: one hydrate, one reload', async () => {
	let release;
	const gate = new Promise((resolve) => (release = resolve));
	const { port: p, log } = port({
		requestHydration: async (stableId) => {
			log.push(`hydrate:${stableId}`);
			await gate;
		}
	});
	const failed = { ...graph.unavailableStemDeckState('boom'), status: 'error' };
	const first = mod.retryDeckStems(4, 'sid', failed, p);
	const second = mod.retryDeckStems(4, 'sid', failed, p);
	assert.equal(second, first, 'the second retry started its own run');
	release();
	await Promise.all([first, second]);
	assert.deepEqual(log, ['hydrate:sid', 'reload']);
	// A track loaded since is its own retry, never the old track's.
	let releaseA;
	const gateA = new Promise((resolve) => (releaseA = resolve));
	const a = port({ requestHydration: async (sid) => { a.log.push(`hydrate:${sid}`); await gateA; } });
	const b = port();
	const forA = mod.retryDeckStems(4, 'track-a', failed, a.port);
	const forB = mod.retryDeckStems(4, 'track-b', failed, b.port);
	assert.notEqual(forB, forA, 'a retry for a newly loaded track joined the old track\'s retry');
	await forB;
	assert.deepEqual(b.log, ['hydrate:track-b', 'reload']);
	releaseA();
	await forA;
	// Control: once settled, the deck can be retried again, and another deck never waits on it.
	await mod.retryDeckStems(4, 'sid', failed, port().port);
	const other = port();
	await mod.retryDeckStems(1, 'sid', failed, other.port);
	assert.deepEqual(other.log, ['hydrate:sid', 'reload']);
});
