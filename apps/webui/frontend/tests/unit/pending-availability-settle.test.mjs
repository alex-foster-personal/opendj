/**
 * @pytest.mark.requirement PERF-RB-03
 * Pin cba7bf1dbb05: the tree said 4 non-broken tracks while the table, with
 * broken rows hidden, showed more than 40. Rows whose disk truth is still
 * pending stay visible under hide-broken (PERF-RB-02), and nothing ever asked
 * again, so they stayed visible for good.
 *
 * [if] a pane holds pending rows [then] it re-asks until they settle [else stop].
 * [if] a fresh row is still pending [then] the held row stays pending, never
 *   guessed [else stop].
 * [if] a held row already settled [then] a later pending answer for it is
 *   ignored [else stop].
 *
 * Regression lines:
 * - if pending rows are never re-asked then hide-broken shows rows the tree
 *   count calls broken -> broken
 * - if a settled row is overwritten by a later pending answer then rows flap
 * - if the loop never stops then an unreachable disk costs a request forever
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');
const row = (id, availability) => ({
	stable_id: id,
	file_availability: availability,
	file_exists:
		availability === 'AVAILABILITY_PENDING' ? null : availability === 'present' ? true : false
});
const hideBroken = (rows) => rows.filter((r) => r.file_exists !== false);

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/pending-availability-settle.ts');
});

describe('applySettledAvailability', () => {
	it('settles pending rows in place and reports what is left', () => {
		const held = [row('a', 'present'), row('b', 'AVAILABILITY_PENDING'), row('c', 'AVAILABILITY_PENDING')];
		const left = mod.applySettledAvailability(held, [
			row('a', 'present'),
			row('b', 'absent'),
			row('c', 'AVAILABILITY_PENDING')
		]);
		assert.equal(left, 1);
		assert.deepEqual(held[1], row('b', 'absent'));
		assert.deepEqual(held[2], row('c', 'AVAILABILITY_PENDING'));
	});

	it('the pin: 44 rows shown under hide-broken become the 4 the tree counted', () => {
		const held = [
			...Array.from({ length: 4 }, (_, i) => row(`p${i}`, 'present')),
			...Array.from({ length: 40 }, (_, i) => row(`x${i}`, 'AVAILABILITY_PENDING'))
		];
		assert.equal(hideBroken(held).length, 44);
		const fresh = held.map((r) => (r.file_exists === null ? row(r.stable_id, 'absent') : r));
		assert.equal(mod.applySettledAvailability(held, fresh), 0);
		assert.equal(hideBroken(held).length, 4);
	});

	it('never un-settles a held row from a later pending answer', () => {
		const held = [row('a', 'absent')];
		assert.equal(mod.applySettledAvailability(held, [row('a', 'AVAILABILITY_PENDING')]), 0);
		assert.deepEqual(held[0], row('a', 'absent'));
	});

	it('a pending row missing from the fresh answer stays pending', () => {
		const held = [row('gone', 'AVAILABILITY_PENDING')];
		assert.equal(mod.applySettledAvailability(held, []), 1);
		assert.equal(held[0].file_exists, null);
	});

	it('hasPendingAvailability sees pending and only pending', () => {
		assert.equal(mod.hasPendingAvailability([row('a', 'present'), row('b', 'absent')]), false);
		assert.equal(mod.hasPendingAvailability([row('a', 'AVAILABILITY_PENDING')]), true);
	});
});

describe('startPendingSettle', () => {
	function harness(answers) {
		const timers = [];
		const held = [row('a', 'AVAILABILITY_PENDING'), row('b', 'AVAILABILITY_PENDING')];
		let fetches = 0;
		const stop = mod.startPendingSettle({
			rows: () => held,
			fetchRows: async () => {
				const answer = answers[Math.min(fetches, answers.length - 1)];
				fetches += 1;
				if (answer instanceof Error) throw answer;
				return answer;
			},
			setTimer: (fn, ms) => {
				timers.push({ fn, ms });
				return timers.length;
			},
			clearTimer: (handle) => {
				timers[handle - 1].cleared = true;
			},
			onError: (exc) => {
				harnessErrors.push(String(exc));
			}
		});
		const harnessErrors = [];
		const fire = async () => {
			const next = timers.find((t) => !t.fired && !t.cleared);
			if (next === undefined) return false;
			next.fired = true;
			await next.fn();
			return true;
		};
		return { timers, held, stop, fire, fetches: () => fetches, errors: harnessErrors };
	}

	it('re-asks on the backoff schedule and stops once everything settled', async () => {
		const h = harness([
			[row('a', 'absent'), row('b', 'AVAILABILITY_PENDING')],
			[row('a', 'absent'), row('b', 'present')]
		]);
		assert.equal(h.timers.length, 1);
		assert.equal(h.timers[0].ms, mod.PENDING_SETTLE_DELAYS_MS[0]);
		assert.equal(await h.fire(), true);
		assert.equal(h.held[0].file_exists, false);
		assert.equal(h.timers[1].ms, mod.PENDING_SETTLE_DELAYS_MS[1]);
		assert.equal(await h.fire(), true);
		assert.equal(h.held[1].file_exists, true);
		assert.equal(await h.fire(), false, 'kept polling after everything settled');
		assert.equal(h.fetches(), 2);
	});

	it('gives up after the last delay when rows never settle', async () => {
		const stuck = [row('a', 'AVAILABILITY_PENDING'), row('b', 'AVAILABILITY_PENDING')];
		const h = harness([stuck]);
		while (await h.fire());
		assert.equal(h.fetches(), mod.PENDING_SETTLE_DELAYS_MS.length);
		assert.ok(mod.PENDING_SETTLE_DELAYS_MS.length >= 3);
	});

	it('does nothing when no row is pending', () => {
		const timers = [];
		mod.startPendingSettle({
			rows: () => [row('a', 'present')],
			fetchRows: async () => assert.fail('fetched with nothing pending'),
			setTimer: (fn, ms) => timers.push({ fn, ms }),
			clearTimer: () => {},
			onError: () => {}
		});
		assert.equal(timers.length, 0);
	});

	it('stop cancels the pending timer and a late answer changes nothing', async () => {
		const h = harness([[row('a', 'absent'), row('b', 'absent')]]);
		const armed = h.timers[0];
		h.stop();
		assert.equal(armed.cleared, true);
		await armed.fn();
		assert.equal(h.held[0].file_exists, null, 'a stopped loop still wrote rows');
		assert.equal(h.timers.length, 1);
	});

	it('a failed re-ask is reported and the loop keeps its schedule', async () => {
		const h = harness([new Error('engine away'), [row('a', 'absent'), row('b', 'absent')]]);
		await h.fire();
		assert.deepEqual(h.errors, ['Error: engine away']);
		assert.equal(await h.fire(), true);
		assert.equal(h.held[0].file_exists, false);
	});
});

test('BrowserPanel starts the settle loop for the active pane', () => {
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	assert.match(panel, /startPendingSettle\(\{/);
	const support = read('src/lib/components/rb/browser/browser-panel-support.ts');
	assert.match(support, /startPendingSettle.*from '\.\/pending-availability-settle'/);
});
