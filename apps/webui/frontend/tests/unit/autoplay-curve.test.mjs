import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/autoplay-curve.ts');
});

describe('autoplay-curve', () => {
	it('keeps hover and keyboard tracing attached to the relocated rank column', () => {
		const source = readFileSync(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url), 'utf8');
		assert.match(source, /onpointerenter=\{\(\) => \(hoveredApId = row.stable_id\)\}/);
		assert.match(source, /onfocus=\{\(\) => \(hoveredApId = row.stable_id\)\}/);
		assert.match(source, /--ap-curve-left:\$\{colWidths.err \+ colWidths.cloud \+ colWidths.order\}px/);
		assert.match(source, /left: var\(--ap-curve-left\)/);
	});
	it('assignLanes packs non-overlapping spans into lane 0', () => {
		const { assignLanes } = mod;
		assert.deepEqual(assignLanes([{ minY: 0, maxY: 10 }, { minY: 20, maxY: 30 }]), [0, 0]);
	});

	it('assignLanes bumps overlapping spans to a new lane', () => {
		const { assignLanes } = mod;
		assert.deepEqual(assignLanes([{ minY: 0, maxY: 20 }, { minY: 10, maxY: 30 }]), [0, 1]);
	});

	it('buildCurveSegments marks skipped ranks and orders by rank', () => {
		const { buildCurveSegments } = mod;
		const chain = ['a', 'b', 'c'];
		const rankOf = new Map([
			['a', 1],
			['b', 2],
			['c', 4]
		]);
		const rowIndexOf = new Map([
			['a', 0],
			['b', 5],
			['c', 2]
		]);
		const segs = buildCurveSegments({
			chain,
			rankOf,
			rowIndexOf,
			rowHeight: 20,
			scrollTop: 0,
			viewportHeight: 400,
			pad: 40
		});
		assert.equal(segs.length, 2);
		assert.equal(segs[0].from.stable_id, 'a');
		assert.equal(segs[0].to.stable_id, 'b');
		assert.equal(segs[0].skips, false);
		assert.equal(segs[1].from.stable_id, 'b');
		assert.equal(segs[1].to.stable_id, 'c');
		assert.equal(segs[1].skips, true);
		// path goes up then down in row-space (b index 5, c index 2)
		assert.equal(segs[1].to.y < segs[1].from.y, true);
	});
});
