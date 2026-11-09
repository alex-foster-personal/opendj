// requirement: LIBORG-10314
// [if] the playlist tree menu has no Export as text item on the export route [then] broken
// [if] the download URL does not name GET /api/v1/playlists/{id}/export.txt [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let exporter;
let registry;
let commands;

before(async () => {
	exporter = await loadTypeScriptModule('src/lib/rb/playlist-text-export.ts');
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
});

test('the download URL is the export route, with the id encoded', () => {
	assert.equal(
		exporter.playlistTextExportUrl('pl 1/x', ''),
		'/api/v1/playlists/pl%201%2Fx/export.txt'
	);
	assert.equal(
		exporter.playlistTextExportUrl('p', 'http://daemon:1'),
		'http://daemon:1/api/v1/playlists/p/export.txt'
	);
});

test('downloadPlaylistText clicks a download link on the route', () => {
	const clicked = [];
	const link = {
		click() {
			clicked.push({ href: this.href, download: this.download });
		},
		remove() {}
	};
	const doc = { createElement: () => link, body: { appendChild: () => {} } };
	exporter.downloadPlaylistText('pl-1', doc);
	assert.deepEqual(clicked, [{ href: '/api/v1/playlists/pl-1/export.txt', download: '' }]);
});

test('the playlist menu offers Export as text on the GET export route with its CLI twin', () => {
	const items = registry.menuFor({ kind: 'playlist' }, { forbidDuplicates: false });
	const item = items.find((entry) => entry.id === 'export-text');
	assert.ok(item, 'export-text item present');
	assert.equal(item.label, 'Export as text');
	const command = commands.MENU_COMMANDS['playlist.export_txt'];
	assert.deepEqual(command, {
		via: 'http',
		method: 'GET',
		path: '/api/v1/playlists/{playlist_id}/export.txt',
		cli: 'opendj playlist export <playlist_id> --format txt'
	});
});
