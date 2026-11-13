/** Skin token CSS compiler contract (#14499). */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { compileSkinTokens } = await loadTypeScriptModule('src/lib/rb/skin-token-css.ts');

test('two valid tokens compile to one scoped rule sorted by name', () => {
	const css = compileSkinTokens('booth', { '--rb-fg': '#fff', '--rb-bg': '#000' });
	assert.equal(
		css,
		'html[data-skin="booth"] .perf-root {\n\t--rb-bg: #000;\n\t--rb-fg: #fff;\n}\n'
	);
	assert.equal(css.match(/\{/g).length, 1);
});

test('bad token name throws naming the token', () => {
	assert.throws(() => compileSkinTokens('booth', { '--color-bg': '#000' }), /--color-bg/);
});

test('escaping values throw naming the token', () => {
	for (const v of ['red; x', 'a{', 'a}', 'url(x)', '@import "x"', '</style>']) {
		assert.throws(() => compileSkinTokens('booth', { '--rb-bg': v }), /--rb-bg/);
	}
});

test('non-slug skin id throws naming the id', () => {
	assert.throws(() => compileSkinTokens('Booth"]', { '--rb-bg': '#000' }), /Booth"\]/);
});
