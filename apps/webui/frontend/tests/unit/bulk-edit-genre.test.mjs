import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as BulkEditModal } from '$lib/components/rb/BulkEditModal.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function renderModal() {
	return mod.render(mod.BulkEditModal, {
		props: {
			stableIds: ['sid-1'],
			etags: { 'sid-1': '"e0"' },
			onclose: () => {},
			onapplied: () => {}
		}
	}).body;
}

test('BulkEditModal exposes genre and comments controls alongside existing fields', () => {
	const html = renderModal();
	assert.match(html, /Genre/);
	assert.match(html, /Comments/);
	assert.match(html, /Rating/);
	assert.match(html, /Notes/);
	assert.match(html, /Add tags/);
	assert.match(html, /Remove tags/);
});
