/**
 * browser-search-query.ts - the `field:operator:value` grammar layered on
 * top of the browser's plain substring search (pin 7ca47b21ead7, issue #936).
 *
 * Pure module, no DOM / no BrowserRow import - covered here in isolation so
 * the grammar's edge cases (malformed input, unknown fields, multi-term AND)
 * are pinned independent of the table that consumes it.
 *
 * Regression lines:
 * - if a plain query (no recognized field prefix) stops matching EXACTLY
 *   like the old whole-string substring search then existing searches break
 * - if `bpm:120-128` stops being an inclusive range then a range query lies
 * - if `rating:>=4` / `<=` / `>` / `<` / `=` don't map to the right comparator
 *   then a numeric filter silently returns the wrong rows
 * - if `key:8A` stops being scoped to the key field then it is no different
 *   from typing `8A` plain
 * - if an unknown field name (e.g. `foo:bar`) returns zero rows instead of
 *   degrading to a plain substring then a typo silently empties the table
 * - if malformed numeric input (e.g. `bpm:abc`) throws or empties the table
 *   instead of degrading to substring then one bad keystroke breaks search
 * - if two terms stop AND-ing together then a compound query over-matches
 * - if matching stops being case-insensitive then a differently-cased query
 *   silently misses rows it used to find
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/browser-search-query.ts');
});

function track(overrides = {}) {
	return {
		title: null,
		artist: null,
		comments: null,
		key: null,
		genre: null,
		bpm: null,
		rating: null,
		...overrides
	};
}

test('plain query with no field prefix matches title/artist/comments/key/genre as a substring, case-insensitively', () => {
	const t = track({ artist: 'Daft Punk' });
	assert.equal(mod.matchesSearchQuery(t, 'DAFT'), true);
	assert.equal(mod.matchesSearchQuery(t, 'zzz'), false);
});

test('plain multi-word query behaves exactly like the old whole-string substring match (not tokenized)', () => {
	// A track whose comments contain the phrase as a run, vs one that has
	// both words but not adjacent - only the former matched before this pin,
	// and a plain (no field prefix) query must keep it that way.
	const adjacent = track({ comments: 'deep house classic' });
	const scattered = track({ title: 'deep cut', artist: 'house band' });
	assert.equal(mod.matchesSearchQuery(adjacent, 'deep house'), true);
	assert.equal(mod.matchesSearchQuery(scattered, 'deep house'), false);
});

test('genre: strict matches a comma-split genre token exactly; genre:~ is loose substring', () => {
	const exact = track({ genre: 'House, Disco' });
	const deep = track({ genre: 'Deep House' });
	assert.equal(mod.matchesSearchQuery(exact, 'genre:House'), true);
	assert.equal(mod.matchesSearchQuery(deep, 'genre:House'), false);
	assert.equal(mod.matchesSearchQuery(deep, 'genre:~House'), true);
});

test('genre: preserves a multi-word tag as one whole-query predicate (chip-click path)', () => {
	const t = track({ genre: 'Deep House' });
	assert.equal(mod.matchesSearchQuery(t, 'genre:Deep House'), true);
	assert.equal(mod.matchesSearchQuery(t, 'genre:~Deep House'), true);
});

test('bpm: inclusive range', () => {
	assert.equal(mod.matchesSearchQuery(track({ bpm: 120 }), 'bpm:120-128'), true);
	assert.equal(mod.matchesSearchQuery(track({ bpm: 128 }), 'bpm:120-128'), true);
	assert.equal(mod.matchesSearchQuery(track({ bpm: 119.9 }), 'bpm:120-128'), false);
	assert.equal(mod.matchesSearchQuery(track({ bpm: 129 }), 'bpm:120-128'), false);
});

test('bpm/rating: comparison operators >=, <=, >, <, =', () => {
	assert.equal(mod.matchesSearchQuery(track({ bpm: 120 }), 'bpm:>=120'), true);
	assert.equal(mod.matchesSearchQuery(track({ bpm: 119 }), 'bpm:>=120'), false);
	assert.equal(mod.matchesSearchQuery(track({ rating: 4 }), 'rating:>=4'), true);
	assert.equal(mod.matchesSearchQuery(track({ rating: 3 }), 'rating:>=4'), false);
	assert.equal(mod.matchesSearchQuery(track({ rating: 4 }), 'rating:<=4'), true);
	assert.equal(mod.matchesSearchQuery(track({ rating: 5 }), 'rating:<=4'), false);
	assert.equal(mod.matchesSearchQuery(track({ rating: 5 }), 'rating:>4'), true);
	assert.equal(mod.matchesSearchQuery(track({ rating: 4 }), 'rating:>4'), false);
	assert.equal(mod.matchesSearchQuery(track({ rating: 3 }), 'rating:<4'), true);
	assert.equal(mod.matchesSearchQuery(track({ rating: 4 }), 'rating:<4'), false);
	assert.equal(mod.matchesSearchQuery(track({ rating: 4 }), 'rating:=4'), true);
	assert.equal(mod.matchesSearchQuery(track({ rating: 4 }), 'rating:4'), true);
	assert.equal(mod.matchesSearchQuery(track({ rating: 3 }), 'rating:4'), false);
});

test('a numeric predicate never matches a null field', () => {
	assert.equal(mod.matchesSearchQuery(track({ bpm: null }), 'bpm:120-128'), false);
	assert.equal(mod.matchesSearchQuery(track({ rating: null }), 'rating:>=4'), false);
});

test('key: scopes the match to the key field only, case-insensitively', () => {
	const t = track({ key: '8A', title: '8A Anthem' });
	assert.equal(mod.matchesSearchQuery(t, 'key:8a'), true);
	assert.equal(mod.matchesSearchQuery(track({ key: '9A' }), 'key:8A'), false);
	// A plain (unprefixed) query still matches title/artist/comments/key/genre
	// as before - key: is an additional scoped predicate, not a replacement.
	assert.equal(mod.matchesSearchQuery(track({ title: '8A Anthem', key: '9A' }), '8A'), true);
});

test('an unknown field name degrades to a plain substring instead of matching nothing', () => {
	const t = track({ comments: 'foo:bar special edition' });
	assert.equal(mod.matchesSearchQuery(t, 'foo:bar'), true);
	assert.equal(mod.matchesSearchQuery(track({ comments: 'nothing here' }), 'foo:bar'), false);
});

test('malformed numeric predicates degrade to a plain substring rather than throwing or matching nothing', () => {
	const t = track({ comments: 'bpm:abc note' });
	assert.equal(mod.matchesSearchQuery(t, 'bpm:abc'), true);
	assert.equal(mod.matchesSearchQuery(track({ comments: 'nope' }), 'bpm:abc'), false);
	// empty value after the colon
	const t2 = track({ comments: 'bpm: placeholder' });
	assert.equal(mod.matchesSearchQuery(t2, 'bpm:'), true);
	// backwards range
	const t3 = track({ comments: 'bpm:128-120 weird' });
	assert.equal(mod.matchesSearchQuery(t3, 'bpm:128-120'), true);
});

test('multiple terms AND together', () => {
	const hit = track({ artist: 'Daft Punk', bpm: 123, rating: 5 });
	const wrongBpm = track({ artist: 'Daft Punk', bpm: 90, rating: 5 });
	const wrongArtist = track({ artist: 'Someone Else', bpm: 123, rating: 5 });
	assert.equal(mod.matchesSearchQuery(hit, 'Daft bpm:120-128 rating:>=4'), true);
	assert.equal(mod.matchesSearchQuery(wrongBpm, 'Daft bpm:120-128 rating:>=4'), false);
	assert.equal(mod.matchesSearchQuery(wrongArtist, 'Daft bpm:120-128 rating:>=4'), false);
});

test('an empty (or whitespace-only) query matches everything', () => {
	assert.equal(mod.matchesSearchQuery(track(), ''), true);
	assert.equal(mod.matchesSearchQuery(track(), '   '), true);
});

test('SEARCH_QUERY_HELP documents the grammar with runnable examples', () => {
	assert.ok(Array.isArray(mod.SEARCH_QUERY_HELP));
	assert.ok(mod.SEARCH_QUERY_HELP.length >= 4);
	for (const entry of mod.SEARCH_QUERY_HELP) {
		assert.equal(typeof entry.example, 'string');
		assert.equal(typeof entry.hint, 'string');
		// Every documented example must actually parse as a predicate, not
		// silently degrade to substring - the explainer must never teach a
		// syntax the parser does not accept.
		assert.equal(
			mod.matchesSearchQuery(track({ bpm: 124, rating: 5, key: '8A', genre: 'House' }), entry.example),
			true,
			`documented example "${entry.example}" should match a fully-populated track`
		);
	}
});
