/**
 * requirement: DECKUX-9956
 *
 * Beat Jump pad mode (issue #9757): [if] a pad sends anything but `beat_jump` with the signed
 * count on it, the catalog still marks beat-jump unbuilt, or the mode is shared across decks
 * [then] fail, [else stop].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const read = (path) =>
	readFileSync(fileURLToPath(new URL(`../../src/lib/${path}`, import.meta.url)), 'utf8');
const deckSvelte = read('components/rb/Deck.svelte');
const padsSvelte = read('components/rb/deck/BeatJumpPads.svelte');

let vite;
let pads;
let catalog;
let state;
let ipc;

test.before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	pads = await vite.ssrLoadModule('/src/lib/rb/beat-jump-pads.ts');
	catalog = await vite.ssrLoadModule('/src/lib/rb/pad-mode-catalog.ts');
	state = await vite.ssrLoadModule('/src/lib/rb/pad-mode-state.svelte.ts');
	ipc = await vite.ssrLoadModule('/src/lib/rb/performance-ipc.svelte.ts');
});

test.after(async () => {
	await vite.close();
});

test('A-D jump back 1, 2, 4, 8 and E-H jump forward 1, 2, 4, 8', () => {
	assert.deepEqual(
		pads.BEAT_JUMP_PADS.map((pad) => [pad.slot, pad.beats]),
		[
			['A', -1],
			['B', -2],
			['C', -4],
			['D', -8],
			['E', 1],
			['F', 2],
			['G', 4],
			['H', 8]
		]
	);
	assert.deepEqual(
		pads.BEAT_JUMP_PADS.map((pad) => pads.beatJumpPadLabel(pad.beats)),
		['-1', '-2', '-4', '-8', '+1', '+2', '+4', '+8']
	);
});

test('each pad sends beat_jump with the signed beat count shown on it', () => {
	for (const pad of pads.BEAT_JUMP_PADS) {
		const command = pads.beatJumpPadCommand(2, pad.beats);
		assert.deepEqual(command, { type: 'beat_jump', deck: 2, beats: pad.beats });
		assert.equal(pads.beatJumpPadLabel(command.beats), pads.beatJumpPadLabel(pad.beats));
	}
	assert.throws(() => pads.beatJumpPadCommand(1, 0), /non-zero integer/);
});

test('the component wires each pad to onJump(pad.beats) and labels it with the signed count', () => {
	assert.match(padsSvelte, /onclick=\{\(\) => void onJump\(pad\.beats\)\}/);
	assert.match(padsSvelte, /\{beatJumpPadLabel\(pad\.beats\)\}/);
	assert.match(deckSvelte, /<BeatJumpPads \{deck\} \{pending\} onJump=\{beatJump\} \/>/);
	assert.match(
		deckSvelte,
		/runPerformanceCommandFromUi\(\{ type: 'beat_jump', deck: deckId, beats \}\)/
	);
});

test('agent parity: the typed dispatcher accepts every pad command as is', () => {
	for (const pad of pads.BEAT_JUMP_PADS) {
		const parsed = ipc.parsePerformanceCommandForTest(pads.beatJumpPadCommand(3, pad.beats));
		assert.deepEqual(parsed, { type: 'beat_jump', deck: 3, beats: pad.beats });
	}
});

test('the catalog marks beat-jump built', () => {
	const entry = catalog.PAD_MODE_CATALOG.find((candidate) => candidate.id === 'beat-jump');
	assert.equal(entry.built, true);
	assert.equal(catalog.padModeMenuLabel(entry), 'Beat Jump');
});

test('the pad mode is per deck and refuses unbuilt modes', () => {
	assert.equal(state.padModeFor(1), 'hot-cue');
	state.setPadMode(1, 'beat-jump');
	assert.equal(state.padModeFor(1), 'beat-jump');
	assert.equal(state.padModeFor(2), 'hot-cue');
	assert.throws(() => state.setPadMode(2, 'sampler'), /not built/);
	assert.throws(() => state.setPadMode(2, 'nope'), /unknown pad mode/);
	state.setPadMode(1, 'hot-cue');
	assert.equal(state.padModeFor(1), 'hot-cue');
});
