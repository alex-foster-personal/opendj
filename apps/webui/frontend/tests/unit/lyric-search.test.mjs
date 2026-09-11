import assert from 'node:assert/strict';
import { after, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Part 3 of #935 (issue #1344): lyric-only search must never delay primary
// title/artist results, and must debounce ON TOP of waiting for them to
// settle. Regression lines this file exists to catch:
//
// - if update() fetches before the primary search has settled then lyric
//   results can render (or flicker) ahead of the metadata results they must
//   follow
// - if update() fetches on every keystroke instead of debouncing then a
//   typed query is one lyric network round trip per character
// - if a stale response is not dropped then a fast edit can render lyric
//   hits for a query the user already changed away from
// - if a failed fetch is swallowed instead of reported then a real backend
//   error looks identical to "no lyric matches"
// - if clearing the query does not clear the state immediately (no debounce
//   wait) then the divider and stale rows linger after the box is emptied

let lib;

test.before(async () => {
	lib = await loadTypeScriptModule('src/lib/rb/lyric-search.ts');
});

after(() => {
	mock.timers.reset();
});

function _fetcherFor(byQuery) {
	return async (query) => {
		const entry = byQuery[query];
		if (entry === undefined) throw new Error(`unexpected query ${JSON.stringify(query)}`);
		if (entry instanceof Error) throw entry;
		return entry;
	};
}

test('does not fetch until the primary search has settled', () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		let fetchCount = 0;
		const changes = [];
		const controller = lib.createLyricSearchController(
			async () => {
				fetchCount += 1;
				return { items: [], total: 0 };
			},
			(state) => changes.push(state),
			() => assert.fail('onError must not fire in this case')
		);

		controller.update('sunny', false, true);
		mock.timers.tick(10_000);

		assert.equal(fetchCount, 0, 'a fetch before the primary search settles races ahead of it');
		assert.deepEqual(
			changes,
			[lib.EMPTY_LYRIC_SEARCH_STATE],
			'the state must clear to empty synchronously while not-yet-settled'
		);
	} finally {
		mock.timers.reset();
	}
});

test('debounces: a burst of updates fires exactly one fetch, for the LAST query', () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const seen = [];
		const controller = lib.createLyricSearchController(
			async (query) => {
				seen.push(query);
				return { items: [{ stable_id: 's1', title: 't', artist: 'a', match_context: query }], total: 1 };
			},
			() => {},
			() => assert.fail('onError must not fire in this case')
		);

		for (const q of ['s', 'su', 'sun', 'sunn', 'sunny']) {
			controller.update(q, true, true);
			mock.timers.tick(50);
		}
		assert.equal(seen.length, 0, 'nothing may fetch while the burst is still arriving');

		mock.timers.tick(lib.LYRIC_SEARCH_DEBOUNCE_MS);
		assert.deepEqual(seen, ['sunny'], 'exactly one fetch, for the last query in the burst');
	} finally {
		mock.timers.reset();
	}
});

test('a stale response is dropped when a newer query supersedes it first', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const fetcher = _fetcherFor({
			sunny: { items: [{ stable_id: 'stale', title: null, artist: null, match_context: 'x' }], total: 1 },
			moonlight: { items: [{ stable_id: 'fresh', title: null, artist: null, match_context: 'y' }], total: 1 }
		});
		const changes = [];
		const controller = lib.createLyricSearchController(
			fetcher,
			(state) => changes.push(state),
			() => assert.fail('onError must not fire in this case')
		);

		controller.update('sunny', true, true);
		mock.timers.tick(lib.LYRIC_SEARCH_DEBOUNCE_MS);
		// Supersede before the first fetch's microtask queue is drained.
		controller.update('moonlight', true, true);
		mock.timers.tick(lib.LYRIC_SEARCH_DEBOUNCE_MS);
		await Promise.resolve();
		await Promise.resolve();

		const stableIds = changes.filter((s) => s !== lib.EMPTY_LYRIC_SEARCH_STATE).map((s) => s.items[0]?.stable_id);
		assert.ok(!stableIds.includes('stale'), 'a response superseded before landing must never render');
		assert.ok(stableIds.includes('fresh'), 'the current query\'s response must still render');
	} finally {
		mock.timers.reset();
	}
});

test('clearing the query clears state immediately, with no debounce wait', () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const changes = [];
		const controller = lib.createLyricSearchController(
			async () => ({ items: [], total: 0 }),
			(state) => changes.push(state),
			() => assert.fail('onError must not fire in this case')
		);

		controller.update('', true, true);

		assert.deepEqual(changes, [lib.EMPTY_LYRIC_SEARCH_STATE]);
	} finally {
		mock.timers.reset();
	}
});

test('a fetch failure reports onError and clears to empty, never throws', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const errors = [];
		const changes = [];
		const controller = lib.createLyricSearchController(
			async () => {
				throw new Error('network down');
			},
			(state) => changes.push(state),
			(message) => errors.push(message)
		);

		controller.update('sunny', true, true);
		mock.timers.tick(lib.LYRIC_SEARCH_DEBOUNCE_MS);
		await Promise.resolve();
		await Promise.resolve();

		assert.equal(errors.length, 1, 'a real backend failure must be reported, not swallowed');
		assert.match(errors[0], /network down/);
	} finally {
		mock.timers.reset();
	}
});

test('cancel() drops a pending fetch so it never applies', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		let fetchCount = 0;
		const changes = [];
		const controller = lib.createLyricSearchController(
			async () => {
				fetchCount += 1;
				return { items: [{ stable_id: 's1', title: null, artist: null, match_context: 'x' }], total: 1 };
			},
			(state) => changes.push(state),
			() => assert.fail('onError must not fire in this case')
		);

		controller.update('sunny', true, true);
		controller.cancel();
		mock.timers.tick(10_000);
		await Promise.resolve();

		assert.equal(fetchCount, 0, 'cancel must drop the pending timer, not just ignore its result');
	} finally {
		mock.timers.reset();
	}
});
