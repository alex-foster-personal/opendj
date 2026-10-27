/**
 * AUTOGAIN-02 in Rust engine mode: the page sends the engine an `auto_gain`
 * command whenever a deck's auto gain is published.
 *
 * Regression lines:
 * - if a published offset never reaches the Rust engine then Rust mode plays unmatched: broken
 * - if an unloaded deck keeps the old track's offset in the engine then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
const sent = [];

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-auto-gain-entry.ts');
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) => {
			sent.push(cmd);
			return Promise.resolve({ type: 'result', id: null, ok: true });
		},
		positionMs: () => null
	});
	m.activateRustEngineMode(null);
});

test('publishing a deck offset sends auto_gain to the engine, and unload sends 0 dB', () => {
	m.publishDeckAutoGain(2, {
		state: 'ready',
		value: {
			stable_id: 'a'.repeat(40), status: 'analysed', enabled: true, target_lufs: -12,
			integrated_lufs: -7.5, true_peak_dbtp: 1, analysed_offset_db: -4.5, user_offset_db: null,
			effective_offset_db: -4.5, limited_by: null, loudness_reason: null,
			offset_min_db: -12, offset_max_db: 12, true_peak_ceiling_dbtp: -1
		}
	});
	assert.deepEqual(sent.at(-1), { type: 'auto_gain', deck: 2, db: -4.5 });
	m.publishDeckAutoGain(2, { state: 'empty' });
	assert.deepEqual(sent.at(-1), { type: 'auto_gain', deck: 2, db: 0 });
	m.installRustEngineClientForTest(null);
});

test('a gain the Rust engine refuses leaves the deck in AG ERR with its gain unknown, never reading as matched', async () => {
	const toasts = [];
	const refusedSends = [];
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) => {
			refusedSends.push(cmd);
			return Promise.reject(new Error('engine closed'));
		},
		positionMs: () => null
	});
	m.activateRustEngineMode((message) => toasts.push(message));
	m.publishDeckAutoGain(3, {
		state: 'ready',
		value: {
			stable_id: 'b'.repeat(40), status: 'analysed', enabled: true, target_lufs: -12,
			integrated_lufs: -7.5, true_peak_dbtp: 1, analysed_offset_db: -4.5, user_offset_db: null,
			effective_offset_db: -4.5, limited_by: null, loudness_reason: null,
			offset_min_db: -12, offset_max_db: 12, true_peak_ceiling_dbtp: -1
		}
	});
	await m.deckAutoGainApplied(3);
	assert.equal(m.deckAutoGain[3].state, 'error');
	assert.match(m.deckAutoGain[3].message, /did not acknowledge -4\.5 dB \(engine closed\); the gain it applies is unknown/);
	await m.deckAutoGainApplied(3);
	assert.deepEqual(refusedSends.map((c) => c.db), [-4.5, 0], 'the 0 dB error state is tried once, no loop');
	assert.ok(toasts.length >= 1);
	m.installRustEngineClientForTest(null);
	m.publishDeckAutoGain(3, { state: 'empty' });
});
