// requirement: SMART-06
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('pin 93b2bc62acca autolist groups genre rating bpm mount under Autolists tab', () => {
	const browser = source('src/lib/components/rb/browser/AutolistBrowser.svelte');
	const nav = source('src/lib/components/rb/browser/LibraryNav.svelte');
	for (const testid of [
		'autolist-group-genre',
		'autolist-group-rating',
		'autolist-group-bpm'
	]) {
		assert.match(browser, new RegExp(`testid: '${testid}'`));
	}
	assert.match(browser, /data-testid=\{group\.testid\}/);
	assert.match(nav, /AutolistBrowser/);
	assert.match(nav, /activeTab === 'autolists'/);
});
