import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
});

test('menuTargetIds returns full selection when row is inside it', () => {
	const selected = ['a', 'b', 'c'];
	const ids = mod.menuTargetIds({ stable_id: 'b', order: 2 }, [1, 2, 3], selected);
	assert.deepEqual(ids, selected);
	assert.notEqual(ids, selected);
});

test('menuTargetIds returns clicked row only when outside selection', () => {
	assert.deepEqual(
		mod.menuTargetIds({ stable_id: 'solo', order: 9 }, [1, 2], ['a', 'b']),
		['solo']
	);
});

test('runCopyPaths joins local paths and toasts success', async () => {
	const toasts = [];
	const item = await mod.runCopyPaths(['a', 'b'], {
		getTrack: async (id) => ({
			track: { file_path: id === 'a' ? '/music/a.flac' : '/music/b.flac' }
		}),
		writeClipboard: async (text) => {
			assert.equal(text, '/music/a.flac\n/music/b.flac');
		},
		pushToast: (message, kind) => toasts.push({ message, kind })
	});
	assert.equal(item, undefined);
	assert.deepEqual(toasts, [{ message: 'Copied 2 paths', kind: 'info' }]);
});

test('runCopyPaths errors when no local paths remain', async () => {
	const toasts = [];
	await mod.runCopyPaths(['stream'], {
		getTrack: async () => ({ track: { file_path: 'spotify:track:abc' } }),
		writeClipboard: async () => {
			throw new Error('clipboard must not run');
		},
		pushToast: (message, kind) => toasts.push({ message, kind })
	});
	assert.deepEqual(toasts, [
		{ message: 'no file_path on GET /api/v1/tracks/stream', kind: 'error' }
	]);
});

test('runReanalyze enqueues beatgrid backfill once and toasts batch summary', async () => {
	const toasts = [];
	const seen = [];
	await mod.runReanalyze(['a', 'b'], {
		enqueueBackfill: async (args) => {
			seen.push(args);
			return { admitted: 2, offered: 2, refused: 0, batch_id: 'batch-1' };
		},
		backfillProgress: async () => ({
			batch_id: 'batch-1',
			state: 'queued',
			workers: 1,
			band: 'under_20_min',
			memory_model: {
				backend: 'own_beatgrid.backfill',
				producer_version: '1',
				floor_mb: 1,
				slope_mb_per_min: 1,
				measured_on: 't',
				source: 't'
			},
			counts: {
				pending: 2,
				running: 0,
				done: 0,
				skipped: 0,
				failed: 0,
				refused: 0,
				cancelled: 0
			},
			total: 2,
			settled: 0,
			created_at: 't',
			updated_at: 't',
			items: []
		}),
		pushToast: (message, kind, _dismiss, _cause, _ctx, groupKey, _action, presentationOverride) =>
			toasts.push({ message, kind, groupKey, presentationOverride }),
		watchBatch: (_batchId, _label, callbacks) => {
			callbacks.onTerminal(
				{
					batch_id: 'batch-1',
					state: 'done',
					workers: 1,
					band: 'under_20_min',
					memory_model: {
						backend: 'own_beatgrid.backfill',
						producer_version: '1',
						floor_mb: 1,
						slope_mb_per_min: 1,
						measured_on: 't',
						source: 't'
					},
					counts: {
						pending: 0,
						running: 0,
						done: 2,
						skipped: 0,
						failed: 0,
						refused: 0,
						cancelled: 0
					},
					total: 2,
					settled: 2,
					created_at: 't',
					updated_at: 't',
					items: []
				},
				{
					message:
						'Re-analyze (beatgrid (own beatgrid backfill)): complete — done 2, failed 0, skipped 0, refused 0, cancelled 0 (batch batch-1)',
					kind: 'info'
				}
			);
			return () => {};
		}
	});
	assert.deepEqual(seen, [
		{
			stableIds: ['a', 'b'],
			lane: 'beatgrid',
			backend: 'own_beatgrid.backfill',
			note: 'track context menu'
		}
	]);
	assert.ok(toasts.some((t) => t.message.includes('beatgrid')));
	assert.ok(toasts.some((t) => t.message.includes('done 2')));
	const terminal = toasts.find((t) => t.presentationOverride?.headline?.includes('complete'));
	assert.ok(terminal, 'terminal toast must carry collapsed headline');
	assert.match(terminal.presentationOverride.headline, /done 2/);
	assert.match(terminal.presentationOverride.detail, /batch batch-1/);
});

test('runReanalyze zero-admitted toast names lane and refusal', async () => {
	const toasts = [];
	await mod.runReanalyze(['a'], {
		enqueueBackfill: async () => ({
			admitted: 0,
			offered: 1,
			refused: 1,
			batch_id: 'batch-zero'
		}),
		backfillProgress: async () => ({
			batch_id: 'batch-zero',
			state: 'done',
			workers: 0,
			band: 'empty',
			memory_model: {
				backend: 'own_beatgrid.backfill',
				producer_version: '1',
				floor_mb: 1,
				slope_mb_per_min: 1,
				measured_on: 't',
				source: 't'
			},
			counts: {
				pending: 0,
				running: 0,
				done: 0,
				skipped: 0,
				failed: 0,
				refused: 1,
				cancelled: 0
			},
			total: 1,
			settled: 1,
			created_at: 't',
			updated_at: 't',
			items: [
				{
					stable_id: 'a',
					lane: 'beatgrid',
					backend: 'own_beatgrid.backfill',
					state: 'refused',
					reason: 'queue full',
					attempts: 0,
					duration_s: null,
					predicted_peak_mb: null
				}
			]
		}),
		pushToast: (message, kind, _dismiss, _cause, _ctx, groupKey, _action, presentationOverride) =>
			toasts.push({ message, kind, groupKey, presentationOverride }),
		watchBatch: () => () => {}
	});
	assert.ok(toasts[0].message.includes('beatgrid'));
	assert.ok(toasts[0].message.includes('queue full'));
	assert.match(toasts[0].presentationOverride.headline, /nothing queued/i);
	assert.match(toasts[0].presentationOverride.headline, /queued 0 of 1/i);
	assert.match(toasts[0].presentationOverride.detail, /batch batch-zero/);
});

test('runReanalyze in-progress update uses running status in collapsed headline', async () => {
	const toasts = [];
	await mod.runReanalyze(['a'], {
		enqueueBackfill: async () => ({
			admitted: 1,
			offered: 1,
			refused: 0,
			batch_id: 'batch-running'
		}),
		backfillProgress: async () => ({
			batch_id: 'batch-running',
			state: 'queued',
			workers: 1,
			band: 'under_20_min',
			memory_model: {
				backend: 'own_beatgrid.backfill',
				producer_version: '1',
				floor_mb: 1,
				slope_mb_per_min: 1,
				measured_on: 't',
				source: 't'
			},
			counts: {
				pending: 1,
				running: 0,
				done: 0,
				skipped: 0,
				failed: 0,
				refused: 0,
				cancelled: 0
			},
			total: 1,
			settled: 0,
			created_at: 't',
			updated_at: 't',
			items: []
		}),
		pushToast: (message, kind, _dismiss, _cause, _ctx, groupKey, _action, presentationOverride) =>
			toasts.push({ message, kind, groupKey, presentationOverride }),
		watchBatch: (_batchId, _label, callbacks) => {
			callbacks.onUpdate(
				{
					batch_id: 'batch-running',
					state: 'running',
					workers: 1,
					band: 'under_20_min',
					memory_model: {
						backend: 'own_beatgrid.backfill',
						producer_version: '1',
						floor_mb: 1,
						slope_mb_per_min: 1,
						measured_on: 't',
						source: 't'
					},
					counts: {
						pending: 0,
						running: 1,
						done: 0,
						skipped: 0,
						failed: 0,
						refused: 0,
						cancelled: 0
					},
					total: 1,
					settled: 0,
					created_at: 't',
					updated_at: 't',
					items: []
				},
				{
					message:
						'Re-analyze (beatgrid (own beatgrid backfill)): running — 0 done, 1 active, 0 failed (batch batch-running)',
					kind: 'info'
				}
			);
			return () => {};
		}
	});
	const inProgress = toasts.find((t) => t.presentationOverride?.headline?.includes('running'));
	assert.ok(inProgress, 'in-progress toast must carry collapsed headline');
	assert.match(inProgress.presentationOverride.headline, /running/i);
	assert.match(inProgress.presentationOverride.headline, /1 active/);
	assert.match(inProgress.presentationOverride.detail, /batch batch-running/);
});

test('runRevealTracks posts once per id and toasts successes', async () => {
	const seen = [];
	const toasts = [];
	await mod.runRevealTracks(['a', 'b'], {
		revealTrack: async (id) => {
			seen.push(id);
		},
		pushToast: (message, kind) => toasts.push({ message, kind })
	});
	assert.deepEqual(seen, ['a', 'b']);
	assert.deepEqual(toasts, [{ message: 'Revealed 2 tracks', kind: 'info' }]);
});

test('title constants match agent-native tooltips', () => {
	assert.equal(mod.REVEAL_TRACK_TITLE, 'POST /api/v1/tracks/{stable_id}:reveal');
	assert.equal(mod.COPY_PATH_TITLE, 'GET /api/v1/tracks/{stable_id}');
	assert.equal(
		mod.REANALYZE_TITLE,
		'POST /api/v1/analysis/backfill/enqueue (`python -m apps.analysis.queue_cli enqueue --lane beatgrid --backend own_beatgrid.backfill`)'
	);
	assert.equal(mod.loadDeckTitle(1, 'sid-123'), 'opendj load 1 sid-123');
});
