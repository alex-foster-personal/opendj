import assert from 'node:assert/strict';
import test from 'node:test';

import { createSplitDrag } from '../../src/lib/layout/split-drag.ts';

const H = { axis: 'horizontal', startPx: 248, min: 200, max: 400 };

test('drag from x=100 at 248px to x=130 gives live 278 without commit', () => {
	const m = createSplitDrag();
	m.beginDrag({ x: 100, y: 0 }, H);
	assert.deepEqual(m.dragTo({ x: 130, y: 0 }), { px: 278, commit: false });
});

test('pointer past max or below min stops at the bound', () => {
	const m = createSplitDrag();
	m.beginDrag({ x: 100, y: 0 }, H);
	assert.equal(m.dragTo({ x: 1000, y: 0 }).px, 400);
	assert.equal(m.dragTo({ x: -1000, y: 0 }).px, 200);
});

test('endDrag returns final px with commit true, once', () => {
	const m = createSplitDrag();
	m.beginDrag({ x: 100, y: 0 }, H);
	m.dragTo({ x: 120, y: 0 });
	assert.deepEqual(m.endDrag(), { px: 268, commit: true });
	assert.throws(() => m.endDrag(), /endDrag/);
});

test('dragTo or endDrag with no drag begun throws naming the call', () => {
	const m = createSplitDrag();
	assert.throws(() => m.dragTo({ x: 1, y: 1 }), /dragTo/);
	assert.throws(() => m.endDrag(), /endDrag/);
});

test('vertical axis uses only the y delta', () => {
	const m = createSplitDrag();
	m.beginDrag({ x: 100, y: 50 }, { ...H, axis: 'vertical' });
	assert.equal(m.dragTo({ x: 500, y: 50 }).px, 248);
	assert.equal(m.dragTo({ x: 500, y: 70 }).px, 268);
});
