// requirement: DECKGAP-7691c
// Review r4404895953: PLAY-27's unsynced start cleared sync_error right after
// the plain start, so the Play glow never showed on that path. The reason is
// now published AFTER the start, on both engines.
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let grid, math;
before(async () => {
	grid = await loadTypeScriptModule('src/lib/player/grid-features.ts');
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

test('the fallback marks the deck after the unsynced start, with the plain tempo wording', async () => {
	const order = [];
	const refusal = new grid.BeatSyncPhaseLockError(
		'Beat Sync: no followers could phase-lock (deck 2: x)',
		[
			{
				deck: 2,
				message:
					'follower grid has no phase-capable beat anchor with tempo ratio within [0.84, 1.16] for beat n=1',
				notice: 'n',
				outOfRange: true
			}
		]
	);
	const how = await grid.joinPhaseOrStartUnsynced(
		2,
		async () => {
			throw refusal;
		},
		async () => {
			order.push('start');
		},
		() => {},
		() => order.push('toast'),
		(deck, reason) => order.push(['mark', deck, reason])
	);
	assert.equal(how, 'unsynced');
	assert.deepEqual(order.slice(0, 2), ['toast', 'start']);
	const [kind, deck, reason] = order[2];
	assert.equal(kind, 'mark');
	assert.equal(deck, 2);
	assert.ok(reason.startsWith(math.TEMPO_TOO_FAR_TO_SYNC), reason);
	assert.equal(math.unsyncedPlayWarning(reason), math.TEMPO_TOO_FAR_TO_SYNC);
});

test('control: a refusal that is not a tempo out of range keeps its own words, and a lock marks nothing', async () => {
	const refusal = new grid.BeatSyncPhaseLockError('m', [
		{ deck: 3, message: 'master is past its grid', notice: 'n', outOfRange: false }
	]);
	const reason = grid.unsyncedStartSyncError(refusal, 3);
	assert.equal(reason, 'Beat Sync is not locked: master is past its grid');
	assert.equal(math.unsyncedPlayWarning(reason), reason, 'the warning is not prefixed twice');
	assert.ok(math.isUnsyncedStartSyncError(reason));
	// Control: a raw engine fault is still a deck error.
	assert.equal(math.isUnsyncedStartSyncError('phase lock lost: worklet died'), false);
	assert.equal(math.isUnsyncedStartSyncError(null), false);
	const marks = [];
	await grid.joinPhaseOrStartUnsynced(
		3,
		async () => {},
		async () => {},
		() => {},
		() => {},
		(d, r) => marks.push([d, r])
	);
	assert.deepEqual(marks, []);
});

test('the deck error strip leaves an unsynced-start reason out (PLAY-27: no deck error)', async () => {
	const { readFileSync } = await import('node:fs');
	const deckSrc = readFileSync(
		new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		deckSrc,
		/deck_errors\[deckId\] \?\?\s*bannerSyncError\(deck\.sync_error\) \?\?\s*deck\.processor_error/
	);
	const banner = readFileSync(
		new URL('../../src/lib/rb/deck-error-id.svelte.ts', import.meta.url),
		'utf8'
	);
	assert.match(banner, /return isUnsyncedStartSyncError\(syncError\) \? null : syncError;/);
});

let m, sent, positions;
before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
});
beforeEach(() => {
	sent = [];
	positions = {};
	m.link.toast = () => {};
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) => {
			sent.push(cmd);
			return Promise.resolve({ type: 'result', id: null, ok: true });
		},
		positionMs: (d) => positions[d] ?? null
	});
	m.rustMode.enabled = true;
	m.rustMode.keyLock = false;
	m.rustMaster.deck = null;
	m.rustMaster.mode = 'auto';
	for (const d of [1, 2, 3, 4]) {
		Object.assign(m.deckStates[d], {
			stable_id: null,
			playing: false,
			audible: false,
			is_master: false,
			anlz: null,
			loop: null,
			cue_ms: null,
			position_ms: 0,
			pitch: 1,
			beat_sync_enabled: false,
			quantize_enabled: false,
			quantize_grid_beats: 1,
			sync_mode: 'beat',
			sync_error: null
		});
		m.pitchRanges[d] = 16;
		m.mixerState.channels[d].fader = 1;
		m.mixerState.channels[d].trim = 0.5;
		m.mixerState.channels[d].assign = 'THRU';
	}
});
after(() => {
	m.installRustEngineClientForTest(null);
	m.rustMode.enabled = false;
});
const beats = (bpm) =>
	Array.from({ length: 300 }, (_, i) => ({ n: (i % 4) + 1, bpm, t: 0.1 + (i * 60) / bpm }));
const load = (d, b, extra = {}) =>
	Object.assign(m.deckStates[d], {
		stable_id: `track-${d}`,
		duration_ms: b.at(-1).t * 1000,
		anlz: { beatgrid: { source: 'rekordbox', beats: b, beat_count: b.length, status: 'ok' } },
		...extra
	});

test('rust mode: an out-of-range follower plays unsynced AND keeps the reason that makes Play glow', async () => {
	m.uiPrefs.beat_sync_max = true;
	load(1, beats(128));
	await m.executeInRustEngine({ type: 'play', deck: 1, playing: true });
	positions[1] = 20_000;
	load(2, beats(160), { beat_sync_enabled: true });
	await m.executeInRustEngine({ type: 'play', deck: 2, playing: true });
	assert.ok(sent.some((c) => c.type === 'play' && c.deck === 2));
	const err = m.deckStates[2].sync_error;
	assert.ok(
		typeof err === 'string' && err.startsWith(math.TEMPO_TOO_FAR_TO_SYNC),
		`sync_error was ${err}`
	);
});

test('rust mode control: a follower that locks carries no sync_error', async () => {
	m.uiPrefs.beat_sync_max = true;
	load(1, beats(128));
	await m.executeInRustEngine({ type: 'play', deck: 1, playing: true });
	positions[1] = 20_000;
	load(2, beats(126), { beat_sync_enabled: true });
	await m.executeInRustEngine({ type: 'play', deck: 2, playing: true });
	assert.equal(m.deckStates[2].sync_error, null);
});
