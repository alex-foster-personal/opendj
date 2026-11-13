/**
 * PlaygroundPanel after the #7502 CSS extract (issue #13071).
 *
 * The panel's rules live in PlaygroundPanel.css, which Svelte does not scope
 * (playground-panel-css-scope.test.mjs pins the .playground-panel rooting).
 * This file pins the two other halves of that contract:
 *
 *   - the panel still renders both consoles inside its .playground-panel
 *     root (SSR render, idle state: no .fatal, no result table);
 *   - the stylesheet is imported by PlaygroundPanel.svelte only. A second
 *     import anywhere in src/ would load the sheet as global CSS outside the
 *     component that owns it.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const SRC = fileURLToPath(new URL('../../src/', import.meta.url));
const OWNER = 'routes/admin/PlaygroundPanel.svelte';

const ENTRY = [
	"export { default as PlaygroundPanel } from './src/routes/admin/PlaygroundPanel.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule(ENTRY);
});

/** Every file under `dir`, as paths relative to src/ with forward slashes. */
function walk(dir) {
	return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
		const full = join(dir, entry.name);
		return entry.isDirectory() ? walk(full) : [relative(SRC, full).split('\\').join('/')];
	});
}

test('PlaygroundPanel renders the API and SQL consoles inside its scoped root', () => {
	const { body } = ssr.render(ssr.PlaygroundPanel, { props: {} });
	assert.match(body, /^(<!--[^>]*-->)*<div class="playground-panel">/);
	const sections = body.match(/<section class="panel">/g) ?? [];
	assert.equal(sections.length, 2, body);
	assert.ok(body.indexOf('API console') < body.indexOf('SQL console'), body);
	assert.match(body, /<button[^>]*>Send<\/button>/);
	assert.match(body, /<button[^>]*>Run<\/button>/);
	assert.equal(body.includes('class="fatal"'), false, 'idle panel shows no error box');
	assert.equal(body.includes('<table'), false, 'idle panel shows no result table');
});

test('PlaygroundPanel.css is imported only by PlaygroundPanel.svelte', () => {
	const importers = walk(SRC).filter(
		(file) =>
			/\.(svelte|ts|js|css)$/.test(file) &&
			readFileSync(join(SRC, file), 'utf8').includes('PlaygroundPanel.css')
	);
	assert.deepEqual(importers, [OWNER]);
});
