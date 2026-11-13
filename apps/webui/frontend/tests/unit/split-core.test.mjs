/** Split-core solver contract for one resizable group. */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { solveLayout } = await loadTypeScriptModule('src/lib/layout/split-core.ts');

const group = {
	id: 'main',
	children: [
		{ id: 'left', min: 100, max: 400, default: 200 },
		{ id: 'center', flex: true, min: 50 },
		{ id: 'right', min: 80, max: 300, default: 150, collapsible: true, collapsedSize: 20 }
	]
};

test('px children take user size or default and flex gets remainder', () => {
	assert.deepEqual(solveLayout(1000, group, { sizes: { left: 250 } }), {
		left: 250,
		right: 150,
		center: 600
	});
});

test('user size outside min..max throws RangeError naming child and range', () => {
	assert.throws(
		() => solveLayout(1000, group, { sizes: { left: 500 } }),
		(e) => e instanceof RangeError && /"left"/.test(e.message) && /100\.\.400/.test(e.message)
	);
});

test('collapsed child resolves to collapsedSize and flex absorbs difference', () => {
	assert.deepEqual(solveLayout(1000, group, { collapsed: { right: true } }), {
		left: 200,
		right: 20,
		center: 780
	});
});

test('summed minimum above container throws naming both numbers', () => {
	assert.throws(
		() => solveLayout(200, group),
		(e) => e instanceof RangeError && /200/.test(e.message) && /230/.test(e.message)
	);
});

test('zero or two flex children throws naming the group', () => {
	const none = { id: 'g0', children: [{ id: 'a', default: 10 }] };
	const two = {
		id: 'g2',
		children: [
			{ id: 'a', flex: true },
			{ id: 'b', flex: true }
		]
	};
	assert.throws(() => solveLayout(100, none), /"g0"/);
	assert.throws(() => solveLayout(100, two), /"g2"/);
});
