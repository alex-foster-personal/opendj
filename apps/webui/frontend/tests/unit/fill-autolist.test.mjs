import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let contract;
let fill;

before(async () => {
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
	fill = await loadTypeScriptModule('src/lib/components/rb/browser/fill-autolist.ts');
});

const emptySel = { genre: [], rating: [], bpm: [] };
const houseSel = { genre: ['House'], rating: [], bpm: [] };

test('empty selection paints zero rows without fetch', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('autolist', 'Autolists');
	let fetchCalls = 0;
	await fill.fillAutolistPane({
		pane: p,
		seq,
		selection: emptySel,
		pageSize: 500,
		fetchPage: async () => {
			fetchCalls += 1;
			return { items: [], tracks: [], total: 0, offset: 0, limit: 500 };
		},
		mapRow: (item, order) => ({ stable_id: item, order })
	});
	assert.equal(fetchCalls, 0);
	assert.equal(p.rows.length, 0);
	assert.equal(p.loading, false);
});

test('page 1 clears loading before page 2', async () => {
	const p = contract.createPaneStore();
	const seq = p.beginLoad('autolist', 'House');
	let fetchCalls = 0;
	let releasePage2;
	const page2Gate = new Promise((resolve) => {
		releasePage2 = resolve;
	});

	const walk = fill.fillAutolistPane({
		pane: p,
		seq,
		selection: houseSel,
		pageSize: 2,
		fetchPage: async (offset, limit) => {
			fetchCalls += 1;
			if (fetchCalls === 1) {
				return {
					items: ['a', 'b', 'c'],
					tracks: ['a', 'b'],
					total: 3,
					offset: 0,
					limit
				};
			}
			await page2Gate;
			return {
				items: ['a', 'b', 'c'],
				tracks: ['c'],
				total: 3,
				offset: 2,
				limit
			};
		},
		mapRow: (item, order) => ({ stable_id: item, order })
	});

	await Promise.resolve();
	assert.equal(p.loading, false);
	assert.equal(p.rows.length, 2);
	releasePage2();
	await walk;
	assert.equal(p.rows.length, 3);
});
