/**
 * SPOT-6555: Connect only where the sign-in can finish.
 *
 * Spotify redirects the browser to http://127.0.0.1:8888/callback, which the
 * ENGINE listens on. A browser on another machine (agentbox, a remote
 * daemon) follows that redirect to its own loopback, so the panel must not
 * offer Connect there.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/spotify-account-api.ts');
});

test('a browser on the engine machine can connect', () => {
	assert.equal(mod.spotifyConnectRunsHere('', '127.0.0.1', false), true);
	assert.equal(mod.spotifyConnectRunsHere('', 'localhost', false), true);
	assert.equal(mod.spotifyConnectRunsHere('http://127.0.0.1:8765', 'localhost', false), true);
});

test('a browser reaching the engine over the network cannot', () => {
	assert.equal(mod.spotifyConnectRunsHere('', 'box.example-tailnet.ts.net', false), false);
	assert.equal(mod.spotifyConnectRunsHere('', '192.168.1.20', false), false);
	// Local page, remote daemon: the listener is on the daemon's machine.
	assert.equal(
		mod.spotifyConnectRunsHere('http://box.example-tailnet.ts.net:8765', '127.0.0.1', false),
		false
	);
});

test('a desktop shell signs in through its own machine, so only the engine address counts', () => {
	assert.equal(mod.spotifyConnectRunsHere('', 'tauri.localhost', true), true);
	assert.equal(mod.spotifyConnectRunsHere('http://127.0.0.1:8765', 'tauri.localhost', true), true);
	assert.equal(
		mod.spotifyConnectRunsHere('http://box.example-tailnet.ts.net:8765', 'tauri.localhost', true),
		false
	);
});

test('a Spotify Client ID is 32 hex digits, pasted with stray whitespace', () => {
	assert.equal(mod.isSpotifyClientId('0123456789abcdef0123456789ABCDEF'), true);
	assert.equal(mod.isSpotifyClientId('  0123456789abcdef0123456789abcdef\n'), true);
	for (const bad of ['', 'not-an-id', 'g'.repeat(32), 'a'.repeat(31), 'a'.repeat(33)]) {
		assert.equal(mod.isSpotifyClientId(bad), false, bad);
	}
});
