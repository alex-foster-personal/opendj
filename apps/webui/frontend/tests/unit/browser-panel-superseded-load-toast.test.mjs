/**
 * A superseded playlist load must not toast an error the user never asked
 * to see.
 *
 * The node:test harness cannot mount a Svelte component, so this evaluates
 * BrowserPanel's real `_loadPane` implementation directly from its source
 * (same technique as library-health-coverage.test.mjs), driving it against
 * the real PaneStore contract from pane-contract.svelte.ts. `beginLoad`
 * hands out a monotonic token, and `completeLoad`/`failLoad` no-op once a
 * newer load has superseded the one holding that token - `_loadPane`'s catch
 * block already discards `failLoad`'s stale-no-op signal on this line, and
 * only wires it up correctly for the newer, non-superseded call.
 *
 * Pin issue #1472: `failLoad` returns whether the call actually applied
 * (false when superseded), exactly like its sibling `completeLoad`, but the
 * catch block in `_loadPane` never reads that return value before pushing an
 * error toast - so a load that lost a race still surfaces an error banner
 * for a request the user has already moved on from.
 *
 * Regression lines:
 * - if a superseded load's catch block still pushes an error toast then a
 *   stale in-flight request surfaces a spurious error for a playlist the
 *   user is no longer looking at -> broken
 * - if a genuine, non-superseded load failure stops toasting then real
 *   errors go silent -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

let contract;
before(async () => {
	contract = await loadTypeScriptModule(
		'src/lib/components/rb/browser/pane-contract.svelte.ts'
	);
});

/** Builds the real `_loadPane`, evaluated straight from BrowserPanel.svelte,
 * with its closure-captured helpers supplied as factory arguments so it can
 * run outside the component. */
function makeLoadPane({ fillPlaylistPaneImpl, pushToast }) {
	const source = readFileSync(PANEL, 'utf8');
	const start = source.indexOf('\tasync function _loadPane(');
	const end = source.indexOf('\n\t/** Reconstructs the minimal PlaylistNode', start);
	assert.ok(start >= 0 && end > start, 'could not isolate BrowserPanel._loadPane');
	const functionSource = source
		.slice(start, end)
		.replace(
			'async function _loadPane(p: PaneStore, node: PlaylistNode): Promise<void> {',
			'async function _loadPane(p, node) {'
		)
		// 1896d37e1 feat(webui): Autolists tab UI (SMART-06) added an autolist
		// branch with a TS `as` cast, which new Function cannot parse.
		.replace('wire as PlaylistTrackRowWire', 'wire');
	assert.doesNotMatch(
		functionSource,
		/: PaneStore|: PlaylistNode|Promise<void>|\bas [A-Z]\w+/,
		'TypeScript annotation survived stripping'
	);
	const factory = Function(
		'panes',
		'setLastPlaylist',
		'fillAllTracksPane',
		'fillPlaylistPane',
		'PLAYLIST_FIRST_PAGE',
		'listPlaylistTracksPage',
		'recordPlaylistSwitchFirstRowsMs',
		'recordOpenToLibraryRows',
		'completeLibraryUsable',
		'recordLibraryLoadTiming',
		'_fetchSmartlistRows',
		'fetchMissingTrackRows',
		'allTracksNonBrokenCount',
		'pushToast',
		'_rowFromPlaylistWire',
		`${functionSource}\nreturn _loadPane;`
	);
	return factory(
		[], // panes[0] is never this test's pane, so setLastPlaylist must never fire
		() => {
			throw new Error('setLastPlaylist must not be called');
		},
		() => {
			throw new Error('fillAllTracksPane must not be called (node.kind is "playlist")');
		},
		fillPlaylistPaneImpl,
		100,
		async () => {
			throw new Error('listPlaylistTracksPage must not be called when fillPlaylistPane is stubbed');
		},
		() => {},
		() => {},
		() => {},
		() => {},
		async () => {
			throw new Error('_fetchSmartlistRows must not be called');
		},
		async () => {
			throw new Error('fetchMissingTrackRows must not be called');
		},
		0,
		pushToast,
		(wire, order) => ({ ...wire, order })
	);
}

function node(name) {
	return { playlist_id: name, name, kind: 'playlist', track_count: 0, broken_count: 0, children: [] };
}

test('a load failure that lost the race to a newer load pushes no toast', async () => {
	const p = contract.createPaneStore();
	const toasts = [];
	let rejectFirst;
	const loadPane = makeLoadPane({
		fillPlaylistPaneImpl: () => new Promise((_resolve, reject) => (rejectFirst = reject)),
		pushToast: (msg, kind) => toasts.push({ msg, kind })
	});

	const firstLoad = loadPane(p, node('First'));
	assert.ok(rejectFirst, 'the first fetch must be in flight before the second load starts');

	// A newer selection supersedes the in-flight load before it settles -
	// exactly the race the pin describes.
	p.beginLoad('second', 'Second');

	rejectFirst(new Error('network exploded'));
	await firstLoad;

	assert.deepEqual(toasts, [], 'a superseded load failure must not surface an error toast');
	assert.equal(p.error, null, 'the superseded failure must not overwrite the current load state either');
});

test('control: a genuine, non-superseded load failure still toasts', async () => {
	const p = contract.createPaneStore();
	const toasts = [];
	const loadPane = makeLoadPane({
		fillPlaylistPaneImpl: () => Promise.reject(new Error('network exploded')),
		pushToast: (msg, kind) => toasts.push({ msg, kind })
	});

	await loadPane(p, node('Only'));

	assert.equal(toasts.length, 1, 'a real failure with no newer load in flight must still toast');
	assert.match(toasts[0].msg, /playlist load failed:.*network exploded/);
	assert.equal(toasts[0].kind, 'error');
	assert.match(p.error, /network exploded/);
});
