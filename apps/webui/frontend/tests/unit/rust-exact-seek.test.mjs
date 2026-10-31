/**
 * CMDK-19 (Codex r4279308132): an exact seek skips the deck's quantize snap in Rust engine mode
 * too, so an aligned Cmd K pair lands on its captured offset under either engine. A plain seek on
 * the same deck still snaps, so the exact flag is not read as "never quantize".
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let sent;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
});

beforeEach(() => {
	sent = [];
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) => {
			sent.push(cmd);
			return Promise.resolve({ type: 'result', id: null, ok: true });
		},
		positionMs: () => null
	});
	m.rustMode.enabled = true;
	m.rustMaster.deck = null;
	m.rustMaster.mode = 'auto';
	m.uiPrefs.beat_sync_max = false;
	const beats = Array.from({ length: 64 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
	Object.assign(m.deckStates[2], {
		stable_id: 'track-2',
		playing: false,
		audible: false,
		is_master: false,
		loop: null,
		cue_ms: null,
		position_ms: 0,
		pitch: 1,
		beat_sync_enabled: false,
		quantize_enabled: true,
		quantize_grid_beats: 1,
		sync_mode: 'beat',
		sync_error: null,
		duration_ms: 32_000,
		anlz: { beatgrid: { source: 'rekordbox', beats, beat_count: beats.length, status: 'ok' } }
	});
});

after(() => {
	m.installRustEngineClientForTest(null);
	m.rustMode.enabled = false;
});

const seeks = () => sent.filter((c) => c.type === 'seek' && c.deck === 2).map((c) => c.position_ms);

test('CMDK-19: an exact seek lands where it was sent in Rust engine mode, off the quantize grid', async () => {
	await m.executeInRustEngine({ type: 'seek', deck: 2, position_ms: 10_200, exact: true });
	assert.deepEqual(seeks(), [10_200]);
});

test('CMDK-19: a plain seek on the same quantized deck still snaps to the grid in Rust engine mode', async () => {
	await m.executeInRustEngine({ type: 'seek', deck: 2, position_ms: 10_200 });
	const [at] = seeks();
	assert.notEqual(at, 10_200, 'quantize still snaps a seek that did not ask to be exact');
	assert.equal(at % 500, 0, 'snapped onto the 120 BPM grid');
});
