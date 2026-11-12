/** Issue #12387: real status HTTP client/cache/sink with synthesized engine state. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Explicitly synthesized, distinct vocal regions for the two tracks.
const freshVocals = {
	status: 'demucs',
	fps: 10,
	regions: [{ start_s: 1, end_s: 2, intensity: 0.8 }]
};
const replacementVocals = {
	status: 'rekordbox',
	fps: 10,
	regions: [{ start_s: 5, end_s: 7, intensity: 0.4 }]
};

function anlzPayload(stable_id, vocals) {
	return {
		stable_id,
		points: 38400,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
		vocals
	};
}

async function harness(t) {
	const statusStarted = Promise.withResolvers();
	const releaseStatus = Promise.withResolvers();
	const published = Promise.withResolvers();
	const statusRequests = [];
	const anlzRequests = [];
	const snapshots = [];
	const writes = [];
	const unexpected = [];
	const server = createServer(async (req, res) => {
		const pathname = new URL(req.url, 'http://localhost').pathname;
		if (pathname === '/api/v1/vocals/old-track/status') {
			statusRequests.push(pathname);
			statusStarted.resolve();
			await releaseStatus.promise;
			res.writeHead(200, { 'content-type': 'application/json' });
			res.end(JSON.stringify({ stable_id: 'old-track', ...freshVocals }));
		} else if (pathname === '/api/v1/tracks/old-track/anlz') {
			anlzRequests.push(req.url);
			res.writeHead(200, { 'content-type': 'application/json' });
			res.end(JSON.stringify(anlzPayload('old-track', freshVocals)));
		} else {
			unexpected.push(pathname);
			res.writeHead(404).end();
		}
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	t.after(async () => {
		releaseStatus.resolve();
		server.closeAllConnections();
		await new Promise((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())));
		assert.deepEqual(unexpected, [], 'unexpected HTTP routes');
	});
	const mod = await loadTypeScriptModule('tests/unit/fixtures/loaded-deck-vocals-entry.ts', {
		viteApiBase: `http://127.0.0.1:${server.address().port}`,
		alias: {
			'$lib/player/beatgrid-lazy': fileURLToPath(
				new URL('./fixtures/loaded-deck-vocals-beatgrid-stub.ts', import.meta.url)
			),
			'$lib/rb/audio-engine.svelte': fileURLToPath(
				new URL('./fixtures/loaded-deck-vocals-engine-stub.ts', import.meta.url)
			)
		}
	});
	const sink = mod.createDeckVocalsSink({
		deckIds: mod.DECK_IDS,
		deckLoadToken: (deck) => mod.deckStates[deck].loadToken,
		deckStableId: (deck) => mod.deckStates[deck].stable_id,
		deckAnlz: (deck) => mod.deckStates[deck].anlz,
		publishDeckAnlz: (deck, anlz) => {
			writes.push({ deck, anlz });
			mod.deckStates[deck].anlz = anlz;
		}
	});
	mod.installAuthoritativeAnlzVocalsSink({
		snapshot(stableId) {
			snapshots.push(stableId);
			sink.snapshot(stableId);
		},
		publish(...args) {
			sink.publish(...args);
			published.resolve();
		}
	});
	function load(deck, stableId, vocals) {
		const anlz = stableId === null ? null : anlzPayload(stableId, vocals);
		Object.assign(mod.deckStates[deck], {
			stable_id: stableId,
			anlz,
			loadToken: mod.deckStates[deck].loadToken + 1
		});
		if (anlz !== null) mod.refreshAnlzCacheEntry(stableId, anlz);
		return anlz;
	}
	load(1, 'old-track', { status: 'not_analyzed' });
	return {
		mod,
		load,
		statusStarted,
		releaseStatus,
		published,
		statusRequests,
		anlzRequests,
		snapshots,
		writes
	};
}

for (const scenario of ['replacement', 'unload', 'unchanged', 'duplicate holder']) {
	test(`late vocal status response: ${scenario}`, { timeout: 10000 }, async (t) => {
		const h = await harness(t);
		if (scenario === 'duplicate holder') h.load(2, 'old-track', { status: 'not_analyzed' });
		const refresh = h.mod.refreshLoadedDeckVocalsFromServer(['old-track']);
		await h.statusStarted.promise;
		assert.equal(h.statusRequests.length, 1, 'status request reached the server before the switch');
		let replacement;
		if (scenario === 'replacement' || scenario === 'duplicate holder') {
			replacement = h.load(1, 'new-track', replacementVocals);
		} else if (scenario === 'unload') {
			h.load(1, null);
		}
		h.releaseStatus.resolve();
		await refresh;

		// A started refresh completes through the real HTTP/cache/sink before assertions.
		// This also drains the unwanted pre-fix fetch deterministically, without sleeps.
		if (h.snapshots.length > 0) await h.published.promise;
		if (scenario === 'replacement' || scenario === 'unload') {
			assert.deepEqual(h.anlzRequests, [], 'a stale status must not initiate an ANLZ bypass fetch');
			assert.deepEqual(h.snapshots, []);
			assert.deepEqual(h.writes, []);
			assert.equal(h.mod.getAnlzEntry('old-track').data.vocals.status, 'not_analyzed');
		} else {
			assert.equal(h.anlzRequests.length, 1);
			assert.match(h.anlzRequests[0], /\/anlz\?/);
			const holder = scenario === 'duplicate holder' ? 2 : 1;
			assert.deepEqual(
				h.writes.map(({ deck }) => deck),
				[holder]
			);
			assert.deepEqual(h.mod.deckStates[holder].anlz.vocals, freshVocals);
			assert.deepEqual(h.mod.getAnlzEntry('old-track').data.vocals, freshVocals);
		}
		if (replacement !== undefined) {
			assert.equal(h.mod.deckStates[1].stable_id, 'new-track');
			assert.equal(h.mod.deckStates[1].anlz, replacement);
			assert.deepEqual(h.mod.deckStates[1].anlz.vocals, replacementVocals);
			assert.equal(h.mod.getAnlzEntry('new-track').data, replacement);
		} else if (scenario === 'unload') {
			assert.equal(h.mod.deckStates[1].stable_id, null);
			assert.equal(h.mod.deckStates[1].anlz, null);
		}
	});
}
