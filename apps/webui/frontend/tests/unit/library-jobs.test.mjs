/**
 * Library job enqueue chunking, menu selection, and lyrics LHS slot.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

test('chunk helper splits 1200 ids into 500/500/200', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/api-library-jobs.ts');
	const ids = Array.from({ length: 1200 }, (_, i) => `t${i}`);
	const chunks = mod.chunkStableIds(ids, 500);
	assert.equal(chunks.length, 3);
	assert.equal(chunks[0].length, 500);
	assert.equal(chunks[1].length, 500);
	assert.equal(chunks[2].length, 200);
	assert.equal(chunks[0][0], 't0');
	assert.equal(chunks[2][199], 't1199');
});

test('menu builder passes the selection of N ids to run', async () => {
	// #2286 (2464797fa) moved the row menu out of TrackTable into
	// TrackContextMenu, where every multi-target action runs on targetIds.
	const src = readFileSync(`${SRC}/lib/components/rb/browser/TrackContextMenu.svelte`, 'utf8');
	assert.match(src, /onstemsdonext \? \(\) => onstemsdonext\(targetIds\)/);
	assert.match(src, /onlyricsdonext \? \(\) => onlyricsdonext\(targetIds\)/);
	assert.match(src, /Stems: do next/);
	assert.match(src, /Lyrics: do next/);
	assert.match(src, /targetIds: menuTargetIds\(row, selectedOrders, selectedIds\)/);
	// ...and targetIds IS the selection of N ids when the clicked row is in it.
	const { menuTargetIds } = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
	const selectedIds = ['t0', 't1', 't2'];
	assert.deepEqual(menuTargetIds({ stable_id: 't1', order: 1 }, [0, 1, 2], selectedIds), selectedIds);
	assert.deepEqual(menuTargetIds({ stable_id: 't9', order: 9 }, [0, 1, 2], selectedIds), ['t9']);
});

test('LHS dots replace other with lyrics', async () => {
	const jobs = await loadTypeScriptModule('src/lib/rb/job-progress.svelte.ts');
	assert.equal(jobs.ANALYSIS_DOT_SLOTS.includes('lyrics'), true);
	assert.equal(jobs.ANALYSIS_DOT_SLOTS.includes('other'), false);
	assert.equal(jobs.ANALYSIS_LABELS.lyrics, 'Lyrics');
	assert.equal(jobs.isJobStuck({ kind: 'stems', phase: 'running', started_at: 0 }, Date.now()), false);
});

test('panel and client name every HTTP verb', () => {
	const panel = readFileSync(
		new URL('../../src/lib/components/rb/library-jobs/LibraryJobQueuePanel.svelte', import.meta.url),
		'utf8'
	);
	const client = readFileSync(new URL('../../src/lib/rb/api-library-jobs.ts', import.meta.url), 'utf8');
	for (const fn of ['enqueueLibraryJobs', 'listLibraryJobs', 'reorderLibraryJob', 'cancelLibraryJob']) {
		assert.equal(client.includes(fn), true, fn);
		assert.equal(panel.includes(fn) || fn === 'enqueueLibraryJobs', true, fn);
	}
	assert.match(client, /library-jobs\/enqueue/);
});


