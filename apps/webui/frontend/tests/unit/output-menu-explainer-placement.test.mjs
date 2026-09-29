/**
 * The audio I/O device menu stacks three pickers vertically. Above/below
 * explainers covered the sibling pickers, so these open beside the menu.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const cluster = source('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte');
const explainer = source('../../src/lib/components/rb/deck/ControlExplainer.svelte');
let clamp;

before(async () => {
	clamp = await loadTypeScriptModule('src/lib/ui/clamp-to-viewport.ts');
});

test('every device picker in the output menu opens its explainer to the right', () => {
	const menu = cluster.slice(cluster.indexOf('{#snippet outputMenu()}'), cluster.indexOf('{/snippet}', cluster.indexOf('{#snippet outputMenu()}')));
	const tags = [...menu.matchAll(/<ControlExplainer[^>]*>/g)].map((m) => m[0]);
	assert.equal(tags.length, 4, 'expected MASTER / MAIN, HEADPHONE CUE, AUDIO IN and MIDI');
	for (const tag of tags) assert.match(tag, /placement="right"/);
});

test('the I/O view the tray opens carries its own MIDI panel entry (CHROME-07)', () => {
	const start = cluster.indexOf('{#snippet outputMenu()}');
	const menu = cluster.slice(start, cluster.indexOf('{/snippet}', start));
	assert.match(menu, /aria-label="Open MIDI panel from audio I\/O"/);
	assert.match(menu, /onclick=\{\(\) => \{\s*if \(!midiUi\.panelOpen\) toggleMidiPanel\(\);/);
});

test('ControlExplainer accepts right placement and anchors to the real trigger rect', () => {
	assert.match(explainer, /placement\?: 'auto' \| 'above' \| 'below' \| 'right'/);
	assert.match(explainer, /preferred: 'right'/);
	assert.match(explainer, /trigger: \{ left: r\.left, top: r\.top, width: r\.width, height: r\.height \}/);
});

test('right placement sits beside the trigger and never overlaps it', () => {
	const trigger = { left: 100, top: 300, width: 180, height: 24 };
	const box = clamp.placeFloating({ trigger, size: { width: 240, height: 120 }, viewport: { width: 1280, height: 800 }, preferred: 'right', gap: 8 });
	assert.equal(box.x, trigger.left + trigger.width + 8);
	assert.equal(box.y, trigger.top);
});

test('right placement flips left when the viewport has no room on the right', () => {
	const trigger = { left: 1000, top: 300, width: 180, height: 24 };
	const box = clamp.placeFloating({ trigger, size: { width: 240, height: 120 }, viewport: { width: 1280, height: 800 }, preferred: 'right', gap: 8 });
	assert.equal(box.x, trigger.left - 240 - 8);
	assert.ok(box.x + 240 <= trigger.left, 'flipped box must not overlap the trigger');
});
