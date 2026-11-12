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
	const marker = '[playlists] refresh failed';
	const idx = BROWSER_PANEL.indexOf(marker);
	assert.ok(idx >= 0, 'expected playlists refresh failed log');
	const catchStart = BROWSER_PANEL.lastIndexOf('} catch (exc) {', idx);
	assert.ok(catchStart >= 0);
	const slice = BROWSER_PANEL.slice(catchStart, idx + marker.length + 40);
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

test('_init browser init catch returns when isPageGoingAway before pushToast', () => {
	const marker = 'pushToast(`browser init failed:';
	const idx = BROWSER_PANEL.indexOf(marker);
	assert.ok(idx >= 0, 'expected browser init failed toast');
	const catchStart = BROWSER_PANEL.lastIndexOf('} catch (exc) {', idx);
	assert.ok(catchStart >= 0);
	const slice = BROWSER_PANEL.slice(catchStart, idx + marker.length + 80);
	assert.match(slice, /if \(isPageGoingAway\(\)\) return;/);
	assert.match(slice, /browser init failed/);
});
