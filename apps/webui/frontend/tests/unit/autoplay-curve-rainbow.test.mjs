// requirement: LIBPG-7694c
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/autoplay-curve.ts');
});

describe('LIBPG-7694c rainbow AutoPlay chain arrowheads', () => {
	it('keeps one hue on forward hops and steps a little when a hop goes back or crosses', () => {
		const { paintChainHops, CURVE_HUE_START, CURVE_HUE_STEP } = mod;
		assert.ok(CURVE_HUE_STEP <= 16, 'step stays small enough that neighbors still look related');
		const forward = paintChainHops(['a', 'b', 'c', 'd'], new Map([['a', 0], ['b', 1], ['c', 2], ['d', 3]]));
		assert.deepEqual(forward.map((hop) => hop.hue), [CURVE_HUE_START, CURVE_HUE_START, CURVE_HUE_START]);
		assert.deepEqual(forward.map((hop) => hop.direction), [1, 1, 1]);

		// b->c returns upward and overlaps a->b, so only that hop advances.
		const back = paintChainHops(['a', 'b', 'c'], new Map([['a', 0], ['b', 3], ['c', 1]]));
		assert.equal(back[0].hue, CURVE_HUE_START);
		assert.equal(back[0].direction, 1);
		assert.equal(back[1].direction, -1);
		assert.equal(back[1].hue, (CURVE_HUE_START + CURVE_HUE_STEP) % 360);

		// c->d goes downward but its span crosses the earlier hops, so it advances too.
		const cross = paintChainHops(
			['a', 'b', 'c', 'd'],
			new Map([['a', 0], ['b', 5], ['c', 1], ['d', 6]])
		);
		assert.equal(cross[2].direction, 1);
		assert.equal(cross[2].hue, (CURVE_HUE_START + CURVE_HUE_STEP * 2) % 360);
	});

	it('arrowHead points down toward a later row and up toward an earlier one', () => {
		const { arrowHead } = mod;
		const down = arrowHead(10, 80, 20);
		const [downTip, downBase] = down.points.split(' ').map((pair) => Number(pair.split(',')[1]));
		assert.ok(downTip > downBase, 'down head tip sits below its base');
		const up = arrowHead(80, 10, 20);
		const [upTip, upBase] = up.points.split(' ').map((pair) => Number(pair.split(',')[1]));
		assert.ok(upTip < upBase, 'up head tip sits above its base');
		assert.equal(arrowHead(0, 4, 20).points, '');
	});

	it('the table paints each hop and the rank cell arrow with that hop color', () => {
		const table = readFileSync(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url), 'utf8');
		assert.match(table, /paintChainHops\(autoPlayOrder\.chain, rowIndexOf\)/);
		assert.match(table, /<polygon points=\{head\.points\} class="ap-curve-head"/);
		assert.match(table, /arrowColor=\{hop\?\.color \?\? null\}/);
		assert.doesNotMatch(table, /marker-end=/, 'the single-color marker is gone');
		const cell = readFileSync(new URL('../../src/lib/components/rb/browser/AutoPlayRankCell.svelte', import.meta.url), 'utf8');
		assert.match(cell, /class="ap-rank-arrow"/);
	});
});
