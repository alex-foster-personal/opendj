/**
 * Column-header explainers default upward so their teaching panel does not
 * obscure the table below. The shared component keeps a deliberate below
 * option for a caller that cannot fit above.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const explainer = source('../../src/lib/components/rb/browser/AutoPlayExplainer.svelte');
const trackTable = source('../../src/lib/components/rb/browser/TrackTable.svelte');
let placement;

before(async () => {
	placement = await loadTypeScriptModule('src/lib/components/rb/browser/column-explainer-placement.ts');
});

test('the shared column explainer defaults above and retains a deliberate below option', () => {
	assert.match(explainer, /placement = 'above'/);
	assert.match(explainer, /placement\?: ColumnExplainerPlacement/);
	assert.match(explainer, /wrapEl\.getBoundingClientRect\(\)/);
	assert.match(explainer, /panelEl\.getBoundingClientRect\(\)/);
});

test('above-first placement clamps into a narrow, short viewport', () => {
	const rect = { left: 4, top: 10, bottom: 28 };
	const panel = { width: 300, height: 184 };
	const viewport = { innerWidth: 320, innerHeight: 200 };
	assert.equal(
		placement.columnExplainerStyle(rect, panel, viewport, 'above'),
		'left:8px;top:8px;z-index:100;'
	);
	assert.equal(
		placement.columnExplainerStyle({ left: 900, top: 500, bottom: 518 }, { width: 300, height: 180 }, { innerWidth: 1280, innerHeight: 800 }, 'above'),
		'left:594px;top:314px;z-index:100;'
	);
});

test('below is used only when an above panel would clip above the viewport', () => {
	assert.equal(
		placement.columnExplainerStyle({ left: 500, top: 20, bottom: 38 }, { width: 300, height: 160 }, { innerWidth: 1280, innerHeight: 800 }, 'above'),
		'left:194px;top:44px;z-index:100;'
	);
});

test('column explainer portals out of the sticky header and retains the pointer corridor', () => {
	assert.match(explainer, /use:_portalToBody/);
	assert.match(explainer, /document\.body\.appendChild\(node\)/);
	assert.match(explainer, /destroy: \(\) => node\.remove\(\)/);
	assert.doesNotMatch(explainer, /panelEl\.showPopover\(\)/);
	assert.doesNotMatch(explainer, /popover="manual"/);
	assert.match(explainer, /HIDE_DELAY_MS/);
	assert.match(explainer, /onpointerenter=\{_show\}/);
});

test('TrackTable still owns the AutoPlay column trigger through the shared explainer', () => {
	assert.match(trackTable, /import AutoPlayExplainer from '\.\/AutoPlayExplainer\.svelte'/);
	const start = trackTable.indexOf('class="h-icon h-autoplay"');
	assert.notEqual(start, -1, 'AutoPlay header is missing');
	const header = trackTable.slice(trackTable.lastIndexOf('<th', start), trackTable.indexOf('</th>', start));
	assert.match(header, /<AutoPlayExplainer/);
	assert.match(header, /<button\s+class="autoplay-sort"/);
	assert.match(header, /aria-pressed=\{sortKey === 'autoplay'\}/);
});

test('AutoPlay header delegates one-click ascending and clear behavior to the pane owner', () => {
	assert.match(trackTable, /onclick=\{\(\) => onsort\('autoplay'\)\}/);
	assert.doesNotMatch(trackTable, /autoPlayColumnSorted/);
	assert.match(trackTable, /const apCurveArrowId = \$derived\(`ap-curve-arrow-\$\{restoreKey\}`\)/);
	assert.match(trackTable, /<marker id=\{apCurveArrowId\}/);
	assert.match(trackTable, /marker-end=\{`url\(#\$\{apCurveArrowId\}\)`\}/);
});

test('every static TrackTable header uses the shared body-portal action, never a native title', () => {
	assert.equal(typeof placement.columnExplainer, 'function', 'the shared placement module must export the static header action');
	assert.match(
		trackTable,
		/import\s+\{[^}]*columnExplainer[^}]*\}\s+from '\.\/(column-explainer-placement|track-table-support)'/
	);
	// Scoped to the headers this PR actually converts (sort/filter/err/cloud/
	// preview/art/venue/stems), not the whole <thead> - the AutoPlay-queue and
	// Energy headers carry their own unrelated, legitimate `title` (aggregate
	// queue status / Mixed In Key readout), added independently on main.
	const converted = [...trackTable.matchAll(/use:columnExplainer=\{\{/g)];
	assert.ok(converted.length > 0, 'no static headers install the shared action');
	for (const match of converted) {
		const open = trackTable.lastIndexOf('<th', match.index);
		const closeTag = trackTable.indexOf('>', match.index);
		const header = trackTable.slice(open, closeTag);
		assert.doesNotMatch(header, /\btitle=/, 'a converted header still competes with the custom explainer');
	}
	assert.match(trackTable, /:global\(\.column-explain-panel\)\s*\{[^}]*z-index:\s*100/s);
});
