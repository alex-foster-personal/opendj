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
		const undoOpen = ipc.applyShellWindowLabelsForTest(['second-screen']);
		try {
			assert.deepEqual(ipc.queryPerformanceState().windows, ['second-screen']);
		} finally {
			undoOpen();
		}
		await globalThis.window.musicDjToolsPerformance.dispatch({
			type: 'window',
			action: 'focus',
			label: 'second-screen'
		});
		const undoFocus = ipc.applyShellWindowLabelsForTest(['second-screen']);
		try {
			assert.deepEqual(ipc.queryPerformanceState().windows, ['second-screen']);
		} finally {
			undoFocus();
		}
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

function memoryLocalStorage() {
	const map = new Map();
	return {
		getItem: (key) => (map.has(key) ? map.get(key) : null),
		setItem: (key, value) => map.set(key, String(value)),
		removeItem: (key) => {
			map.delete(key);
		},
		clear: () => {
			map.clear();
		}
	};
}

test('in Tauri a window command succeeds through control_shell_window and shell reports', async () => {
	const invokeCalls = [];
	let shellHandler = null;
	globalThis.__tauriInvokeMock = async (command, args) => {
		invokeCalls.push([command, args]);
		if (command === 'query_shell_window_labels') return [];
		return undefined;
	};
	globalThis.__tauriListenMock = async (_event, handler) => {
		shellHandler = handler;
		return () => {
			shellHandler = null;
		};
	};
	const previousStorage = globalThis.localStorage;
	const restore = withBrowserWindow();
	const store = memoryLocalStorage();
	globalThis.localStorage = store;
	globalThis.window = { ...globalThis.window, localStorage: store };
	globalThis.__TAURI_INTERNALS__ = {};
	await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	const tauriIpc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: API_BASE,
		alias: {
			'@tauri-apps/api/core': new URL('./tauri-core-mock.mjs', import.meta.url).pathname,
			'@tauri-apps/api/event': new URL('./tauri-event-mock.mjs', import.meta.url).pathname
		}
	});
	const uninstall = tauriIpc.installPerformanceBrowserIpc();
	await new Promise((resolve) => setTimeout(resolve, 0));
	try {
		assert.deepEqual(tauriIpc.queryPerformanceState().windows, []);
		await globalThis.window.musicDjToolsPerformance.dispatch({
			type: 'window',
			action: 'open',
			label: 'second-screen'
		});
		assert.deepEqual(invokeCalls[invokeCalls.length - 1], [
			'control_shell_window',
			{ action: 'open', label: 'second-screen' }
		]);
		assert.equal(typeof shellHandler, 'function');
		shellHandler({ payload: { labels: ['second-screen'] } });
		assert.deepEqual(tauriIpc.queryPerformanceState().windows, ['second-screen']);
		shellHandler({ payload: { labels: [] } });
		assert.deepEqual(tauriIpc.queryPerformanceState().windows, []);
	} finally {
		uninstall();
		delete globalThis.__tauriInvokeMock;
		delete globalThis.__tauriListenMock;
		delete globalThis.__TAURI_INTERNALS__;
		if (previousStorage === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = previousStorage;
		restore();
	}
});

test('in Electron the shell window reporter updates query state from list and onChange', async () => {
	let labelsHandler = null;
	let listCalls = 0;
	globalThis.opendjShell = {
		kind: 'electron',
		version: 3,
		windows: {
			async open() {},
			async close() {},
			async focus() {},
			async list() {
				listCalls += 1;
				return listCalls === 1 ? [] : ['second-screen'];
			},
			onChange(listener) {
				labelsHandler = listener;
				return () => {
					labelsHandler = null;
				};
			}
		}
	};
	const restore = withBrowserWindow();
	const uninstall = ipc.installPerformanceBrowserIpc();
	await new Promise((resolve) => setTimeout(resolve, 0));
	try {
		assert.deepEqual(ipc.queryPerformanceState().windows, []);
		labelsHandler(['second-screen']);
		assert.deepEqual(ipc.queryPerformanceState().windows, ['second-screen']);
		labelsHandler([]);
		assert.deepEqual(ipc.queryPerformanceState().windows, []);
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
