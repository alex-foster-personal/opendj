/**
 * Issue #2456 / LIBM-63 / FLOW-14: BulkEditModal shows selection consensus.
 *
 * [if] a multi-row selection with a shared rating/notes value is opened in Bulk Edit
 *   [then] that shared value is shown, not a hardcoded default
 * [if] a multi-row selection has mixed values for a field
 *   [then] the modal shows "Multiple," never a fabricated single value
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const BULK_EDIT_MODAL = `${SRC}/lib/components/rb/BulkEditModal.svelte`;
const TRACK_EDIT_MODALS = `${SRC}/lib/components/rb/TrackEditModals.svelte`;
const BROWSER_PANEL = `${SRC}/lib/components/rb/BrowserPanel.svelte`;

function stripComments(src) {
	return src
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/\/\/.*$/gm, '');
}

function row(stable_id, rating, comments) {
	return { stable_id, rating, comments };
}

async function renderBulkEditModal(stableIds, rows) {
	const etags = Object.fromEntries(stableIds.map((id) => [id, 'etag']));
	const entry = [
		"export { default as BulkEditModal } from '$lib/components/rb/BulkEditModal.svelte';",
		"export { render } from 'svelte/server';"
	].join('\n');
	const ssr = await loadSvelteSsrModule(entry);
	return ssr.render(ssr.BulkEditModal, {
		props: {
			stableIds,
			etags,
			rows,
			onclose: () => {},
			onapplied: () => {}
		}
	}).body;
}

test('shared rating and notes render actual values, not hardcoded 3 or Multiple', async () => {
	const stableIds = ['a', 'b'];
	const rows = [row('a', 4, 'warmup'), row('b', 4, 'warmup')];
	const html = await renderBulkEditModal(stableIds, rows);
	assert.match(html, /warmup/);
	assert.match(html, /value="4"/);
	assert.doesNotMatch(html, /Multiple/);
	assert.doesNotMatch(html, /value="3"/);
});

test('mixed rating and notes show Multiple readout, not fabricated input values', async () => {
	const stableIds = ['a', 'b'];
	const rows = [row('a', 4, 'a'), row('b', 5, 'b')];
	const html = await renderBulkEditModal(stableIds, rows);
	assert.match(html, /Multiple/);
	assert.doesNotMatch(html, /value="4"/);
	assert.doesNotMatch(html, /value="5"/);
	assert.doesNotMatch(html, /value="a"/);
	assert.doesNotMatch(html, /value="b"/);
});

test('all-null ratings and blank comments show no Multiple and no value="3"', async () => {
	const stableIds = ['a', 'b'];
	const rows = [row('a', null, ''), row('b', null, '')];
	const html = await renderBulkEditModal(stableIds, rows);
	assert.doesNotMatch(html, /Multiple/);
	assert.doesNotMatch(html, /value="3"/);
});

test('BulkEditModal source drops hardcoded rating default and uses bulk-edit-values', () => {
	const src = stripComments(readFileSync(BULK_EDIT_MODAL, 'utf8'));
	assert.doesNotMatch(src, /\$state\(\s*3\s*\)/);
	assert.match(src, /bulkEditFieldValues/);
	assert.match(src, /MIXED_READOUT/);
	assert.match(src, /\{MIXED_READOUT\}/);
});

test('TrackEditModals passes rows only to BulkEditModal', () => {
	const src = stripComments(readFileSync(TRACK_EDIT_MODALS, 'utf8'));
	assert.match(src, /<BulkEditModal[^>]*\{rows\}/);
	assert.doesNotMatch(src, /<FindReplaceModal[^>]*\{rows\}/);
	assert.doesNotMatch(src, /<MyTagEditorModal[^>]*\{rows\}/);
});

test('BrowserPanel passes pane.rows to TrackEditModals without a new import', () => {
	const panel = readFileSync(BROWSER_PANEL, 'utf8');
	const importCountBefore = (panel.match(/^import\s/gm) ?? []).length;
	assert.match(panel, /rows=\{pane\.rows\}/);
	const importCountAfter = (panel.match(/^import\s/gm) ?? []).length;
	assert.equal(importCountBefore, importCountAfter);
});
