/**
 * DECKUX-10470: per-deck beat jump size (1/2 to 64 beats) and its typed command.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Each deck has a size, default 4, mirrored as beat_jump_size_beats.
 *     [if] a fresh deck reports anything but 4 [then] broken
 *   ✔︎ ✅ 🎯 beat_jump_size {deck, beats} sets one deck only and refuses illegal sizes.
 *     [if] 3, '4', 128, a missing beats, an extra key or deck 9 is accepted [then ⛔️]
 *   ✔︎ ✅ 🎯 < > step through the eight sizes and stop at the ends.
 *   ✔︎ ✅ 🎯 A restore does not echo to persistence; a set does.
 *   ✔︎ ✅ 🎯 BeatJump renders size steppers and two jump buttons.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;
let sizes;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	sizes = await loadTypeScriptModule('src/lib/rb/beat-jump-size.svelte.ts');
});

test('an agent sets and reads back one deck beat jump size through the dispatcher', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const handle = globalThis.window.musicDjToolsPerformance;
		for (const deck of [1, 2, 3, 4]) {
			assert.equal(
				handle.query().decks[deck].beat_jump_size_beats,
				4,
				'if the default is not 4 then broken'
			);
		}
		assert.equal(
			ipc.performanceCommandQueueScopes({ type: 'beat_jump_size', deck: 2, beats: 8 }),
			null
		);
		const after = await handle.dispatch({ type: 'beat_jump_size', deck: 2, beats: 0.5 });
		assert.equal(
			after.decks[2].beat_jump_size_beats,
			0.5,
			'if the snapshot does not mirror the size then broken'
		);
		assert.equal(after.decks[1].beat_jump_size_beats, 4, 'if a sibling deck moves then broken');
		for (const bad of [
			{ type: 'beat_jump_size', deck: 2, beats: 3 },
			{ type: 'beat_jump_size', deck: 2, beats: 128 },
			{ type: 'beat_jump_size', deck: 2, beats: '4' },
			{ type: 'beat_jump_size', deck: 2 },
			{ type: 'beat_jump_size', deck: 2, beats: 4, extra: 1 },
			{ type: 'beat_jump_size', deck: 9, beats: 4 }
		]) {
			await assert.rejects(
				() => handle.dispatch(bad),
				`if ${JSON.stringify(bad)} is accepted then broken`
			);
		}
		assert.equal(handle.query().decks[2].beat_jump_size_beats, 0.5);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('the size stepper walks all eight sizes and stops at 1/2 and 64', () => {
	let size = 0.5;
	const seen = [size];
	for (let i = 0; i < 10; i++) {
		size = sizes.steppedBeatJumpSize(size, 1);
		if (size !== seen.at(-1)) seen.push(size);
	}
	assert.deepEqual(seen, [0.5, 1, 2, 4, 8, 16, 32, 64]);
	assert.equal(sizes.steppedBeatJumpSize(64, 1), 64);
	assert.equal(sizes.steppedBeatJumpSize(0.5, -1), 0.5);
	assert.equal(sizes.beatJumpSizeLabel(0.5), '1/2');
	assert.throws(() => sizes.assertBeatJumpSize(3), /one of 1\/2/);
});

test('a restore does not fire the persistence listener, a set does', () => {
	const calls = [];
	const off = sizes.onBeatJumpSizeChange((deck, size) => calls.push([deck, size]));
	try {
		sizes.restoreBeatJumpSizes({ deck_1: 16, deck_2: 4, deck_3: 0.5, deck_4: 64 });
		assert.equal(sizes.beatJumpSizes[3], 0.5);
		assert.deepEqual(calls, [], 'if a boot restore echoes back to disk then broken');
		sizes.setBeatJumpSize(1, 32);
		assert.deepEqual(calls, [[1, 32]], 'if a user change is not persisted then broken');
		assert.deepEqual(sizes.beatJumpSizesToPrefs(), {
			deck_1: 32,
			deck_2: 4,
			deck_3: 0.5,
			deck_4: 64
		});
		assert.throws(() =>
			sizes.beatJumpSizesFromPrefs({ deck_1: 3, deck_2: 4, deck_3: 4, deck_4: 4 })
		);
		assert.throws(() => sizes.beatJumpSizesFromPrefs({ deck_5: 4 }), /unknown key/);
	} finally {
		off();
	}
});

test('BeatJump renders size steppers and two jump buttons, not four fixed jumps', async () => {
	const entry = [
		"export { default as BeatJump } from '$lib/components/rb/deck/BeatJump.svelte';",
		"export { render } from 'svelte/server';"
	].join('\n');
	const ssr = await loadSvelteSsrModule(entry);
	const beats = Array.from({ length: 64 }, (_, i) => ({
		n: (i % 4) + 1,
		bpm: 120,
		t: 0.5 + i * 0.5
	}));
	const deck = {
		deck_id: 2,
		stable_id: 'trk-1',
		anlz: { beatgrid: { beats } },
		duration_ms: 33000,
		position_ms: 16000,
		loop: null
	};
	const body = ssr.render(ssr.BeatJump, {
		props: {
			deck,
			pending: false,
			onJump: async () => {},
			onSize: async () => {}
		}
	}).body;
	assert.match(body, /data-performance-control="beat-jump-size-down"/);
	assert.match(body, /data-performance-control="beat-jump-size-up"/);
	const jumps = body.match(/data-performance-control="beat-jump"/g);
	assert.equal(jumps?.length, 2, 'if there are not exactly two jump buttons then broken');
	assert.doesNotMatch(body, /data-beats="-8"/, 'if the fixed four jumps remain then broken');
});
