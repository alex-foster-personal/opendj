import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Playlist drag codec (split out of pane-contract). Regression lines:
// - if decodePlaylistDrag accepts a foreign or malformed payload, or throws
//   instead of returning null, then a drag from another app crashes the
//   tab bar -- broken
// - if PLAYLIST_DRAG_MIME changes then PlaylistTree's drag and PaneTabs'
//   drop stop agreeing and every playlist drop is ignored -- broken

let drag;

before(async () => {
	drag = await loadTypeScriptModule('src/lib/components/rb/browser/playlist-drag.ts');
});

test('PLAYLIST_DRAG_MIME is the agreed dataTransfer type', () => {
	assert.equal(drag.PLAYLIST_DRAG_MIME, 'application/x-mdt-playlist');
});

test('playlist drag payloads round-trip, and junk decodes to null', () => {
	const payload = {
		playlist_id: 'pl-7',
		name: 'Peak Time',
		track_count: 42,
		kind: 'playlist'
	};
	assert.deepEqual(
		drag.decodePlaylistDrag(drag.encodePlaylistDrag(payload)),
		payload
	);

	for (const junk of [
		'',
		'   ',
		'not json at all',
		'null',
		'[]',
		'"a string"',
		JSON.stringify({ playlist_id: '', name: 'n', track_count: 1, kind: 'playlist' }),
		JSON.stringify({ name: 'n', track_count: 1, kind: 'playlist' }),
		JSON.stringify({ playlist_id: 'p', track_count: 1, kind: 'playlist' }),
		JSON.stringify({ playlist_id: 'p', name: 'n', kind: 'playlist' }),
		JSON.stringify({ playlist_id: 'p', name: 'n', track_count: 1 }),
		JSON.stringify({ playlist_id: 'p', name: 'n', track_count: 1, kind: 'nope' })
	]) {
		assert.equal(drag.decodePlaylistDrag(junk), null, `should reject ${junk}`);
	}
});
