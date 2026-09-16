// requirement: LIBM-127
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// The pinned Node (22.14.0) cannot import a .ts file directly, so the module is
// bundled by esbuild like every other TypeScript-backed unit test here.
let filterRowsByTag;
let taglistPaneId;
let tagNameFromPaneId;
let tracksQueryForTaglist;

before(async () => {
	({ filterRowsByTag, taglistPaneId, tagNameFromPaneId, tracksQueryForTaglist } =
		await loadTypeScriptModule('src/lib/components/rb/browser/taglist-nav.ts'));
});

test('taglistPaneId encodes the tag name verbatim', () => {
	assert.equal(taglistPaneId('openers'), 'taglist:openers');
});

test('tagNameFromPaneId decodes taglist pane ids', () => {
	assert.equal(tagNameFromPaneId('taglist:openers'), 'openers');
	assert.equal(tagNameFromPaneId('all'), null);
});

test('tracksQueryForTaglist maps pane id to tag query', () => {
	assert.deepEqual(tracksQueryForTaglist('taglist:openers'), { tag: 'openers' });
});

test('filterRowsByTag keeps rows that include the tag', () => {
	const rows = [
		{ stable_id: 'a', tags: ['openers', 'peak'] },
		{ stable_id: 'b', tags: ['peak'] },
		{ stable_id: 'c', tags: [] }
	];
	const filtered = filterRowsByTag(rows, 'openers');
	assert.equal(filtered.length, 1);
	assert.equal(filtered[0].stable_id, 'a');
});
