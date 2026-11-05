/**
 * PAIR-10 / Sol P1 on PR #6062: pairing must be hidden on read-only panes (Spotify,
 * a taglist, an autolist). Those panes report as `collection` to the menu unless
 * the flag is threaded through, and the registry only hides pairing for read-only
 * sources, so each link in the chain is pinned here.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const PANEL = new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url);
const TABLE = new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url);
const MENU = new URL(
	'../../src/lib/components/rb/browser/TrackContextMenu.svelte',
	import.meta.url
);

describe('read-only panes hide the pair command', () => {
	it('BrowserPanel derives the read-only pane for Spotify, autolists, and taglists', () => {
		const panel = readFileSync(PANEL, 'utf8');
		assert.match(panel, /source === 'spotify' \|\|\s*isAutolistId\(pane\.playlist_id\) \|\|/);
		assert.match(panel, /\.startsWith\('taglist:'\)/);
		assert.match(panel, /readOnly=\{readOnlyPane\}/);
	});

	it('TrackTable forwards the flag to the track menu', () => {
		const table = readFileSync(TABLE, 'utf8');
		assert.match(table, /\{readOnly\}/);
	});

	it('the menu classifies a read-only row as readonly-import, which the registry treats as read-only', () => {
		const menu = readFileSync(MENU, 'utf8');
		assert.match(menu, /readOnly\s*\n?\s*\? 'readonly-import'/);
	});
});
