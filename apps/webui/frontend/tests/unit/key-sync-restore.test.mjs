/**
 * KEYSYNC-01: performance session snapshot persists KEY SYNC mode and target;
 * restore replays them; legacy blobs without those fields still parse.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let session;
let snap;

function channel() {
	return {
		trim: 0.5,
		eq_high: 0.5,
		eq_mid: 0.5,
		eq_low: 0.5,
		filter: 0.5,
		fader: 1,
		assign: 'THRU'
	};
}

function stemDeck() {
	return {
		vocal: { muted: false, solo: false, gain: 0.5 },
		instrumental: { muted: false, solo: false, gain: 0.5 },
		drums: { muted: false, solo: false, gain: 0.5 }
	};
}

function deckBase(overrides = {}) {
	return {
		stable_id: 'sid-key-restore',
		position_ms: 0,
		pitch: 1,
		pitch_range: 8,
		quantize_enabled: true,
		beat_sync_enabled: false,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		playing: false,
		...overrides
	};
}

function snapshotInput(overrides = {}) {
	return {
		captured_at_ms: 1_700_000_000_000,
		playlist_id: null,
		master_deck: 1,
		decks: {
			1: deckBase({ stable_id: 'sid-1' }),
			2: deckBase({
				stable_id: 'sid-2',
				key_sync_enabled: true,
				key_sync_mode: 'relative',
				key_sync_target: 3
			}),
			3: deckBase({ stable_id: 'sid-3' }),
			4: deckBase()
		},
		mixer: {
			crossfader: 0.5,
			master: 0.8,
			master_set_by_operator: true,
			channels: { 1: channel(), 2: channel(), 3: channel(), 4: channel() }
		},
		stems: { 1: stemDeck(), 2: stemDeck(), 3: stemDeck(), 4: stemDeck() },
		...overrides
	};
}

before(async () => {
	({ session, snapshot: snap } = await loadTypeScriptModule(
		'tests/unit/fixtures/performance-session-restore-entry.ts'
	));
});

test('KEYSYNC-01: snapshot round-trip keeps relative mode and deck 3 target; restore dispatches them', async () => {
	const raw = snap.serializePerformanceSession(snapshotInput());
	const parsed = snap.parsePerformanceSession(raw);
	assert.notEqual(parsed, null);
	assert.equal(parsed.decks[2].key_sync_mode, 'relative');
	assert.equal(parsed.decks[2].key_sync_target, 3);
	const dispatched = [];
	await session.restoreDeckConfigFromSnapshot(
		async (cmd) => {
			dispatched.push(cmd);
			return null;
		},
		2,
		parsed
	);
	const ks = dispatched.find((c) => c.type === 'key_sync');
	assert.deepEqual(ks, {
		type: 'key_sync',
		deck: 2,
		enabled: true,
		mode: 'relative',
		target: 3
	});
});

test('KEYSYNC-01: live master target serializes as master and restores', async () => {
	const input = snapshotInput({
		decks: {
			1: deckBase({ stable_id: 'sid-1' }),
			2: deckBase({
				stable_id: 'sid-2',
				key_sync_enabled: true,
				key_sync_mode: 'exact',
				key_sync_target: 'master'
			}),
			3: deckBase(),
			4: deckBase()
		}
	});
	const parsed = snap.parsePerformanceSession(snap.serializePerformanceSession(input));
	assert.notEqual(parsed, null);
	assert.equal(parsed.decks[2].key_sync_target, 'master');
	const dispatched = [];
	await session.restoreDeckConfigFromSnapshot(async (cmd) => dispatched.push(cmd), 2, parsed);
	const ks = dispatched.find((c) => c.type === 'key_sync');
	assert.deepEqual(ks, {
		type: 'key_sync',
		deck: 2,
		enabled: true,
		mode: 'exact',
		target: 'master'
	});
});

test('KEYSYNC-01: legacy snapshot without mode/target parses; restore sends key_sync without mode/target', async () => {
	const blob = JSON.parse(snap.serializePerformanceSession(snapshotInput()));
	for (const deckId of [1, 2, 3, 4]) {
		delete blob.decks[String(deckId)].key_sync_mode;
		delete blob.decks[String(deckId)].key_sync_target;
	}
	const parsed = snap.parsePerformanceSession(JSON.stringify(blob));
	assert.notEqual(parsed, null);
	const dispatched = [];
	await session.restoreDeckConfigFromSnapshot(async (cmd) => dispatched.push(cmd), 2, parsed);
	const ks = dispatched.find((c) => c.type === 'key_sync');
	assert.deepEqual(ks, { type: 'key_sync', deck: 2, enabled: true });
	assert.equal(Object.keys(ks).sort().join(','), 'deck,enabled,type');
});

test('KEYSYNC-01: invalid key_sync_mode or key_sync_target rejects the whole snapshot', () => {
	const badMode = JSON.parse(snap.serializePerformanceSession(snapshotInput()));
	badMode.decks['2'].key_sync_mode = 'loose';
	assert.equal(snap.parsePerformanceSession(JSON.stringify(badMode)), null);
	const badTarget = JSON.parse(snap.serializePerformanceSession(snapshotInput()));
	badTarget.decks['2'].key_sync_target = 7;
	assert.equal(snap.parsePerformanceSession(JSON.stringify(badTarget)), null);
});
