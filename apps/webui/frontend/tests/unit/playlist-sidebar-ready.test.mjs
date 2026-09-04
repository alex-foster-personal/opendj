import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compile } from 'svelte/compiler';

const filename = new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url);
const source = readFileSync(filename, 'utf8');

test('playlist sidebar becomes usable when metadata arrives, before the all-track page walk', () => {
	const init = source.slice(source.indexOf('async function _init()'), source.indexOf('async function _restoreBootPane()'));
	const metadata = init.indexOf('playlists = lists;');
	const ready = init.indexOf('playlistsLoading = false;', metadata);
	const tracks = init.indexOf('await _restoreBootPane();');
	assert.ok(metadata >= 0 && ready > metadata && ready < tracks,
		'playlist navigation must not wait for every track to load');
	compile(source, { filename: filename.pathname, generate: 'client' });
});
