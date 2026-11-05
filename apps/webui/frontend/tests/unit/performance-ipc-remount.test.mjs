/**
 * The performance page keeps its browser IPC up after unmount until the plan
 * runner has settled (ea55829580). A remount must take that retiring install
 * over, while a live install still refuses a second one, and the late cleanup
 * of the retired install must not touch the new session.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

test('a remount takes over a retiring install, and the retired cleanup leaves the new session alone', async () => {
	globalThis.window = {};
	try {
		const uninstallOld = ipc.installPerformanceBrowserIpc();
		const oldIpc = window.musicDjToolsPerformance;
		ipc.retirePerformanceBrowserIpc();

		const uninstallNew = ipc.installPerformanceBrowserIpc();
		const newIpc = window.musicDjToolsPerformance;
		assert.notEqual(newIpc, oldIpc);

		await newIpc.dispatch({ type: 'master_volume', value: 0.4 });
		uninstallOld();
		assert.equal(window.musicDjToolsPerformance, newIpc);
		assert.equal(ipc.queryPerformanceState().mixer.master, 0.4);
		await newIpc.dispatch({ type: 'master_volume', value: 1 });
		uninstallNew();
		assert.equal(window.musicDjToolsPerformance, undefined);
	} finally {
		delete globalThis.window;
	}
});

test('a live install still refuses a second install without retirement', () => {
	globalThis.window = {};
	try {
		const uninstall = ipc.installPerformanceBrowserIpc();
		assert.throws(() => ipc.installPerformanceBrowserIpc(), /already installed/);
		uninstall();
	} finally {
		delete globalThis.window;
	}
});

test('the performance page retires its install on unmount before the deferred cleanup', async () => {
	const source = await readFile(
		new URL('../../src/routes/performance/+page.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		source,
		/retirePerformanceBrowserIpc\(\);\s*void mirrorClosed\.finally\(\(\) => ipcUninstall\?\.\(\)\);/
	);
});
