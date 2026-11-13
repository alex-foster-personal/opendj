/**
 * Issue #13416: the opendj `playlist_undo` and `playlist_redo` verbs run
 * against the real performance command handler, not the pytest page model
 * (tests/opendj_cli/conftest.py has no executor for them on purpose).
 *
 * The CLI table (apps/opendj_cli/catalog.py) emits the bare `{ type }` wire
 * command for both verbs; these tests send exactly that record through the
 * window IPC bridge the agent bus calls, with the real performance-ipc module.
 *
 * [if] a mounted playlist-history panel can undo through opendj [then ⛔️]
 * [if] a mounted playlist-history panel can redo through opendj [then ⛔️]
 * [if] no playlist-history panel is mounted [then ⛔️]
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

const catalog = readFileSync(
	fileURLToPath(new URL('../../../../opendj_cli/catalog.py', import.meta.url)),
	'utf8'
);

/** The wire record the CLI verb sends, read from its zero-arg catalog row. */
function cliWireCommand(verb) {
	const row = new RegExp(`Verb\\("${verb}", "${verb}", \\(\\)`);
	assert.match(catalog, row, `opendj verb ${verb} must be a zero-arg verb emitting only { type }`);
	return { type: verb };
}

/** Install the IPC bridge, optionally mount a recording history panel, and run `body`. */
async function withBridge(mountPanel, body) {
	const calls = [];
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	const unregister = mountPanel
		? ipc.registerPlaylistHistoryAdapter({
				undo: async () => {
					calls.push('undo');
				},
				redo: async () => {
					calls.push('redo');
				},
				jump: async (commandId) => {
					calls.push(`jump:${commandId}`);
				}
			})
		: () => {};
	try {
		await body(globalThis.window.musicDjToolsPerformance, calls);
	} finally {
		unregister();
		uninstall();
		delete globalThis.window;
	}
}

test('#13416: opendj playlist_undo undoes through a mounted playlist-history panel', async () => {
	// The CLI's undo command reaches the panel's undo exactly once, and nothing else.
	await withBridge(true, async (bridge, calls) => {
		await bridge.dispatch(cliWireCommand('playlist_undo'));
		assert.deepEqual(calls, ['undo']);
	});
});

test('#13416: opendj playlist_redo redoes through a mounted playlist-history panel', async () => {
	// The CLI's redo command reaches the panel's redo exactly once, and nothing else.
	await withBridge(true, async (bridge, calls) => {
		await bridge.dispatch(cliWireCommand('playlist_redo'));
		assert.deepEqual(calls, ['redo']);
	});
});

test('#13416: opendj playlist_undo and playlist_redo are refused when no history panel is mounted', async () => {
	// Without a panel both verbs reject with the named reason instead of silently succeeding.
	await withBridge(false, async (bridge) => {
		await assert.rejects(
			bridge.dispatch(cliWireCommand('playlist_undo')),
			/playlist_undo requires a mounted playlist history panel/
		);
		await assert.rejects(
			bridge.dispatch(cliWireCommand('playlist_redo')),
			/playlist_redo requires a mounted playlist history panel/
		);
	});
});

test('#13416: the bus refuses an undo or redo record carrying fields the CLI never sends', async () => {
	// The wire validator keeps the zero-arg contract the CLI verbs rely on.
	await withBridge(true, async (bridge, calls) => {
		await assert.rejects(bridge.dispatch({ type: 'playlist_undo', steps: 2 }));
		await assert.rejects(bridge.dispatch({ type: 'playlist_redo', steps: 2 }));
		assert.deepEqual(calls, []);
	});
});
