/**
 * PLAY-18: the Trackify IPC's toggle_autoplay(false) is gated like the
 * performance IPC bridge. Only a user turns AutoPlay off.
 *
 * Regression lines:
 *   [if] toggle_autoplay(false) without byUser switches AutoPlay off [then] broken
 *   [if] toggle_autoplay(false, true) does not switch it off [then] a person's ask is ignored - broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"export { installTrackifyBrowserIpc } from '$lib/rb/trackify-ipc.svelte';",
	"export { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';"
].join('\n');

// REQ: PLAY-18
test('toggle_autoplay(false) needs byUser; without it AutoPlay stays on and one WARN names the IPC', async () => {
	const hadWindow = 'window' in globalThis;
	globalThis.window ??= {};
	const mod = await loadRuneModule(ENTRY);
	const uninstall = mod.installTrackifyBrowserIpc();
	const realWarn = console.warn;
	const warned = [];
	console.warn = (...args) => warned.push(args.join(' '));
	try {
		mod.setAutoPlayEnabled(true);
		globalThis.window.musicDjToolsTrackify.toggle_autoplay(false);
		assert.equal(mod.uiPrefs.auto_play_enabled, true, 'if the Trackify IPC can switch AutoPlay off with no user then broken');
		assert.match(warned.join('\n'), /not applied, AutoPlay stays on .*caller: window\.musicDjToolsTrackify\.toggle_autoplay/);
		assert.throws(() => globalThis.window.musicDjToolsTrackify.toggle_autoplay(false, 'yes'), /byUser must be boolean/);
		globalThis.window.musicDjToolsTrackify.toggle_autoplay(false, true);
		assert.equal(mod.uiPrefs.auto_play_enabled, false, 'a person\'s off lands');
		globalThis.window.musicDjToolsTrackify.toggle_autoplay(true);
		assert.equal(mod.uiPrefs.auto_play_enabled, true, 'on needs no provenance');
	} finally {
		console.warn = realWarn;
		uninstall?.();
		if (!hadWindow) delete globalThis.window;
	}
});
