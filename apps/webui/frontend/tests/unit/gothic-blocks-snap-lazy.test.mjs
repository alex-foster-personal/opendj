/**
 * Requirements: DECKUX-42.
 *
 * The Gothic block snapper (wave/block-layout.ts) loads on demand through
 * wave/block-layout-lazy.svelte.ts. Regression lines:
 * - if a failed fetch is cached then Gothic rows keep the fixed pitch until a reload
 * - if a failed fetch is not surfaced then the fixed pitch looks like snapping that works
 * - if a failed fetch is retried on the next call then a playing deck re-requests
 *   the dead chunk every frame
 * - if the retry waits for a redraw then a stopped deck row never recovers
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = 'tests/unit/fixtures/wave-render-block-snap-entry.ts';
const FAIL = { failImports: [/\/block-layout$/] };
const settle = async () => {
	for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r));
};

describe('block-layout-lazy loader', () => {
	it('a failed fetch rejects loudly, is surfaced, and is not cached', async () => {
		const mod = await loadTypeScriptModule(ENTRY, FAIL);
		assert.equal(mod.blockSnapLoadError(), null, 'CONTROL: no error before any fetch');
		const first = mod.loadBlockSnapApi();
		await assert.rejects(first, /Failed to fetch dynamically imported module/);
		assert.match(mod.blockSnapLoadError() ?? '', /Failed to fetch dynamically imported module/);
		const second = mod.loadBlockSnapApi();
		assert.notEqual(second, first, 'a rejected fetch must not be cached');
		await second.catch(() => {});
	});

	it('after a failure, frames back off; past the window it retries and clears the error', async () => {
		const mod = await loadTypeScriptModule(ENTRY, FAIL);
		const realNow = Date.now;
		const realError = console.error;
		const realSetTimeout = globalThis.setTimeout;
		const timers = [];
		let now = 1_000_000;
		let errors = 0;
		Date.now = () => now;
		globalThis.setTimeout = (fn, ms) => {
			timers.push({ fn, ms });
			return timers.length;
		};
		console.error = () => {
			errors += 1;
		};
		try {
			assert.equal(mod.blockSnapApiFor(true), null);
			await settle();
			assert.equal(errors, 1, 'first failure is reported');
			assert.notEqual(mod.blockSnapLoadError(), null);
			assert.deepEqual(
				timers.map((t) => t.ms),
				[1_000],
				'the failure scheduled its own retry'
			);
			// Sixty frames inside the window start no fetch. A refetch would land
			// (esbuild's test bundle resolves a re-import of the failing module).
			for (let i = 0; i < 60; i += 1) assert.equal(mod.blockSnapApiFor(true), null);
			await settle();
			assert.equal(mod.blockSnapApiFor(true), null, 'no refetch inside the backoff window');
			assert.equal(timers.length, 1);
			assert.equal(errors, 1);
			// No row redraws: the timer alone retries, and the retry lands.
			now += 1_001;
			timers[0].fn();
			await settle();
			assert.notEqual(mod.blockSnapApiFor(true), null, 'the scheduled retry loaded the snapper');
			assert.equal(mod.blockSnapLoadError(), null, 'a successful retry clears the error');
		} finally {
			Date.now = realNow;
			globalThis.setTimeout = realSetTimeout;
			console.error = realError;
		}
	});

	it('want false fetches nothing and a failure nobody wanted schedules no retry', async () => {
		const mod = await loadTypeScriptModule(ENTRY, FAIL);
		const realSetTimeout = globalThis.setTimeout;
		const timers = [];
		globalThis.setTimeout = (fn, ms) => {
			timers.push(ms);
			return timers.length;
		};
		try {
			for (let i = 0; i < 3; i += 1) assert.equal(mod.blockSnapApiFor(false), null);
			await settle();
			assert.equal(mod.blockSnapLoadError(), null, 'want false started no fetch');
			await mod.loadBlockSnapApi().catch(() => {});
			assert.equal(timers.length, 0);
		} finally {
			globalThis.setTimeout = realSetTimeout;
		}
	});
});
