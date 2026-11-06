/**
 * K1 / #9393: pure selector tree and spatial navigation (selector-tree.ts).
 *
 * Regression lines:
 * - if move() does not follow spatial half-plane and distance rules then broken
 * - if drillIn does not prefer last-visited child or top-left fallback then broken
 * - if drillOut at root throws or drillIn on a leaf changes selection then broken
 * - if the module gains import-from lines or DOM-like names then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
	createSelectorTree,
	pickNeighbour,
	spatialScore
} from '../../src/lib/keyboard/selector-tree.ts';

const leaf = (id, x, y, parentId = 'root', w = 100, h = 100) => ({
	id,
	parentId,
	kind: 'leaf',
	rect: { x, y, width: w, height: h }
});

test('2x2 grid: right then down from top-left', () => {
	const root = {
		id: 'root',
		parentId: null,
		kind: 'section',
		rect: { x: 0, y: 0, width: 220, height: 220 }
	};
	const A = leaf('A', 0, 0);
	const B = leaf('B', 120, 0);
	const C = leaf('C', 0, 120);
	const D = leaf('D', 120, 120);
	const tree = createSelectorTree([root, A, B, C, D], 'A');
	assert.equal(tree.move('right'), 'B');
	assert.equal(tree.selected(), 'B');
	assert.equal(tree.move('down'), 'D');
	assert.equal(tree.selected(), 'D');
	assert.equal(tree.move('left'), 'C');
	assert.equal(tree.move('up'), 'A');
});

test('half-plane excludes diagonal candidate; aligned candidate wins', () => {
	const S = {
		id: 'S',
		parentId: 'root',
		kind: 'leaf',
		rect: { x: 0, y: 0, width: 100, height: 100 }
	};
	const X = {
		id: 'X',
		parentId: 'root',
		kind: 'leaf',
		rect: { x: 90, y: 110, width: 100, height: 100 }
	};
	const Y = {
		id: 'Y',
		parentId: 'root',
		kind: 'leaf',
		rect: { x: 400, y: 0, width: 100, height: 100 }
	};
	const root = {
		id: 'root',
		parentId: null,
		kind: 'section',
		rect: { x: 0, y: 0, width: 900, height: 400 }
	};
	const tree1 = createSelectorTree([root, S, X, Y], 'S');
	assert.equal(tree1.move('right'), 'Y');
	assert.equal(spatialScore(S.rect, X.rect, 'right'), null);
	const tree2 = createSelectorTree([root, S, X], 'S');
	assert.equal(tree2.move('right'), 'S');
	assert.equal(tree2.selected(), 'S');
});

test('orthogonal penalty and id tie-break on pickNeighbour', () => {
	const S = {
		id: 'S',
		parentId: 'r',
		kind: 'leaf',
		rect: { x: 0, y: 0, width: 100, height: 100 }
	};
	const P = {
		id: 'P',
		parentId: 'r',
		kind: 'leaf',
		rect: { x: 150, y: 0, width: 100, height: 100 }
	};
	const Q = {
		id: 'Q',
		parentId: 'r',
		kind: 'leaf',
		rect: { x: 110, y: 150, width: 100, height: 100 }
	};
	const n1 = {
		id: 'n1',
		parentId: 'r',
		kind: 'leaf',
		rect: { x: 200, y: 0, width: 50, height: 50 }
	};
	const n2 = {
		id: 'n2',
		parentId: 'r',
		kind: 'leaf',
		rect: { x: 200, y: 0, width: 50, height: 50 }
	};
	assert.equal(pickNeighbour(S, [S, P, Q], 'right')?.id, 'P');
	assert.equal(pickNeighbour(S, [S, n1, n2], 'right')?.id, 'n1');
});

test('drillIn prefers last-visited child; first drill uses top-left not array order', () => {
	const S = {
		id: 'S',
		parentId: null,
		kind: 'section',
		rect: { x: 0, y: 0, width: 300, height: 300 }
	};
	const A = leaf('A', 0, 0, 'S');
	const B = leaf('B', 120, 0, 'S');
	const C = leaf('C', 0, 120, 'S');
	const tree = createSelectorTree([S, C, B, A], 'S');
	assert.equal(tree.drillIn(), 'A');
	assert.equal(tree.move('down'), 'C');
	assert.equal(tree.drillOut(), 'S');
	assert.equal(tree.drillIn(), 'C');
	assert.equal(tree.selected(), 'C');
});

test('drillOut at root is no-op; drillIn on leaf is no-op', () => {
	const S = {
		id: 'S',
		parentId: null,
		kind: 'section',
		rect: { x: 0, y: 0, width: 100, height: 100 }
	};
	const L = leaf('L', 0, 0, 'S');
	const tree = createSelectorTree([S, L], 'S');
	assert.doesNotThrow(() => {
		assert.equal(tree.drillOut(), 'S');
		assert.equal(tree.selected(), 'S');
	});
	tree.drillIn();
	assert.equal(tree.selected(), 'L');
	assert.equal(tree.drillIn(), 'L');
});

test('no wrap at grid edge; no move to a node that is not a sibling', () => {
	const root = {
		id: 'root',
		parentId: null,
		kind: 'section',
		rect: { x: 0, y: 0, width: 500, height: 300 }
	};
	const A = leaf('A', 0, 0, 'root');
	const B = leaf('B', 120, 0, 'root');
	const C = leaf('C', 0, 120, 'root');
	const D = leaf('D', 120, 120, 'root');
	const grid = createSelectorTree([root, A, B, C, D], 'B');
	assert.equal(grid.move('right'), 'B');
	assert.equal(grid.move('up'), 'B');
	const grid2 = createSelectorTree([root, A, B, C, D], 'D');
	assert.equal(grid2.move('right'), 'D');

	const p1 = {
		id: 'p1',
		parentId: 'root',
		kind: 'section',
		rect: { x: 0, y: 0, width: 260, height: 200 }
	};
	const p2 = {
		id: 'p2',
		parentId: 'root',
		kind: 'section',
		rect: { x: 300, y: 0, width: 200, height: 200 }
	};
	const L1 = leaf('L1', 0, 0, 'p1');
	const L2 = leaf('L2', 120, 0, 'p1');
	const far = leaf('far', 340, 0, 'p2');
	const rootWide = {
		id: 'root',
		parentId: null,
		kind: 'section',
		rect: { x: 0, y: 0, width: 600, height: 300 }
	};
	const nested = createSelectorTree([rootWide, p1, p2, L1, L2, far], 'L2');
	assert.equal(nested.move('right'), 'L2');
	assert.equal(nested.selected(), 'L2');
});

test('validation throws on duplicate id, unknown parent, leaf parent, unknown initial', () => {
	const goodParent = {
		id: 'p',
		parentId: null,
		kind: 'section',
		rect: { x: 0, y: 0, width: 10, height: 10 }
	};
	const c1 = leaf('c', 0, 0, 'p');
	assert.throws(() =>
		createSelectorTree([goodParent, c1, { ...c1, rect: { x: 1, y: 1, width: 1, height: 1 } }], 'c')
	);
	assert.throws(() => createSelectorTree([goodParent, { ...c1, parentId: 'missing' }], 'c'));
	const leafParent = {
		id: 'leafp',
		parentId: 'p',
		kind: 'leaf',
		rect: { x: 0, y: 0, width: 1, height: 1 }
	};
	const badChild = leaf('bad', 0, 0, 'leafp');
	assert.throws(() => createSelectorTree([goodParent, leafParent, badChild], 'bad'));
	assert.throws(() => createSelectorTree([goodParent, c1], 'nope'));
});

test('module purity: no import-from lines; no DOM API names in source', () => {
	const src = readFileSync(
		new URL('../../src/lib/keyboard/selector-tree.ts', import.meta.url),
		'utf8'
	);
	const lines = src.split('\n');
	const importLines = lines.filter((line) => {
		const t = line.trim();
		if (/^\s*import\s+/.test(line) && !/\bfrom\b/.test(line)) return true;
		if (/\bfrom\b/.test(line) && /^\s*(import|export)\b/.test(line)) return true;
		return false;
	});
	assert.equal(importLines.length, 0, importLines.join('\n'));
	assert.ok(!/\b(document|window|HTMLElement|getBoundingClientRect)\b/.test(src));
	assert.ok(!/\bsvelte\b/i.test(src));
});
