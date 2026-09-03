/**
 * PARITY-02 rbx-vs-own analysis source toggle: engine.refreshDecksForAnalysisSourceChange
 * (audio-engine.svelte.ts), the fix for discussion_r3921666943 - BLOCKING "Refresh
 * decks and caches when switching source".
 *
 * Before this existed, setAnalysisSource only replaced analysisSourceState: neither
 * the shared one-fetch-per-session ANLZ cache nor a loaded deck's terminal st.anlz
 * was ever refreshed, so a source switch changed the toggle's own highlight while
 * the waveform/beatgrid kept rendering the pre-switch source.
 *
 * Runs against a REAL FastAPI server (fixtures/analysis_source_anlz_server.py) that
 * serves /anlz through the production `_resolve_beatgrid_source` function against a
 * real analysis state.db - not a fabricated `globalThis.fetch` response - per
 * discussion_r3921839834: the old version of this test could pass with the swap
 * logic itself broken, since it never ran. `/test/requests` is the server's own real
 * ASGI-middleware access log, so "no fetch happened" is read from the server's own
 * account of what it served, not from a spy on the client's fetch.
 *
 * Regression lines:
 * - if a deck with a loaded track doesn't get a fresh, cache-bypassing /anlz fetch
 *   republished onto st.anlz then broken
 * - if an unloaded deck (stable_id null) triggers a fetch anyway then broken
 * - if a deck swapped to a different track mid-request has its stale response
 *   written onto the new track's anlz then broken
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(new URL('./fixtures/analysis_source_anlz_server.py', import.meta.url));

const SID_TRACK_A = 'real-track-a-own-grid';
const SID_TRACK_B = 'real-track-b-own-grid';
const SID_SLOW = 'real-track-slow-own-grid';

let audio;
let serverProcess;
let apiBase;

/** Every URL the real server has served so far, oldest first. */
async function requestLog() {
	const res = await fetch(`${apiBase}/test/requests`);
	return res.json();
}

before(async () => {
	serverProcess = spawn('uv', ['run', '--no-sync', 'python', SERVER_SCRIPT], {
		cwd: REPOSITORY_ROOT,
		env: { ...process.env, MDT_LIBRARY_MODE: 'local' },
		stdio: ['ignore', 'pipe', 'inherit']
	});
	const port = await new Promise((resolve, reject) => {
		const rl = createInterface({ input: serverProcess.stdout });
		serverProcess.once('exit', (code) => reject(new Error(`fixture server exited early (${code})`)));
		rl.on('line', (line) => {
			const m = /^READY (\d+)$/.exec(line);
			if (m) resolve(Number(m[1]));
		});
	});
	apiBase = `http://127.0.0.1:${port}`;

	// Real PUT against the real /api/v1/analysis-source route - the same
	// endpoint AnalysisSourceToggle.svelte drives - so every /anlz this
	// server serves for the rest of the file goes through the real 'own'
	// branch of _resolve_beatgrid_source.
	const put = await fetch(`${apiBase}/api/v1/analysis-source`, {
		method: 'PUT',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ feature: 'beatgrid', source: 'own' })
	});
	assert.equal(put.status, 200, 'fixture server rejected the real source switch');

	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', { viteApiBase: apiBase });
});

after(() => {
	serverProcess?.kill();
});

beforeEach(() => {
	for (const deck of audio.DECK_IDS) {
		audio.deckStates[deck].stable_id = null;
		audio.deckStates[deck].anlz = null;
	}
});

test('refreshes a loaded deck onto a fresh, real own-source anlz payload', async () => {
	audio.deckStates[1].stable_id = SID_TRACK_A;
	audio.deckStates[1].anlz = { beatgrid: { beat_count: 4, beats: [] } };

	const before_ = await requestLog();
	await audio.engine.refreshDecksForAnalysisSourceChange();
	const after_ = (await requestLog()).slice(before_.length);

	assert.equal(after_.length, 1, 'only the one loaded deck should fetch');
	assert.match(after_[0], new RegExp(`/tracks/${SID_TRACK_A}/anlz\\?points=`));
	assert.equal(
		audio.deckStates[1].anlz.beatgrid_source,
		'own',
		'deck 1 must adopt the real own-source payload, not a leftover rekordbox one'
	);
	assert.ok(
		audio.deckStates[1].anlz.beatgrid.beat_count > 0,
		'the real own analysis for this track has downbeats; the served grid must not be empty'
	);
});

test('an unloaded deck (no stable_id) triggers no fetch and is left untouched', async () => {
	const before_ = await requestLog();
	await audio.engine.refreshDecksForAnalysisSourceChange();
	const after_ = (await requestLog()).slice(before_.length);

	assert.equal(after_.length, 0, 'no deck has a stable_id, so the real server must see no request');
	for (const deck of audio.DECK_IDS) {
		assert.equal(audio.deckStates[deck].anlz, null);
	}
});

test('refreshes every loaded deck independently with its own real beatgrid', async () => {
	audio.deckStates[1].stable_id = SID_TRACK_A;
	audio.deckStates[2].stable_id = SID_TRACK_B;
	audio.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };
	audio.deckStates[2].anlz = { beatgrid: { beat_count: 1, beats: [] } };

	await audio.engine.refreshDecksForAnalysisSourceChange();

	// SID_TRACK_A and SID_TRACK_B are seeded (fixtures/analysis_source_anlz_server.py)
	// with 10 and 20 real downbeats respectively - a genuinely different own grid
	// per track, not two branches of one fabricated response.
	assert.ok(audio.deckStates[1].anlz.beatgrid.beat_count > 0);
	assert.ok(audio.deckStates[2].anlz.beatgrid.beat_count > 0);
	assert.notEqual(
		audio.deckStates[1].anlz.beatgrid.beat_count,
		audio.deckStates[2].anlz.beatgrid.beat_count,
		'two tracks with different real downbeat counts must not collapse to the same grid'
	);
});

test('a deck swapped to a different track mid-request keeps the new track, not the stale response', async () => {
	audio.deckStates[1].stable_id = SID_SLOW; // server-side real 150ms delay
	audio.deckStates[1].anlz = { beatgrid: { beat_count: 1, beats: [] } };

	const refreshPromise = audio.engine.refreshDecksForAnalysisSourceChange();
	// Simulate a real deck load landing before the analysis-source refetch settles -
	// the slow request above is still in flight against the real server.
	await new Promise((resolve) => setTimeout(resolve, 0));
	audio.deckStates[1].stable_id = 'track-c';
	const newAnlz = { beatgrid: { beat_count: 7, beats: [] } };
	audio.deckStates[1].anlz = newAnlz;

	await refreshPromise;

	assert.equal(audio.deckStates[1].stable_id, 'track-c');
	assert.equal(
		audio.deckStates[1].anlz,
		newAnlz,
		'the stale slow-track response must not overwrite the deck that has since loaded track-c'
	);
});
