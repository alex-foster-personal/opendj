/**
 * Issue #11393: PaneStore.rows and search_results use $state.raw with whole-row
 * replacements for lazy hydration and edits.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');

let contract;
before(async () => {
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
});

function _row(overrides = {}) {
	return {
		stable_id: 'sid-a',
		item_id: null,
		order: 1,
		title: 't',
		artist: null,
		key: null,
		bpm: null,
		rating: 3,
		etag: 'e1',
		comments: null,
		duration_ms: null,
		genre: null,
		energy: null,
		energy_source: null,
		energy_reason: '',
		file_exists: true,
		file_availability: 'present',
		quality: null,
		play_count: 0,
		is_streaming: null,
		strip: null,
		vocals: { status: 'not_analyzed' },
		stems: { status: 'none' },
		rb_meta: null,
		revealed: false,
		match_context: null,
		...overrides
	};
}

test('[if] PaneStore loads rows [then] rows and search_results are $state.raw arrays [else stop]', () => {
	const src = read('src/lib/components/rb/browser/pane-contract.svelte.ts');
	assert.match(src, /\brows = \$state\.raw<BrowserRow\[\]>\(\[\]\)/);
	assert.match(src, /\bsearch_results = \$state\.raw<BrowserRow\[\]>\(\[\]\)/);
});

test('[if] a rating lands on a raw row [then] updatePaneRow replaces the whole row [else stop]', () => {
	const pane = contract.createPaneStore();
	const before = _row({ rating: 1 });
	pane.rows = [before];
	contract.updatePaneRow(pane, 'sid-a', (row) => ({ ...row, rating: 5, etag: 'e2' }));
	assert.notEqual(pane.rows[0], before);
	assert.equal(pane.rows[0].rating, 5);
	assert.equal(pane.rows[0].etag, 'e2');
});

test('[if] availability settles on pending rows [then] merge returns new row objects [else stop]', async () => {
	const pending = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pending-availability-settle.ts'
	);
	const held = [
		{
			stable_id: 'a',
			file_exists: null,
			file_availability: 'AVAILABILITY_PENDING'
		}
	];
	const { rows, stillPending } = pending.applySettledAvailability(held, [
		{ stable_id: 'a', file_exists: true, file_availability: 'present' }
	]);
	assert.equal(stillPending, 0);
	assert.notEqual(rows[0], held[0]);
	assert.equal(rows[0].file_exists, true);
});

test('[if] a strip decode arrives [then] applyDecodedStripAcrossPanes replaces rows [else stop]', () => {
	const strip = { columns: 'e30=', max: 1 };
	const pane = contract.createPaneStore();
	const before = _row({ strip: null });
	pane.rows = [before];
	contract.applyDecodedStripAcrossPanes([pane], 'sid-a', strip);
	assert.notEqual(pane.rows[0], before);
	assert.deepEqual(pane.rows[0].strip, strip);
});

test('[if] row edit sites are wired [then] no listed TS module assigns into row fields in place [else stop]', () => {
	const paths = [
		'src/lib/components/rb/browser/pane-contract.svelte.ts',
		'src/lib/components/rb/browser/pending-availability-settle.ts',
		'src/lib/components/rb/browser/browser-row-wire.ts'
	];
	const banned = [
		/row\.revealed\s*=\s*[^=]/,
		/row\.rb_meta\s*=\s*[^=]/,
		/row\.rating\s*=\s*[^=]/,
		/row\.etag\s*=\s*[^=]/,
		/row\.grid_quality\s*=\s*[^=]/,
		/row\.file_exists\s*=\s*[^=]/,
		/row\.file_availability\s*=\s*[^=]/,
		/row\.strip\s*=\s*[^=]/,
		/Object\.assign\(row,/
	];
	for (const path of paths) {
		const src = read(path);
		for (const pattern of banned) {
			assert.equal(pattern.test(src), false, `${path} still matches ${pattern}`);
		}
	}
});
