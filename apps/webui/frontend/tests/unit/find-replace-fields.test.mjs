import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as FindReplaceModal } from '$lib/components/rb/FindReplaceModal.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function renderModal() {
	return mod.render(mod.FindReplaceModal, {
		props: {
			stableIds: ['sid-1'],
			etags: { 'sid-1': '"e0"' },
			onclose: () => {},
			onapplied: () => {}
		}
	}).body;
}

test('FindReplaceModal offers notes, genre, and comments field options', () => {
	const html = renderModal();
	assert.match(html, /value="notes"/);
	assert.match(html, /value="genre"/);
	assert.match(html, /value="comments"/);
	assert.match(html, />Genre</);
	assert.match(html, /Find &amp; Replace - 1 track\(s\)/);
	assert.equal(html.includes('Find &amp; Replace - 1 track(s) - Notes'), false);
});
