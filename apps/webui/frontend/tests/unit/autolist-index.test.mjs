import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let index;

before(async () => {
	index = await loadTypeScriptModule('src/lib/smartlists/autolist-index.ts');
});

function synthRows(n) {
	const rows = [];
	for (let i = 0; i < n; i += 1) {
		rows.push({
			stable_id: `t-${i}`,
			genre: i % 3 === 0 ? 'House' : 'Techno',
			rating: i % 5 === 0 ? 5 : 3,
			bpm: 120 + (i % 10)
		});
	}
	return rows;
}

test('queryAutolistIndex returns genre posting list for House', () => {
	const built = index.buildAutolistIndex(synthRows(8000));
	const ids = index.queryAutolistIndex(built, { genre: ['House'], rating: [], bpm: [] });
	assert.ok(ids.length > 0);
	for (const id of ids) {
		const n = Number(id.slice(2));
		assert.equal(n % 3, 0);
	}
});

test('House and 5-star intersection narrows result', () => {
	const built = index.buildAutolistIndex(synthRows(8000));
	const houseOnly = index.queryAutolistIndex(built, { genre: ['House'], rating: [], bpm: [] });
	const both = index.queryAutolistIndex(built, { genre: ['House'], rating: ['5'], bpm: [] });
	assert.ok(both.length <= houseOnly.length);
});
