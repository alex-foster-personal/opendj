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
	assert.match(menu, /aria-label="Open MIDI panel from audio I\/O"[^>]*onclick=\{openMidiDrawer\}/);
});

// Codex P2 on PR #3896: the pinned I/O popover (z-index 80) stayed open over
// the MIDI drawer (41) it had just opened. Both MIDI entries go through one
// handler that opens the drawer AND unpins the I/O view; neither opens it.
test('both MIDI entries open the drawer and close the I/O view, and neither opens it', () => {
	const script = cluster.slice(0, cluster.indexOf('</script>'));
	const handler = script.slice(script.indexOf('function openMidiDrawer(): void {'));
	const handlerBody = handler.slice(0, handler.indexOf('\n\t}'));
	assert.match(handlerBody, /if \(!midiUi\.panelOpen\) toggleMidiPanel\(\);\s*closeIoView\(\);/);
	for (const label of ['Open MIDI panel', 'Open MIDI panel from audio I/O']) {
		const at = cluster.indexOf(`aria-label="${label}"`);
		const tag = cluster.slice(at, cluster.indexOf('>', cluster.indexOf('onclick=', at)));
		assert.match(tag, /onclick=\{openMidiDrawer\}/, label);
	}
	assert.doesNotMatch(cluster, /openIoView/, 'no MIDI path may open the I/O view');
});

test('the I/O button still pins and unpins the I/O view itself (control)', () => {
	assert.match(cluster, /programmaticOpen=\{ioSurface\.open\}/);
	assert.match(cluster, /onProgrammaticClose=\{closeIoView\}/);
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
