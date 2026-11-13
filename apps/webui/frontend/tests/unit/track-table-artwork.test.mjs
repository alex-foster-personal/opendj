// requirement: LIBUX-13631
/**
 * Issue #13093: library track-row artwork layout (pin 5a833ab52fda).
 * Shipped behavior is LIBUX-26; these three source-shape tests map 1:1 to
 * the issue acceptance bullets without duplicating pin-5a833ab52fda-artwork.test.mjs.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const TABLE = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);
const table = readFileSync(TABLE, 'utf8');

/** Every `selector { body }` rule in TrackTable's stylesheet, comments stripped. */
function rules(src = table) {
	const style = src.slice(src.lastIndexOf('<style>'));
	const css = style.replace(/\/\*[\s\S]*?\*\//g, '');
	const out = [];
	const re = /([^{}]+)\{([^{}]*)\}/g;
	for (let m = re.exec(css); m !== null; m = re.exec(css)) {
		out.push({ selector: m[1].trim(), body: m[2] });
	}
	return out;
}

function ruleBody(selector) {
	const rule = rules().find((r) => r.selector === selector);
	assert.ok(rule, `missing CSS rule ${selector}`);
	return rule.body;
}

const artCell = table.slice(table.indexOf('class="c-art"'), table.indexOf('class="c-title"'));

test('library row with artwork fills the cell and hides slate after load', () => {
	assert.match(artCell, /\{#if row\.artwork_available === true\}/);
	assert.match(artCell, /_showArtworkImg\(row\.stable_id, row\.artwork_available\)/);
	assert.match(artCell, /<img[\s\S]*onload=\{\(e\) => _onArtworkLoad\(e, row\.stable_id\)\}/);

	const imgBody = ruleBody('.c-art img');
	assert.match(imgBody, /object-fit:\s*cover/);
	assert.match(imgBody, /object-position:\s*center 66\.67%/);
	assert.match(imgBody, /width:\s*100%/);
	assert.match(imgBody, /height:\s*100%/);
	assert.match(imgBody, /position:\s*absolute/);
	assert.match(imgBody, /inset:\s*0/);

	const hideSlate = ruleBody('.c-art:has(img.art-loaded) .art-slate');
	assert.match(hideSlate, /display:\s*none/);

	assert.match(table, /function _onArtworkLoad\([\s\S]*classList\.add\('art-loaded'\)/);
});

test('library row without artwork keeps a sized slate and omits img markup', () => {
	const slateBody = ruleBody('.art-slate');
	assert.match(slateBody, /width:\s*var\(--tt-art\)/);
	assert.match(slateBody, /height:\s*var\(--tt-art\)/);

	const cellBody = ruleBody('.c-art');
	assert.match(cellBody, /width:\s*var\(--tt-art\)/);

	const style = table.slice(table.lastIndexOf('<style>'));
	const compactRoot = rules(style).find((r) => r.selector === '.tt-root');
	assert.ok(compactRoot);
	assert.match(compactRoot.body, /--tt-row-h:\s*22px/);
	assert.match(compactRoot.body, /--tt-art:\s*22px/);
	const cosyRoot = rules(style).find((r) => r.selector === ".tt-root[data-density='cosy']");
	assert.ok(cosyRoot);
	assert.match(cosyRoot.body, /--tt-row-h:\s*30px/);
	assert.match(cosyRoot.body, /--tt-art:\s*30px/);

	assert.match(artCell, /\{#if row\.artwork_available === true\}[\s\S]*<img/);
	const afterArtGuard = artCell.split(/\{#if row\.artwork_available === true\}/)[0];
	assert.doesNotMatch(afterArtGuard, /<img\b/);

	assert.match(artCell, /title=\{row\.artwork_available !== true/);
});

test('virtualized library rows stay keyed so artwork load state does not leak', () => {
	assert.match(
		table,
		/const visibleRows = \$derived\(rows\.slice\(windowInfo\.startIndex, windowInfo\.endIndex\)\)/
	);

	const eachLine = table.split('\n').find((line) => line.includes('{#each visibleRows as row'));
	assert.ok(eachLine, 'missing {#each visibleRows} in TrackTable');
	assert.match(eachLine, /`\$\{row\.stable_id\}:\$\{row\.order\}`/);

	assert.doesNotMatch(table, /row\.artLoaded/);
	assert.match(
		table,
		/\(event\.currentTarget as HTMLImageElement\)\.classList\.add\('art-loaded'\)/
	);
});
