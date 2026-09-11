/**
 * Business-logic coverage for src/lib/rb/pairing-capture.ts (PAIR-03): the
 * module CreatePairingSheet.svelte's Align hotcues / Reload sync actions call.
 * Bundled through lib/api.ts onto the one typed client, so assertions read
 * fetch's Request the same way tests/unit/api-base.test.mjs does; the wire
 * shape of the two underlying api.ts calls is pinned there, this file pins
 * the re-mapping and partial-failure behavior that sits on top of them.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://pairing-capture.example.test';

let pairingCapture;
let originalFetch;

before(async () => {
	pairingCapture = await loadTypeScriptModule('src/lib/rb/pairing-capture.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

function snapshotRow(overrides = {}) {
	return {
		id: 'snap-1',
		stable_a: 'track-a',
		stable_b: 'track-b',
		master_side: 'a',
		sync_mode: 'bar',
		a_tempo_ratio: 1.02,
		b_tempo_ratio: 0.99,
		a_position_beat_n: null,
		a_position_phase: null,
		a_position_ms: 12000,
		b_position_beat_n: null,
		b_position_phase: null,
		b_position_ms: 8000,
		captured_at: '2026-09-01T00:00:00Z',
		...overrides
	};
}

function hotCue(slot, in_ms) {
	return {
		slot,
		in_ms,
		out_ms: null,
		is_loop: false,
		beat_loop_size: null,
		color_table_index: null,
		comment: null
	};
}

// -------------------------------------------------------- latestSyncSnapshot

test('latestSyncSnapshot queries both stable_a/stable_b orders with limit=1', async () => {
	const seen = [];
	globalThis.fetch = async (request) => {
		seen.push(request.url);
		return jsonResponse([]);
	};

	await pairingCapture.latestSyncSnapshot('track-a', 'track-b');

	assert.equal(seen.length, 2, 'the repo matches stable_a/stable_b directionally, so both orders must be asked');
	assert.ok(seen.some((u) => u.includes('stable_a=track-a') && u.includes('stable_b=track-b')));
	assert.ok(seen.some((u) => u.includes('stable_a=track-b') && u.includes('stable_b=track-a')));
	assert.ok(seen.every((u) => u.includes('limit=1')));
});

test('latestSyncSnapshot returns null when neither order has a row', async () => {
	globalThis.fetch = async () => jsonResponse([]);

	const result = await pairingCapture.latestSyncSnapshot('track-a', 'track-b');

	assert.equal(result, null);
});

test('latestSyncSnapshot passes a direct-order row through unchanged', async () => {
	globalThis.fetch = async (request) => {
		const isDirect = new URL(request.url).searchParams.get('stable_a') === 'track-a';
		return jsonResponse(isDirect ? [snapshotRow()] : []);
	};

	const result = await pairingCapture.latestSyncSnapshot('track-a', 'track-b');

	assert.deepEqual(result, {
		masterIsA: true,
		aTempoRatio: 1.02,
		bTempoRatio: 0.99,
		aPositionMs: 12000,
		bPositionMs: 8000
	});
});

test('latestSyncSnapshot re-maps a snapshot captured with the decks reversed', async () => {
	// Real scenario: the same pair was captured last time with the two tracks
	// on the opposite decks. The repo's exact-column match means the row comes
	// back as stable_a=track-b/stable_b=track-a; its own "a" fields describe
	// OUR track-b, not our track-a, so they must be swapped on the way out.
	globalThis.fetch = async (request) => {
		const isReversed = new URL(request.url).searchParams.get('stable_a') === 'track-b';
		return jsonResponse(
			isReversed
				? [snapshotRow({ stable_a: 'track-b', stable_b: 'track-a', master_side: 'a' })]
				: []
		);
	};

	const result = await pairingCapture.latestSyncSnapshot('track-a', 'track-b');

	assert.deepEqual(result, {
		masterIsA: false, // the row's master ('a' = its stable_a = OUR track-b) is our "b"
		aTempoRatio: 0.99, // row.b_tempo_ratio belongs to row.stable_b = OUR track-a
		bTempoRatio: 1.02,
		aPositionMs: 8000,
		bPositionMs: 12000
	});
});

test('latestSyncSnapshot keeps whichever order captured more recently', async () => {
	globalThis.fetch = async (request) => {
		const isDirect = new URL(request.url).searchParams.get('stable_a') === 'track-a';
		return jsonResponse([
			isDirect
				? snapshotRow({ a_tempo_ratio: 1.1, b_tempo_ratio: 1.2, captured_at: '2026-09-01T00:00:00Z' })
				: snapshotRow({
						stable_a: 'track-b',
						stable_b: 'track-a',
						master_side: 'a',
						a_tempo_ratio: 2.1,
						b_tempo_ratio: 2.2,
						captured_at: '2026-09-05T00:00:00Z'
					})
		]);
	};

	const result = await pairingCapture.latestSyncSnapshot('track-a', 'track-b');

	// The reversed row is newer, so it wins and gets re-mapped: its "b" (2.2)
	// is OUR track-a, its "a" (2.1, and its master) is OUR track-b.
	assert.equal(result.aTempoRatio, 2.2);
	assert.equal(result.bTempoRatio, 2.1);
	assert.equal(result.masterIsA, false);
});

// -------------------------------------------------------------- alignHotcues

test('alignHotcues posts one alignment per matching slot letter, skipping the rest', async () => {
	const posted = [];
	globalThis.fetch = async (request) => {
		const body = await request.clone().json();
		posted.push(body);
		return jsonResponse({ id: `al-${posted.length}`, ...body, created_at: '2026-09-01T00:00:00Z' }, { status: 201 });
	};

	const result = await pairingCapture.alignHotcues(
		'track-a',
		'track-b',
		[hotCue('A', 1000), hotCue('B', 2000)],
		[hotCue('A', 1500), hotCue('C', 3000)]
	);

	assert.equal(result.paired, 1);
	assert.equal(posted.length, 1);
	assert.deepEqual(posted[0], {
		stable_a: 'track-a',
		stable_b: 'track-b',
		anchor_a_kind: 'hotcue',
		anchor_b_kind: 'hotcue',
		anchor_a_slot: 'A',
		anchor_b_slot: 'A',
		anchor_a_ms: 1000,
		anchor_b_ms: 1500,
		label: 'HC A'
	});
});

test('alignHotcues makes no request when no slot letter matches on both decks', async () => {
	let called = false;
	globalThis.fetch = async () => {
		called = true;
		return jsonResponse({});
	};

	const result = await pairingCapture.alignHotcues(
		'track-a',
		'track-b',
		[hotCue('A', 1000)],
		[hotCue('B', 2000)]
	);

	assert.equal(result.paired, 0);
	assert.equal(called, false);
});

test('alignHotcues reports how many pairs landed when a POST fails partway through', async () => {
	let calls = 0;
	globalThis.fetch = async (request) => {
		calls += 1;
		if (calls === 2) {
			return jsonResponse(
				{ detail: { code: 'PAIRING_CAPTURE_INVALID', message: 'boom' } },
				{ status: 422 }
			);
		}
		const body = await request.clone().json();
		return jsonResponse({ id: `al-${calls}`, ...body, created_at: '2026-09-01T00:00:00Z' }, { status: 201 });
	};

	const hotCuesA = [hotCue('A', 1000), hotCue('B', 2000), hotCue('C', 3000)];
	const hotCuesB = [hotCue('A', 1100), hotCue('B', 2100), hotCue('C', 3100)];

	const caught = await pairingCapture.alignHotcues('track-a', 'track-b', hotCuesA, hotCuesB).then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof pairingCapture.PartialAlignmentError, 'expected a PartialAlignmentError');
	assert.equal(caught.paired, 1, 'the A slot landed before the B slot failed');
	assert.equal(calls, 2, 'the C slot must not be attempted after a failure - earlier POSTs are already durable');
});
