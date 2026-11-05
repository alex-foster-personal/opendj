/**
 * PlaygroundPanel.css is pulled into PlaygroundPanel.svelte's <style> with
 * @import, which Svelte does not scope: Vite inlines it as GLOBAL CSS. Every
 * rule must be rooted at the panel's own .playground-panel element.
 *
 * Regression line: if any selector is not rooted at .playground-panel then the
 * file restyles every admin page (a bare `h3`, `button`, `input`, or `.panel`
 * would leak once the playground chunk has loaded).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const dir = new URL('../../src/routes/admin/', import.meta.url);
const css = readFileSync(fileURLToPath(new URL('PlaygroundPanel.css', dir)), 'utf8').replace(
	/\/\*[\s\S]*?\*\//g,
	''
);
const panel = readFileSync(fileURLToPath(new URL('PlaygroundPanel.svelte', dir)), 'utf8');
const selectors = [...css.matchAll(/([^{}]+)\{[^}]*\}/g)].flatMap((m) =>
	m[1].split(',').map((s) => s.trim())
);

test('the playground stylesheet declares rules and is imported by the panel', () => {
	assert.ok(
		selectors.length >= 15,
		`expected the playground panel rules, found ${selectors.length}`
	);
	assert.match(panel, /<style>\s*@import '\.\/PlaygroundPanel\.css';/);
	assert.match(panel, /class="playground-panel"/);
	assert.equal(panel.includes('max-width: 1180px'), false);
});

test('every playground selector is rooted at .playground-panel', () => {
	const unrooted = selectors.filter((s) => !/^\.playground-panel(\b|[.\s:>])/.test(s));
	assert.deepEqual(unrooted, [], `unscoped global selectors: ${unrooted.join(' | ')}`);
});
