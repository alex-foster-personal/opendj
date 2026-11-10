import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://window-command.example.test';

let ipc;

before(async () => {
	await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	delete globalThis.opendjShell;
});

function withBrowserWindow() {
	const previous = globalThis.window;
	globalThis.window = previous ?? {};
	return () => {
		if (previous === undefined) delete globalThis.window;
		else globalThis.window = previous;
	};
}

test('window command parses open, close and focus for second-screen', () => {
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({ type: 'window', action: 'open', label: 'second-screen' }),
		{
			type: 'window',
			action: 'open',
			label: 'second-screen'
		}
	);
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({ type: 'window', action: 'close', label: 'second-screen' }),
		{
			type: 'window',
			action: 'close',
			label: 'second-screen'
		}
	);
	assert.deepEqual(
		ipc.parsePerformanceCommandForTest({ type: 'window', action: 'focus', label: 'second-screen' }),
		{
			type: 'window',
			action: 'focus',
			label: 'second-screen'
		}
	);
});

test('an unknown window label is refused with the known list', () => {
	assert.throws(
		() => ipc.parsePerformanceCommandForTest({ type: 'window', action: 'open', label: 'booth' }),
		/unknown window label "booth"; known: second-screen/
	);
});

test('in a browser tab the command is refused with a reason that names the shell', async () => {
	const restore = withBrowserWindow();
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			globalThis.window.musicDjToolsPerformance.dispatch({
				type: 'window',
				action: 'open',
				label: 'second-screen'
			}),
			/desktop shell/
		);
		assert.deepEqual(ipc.queryPerformanceState().windows, []);
	} finally {
		uninstall();
		restore();
	}
});

test('a successful shell open, focus and close talk to the Electron bridge', async () => {
	const calls = [];
	globalThis.opendjShell = {
		kind: 'electron',
		version: 3,
		windows: {
			async open(label) {
				calls.push(['open', label]);
			},
			async close(label) {
				calls.push(['close', label]);
			},
			async focus(label) {
				calls.push(['focus', label]);
			}
		}
	};
	const restore = withBrowserWindow();
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		assert.deepEqual(ipc.queryPerformanceState().windows, []);
		await globalThis.window.musicDjToolsPerformance.dispatch({
			type: 'window',
			action: 'open',
			label: 'second-screen'
		});
		assert.deepEqual(ipc.queryPerformanceState().windows, ['second-screen']);
		await globalThis.window.musicDjToolsPerformance.dispatch({
			type: 'window',
			action: 'focus',
			label: 'second-screen'
		});
		assert.deepEqual(ipc.queryPerformanceState().windows, ['second-screen']);
		await globalThis.window.musicDjToolsPerformance.dispatch({
			type: 'window',
			action: 'close',
			label: 'second-screen'
		});
		assert.deepEqual(ipc.queryPerformanceState().windows, []);
		assert.deepEqual(calls, [
			['open', 'second-screen'],
			['focus', 'second-screen'],
			['close', 'second-screen']
		]);
	} finally {
		uninstall();
		restore();
	}
});

test('a refused open leaves the reported windows empty', async () => {
	globalThis.opendjShell = {
		kind: 'electron',
		version: 3,
		windows: {
			async open() {
				throw new Error('boom');
			},
			async close() {},
			async focus() {}
		}
	};
	const restore = withBrowserWindow();
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			globalThis.window.musicDjToolsPerformance.dispatch({
				type: 'window',
				action: 'open',
				label: 'second-screen'
			}),
			/boom/
		);
		assert.deepEqual(ipc.queryPerformanceState().windows, []);
	} finally {
		uninstall();
		restore();
	}
});
