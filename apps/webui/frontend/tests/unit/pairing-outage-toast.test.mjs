/**
 * One pairing-outage toast, however the two stores fail and recover (Sol P1,
 * #6062: the latch re-armed whenever both errors were momentarily null, so a
 * graph that recovered while the index was still in flight let the index's
 * next failure toast a second time in the same outage).
 *
 * The latch is pairing-outage-toast.ts. Its inputs are the two stores' `error`
 * and `pending`, so the staggered sequences here drive the same state the real
 * stores publish, and the real PairingIndex is driven once with a deferred
 * response to prove its `pending` flag follows the in-flight lookup.
 */
import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://pairing-outage-toast.example.test';

let toastMod;
let indexMod;
let originalFetch;

before(async () => {
	toastMod = await loadTypeScriptModule('src/lib/rb/pairing-outage-toast.ts');
	indexMod = await loadTypeScriptModule('src/lib/rb/pairing-index.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

function harness() {
	const toasts = [];
	const update = toastMod.pairingOutageToaster((message) => toasts.push(message));
	return { toasts, update };
}

const settled = { error: null, pending: false };
const pending = { error: null, pending: true };
const failed = (message) => ({ error: message, pending: false });

describe('pairing-outage-toast', () => {
	it('one outage across both stores toasts once, the graph message first', () => {
		const { toasts, update } = harness();
		update(failed('pairing list failed: down'), pending);
		update(failed('pairing list failed: down'), failed('pairing lookup failed: down'));
		update(failed('pairing list failed: down'), settled);
		assert.deepEqual(toasts, ['pairing list failed: down']);
	});

	it('a graph that recovers while the index is still pending does not re-arm the latch', () => {
		const { toasts, update } = harness();
		update(failed('pairing list failed: down'), settled);
		// Graph succeeded; index still in flight from the same change.
		update(settled, pending);
		// Index then fails in the SAME outage: must stay silent.
		update(settled, failed('pairing lookup failed: down'));
		assert.deepEqual(toasts, ['pairing list failed: down']);
	});

	it('a fully settled recovery re-arms, and the next outage toasts again', () => {
		const { toasts, update } = harness();
		update(failed('pairing list failed: one'), settled);
		update(settled, settled);
		update(failed('pairing list failed: two'), settled);
		assert.deepEqual(toasts, ['pairing list failed: one', 'pairing list failed: two']);
	});

	it('an index failure that lands first toasts once; the graph failing after stays silent', () => {
		const { toasts, update } = harness();
		update(pending, failed('pairing lookup failed: first'));
		update(failed('pairing list failed: second'), failed('pairing lookup failed: first'));
		assert.deepEqual(toasts, ['pairing lookup failed: first']);
	});

	it('a pending store with no error is not an outage and never toasts', () => {
		const { toasts, update } = harness();
		update(pending, pending);
		update(settled, pending);
		assert.deepEqual(toasts, []);
	});
});

describe('PairingIndex.pending', () => {
	it('is true while a lookup is in flight and false once it settles', async () => {
		// listPairingsFor asks twice (as-from and as-to), so hold both responses
		// and release them together.
		const held = [];
		let onBothRequested;
		const bothRequested = new Promise((resolve) => (onBothRequested = resolve));
		globalThis.fetch = () =>
			new Promise((resolve) => {
				held.push(() =>
					resolve(
						new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } })
					)
				);
				if (held.length === 2) onBothRequested();
			});
		const index = new indexMod.PairingIndex();
		const refreshing = index.refresh(() => 'master');
		assert.equal(index.pending, true, 'a lookup in flight must read as pending');
		await bothRequested;
		for (const release of held) release();
		await refreshing;
		assert.equal(index.pending, false, 'a settled lookup must clear pending');
		assert.equal(index.error, null);
	});

	it('a failed lookup clears pending and sets error', async () => {
		globalThis.fetch = async () => new Response('nope', { status: 500 });
		const index = new indexMod.PairingIndex();
		await index.refresh(() => 'master');
		assert.equal(index.pending, false);
		assert.match(index.error ?? '', /pairing lookup failed/);
	});
});
