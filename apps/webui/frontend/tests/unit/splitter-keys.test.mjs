import assert from 'node:assert/strict';
import test from 'node:test';

import { splitterKeyStep } from '../../src/lib/layout/splitter-keys.ts';

const base = { px: 100, min: 40, max: 200, collapsed: false, collapsible: true };

test('ArrowRight/ArrowDown grow by 8, 64 with Shift, capped at max', () => {
	assert.deepEqual(splitterKeyStep('ArrowRight', {}, base), { px: 108, collapsed: false });
	assert.deepEqual(splitterKeyStep('ArrowDown', { shift: true }, base), {
		px: 164,
		collapsed: false
	});
	assert.deepEqual(splitterKeyStep('ArrowRight', { shift: true }, { ...base, px: 180 }), {
		px: 200,
		collapsed: false
	});
});

test('ArrowLeft/ArrowUp at min stay at min', () => {
	const atMin = { ...base, px: 40 };
	assert.deepEqual(splitterKeyStep('ArrowLeft', {}, atMin), { px: 40, collapsed: false });
	assert.deepEqual(splitterKeyStep('ArrowUp', { shift: true }, atMin), {
		px: 40,
		collapsed: false
	});
});

test('Home/End go to min/max', () => {
	assert.equal(splitterKeyStep('Home', {}, base).px, 40);
	assert.equal(splitterKeyStep('End', {}, base).px, 200);
});

test('Enter toggles collapsed on collapsible handle, null otherwise', () => {
	assert.deepEqual(splitterKeyStep('Enter', {}, base), { px: 100, collapsed: true });
	assert.equal(splitterKeyStep('Enter', {}, { ...base, collapsible: false }), null);
});

test('other keys return null', () => {
	for (const key of ['a', 'Q', ' ', 'Tab']) {
		assert.equal(splitterKeyStep(key, {}, base), null);
	}
});
