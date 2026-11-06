import assert from 'node:assert/strict';
import { join } from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const BROWSER_PANEL = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/components/rb/BrowserPanel.svelte'),
	'utf8'
);

test('_refreshPlaylists checks isPageGoingAway before logging', () => {
	const idxCatch = BROWSER_PANEL.indexOf('} catch (exc) {\n\t\t\tif (isPageGoingAway()) return;');
	assert.ok(idxCatch >= 0, 'expected _refreshPlaylists catch with isPageGoingAway first');
	const slice = BROWSER_PANEL.slice(idxCatch, idxCatch + 400);
	assert.match(slice, /if \(isPageGoingAway\(\)\) return;/);
	assert.match(slice, /\[playlists\] refresh failed/);
});

test('_pushPaneLoadError returns when isPageGoingAway before pushToast', () => {
	const fnStart = BROWSER_PANEL.indexOf('function _pushPaneLoadError(');
	assert.ok(fnStart >= 0);
	const fnBody = BROWSER_PANEL.slice(fnStart, fnStart + 500);
	assert.match(fnBody, /if \(isPageGoingAway\(\)\) return;/);
	assert.match(fnBody, /pushToast\(/);
});
