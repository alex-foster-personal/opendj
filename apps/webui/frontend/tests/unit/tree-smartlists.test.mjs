import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = `export { TreeSmartlists } from '$lib/components/rb/browser/tree-smartlists.svelte.ts';`;

function flush() {
	return new Promise((resolve) => setImmediate(resolve));
}

function makeSubscriptions() {
	const kinds = new Map();
	let resync;
	return {
		subscribeKind(kind, listener) {
			kinds.set(kind, listener);
			return () => kinds.delete(kind);
		},
		subscribeResync(listener) {
			resync = listener;
			return () => (resync = undefined);
		},
		fireKind(kind) {
			kinds.get(kind)?.([], {});
		},
		fireResync() {
			resync?.('gap');
		},
		subscribedKinds() {
			return [...kinds.keys()];
		}
	};
}

async function loadClass() {
	return (await loadRuneModule(ENTRY)).TreeSmartlists;
}

test('constructor lists with counts and an empty result settles loading', async () => {
	const TreeSmartlists = await loadClass();
	const subscriptions = makeSubscriptions();
	const calls = [];
	const store = new TreeSmartlists(
		() => undefined,
		async (options) => {
			calls.push(options);
			return [];
		},
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	await flush();
	assert.deepEqual(calls, [{ includeCounts: true }]);
	assert.deepEqual(subscriptions.subscribedKinds(), [
		'tracks',
		'smartlists',
		'mytags',
		'pairings'
	]);
	assert.deepEqual(store.rows, []);
	assert.equal(store.error, null);
	store.destroy();
});

test('backend errors expose their code', async () => {
	const TreeSmartlists = await loadClass();
	const subscriptions = makeSubscriptions();
	const error = Object.assign(new Error('unavailable'), {
		name: 'RbApiError',
		code: 'SMARTLISTS_DB_UNAVAILABLE'
	});
	const store = new TreeSmartlists(
		() => undefined,
		async () => {
			throw error;
		},
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	await flush();
	assert.equal(store.error, 'SMARTLISTS_DB_UNAVAILABLE');
	store.destroy();
});

test('track and smartlist events coalesce, and destroy unsubscribes', async () => {
	const TreeSmartlists = await loadClass();
	const subscriptions = makeSubscriptions();
	const gates = [];
	let calls = 0;
	const list = () => {
		calls += 1;
		return new Promise((resolve) => gates.push(resolve));
	};
	const store = new TreeSmartlists(
		() => undefined,
		list,
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	assert.equal(calls, 1);
	subscriptions.fireKind('tracks');
	subscriptions.fireKind('smartlists');
	subscriptions.fireResync();
	assert.equal(calls, 1);
	gates[0]([]);
	await flush();
	assert.equal(calls, 2, 'overlapping invalidations collapse to one trailing reload');
	gates[1]([]);
	await flush();
	store.destroy();
	subscriptions.fireKind('tracks');
	await flush();
	assert.equal(calls, 2);
});

test('click invokes an available selection callback and otherwise no-ops', async () => {
	const TreeSmartlists = await loadClass();
	const subscriptions = makeSubscriptions();
	const row = { id: 'sl-1' };
	let selected = null;
	const store = new TreeSmartlists(
		() => (value) => (selected = value),
		async () => [],
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	store.click(row);
	assert.equal(selected, row);
	store.destroy();

	const inertSubscriptions = makeSubscriptions();
	const inert = new TreeSmartlists(
		() => undefined,
		async () => [],
		inertSubscriptions.subscribeKind,
		inertSubscriptions.subscribeResync
	);
	assert.doesNotThrow(() => inert.click(row));
	inert.destroy();
});
