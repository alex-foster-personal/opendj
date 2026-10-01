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

const MEMORY_MODEL_FIXTURE = {
	backend: 'own_beatgrid.backfill',
	producer_version: '1',
	floor_mb: 1,
	slope_mb_per_min: 1,
	measured_on: 'test',
	source: 'test'
};

const PLAN_LABEL = 'beatgrid (own beatgrid backfill)';

test('formatReanalyzeProgressToast reports queued and running in-progress status', () => {
	for (const state of ['queued', 'running']) {
		const progress = {
			batch_id: 'qb_in_progress',
			state,
			workers: 2,
			band: 'under_20_min',
			memory_model: MEMORY_MODEL_FIXTURE,
			counts: {
				pending: state === 'queued' ? 2 : 0,
				running: state === 'running' ? 2 : 0,
				done: 1,
				skipped: 0,
				failed: 0,
				refused: 0,
				cancelled: 0
			},
			total: 3,
			settled: 1,
			created_at: 't',
			updated_at: 't',
			items: []
		};
		const toast = mod.formatReanalyzeProgressToast(progress, PLAN_LABEL);
		assert.match(toast.message, new RegExp(state, 'i'));
		assert.match(toast.message, /1 done/);
		assert.match(toast.message, /2 active/);

		const collapsed = mod.toReanalyzeToastPresentation(toast);
		assert.match(collapsed.headline, new RegExp(state, 'i'));
		assert.match(collapsed.headline, /1 done/);
		assert.match(collapsed.headline, /2 active/);
		assert.match(collapsed.detail, /batch qb_in_progress/);
		assert.notEqual(collapsed.headline, collapsed.detail);
	}
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

test('toReanalyzeToastPresentation puts queue status in headline and batch in detail', () => {
	const memoryModel = {
		backend: 'own_beatgrid.backfill',
		producer_version: '1',
		floor_mb: 1,
		slope_mb_per_min: 1,
		measured_on: 'test',
		source: 'test'
	};
	const enqueue = mod.formatReanalyzeEnqueueToast(
		{
			batch_id: 'qb_test',
			offered: 100,
			admitted: 0,
			refused: 100,
			workers: 1,
			band: 'empty',
			memory_model: memoryModel
		},
		null,
		'beatgrid (own beatgrid backfill)'
	);
	const collapsed = mod.toReanalyzeToastPresentation(enqueue);
	assert.match(collapsed.headline, /nothing queued/i);
	assert.match(collapsed.headline, /queued 0 of 100/i);
	assert.match(collapsed.headline, /beatgrid/i);
	assert.match(collapsed.detail, /batch qb_test/);
	assert.notEqual(collapsed.headline, collapsed.detail);

	const terminal = mod.toReanalyzeToastPresentation(
		mod.formatReanalyzeProgressToast(
			{
				batch_id: 'qb_done',
				state: 'done',
				workers: 1,
				band: 'under_20_min',
				memory_model: memoryModel,
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
			'beatgrid (own beatgrid backfill)'
		)
	);
	assert.match(terminal.headline, /complete/i);
	assert.match(terminal.headline, /done 2/);
	assert.match(terminal.detail, /batch qb_done/);
});
