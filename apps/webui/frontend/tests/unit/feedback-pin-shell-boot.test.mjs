/**
 * The deferred comment-pin shell fails visibly (Sol review of #3903, P1).
 *
 * Moving the pin layer, the topbar pin button and the `m` hotkey behind a
 * deferred boot task took them off the library page's first paint, but it
 * also turned a missing chunk from a page failure into a silent one: the
 * detached promise rejected unhandled, `onReady` never ran, and the topbar
 * kept an empty 28px slot with no pins, no button and no hotkey.
 *
 * Regression lines:
 * - if a rejected load does not reach `onError` then the feature vanishes
 *   without a word again
 * - if `onReady` throwing does not reach `onError` then a shell that loaded
 *   but could not mount is just as silent
 * - if a successful load calls `onError` then every boot raises a false
 *   error toast (the overshoot)
 * - if the load runs before the scheduler releases it then the shell is back
 *   on the first paint and the bundle budget pays for it again
 * - if the layout stops wiring `onError` to an error toast and a marked slot
 *   then the failure is caught here and dropped there
 *
 * WHAT THIS FILE IS, AND IS NOT. It runs `deferFeedbackPinShell` itself for
 * real, through the loader and scheduler seams that are its production
 * signature; the components a loader resolves to and the boot window are
 * handed in, because node:test can mount neither a Svelte component nor a
 * browser idle frame, and the last test pins the layout's wiring by source
 * shape. None of that mounts the layout, runs the real boot scheduler, or
 * fetches the real chunk. The proof through that path, a real browser, the
 * real scheduler, the real `import()` and a chunk request that fails at the
 * network layer, is tests/e2e/lazy-chunk-failures.spec.ts (Codex review of
 * #3862, P1). This file is the fast contract of the seam, kept because it
 * names each regression line in one process and fails in under a second.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { manualBootScheduler } from './fake-boot-scheduler.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/feedback-pin-shell-boot.ts');
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const SHELL = {
	layer: function Layer() {},
	shellButton: function Button() {},
	dock: function Dock() {},
	installCommentPinHotkeys: () => () => {}
};

test('a load that rejects reaches onError, and onReady never runs', async () => {
	const { scheduler, release } = manualBootScheduler();
	const failure = new Error('Failed to fetch dynamically imported module');
	const ready = [];
	const errors = [];
	mod.deferFeedbackPinShell(
		(shell) => ready.push(shell),
		(error) => errors.push(error),
		() => Promise.reject(failure),
		scheduler
	);
	release();
	await settle();
	assert.deepEqual(ready, []);
	assert.deepEqual(errors, [failure]);
});

test('an onReady that throws reaches onError too', async () => {
	const { scheduler, release } = manualBootScheduler();
	const failure = new Error('hotkey install failed');
	const errors = [];
	mod.deferFeedbackPinShell(
		() => {
			throw failure;
		},
		(error) => errors.push(error),
		() => Promise.resolve(SHELL),
		scheduler
	);
	release();
	await settle();
	assert.deepEqual(errors, [failure]);
});

test('a load that succeeds reaches onReady and never onError', async () => {
	const { scheduler, release } = manualBootScheduler();
	const ready = [];
	const errors = [];
	mod.deferFeedbackPinShell(
		(shell) => ready.push(shell),
		(error) => errors.push(error),
		() => Promise.resolve(SHELL),
		scheduler
	);
	release();
	await settle();
	assert.deepEqual(ready, [SHELL]);
	assert.deepEqual(errors, []);
});

test('nothing loads until the boot scheduler releases the task', async () => {
	const { scheduler, pending, release } = manualBootScheduler();
	let loads = 0;
	mod.deferFeedbackPinShell(
		() => {},
		() => {},
		() => {
			loads += 1;
			return Promise.resolve(SHELL);
		},
		scheduler
	);
	await settle();
	assert.equal(loads, 0, 'the shell loaded during mount, on the first paint');
	assert.equal(pending(), 1);
	release();
	assert.equal(loads, 1);
});

test('the layout turns a failure into an error toast and a marked slot', () => {
	const layout = readFileSync(new URL('../../src/routes/+layout.svelte', import.meta.url), 'utf8');
	const call = layout.slice(layout.indexOf('deferFeedbackPinShell('), layout.indexOf('const id = setInterval'));
	assert.match(call, /pinShellError = /);
	assert.match(call, /pushToast\(`Comment pins failed to load: \$\{pinShellError\}`, 'error'/);
	assert.match(layout, /\{:else if pinShellError\}[\s\S]*aria-label="Comment pins failed to load"[\s\S]*title=\{`Comment pins failed to load/);
});
