/**
 * Issue #1764, PR #1656 thread r3978915642: background library refresh must
 * still run the boot-pane retry when a side read (playlist tree, coverage,
 * reconcile) rejects.
 *
 * Regression lines:
 * - if a rejected playlist refresh skips the boot-pane retry -> broken
 * - if count/panes run before every side load has settled -> broken
 * - if refreshCountAndPanes returns false and the retry still runs -> broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BROWSER_PANEL = 'src/lib/components/rb/BrowserPanel.svelte';

async function loadPass() {
	return loadTypeScriptModule('src/lib/components/rb/browser/library-refresh-pass.ts');
}

test('a rejected playlist refresh still runs the boot-pane retry', async () => {
	const { runLibraryRefreshPass } = await loadPass();
	const order = [];
	const errors = [];
	await runLibraryRefreshPass({
		sideLoads: [
			async () => {
				order.push('side-0');
			},
			async () => {
				order.push('side-1');
			},
			async () => {
				order.push('side-2');
				throw new Error('playlists 503');
			}
		],
		refreshCountAndPanes: async () => {
			order.push('count-panes');
			return true;
		},
		retryBootPane: async () => {
			order.push('retry');
		},
		onSideLoadError: (exc) => errors.push(exc)
	});
	assert.equal(order.join(','), 'side-0,side-1,side-2,count-panes,retry');
	assert.equal(errors.length, 1);
	assert.equal(String(errors[0]), 'Error: playlists 503');
});

test('a side read that throws synchronously still runs the boot-pane retry', async () => {
	const { runLibraryRefreshPass } = await loadPass();
	let retried = false;
	await runLibraryRefreshPass({
		sideLoads: [
			() => {
				throw new Error('sync');
			}
		],
		refreshCountAndPanes: async () => true,
		retryBootPane: async () => {
			retried = true;
		},
		onSideLoadError: () => {}
	});
	assert.equal(retried, true);
});

test('count and panes wait for every side read to settle', async () => {
	const { runLibraryRefreshPass } = await loadPass();
	/** @type {(value?: void) => void} */
	let releaseSlow;
	const slow = new Promise((resolve) => {
		releaseSlow = resolve;
	});
	let countRan = false;
	const pass = runLibraryRefreshPass({
		sideLoads: [
			async () => {
				await slow;
			}
		],
		refreshCountAndPanes: async () => {
			countRan = true;
			return true;
		},
		retryBootPane: async () => {},
		onSideLoadError: () => {}
	});
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(countRan, false);
	releaseSlow();
	await pass;
	assert.equal(countRan, true);
});

test('a page going away ends the pass without the retry', async () => {
	const { runLibraryRefreshPass } = await loadPass();
	let retried = false;
	await runLibraryRefreshPass({
		sideLoads: [async () => {}],
		refreshCountAndPanes: async () => false,
		retryBootPane: async () => {
			retried = true;
		},
		onSideLoadError: () => {}
	});
	assert.equal(retried, false);
});

test('no side-read failure, no error report', async () => {
	const { runLibraryRefreshPass } = await loadPass();
	let errors = 0;
	let retried = false;
	await runLibraryRefreshPass({
		sideLoads: [async () => {}],
		refreshCountAndPanes: async () => true,
		retryBootPane: async () => {
			retried = true;
		},
		onSideLoadError: () => {
			errors += 1;
		}
	});
	assert.equal(errors, 0);
	assert.equal(retried, true);
});

test('BrowserPanel wires runLibraryRefreshPass and keeps shouldRetryBootPane in the retry function', () => {
	const source = readFileSync(BROWSER_PANEL, 'utf8');
	assert.match(
		source,
		/import \{ runLibraryRefreshPass \} from '\.\/browser\/library-refresh-pass'/
	);
	const retryFn = source.match(
		/async function _retryStrandedBootPane\(\): Promise<void> \{[\s\S]*?\n\t\}/
	)?.[0];
	assert.ok(retryFn, '_retryStrandedBootPane body');
	assert.match(retryFn, /shouldRetryBootPane\(/);
	assert.match(retryFn, /await _restoreBootPane\(\);/);
});
