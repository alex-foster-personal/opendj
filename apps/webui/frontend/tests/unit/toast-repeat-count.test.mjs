/** Pin 2b3794faa443: repeated control failures update one visible toast.
 * Uses the real Svelte compiler and store, without substituted APIs or state.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { build, transformSync } from 'esbuild';
import { compileModule } from 'svelte/compiler';

import { importBundledSource } from './import-bundled-source.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const root = fileURLToPath(new URL('../..', import.meta.url));
const bundled = await build({
	absWorkingDir: root,
	stdin: { contents: "export * from './src/lib/stores.svelte.ts'; export { findPerfEventById } from './src/lib/rb/perf-event-log.ts';", resolveDir: root },
	alias: { $lib: `${root}/src/lib` },
	define: { 'import.meta.env.VITE_API_BASE': 'undefined' },
	bundle: true, format: 'esm', platform: 'node', write: false,
	plugins: [{ name: 'real-svelte-runes', setup(builder) {
		builder.onLoad({ filter: /\.svelte\.ts$/ }, ({ path }) => {
			const typed = transformSync(readFileSync(path, 'utf8'), { loader: 'ts' }).code;
			return { contents: compileModule(typed, { filename: path, generate: 'server' }).js.code,
				resolveDir: path.slice(0, path.lastIndexOf('/')) };
		});
	} }, viteUrlSuffixPlugin]
});
const stores = await importBundledSource(bundled.outputFiles[0].text, 'toast-repeat-count-stores');
function clear() {
	for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
}

test('if one control repeats with changing values then one toast counts every occurrence', () => {
	try {
		stores.pushToast('ratio 1.21 rejected', 'info', 5000, undefined, {}, 'deck:1:tempo');
		const first = { ...stores.toasts[0] };
		stores.pushToast('ratio 1.22 rejected', 'info', 5000, undefined, {}, 'deck:1:tempo');
		assert.equal(stores.toasts.length, 1);
		assert.equal(stores.toasts[0].count, 2);
		assert.equal(stores.toasts[0].id, first.id, 'DOM key remains stable during a drag');
		assert.notEqual(stores.toasts[0].logId, first.logId, 'each event keeps a distinct log id');
		assert.equal(stores.toasts[0].message, 'ratio 1.22 rejected');
		assert.equal(stores.findPerfEventById(first.logId).message, first.message);
		assert.equal(stores.findPerfEventById(stores.toasts[0].logId).message, 'ratio 1.22 rejected');
	} finally { clear(); }
});

test('if deck, control or severity differs then its messages remain separate', () => {
	try {
		stores.pushToast('first', 'info', 5000, undefined, {}, 'deck:1:tempo');
		stores.pushToast('second', 'info', 5000, undefined, {}, 'deck:2:tempo');
		stores.pushToast('third', 'info', 5000, undefined, {}, 'deck:1:gain');
		stores.pushToast('error', 'error', 5000, undefined, {}, 'deck:1:tempo');
		assert.equal(stores.toasts.length, 4);
		assert.ok(stores.toasts.every((toast) => toast.count === 1));
	} finally { clear(); }
});

test('if a held message repeats then it stays held and dismissal starts a fresh count', () => {
	try {
		stores.pushToast('held', 'info', 5000, undefined, {}, 'deck:1:tempo');
		stores.holdToast(stores.toasts[0].logId);
		stores.pushToast('held again', 'info', 5000, undefined, {}, 'deck:1:tempo');
		assert.equal(stores.toasts.length, 1);
		assert.equal(stores.toastTimerArmed(stores.toasts[0].logId), false);
		stores.releaseToast(stores.toasts[0].logId);
		assert.equal(stores.toastTimerArmed(stores.toasts[0].logId), true);
		clear();
		stores.pushToast('fresh', 'info', 5000, undefined, {}, 'deck:1:tempo');
		assert.equal(stores.toasts[0].count, 1);
	} finally { clear(); }
});

test('if no grouping is requested then identical messages keep their separate identities', () => {
	try {
		stores.pushToast('same words');
		stores.pushToast('same words');
		assert.equal(stores.toasts.length, 2);
		assert.notEqual(stores.toasts[0].logId, stores.toasts[1].logId);
	} finally { clear(); }
});

test('if a held toast repeats then every previously returned id can still release or dismiss it', () => {
	try {
		stores.pushToast('first failure', 'info', 5000, undefined, {}, 'deck:1:tempo');
		const firstId = stores.toasts[0].logId;
		assert.equal(stores.holdToast(firstId), true);
		stores.pushToast('second failure', 'info', 5000, undefined, {}, 'deck:1:tempo');
		const secondId = stores.toasts[0].logId;
		stores.pushToast('third failure', 'info', 5000, undefined, {}, 'deck:1:tempo');
		assert.equal(stores.releaseToast(firstId), true);
		assert.equal(stores.toastTimerArmed(firstId), true);
		assert.equal(stores.dismissToast(secondId), true);
		assert.equal(stores.toasts.length, 0);
		assert.equal(stores.holdToast(firstId), false);
	} finally { clear(); }
});

test('if the toast count is absent from UI or IPC then the repeat signal is broken', () => {
	const view = readFileSync(`${root}/src/lib/components/rb/ToastStack.svelte`, 'utf8');
	const ipc = readFileSync(`${root}/src/lib/rb/performance-ipc.svelte.ts`, 'utf8');
	assert.match(view, /toast\.count\s*>\s*1/);
	assert.match(view, /data-toast-count/);
	assert.match(ipc, /count:\s*toast\.count/);
	assert.match(ipc, /`performance:\$\{deck\}:\$\{command\.type\}:\$\{subcontrol\}`/);
	for (const discriminator of ['band', 'stem', 'slot']) {
		assert.ok(ipc.includes(`'${discriminator}' in command`), `${discriminator} must distinguish separate controls`);
	}
});

test('if follower sync skips repeat while dragging then they use the same counted-toast path', () => {
	// Pin 9bf12adccb45 moved the decision out of the engine: what a completed
	// sync says is now beatSyncOutcomeNotices in beat-sync-math.ts, returning
	// the message and its grouping key as data. Both ends are checked, because
	// a key that is set but never passed to pushToast groups nothing.
	const math = readFileSync(`${root}/src/lib/rb/beat-sync-math.ts`, 'utf8');
	const skippedNotice = math.slice(
		math.indexOf('`Beat Sync skipped deck(s)'),
		math.indexOf('events: planFailed.map')
	);
	assert.ok(skippedNotice.length > 0, 'the skipped-follower notice must still exist');
	assert.match(skippedNotice, /`beat-sync-followers:\$\{master\}`/);

	const engine = readFileSync(`${root}/src/lib/rb/audio-engine.svelte.ts`, 'utf8');
	assert.match(
		engine,
		/pushToast\(\s*notice\.message,\s*notice\.kind,[^)]*notice\.groupKey\s*\)/,
		"the engine must hand the notice's groupKey to pushToast, or nothing coalesces"
	);
});
