import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/genre-source.ts');
});

test('the genre cell names the provider the shown genre came from (GENRE-04)', () => {
	assert.equal(
		mod.genreCellTitle('Afro House', 'odj-fused', null),
		"Afro House (from Open DJ's inferred genre)"
	);
	assert.equal(mod.genreCellTitle('Afro House', 'beatport', null), 'Afro House (from Beatport)');
	assert.equal(mod.genreCellTitle('Garage', 'agent.discogs', null), 'Garage (from agent.discogs)');
});

test('an empty genre shows why, and a genre with no source shows the genre alone', () => {
	assert.equal(mod.genreCellTitle('', null, 'no genre in rekordbox'), 'no genre in rekordbox');
	assert.equal(mod.genreCellTitle('', null, null), undefined);
	assert.equal(mod.genreCellTitle('House', null, null), 'House');
});

test('the lazily loaded rekordbox genre fills an empty row unless the precedence hides the library tag (GENRE-04)', () => {
	const rb = { genre: 'Techno' };
	assert.equal(mod.listingGenre({ genre: 'House', genre_reason: null, rb_meta: rb }), 'House');
	assert.equal(
		mod.listingGenre({ genre: null, genre_reason: 'no genre in rekordbox', rb_meta: rb }),
		'Techno'
	);
	assert.equal(
		mod.listingGenre({
			genre: null,
			genre_reason: mod.GENRE_REASON_LIBRARY_TAG_HIDDEN,
			rb_meta: rb
		}),
		null
	);
	assert.equal(mod.listingGenre({ genre: null, genre_reason: null, rb_meta: null }), null);
});
