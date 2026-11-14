/**
 * Issue #10788 (S5, S11): the sampler clip pool (spec 5.3 residency), ported from the v1 engine's
 * hot pool. A hit is never fetched or decoded twice, the pool stays inside its byte budget by
 * dropping the least recently used unpinned clip, and unity stems never fetch a stem part.
 * The offline stem-mix render needs a real OfflineAudioContext and is not exercised here.
 *
 * Regression lines:
 * - if a slot is asked for twice then one fetch and one decode happen
 * - if a decode pushes the pool over budget then the least recently used unpinned clip goes
 * - if the oldest clip is pinned then the next oldest goes instead
 * - if the vector's stems are unity then no part clip is fetched
 * - if a slot was warmed then its buffer decodes from the warm bytes without a second fetch
 * - if a residency plan names more hot slots than fit then the first ones decode, in order, and the rest wait
 * - if the residency plan is empty then nothing is fetched or decoded
 * - if the budget is a function then a lower tier budget evicts on the next store
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let P;
before(async () => {
	P = await loadTypeScriptModule('src/lib/player/sampler/sampler-pool.ts');
});

// A fake decode context: every clip decodes to a stereo buffer of `frames` samples (8 B per frame).
const frames = 100;
const harness = (budget, pinned = new Set(), clipFrames = frames) => {
	const fetches = [];
	const decodes = [];
	const fetch = async (slotId, part) => {
		fetches.push(part === undefined ? slotId : `${slotId}?part=${part}`);
		return new ArrayBuffer(16);
	};
	const ctx = {
		sampleRate: 48000,
		decodeAudioData: async (bytes) => {
			decodes.push(bytes.byteLength);
			return { length: clipFrames, numberOfChannels: 2 };
		}
	};
	return { pool: new P.SamplerClipPool(fetch, () => pinned, budget), ctx, fetches, decodes };
};
const slot = (slot_id, vector = null) => ({
	slot_id,
	vector,
	stems_ready: true,
	stem_parts: null,
	in_ms: 10_000,
	out_ms: 12_000
});

test('if a slot is asked for twice then one fetch and one decode happen', async () => {
	/** [if] the same slot is asked for twice [then] the second is a pool hit with no fetch or decode, [else stop]. */
	const h = harness(P.HOT_POOL_BYTES);
	const a = await h.pool.buffer(slot('a'), h.ctx);
	assert.equal(await h.pool.buffer(slot('a'), h.ctx), a);
	assert.deepEqual([h.fetches, h.decodes.length], [['a'], 1]);
	assert.equal(h.pool.bytes(), P.bufferBytes(a));
});

test('if a decode pushes the pool over budget then the least recently used unpinned clip goes', async () => {
	/** [if] three 800 B clips meet a 2000 B budget after a re-touch of a [then] b is evicted, [else stop]. */
	const h = harness(2000);
	await h.pool.buffer(slot('a'), h.ctx);
	await h.pool.buffer(slot('b'), h.ctx);
	await h.pool.buffer(slot('a'), h.ctx);
	await h.pool.buffer(slot('c'), h.ctx);
	assert.deepEqual(h.pool.keys(), ['a@48000', 'c@48000']);
	assert.ok(h.pool.bytes() <= 2000);
});

test('if the oldest clip is pinned then the next oldest goes instead', async () => {
	/** [if] a is pinned and the pool overflows [then] b goes and a stays, [else stop]. */
	const h = harness(2000, new Set(['a']));
	for (const id of ['a', 'b', 'c']) await h.pool.buffer(slot(id), h.ctx);
	assert.deepEqual(h.pool.keys(), ['a@48000', 'c@48000']);
});

test('if the vector stems are unity then no part clip is fetched', async () => {
	/** [if] the slot's vector stems are unity on a stems_ready track [then] only the mix clip is fetched, [else stop]. */
	const h = harness(P.HOT_POOL_BYTES);
	const unity = { gain_db: 0, mute: false, solo: false };
	const stems = { vocals: unity, drums: unity, bass: unity, inst: unity };
	await h.pool.buffer(slot('a', { stems }), h.ctx);
	assert.deepEqual(h.fetches, ['a']);
});

test('if a slot was warmed then its buffer decodes from the warm bytes without a second fetch', async () => {
	/** [if] warm() prefetched a slot [then] buffer() decodes its bytes and fetches nothing more, [else stop]. */
	const h = harness(P.HOT_POOL_BYTES);
	h.pool.warm([slot('a')], 48000, (e) => assert.fail(String(e)));
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(h.pool.bytes(), 16);
	await h.pool.buffer(slot('a'), h.ctx);
	assert.deepEqual([h.fetches, h.decodes], [['a'], [16]]);
	assert.equal(h.pool.bytes(), frames * 2 * 4);
});

test('if a pool key is read then its slot id is the part before the rate', () => {
	/** [if] a mix key or a stem-mix key is parsed [then] slotIdOfPoolKey returns the slot id, [else stop]. */
	assert.equal(P.slotIdOfPoolKey('rb:7:A@48000'), 'rb:7:A');
	assert.equal(P.slotIdOfPoolKey('rb:7:A@48000#stems-0a1b2c3d'), 'rb:7:A');
});

test('a residency plan promotes in order and stops at the budget', async () => {
	/** [if] four hot slots meet a budget of 2.5 clips [then] a and b decode in order, then it stops, [else stop]. */
	const each = P.hotBytesEstimate(slot('a'), 48000);
	assert.equal(each, Math.ceil(2.04 * 48000) * 8);
	const h = harness(2.5 * each);
	const plan = ['a', 'b', 'c', 'd'].map((id) => ({ slot: slot(id), tier: 'hot' }));
	await h.pool.promote(plan, h.ctx, (e) => assert.fail(String(e)));
	assert.deepEqual(h.fetches, ['a', 'b']);
	assert.deepEqual(h.pool.keys(), ['a@48000', 'b@48000']);
});

test('an empty residency plan does no work', async () => {
	/** [if] the plan is empty (closed panel, no intent) [then] nothing is fetched or decoded, [else stop]. */
	const h = harness(P.HOT_POOL_BYTES);
	await h.pool.promote([], h.ctx, (e) => assert.fail(String(e)));
	assert.deepEqual([h.fetches, h.decodes], [[], []]);
});

test('a resident slot named again is touched, so off-screen slots age out first', async () => {
	/** [if] a and b are hot and the plan names a again before c [then] b is the one evicted, [else stop]. */
	const each = P.hotBytesEstimate(slot('a'), 48000);
	const h = harness(2.5 * each, new Set(), each / 8);
	await h.pool.buffer(slot('a'), h.ctx);
	await h.pool.buffer(slot('b'), h.ctx);
	await h.pool.promote(
		[
			{ slot: slot('a'), tier: 'hot' },
			{ slot: slot('c'), tier: 'hot' }
		],
		h.ctx,
		(e) => assert.fail(String(e))
	);
	assert.deepEqual(h.pool.keys(), ['a@48000', 'c@48000']);
});

test('a budget function is read at each eviction', async () => {
	/** [if] the tier budget drops from 2000 B to 1000 B [then] the next store evicts down to it, pins kept, [else stop]. */
	let budget = 2000;
	const fetch = async () => new ArrayBuffer(16);
	const ctx = {
		sampleRate: 48000,
		decodeAudioData: async () => ({ length: frames, numberOfChannels: 2 })
	};
	const pool = new P.SamplerClipPool(
		fetch,
		() => new Set(['a']),
		() => budget
	);
	await pool.buffer(slot('a'), ctx);
	await pool.buffer(slot('b'), ctx);
	budget = 1000;
	await pool.buffer(slot('c'), ctx);
	assert.deepEqual(pool.keys(), ['a@48000']);
});
