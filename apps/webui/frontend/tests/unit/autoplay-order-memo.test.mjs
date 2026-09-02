/**
 * The AutoPlay poll must not re-simulate the charted order when nothing it
 * depends on has changed.
 *
 * Wed 2 Sep 2026, the maintainer's Chrome review loop: 1.5 fps, two long tasks of ~1.5 s
 * in every 2 s window, all attributed by the Long Animation Frames API to the
 * 250 ms setInterval in auto-play.svelte.ts. _refreshChartedOrder called
 * simulateAutoPlayChain over the open playlist (8558 rows, O(rows^2)) on every
 * tick, with identical inputs each time. Memory and load were green; this was
 * the app.
 *
 * [if] two polls with identical inputs both simulate [then] the main thread is
 *   saturated at 4 simulations a second on a big library - broken.
 * [if] any single input changes and the key does not [then] the order column
 *   goes stale after a playlist edit, a pref flip or a played-set change - broken.
 * [if] _refreshChartedOrder stops consulting the key before simulating [then]
 *   the memo exists but the poll ignores it - broken.
 * [if] simulateAutoPlayChain goes back to playlist.find per step [then] the
 *   one-off simulation is O(rows^2) again - broken.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const CONTROLLER_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
	'utf8'
);
const CHAIN_SOURCE = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/auto-play-chain.ts', import.meta.url)),
	'utf8'
);

const BASE = Object.freeze({
	feed_epoch: 3,
	source_stable_id: 'sid-a',
	enforce_play_order: false,
	maximize_reach: true,
	min_tempo_ratio: 0.84,
	max_tempo_ratio: 1.16,
	exclude_ids: new Set(['sid-x']),
	played_ids: new Set(['sid-p', 'sid-q'])
});

describe('chartedOrderKey', () => {
	let mod;
	before(async () => {
		mod = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	});

	it('identical inputs yield an identical key, so a steady poll is a no-op', () => {
		const a = mod.chartedOrderKey(BASE);
		const b = mod.chartedOrderKey({ ...BASE, exclude_ids: new Set(['sid-x']), played_ids: new Set(['sid-q', 'sid-p']) });
		assert.equal(a, b, 'if two polls with identical inputs produce different keys then every tick simulates - broken');
	});

	it('every input the simulation reads changes the key', () => {
		const base = mod.chartedOrderKey(BASE);
		const variants = {
			feed_epoch: { ...BASE, feed_epoch: 4 },
			source_stable_id: { ...BASE, source_stable_id: 'sid-b' },
			enforce_play_order: { ...BASE, enforce_play_order: true },
			maximize_reach: { ...BASE, maximize_reach: false },
			min_tempo_ratio: { ...BASE, min_tempo_ratio: 0.9 },
			max_tempo_ratio: { ...BASE, max_tempo_ratio: 1.1 },
			exclude_ids: { ...BASE, exclude_ids: new Set(['sid-x', 'sid-y']) },
			played_ids: { ...BASE, played_ids: new Set(['sid-p']) }
		};
		for (const [name, input] of Object.entries(variants)) {
			assert.notEqual(mod.chartedOrderKey(input), base, `if ${name} changes and the key does not then the order column goes stale - broken`);
		}
	});
});

describe('the poll consults the memo (source guards; the controller is rune-bound)', () => {
	it('_refreshChartedOrder computes the key and returns before simulating when it is unchanged', () => {
		const start = CONTROLLER_SOURCE.indexOf('function _refreshChartedOrder(');
		const end = CONTROLLER_SOURCE.indexOf('\n}\n', start);
		const body = CONTROLLER_SOURCE.slice(start, end);
		const keyAt = body.indexOf('chartedOrderKey(');
		const guardAt = body.indexOf('if (key === _chartedOrderKey) return;');
		const simAt = body.indexOf('simulateAutoPlayChain(');
		assert.ok(keyAt > 0 && guardAt > keyAt && simAt > guardAt,
			'if _refreshChartedOrder simulates before checking the memo key then the poll ignores the memo - broken');
	});

	it('simulateAutoPlayChain indexes the playlist once instead of a linear find per step', () => {
		const start = CHAIN_SOURCE.indexOf('export function simulateAutoPlayChain(');
		const body = CHAIN_SOURCE.slice(start, CHAIN_SOURCE.indexOf('\n}\n', start));
		assert.ok(body.includes('new Map(input.playlist.map('), 'if the chain builds no index then the simulation is O(rows^2) - broken');
		assert.ok(!body.includes('input.playlist.find('), 'if the chain still does playlist.find per step then the index is unused - broken');
	});
});

describe('simulateAutoPlayChain at library scale', () => {
	const keys = ['1A', '2A', '3A', '4A', '5A', '6A', '7A', '8A', '9A', '10A', '11A', '12A'];
	const playlist = Array.from({ length: 8558 }, (_, i) => ({
		stable_id: `sid-${i}`,
		key: keys[i % keys.length],
		bpm: 120 + (i % 20),
		file_exists: true,
		play_order: i
	}));

	it('honours max_chain_length (start row included)', async () => {
		const chain = await loadTypeScriptModule('src/lib/rb/auto-play-chain.ts');
		const out = chain.simulateAutoPlayChain({
			playlist,
			start_stable_id: 'sid-0',
			enforce_play_order: true,
			maximize_reach: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16,
			max_chain_length: 65
		});
		assert.equal(out.length, 65, 'if the cap is ignored then the column walk is the whole library again - broken');
		assert.equal(out[0], 'sid-0');
	});

	it('the charted horizon under maximize_reach costs well under one poll interval', async () => {
		const chain = await loadTypeScriptModule('src/lib/rb/auto-play-chain.ts');
		const t0 = performance.now();
		const out = chain.simulateAutoPlayChain({
			playlist,
			start_stable_id: 'sid-0',
			enforce_play_order: false,
			maximize_reach: true,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16,
			max_chain_length: 65
		});
		const ms = performance.now() - t0;
		assert.ok(out.length > 1, 'sanity: the chain advances');
		// 77 ms on the Air; a shared CI runner measured 273 ms once (Wed 2 Sep 2026).
		// The symptom this guards is the ~1.5 s long task in the header, and this is
		// one synchronous per-handoff call (source_stable_id is a memo-key input), so
		// the line must sit well under that: 750 ms is half the freeze and ~2.7x the
		// worst runner jitter seen. The old whole-library walk was 6900 ms.
		assert.ok(ms < 750, `if the charted simulation costs ${ms.toFixed(0)}ms then every handoff freezes the UI - broken`);
	});

	it('the controller passes the horizon, so the column never walks the whole library', () => {
		assert.ok(
			CONTROLLER_SOURCE.includes('max_chain_length: CHARTED_ORDER_HORIZON + 1'),
			'if _refreshChartedOrder simulates without max_chain_length then a 7 s freeze returns at every handoff - broken'
		);
	});
});
