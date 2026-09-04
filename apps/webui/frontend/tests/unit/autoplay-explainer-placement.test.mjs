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
		'left:8px;top:8px;'
	);
	assert.equal(
		placement.columnExplainerStyle({ left: 900, top: 500, bottom: 518 }, { width: 300, height: 180 }, { innerWidth: 1280, innerHeight: 800 }, 'above'),
		'left:594px;top:314px;'
	);
});

test('below is used only when an above panel would clip above the viewport', () => {
	assert.equal(
		placement.columnExplainerStyle({ left: 500, top: 20, bottom: 38 }, { width: 300, height: 160 }, { innerWidth: 1280, innerHeight: 800 }, 'above'),
		'left:194px;top:44px;'
	);
});
