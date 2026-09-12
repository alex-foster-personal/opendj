/**
 * DECKUX-17: Beat Sync MASTER election policy (issue #320).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/master-election.ts');
});

function deck(partial = {}) {
	const id = partial.id ?? 1;
	return {
		id,
		loaded: true,
		playing: true,
		beat_sync_enabled: false,
		fader: 1,
		trim: 0.5,
		assign: id % 2 === 1 ? 'A' : 'B',
		...partial
	};
}

function input(partial = {}) {
	return {
		crossfader: 0.5,
		master: 1,
		decks: [],
		...partial
	};
}

test('none playing returns null', () => {
	assert.equal(mod.electMaster(input()), null);
	assert.equal(mod.electMaster(input({ decks: [deck({ playing: false })] })), null);
});

test('one playing wins even with fader at zero', () => {
	assert.equal(
		mod.electMaster(input({ decks: [deck({ id: 2, fader: 0 })] })),
		2
	);
});

test('on-air deck beats a fader-down cue deck', () => {
	const winner = mod.electMaster(
		input({
			decks: [
				deck({ id: 1, fader: 0, assign: 'A' }),
				deck({ id: 2, fader: 1, assign: 'B' })
			]
		})
	);
	assert.equal(winner, 2);
});

test('Beat Sync follower beats an unsynced on-air deck', () => {
	const winner = mod.electMaster(
		input({
			decks: [
				deck({ id: 2, beat_sync_enabled: false }),
				deck({ id: 3, beat_sync_enabled: true })
			]
		})
	);
	assert.equal(winner, 3);
});

test('loudest synced on-air deck wins', () => {
	const winner = mod.electMaster(
		input({
			decks: [
				deck({ id: 2, beat_sync_enabled: true, fader: 0.4 }),
				deck({ id: 4, beat_sync_enabled: true, fader: 1 })
			]
		})
	);
	assert.equal(winner, 4);
});

test('equal gain synced on-air ties go to lowest deck id', () => {
	const winner = mod.electMaster(
		input({
			decks: [
				deck({ id: 4, beat_sync_enabled: true }),
				deck({ id: 2, beat_sync_enabled: true })
			]
		})
	);
	assert.equal(winner, 2);
});

test('all playing but none on-air falls back to lowest id', () => {
	const winner = mod.electMaster(
		input({
			decks: [
				deck({ id: 1, fader: 0 }),
				deck({ id: 2, fader: 0 }),
				deck({ id: 3, fader: 0 }),
				deck({ id: 4, fader: 0 })
			]
		})
	);
	assert.equal(winner, 1);
});

test('the crossfade law here still matches the one the engine applies', () => {
	const engineLaw = engineBlockAfter(
		'function _xfGainFor(assign: CrossfaderAssign, x: number): number {'
	);
	for (const expression of [
		"if (assign === 'THRU') return 1;",
		'Math.cos((x * Math.PI) / 2)',
		'Math.cos(((1 - x) * Math.PI) / 2)'
	]) {
		assert.ok(
			engineLaw.includes(expression),
			`the engine no longer computes "${expression}"; re-derive master-election`
		);
	}
});
