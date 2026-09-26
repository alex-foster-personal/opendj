import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/reanalyze-batch-feedback.ts');

test('formatReanalyzeEnqueueToast explains zero-admitted refusals', () => {
	const result = {
		batch_id: 'qb_test',
		offered: 2,
		admitted: 0,
		refused: 2,
		workers: 1,
		band: 'empty',
		memory_model: {
			backend: 'own_beatgrid.backfill',
			producer_version: '1',
			floor_mb: 1,
			slope_mb_per_min: 1,
			measured_on: 'test',
			source: 'test'
		}
	};
	const progress = {
		batch_id: 'qb_test',
		state: 'done',
		workers: 1,
		band: 'empty',
		memory_model: result.memory_model,
		counts: {
			pending: 0,
			running: 0,
			done: 0,
			skipped: 0,
			failed: 0,
			refused: 2,
			cancelled: 0
		},
		total: 2,
		settled: 2,
		created_at: 't',
		updated_at: 't',
		items: [
			{
				stable_id: 'a',
				lane: 'beatgrid',
				backend: 'own_beatgrid.backfill',
				state: 'refused',
				reason: 'already fresh',
				attempts: 0,
				duration_s: null,
				predicted_peak_mb: null
			}
		]
	};
	const toast = mod.formatReanalyzeEnqueueToast(result, progress, 'beatgrid (own beatgrid backfill)');
	assert.match(toast.message, /beatgrid/);
	assert.match(toast.message, /queued 0 of 2/i);
	assert.match(toast.message, /already fresh/);
	assert.equal(toast.kind, 'warn');
});

test('formatReanalyzeProgressToast reports terminal counts', () => {
	const progress = {
		batch_id: 'qb_done',
		state: 'done',
		workers: 2,
		band: 'under_20_min',
		memory_model: {
			backend: 'own_beatgrid.backfill',
			producer_version: '1',
			floor_mb: 1,
			slope_mb_per_min: 1,
			measured_on: 'test',
			source: 'test'
		},
		counts: {
			pending: 0,
			running: 0,
			done: 3,
			skipped: 1,
			failed: 0,
			refused: 0,
			cancelled: 0
		},
		total: 4,
		settled: 4,
		created_at: 't',
		updated_at: 't',
		items: []
	};
	const toast = mod.formatReanalyzeProgressToast(progress, 'beatgrid (own beatgrid backfill)');
	assert.match(toast.message, /complete/i);
	assert.match(toast.message, /done 3/);
	assert.match(toast.message, /skipped 1/);
});
