/**
 * Draft-persistence storage failures (Sol review r3941617668,
 * FeedbackWidget.svelte:164, P1 BLOCKING): a private/incognito window, storage
 * disabled by policy, or a full quota all throw from `localStorage`. Silently
 * catching that and continuing is exactly the data-loss condition REFRESH-01
 * and the draft-persistence feature exist to prevent, so both the write side
 * (`persistParkedPinDraft`) and the read side (`readParkedPinDraft`) must
 * report a storage failure through their `onError` hook rather than
 * swallowing it - the in-memory draft is left untouched either way.
 *
 * Regression lines:
 * - if a write failure is swallowed then a half-typed comment vanishes on
 *   refresh with nothing telling the maintainer it happened
 * - if a write failure throws INSIDE the caller's effect then the whole
 *   widget tears down over a draft that was still perfectly usable in memory
 * - if a read failure collapses silently into "no draft" then it is
 *   indistinguishable from there simply being none
 * - if a merely-corrupt (but readable) stored value now also reports an
 *   error then a stale/old-shaped draft starts spamming toasts on every boot
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let persistMod;
let restoreMod;

before(async () => {
	persistMod = await loadTypeScriptModule('src/lib/rb/feedback-pin-draft-persist.ts');
	restoreMod = await loadTypeScriptModule('src/lib/rb/feedback-pin-draft-restore.ts');
});

/** A minimal fake `Storage` whose `setItem`/`getItem`/`removeItem` can each be
 * told to throw, so tests need not spin up jsdom or a real browser. */
function fakeStorage({ throwOnSetItem = false, throwOnGetItem = false, getItemReturns = null } = {}) {
	return {
		setItem() {
			if (throwOnSetItem) throw new DOMException('storage disabled', 'SecurityError');
		},
		getItem() {
			if (throwOnGetItem) throw new DOMException('storage disabled', 'SecurityError');
			return getItemReturns;
		},
		removeItem() {}
	};
}

const draft = () => ({
	point: { x_pct: 10, y_pct: 20 },
	anchor: '.c-bpm',
	text: 'half typed',
	page: '/performance'
});

describe('persistParkedPinDraft (write side)', () => {
	it('writes normally and never calls onError when storage works', () => {
		const storage = fakeStorage();
		let called = false;
		persistMod.persistParkedPinDraft(storage, draft(), false, () => {
			called = true;
		});
		assert.equal(called, false);
	});

	it('reports a write failure through onError instead of throwing', () => {
		const storage = fakeStorage({ throwOnSetItem: true });
		let reported = null;
		assert.doesNotThrow(() => {
			persistMod.persistParkedPinDraft(storage, draft(), false, (err) => {
				reported = err;
			});
		});
		assert.ok(reported instanceof Error, 'the real storage error should reach onError');
		assert.match(reported.message, /storage disabled/);
	});

	it('reports a clear-on-empty failure the same way as a save failure', () => {
		const storage = {
			setItem() {},
			removeItem() {
				throw new Error('quota exceeded');
			}
		};
		let reported = null;
		persistMod.persistParkedPinDraft(storage, null, false, (err) => {
			reported = err;
		});
		assert.ok(reported instanceof Error);
	});

	it('does not call onError for a clean clear', () => {
		const storage = fakeStorage();
		let called = false;
		persistMod.persistParkedPinDraft(storage, null, false, () => {
			called = true;
		});
		assert.equal(called, false);
	});
});

describe('readParkedPinDraft (read side)', () => {
	it('restores a valid draft and never calls onError when storage works', () => {
		const storage = fakeStorage({ getItemReturns: JSON.stringify(draft()) });
		let called = false;
		const { draft: restored, foreign } = restoreMod.readParkedPinDraft(
			storage,
			'/performance',
			{ width: 1000, height: 800 },
			() => {
				called = true;
			}
		);
		assert.equal(called, false);
		assert.equal(foreign, false);
		assert.equal(restored.text, 'half typed');
	});

	it('reports a storage read failure through onError instead of collapsing silently', () => {
		const storage = fakeStorage({ throwOnGetItem: true });
		let reported = null;
		const { draft: restored, foreign } = restoreMod.readParkedPinDraft(
			storage,
			'/performance',
			{ width: 1000, height: 800 },
			(err) => {
				reported = err;
			}
		);
		assert.ok(reported instanceof Error, 'the real storage error should reach onError, not be swallowed');
		assert.match(reported.message, /storage disabled/);
		assert.equal(restored, null, 'there is nothing safe to restore from a read that failed outright');
		assert.equal(foreign, false);
	});

	it('does NOT report a merely-corrupt stored value - that is still "no draft", not a failure', () => {
		const storage = fakeStorage({ getItemReturns: 'not json at all {{{' });
		let called = false;
		const { draft: restored } = restoreMod.readParkedPinDraft(
			storage,
			'/performance',
			{ width: 1000, height: 800 },
			() => {
				called = true;
			}
		);
		assert.equal(called, false, 'a corrupt value is a design-covered case, not a storage failure');
		assert.equal(restored, null);
	});

	it('does not report anything when there is simply no stored draft', () => {
		const storage = fakeStorage({ getItemReturns: null });
		let called = false;
		restoreMod.readParkedPinDraft(storage, '/performance', { width: 1000, height: 800 }, () => {
			called = true;
		});
		assert.equal(called, false);
	});
});
