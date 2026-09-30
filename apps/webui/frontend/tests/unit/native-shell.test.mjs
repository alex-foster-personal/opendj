/**
 * The shell-neutral native bridge (apps/webui/frontend/src/lib/shell/native-shell.ts).
 *
 * - if an Electron page is read as a browser tab then the folder picker,
 *   sign-in, quit and update all silently vanish in the Electron app -> broken
 * - if a browser tab is read as a shell then a tab offers controls that throw
 *   -> broken (the over-detection control)
 * - if a lookalike global (wrong kind, null) counts as Electron then any page
 *   script can impersonate the shell -> broken
 * - if the Electron branch is not preferred when both globals exist then a
 *   stray Tauri global reroutes calls to a dead plugin -> broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/shell/native-shell.ts');
});

function electronScope(overrides = {}) {
	const calls = [];
	const bridge = {
		kind: 'electron',
		version: 1,
		pickFolder: async (options) => {
			calls.push(['pickFolder', options]);
			return '/Users/dev/Music';
		},
		openExternal: async (url) => {
			calls.push(['openExternal', url]);
		},
		exit: async (code) => {
			calls.push(['exit', code]);
		},
		relaunch: async () => {
			calls.push(['relaunch']);
		},
		applyUpdate: async (onProgress) => {
			onProgress({ phase: 'checking' });
			calls.push(['applyUpdate']);
			return { kind: 'no-update' };
		},
		...overrides
	};
	return { scope: { opendjShell: bridge }, calls };
}

test('a browser tab has no native shell', () => {
	assert.equal(mod.nativeShellKind({}), null);
});

test('the Electron preload bridge is detected', () => {
	assert.equal(mod.nativeShellKind(electronScope().scope), 'electron');
});

test('the Tauri global is still detected until cutover', () => {
	assert.equal(mod.nativeShellKind({ __TAURI_INTERNALS__: {} }), 'tauri');
});

test('a lookalike opendjShell is not the Electron shell', () => {
	assert.equal(mod.nativeShellKind({ opendjShell: null }), null);
	assert.equal(mod.nativeShellKind({ opendjShell: { kind: 'tauri' } }), null);
	assert.equal(mod.nativeShellKind({ opendjShell: 'electron' }), null);
});

test('Electron wins when both globals are present', () => {
	const { scope } = electronScope();
	scope.__TAURI_INTERNALS__ = {};
	assert.equal(mod.nativeShellKind(scope), 'electron');
});

test('pickFolder, openExternal and exitApp go through the Electron bridge', async () => {
	const { scope, calls } = electronScope();
	assert.equal(await mod.pickFolder('Choose a folder', scope), '/Users/dev/Music');
	await mod.openExternal('https://accounts.google.com/o/oauth2/auth', scope);
	await mod.exitApp(0, scope);
	assert.deepEqual(calls, [
		['pickFolder', { title: 'Choose a folder' }],
		['openExternal', 'https://accounts.google.com/o/oauth2/auth'],
		['exit', 0]
	]);
});

test('a cancelled Electron picker is null, not an empty path', async () => {
	const { scope } = electronScope({ pickFolder: async () => null });
	assert.equal(await mod.pickFolder('Choose a folder', scope), null);
});

test('every native call outside a shell fails loud instead of doing nothing', async () => {
	await assert.rejects(mod.pickFolder('x', {}), /browser tab/);
	await assert.rejects(mod.openExternal('https://example.com', {}), /browser tab/);
	await assert.rejects(mod.exitApp(0, {}), /browser tab/);
});

test('electronApplyUpdate is null outside Electron so callers keep their own path', () => {
	assert.equal(mod.electronApplyUpdate(() => {}, {}), null);
	assert.equal(mod.electronApplyUpdate(() => {}, { __TAURI_INTERNALS__: {} }), null);
});

test('electronApplyUpdate forwards progress and the outcome', async () => {
	const { scope } = electronScope();
	const seen = [];
	const outcome = await mod.electronApplyUpdate((p) => seen.push(p.phase), scope);
	assert.deepEqual(outcome, { kind: 'no-update' });
	assert.deepEqual(seen, ['checking']);
});
