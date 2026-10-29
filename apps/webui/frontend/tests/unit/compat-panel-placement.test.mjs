/**
 * Compatible dropdown placement (PR #6574, review 4393702079).
 *
 * [if] the panel grows (Mashup to Transition adds rules) and placement is not recomputed [then] it runs off-screen - broken.
 * [if] the height is not capped to the room on its side [then] a tall panel spills past the window - broken.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
before(async () => {
	m = await loadTypeScriptModule('src/lib/components/rb/browser/compat-panel-placement.ts');
});

const VIEW = { width: 1440, height: 900 };

test('opens below when the whole panel fits', () => {
	assert.deepEqual(m.compatPanelPlacement({ left: 100, top: 100, bottom: 120 }, 300, 300, VIEW), {
		left: 100,
		top: 124,
		maxHeight: 900 - 8 - 124
	});
});

test('opens upward near the bottom, and the top edge stays on-screen', () => {
	const p = m.compatPanelPlacement({ left: 100, top: 800, bottom: 820 }, 500, 300, VIEW);
	assert.equal(p.top, 800 - 4 - 500);
	assert.ok(p.top >= 8);
	assert.equal(p.maxHeight, 800 - 4 - 8);
});

test('a panel taller than either side is capped to the bigger side and scrolls', () => {
	const p = m.compatPanelPlacement({ left: 100, top: 600, bottom: 620 }, 2000, 300, VIEW);
	assert.equal(p.maxHeight, 600 - 4 - 8);
	assert.equal(p.top, 8);
	// Control: the capped panel never runs past the window either way.
	assert.ok(p.top + p.maxHeight <= VIEW.height - 8);
});

test('the panel re-places on resize and scrolls when capped', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/browser/CompatibleFilterPanel.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /new ResizeObserver\(_place\)/);
	assert.match(src, /window\.addEventListener\('resize', _place\)/);
	assert.match(src, /overflow-y: auto;/);
	assert.match(src, /panelEl\.scrollHeight/);
});
