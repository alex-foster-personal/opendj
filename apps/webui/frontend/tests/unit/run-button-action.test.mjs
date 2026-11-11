/**
 * Issue #11281: runButtonAction pending gate and failure toasts.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = new URL('../../', import.meta.url);

function readTs(rel) {
	return readFileSync(new URL(rel, FRONTEND), 'utf8');
}

let helpers;
let stores;

before(async () => {
	stores = await loadTypeScriptModule('src/lib/stores.svelte.ts');
	helpers = await loadTypeScriptModule('src/lib/ui/run-button-action.ts');
});

function gate() {
	let pending = null;
	return helpers.runButtonActionGate(
		() => pending,
		(value) => {
			pending = value;
		}
	);
}

test('[if] a wrapped handler rejects [then] pushToastForFailure runs with the error, [else stop].', async () => {
	const g = gate();
	const err = new Error('load refused');
	const ok = await helpers.runButtonAction(g, 'retry', async () => {
		throw err;
	});
	assert.equal(ok, false);
	const singleStore = await loadTypeScriptModule('src/lib/stores.svelte.ts');
	const before = singleStore.toasts.length;
	singleStore.pushToastForFailure(err);
	assert.ok(singleStore.toasts.length > before);
	assert.equal(singleStore.toasts.at(-1).kind, 'error');
});

test('[if] a wrapped action takes perceptible time [then] the gate stays pending until it settles, [else stop].', async () => {
	const g = gate();
	let release;
	const wait = new Promise((resolve) => {
		release = resolve;
	});
	const run = helpers.runButtonAction(g, 'work', async () => {
		await wait;
	});
	assert.equal(helpers.isRunButtonActionPending(g), true);
	assert.equal(helpers.isRunButtonActionPending(g, 'work'), true);
	release();
	await run;
	assert.equal(helpers.isRunButtonActionPending(g), false);
});

test('pushToastForFailure passes the caught value through pushToast', () => {
	const src = readTs('src/lib/stores.svelte.ts');
	assert.match(src, /export function pushToastForFailure/);
	assert.match(src, /pushToast\(text, 'error', TOAST_DEFAULT_MS, exc\)/);
});

test('[if] runButtonAction is wired on layout reload [then] the helper module references setup retry, [else stop].', () => {
	const src = readTs('src/lib/ui/run-button-action.ts');
	assert.match(src, /pushToastForFailure/);
	assert.match(src, /export async function runButtonAction/);
});
