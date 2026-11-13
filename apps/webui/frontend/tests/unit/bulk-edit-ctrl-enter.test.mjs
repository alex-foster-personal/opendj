/**
 * Issue #12299 / LIBM-64: Ctrl+Enter (Cmd+Enter) in a Bulk Edit field runs the normal apply path.
 *
 * [if] a keydown is Ctrl+Enter or Cmd+Enter without Shift or Alt [then] it is the apply chord
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { isApplyChord } = await loadTypeScriptModule('src/lib/rb/bulk-edit-values.ts');

function chord(over = {}) {
	return { key: 'Enter', ctrlKey: true, metaKey: false, shiftKey: false, altKey: false, ...over };
}

test('the apply chord is Ctrl+Enter or Cmd+Enter only', () => {
	assert.equal(isApplyChord(chord()), true);
	assert.equal(isApplyChord(chord({ ctrlKey: false, metaKey: true })), true);
	assert.equal(isApplyChord(chord({ ctrlKey: false })), false);
	assert.equal(isApplyChord(chord({ shiftKey: true })), false);
	assert.equal(isApplyChord(chord({ altKey: true })), false);
	assert.equal(isApplyChord(chord({ key: 'a' })), false);
});
