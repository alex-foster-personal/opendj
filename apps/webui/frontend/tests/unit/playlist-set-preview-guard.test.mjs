import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const browserRoot = join(dirname(fileURLToPath(import.meta.url)), '../../src/lib/components/rb/browser');
const panelRoot = join(dirname(fileURLToPath(import.meta.url)), '../../src/lib/components/rb');

const forbidden = [
	join(browserRoot, 'TrackTable.svelte'),
	join(browserRoot, 'PreviewStrip.svelte'),
	join(panelRoot, 'BrowserPanel.svelte')
];
const allowed = join(browserRoot, 'PlaylistSetTabs.svelte');

test('SET-05 preview guard keeps perform endpoint out of preview surfaces', () => {
	for (const path of forbidden) {
		const source = readFileSync(path, 'utf8');
		assert.doesNotMatch(source, /createPlaylistSetRun/);
		assert.doesNotMatch(source, /\/sets\/.+\/runs/);
	}
	const tabs = readFileSync(allowed, 'utf8');
	assert.match(tabs, /createPlaylistSetRun/);
});
