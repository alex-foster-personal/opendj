/**
 * KEYSYNC-01: exact and relative KEY SYNC (mimics Serato Pitch 'n Time Key
 * Sync, which transposes a deck onto the other deck's key; the Pioneer
 * "compatible" behaviour stays available as mode `compatible`).
 *
 * The semitone difference is the shortest signed path, -6..+6. At the
 * tritone tie it is +6, the same rule as Mixxx's KeyUtils shortest steps.
 *
 * Regression lines:
 * - if any same-mode Camelot pair does not land on the leader's exact key then exact KEY SYNC plays in the wrong key
 * - if any shift leaves -6..+6 or is not the shortest path then KEY SYNC transposes further than it needs to
 * - if the tritone tie is not +6 then the same pair can sync up one night and down the next
 * - if exact mode quietly transposes a minor deck onto a major leader then "exact" lies
 * - if relative mode does not land on the leader's Camelot number then the relative fallback is wrong
 * - if an exact-mode mode mismatch reads 'following' then the light comes on over a key it cannot match
 * - if a guessed key ('8A?' via key_guess) cannot be followed then a guessed-key deck can never key sync
 * - if a target deck is ignored then KEY SYNC can only follow MASTER
 * - if exact KEY SYNC ignores Master Tempo-off tempo pitch on the target then an 8A target at +6% follows as catalog 8A instead of audible 3A
 * - if the ladder uses catalog only for the target at +6% MT off then `key_options` rows disagree with exact sync
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const _frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/player/key/camelot.ts');
});

const KEYS = [];
for (let n = 1; n <= 12; n += 1) KEYS.push(`${n}A`, `${n}B`);

function root(key) {
	return mod.parseCamelotKey(key).root;
}

function pc(value) {
	return ((value % 12) + 12) % 12;
}

test('exact: every same-mode pair lands on the leader key by the shortest signed path', () => {
	let checked = 0;
	for (const follower of KEYS) {
		for (const leader of KEYS) {
			if (follower.at(-1) !== leader.at(-1)) continue;
			const shift = mod.keySyncSemitoneDelta(follower, leader, 'exact');
			assert.ok(Number.isInteger(shift) && shift >= -6 && shift <= 6, `${follower}->${leader}: ${shift} outside -6..6`);
			assert.equal(mod.effectiveCamelotKey(follower, shift), leader, `${follower}+${shift} must be ${leader}`);
			const distance = pc(root(leader) - root(follower));
			assert.equal(Math.abs(shift), Math.min(distance, 12 - distance), `${follower}->${leader} not shortest`);
			checked += 1;
		}
	}
	assert.equal(checked, 288, 'all 24 keys x 12 same-mode leaders');
});

test('exact: the tritone tie is +6, and the same key is 0', () => {
	assert.equal(mod.keySyncSemitoneDelta('8A', '8A', 'exact'), 0);
	// 8A (A minor) and 2A (Eb minor) are a tritone apart.
	assert.equal(mod.keySyncSemitoneDelta('8A', '2A', 'exact'), 6);
	assert.equal(mod.keySyncSemitoneDelta('2A', '8A', 'exact'), 6);
	assert.equal(mod.keySyncSemitoneDelta('8B', '2B', 'exact'), 6);
	assert.equal(mod.keySyncSemitoneDelta('8A', '3A', 'exact'), 1, 'A minor -> Bb minor is up one');
	assert.equal(mod.keySyncSemitoneDelta('8A', '1A', 'exact'), -1, 'A minor -> Ab minor is down one');
});

test('exact: a mode mismatch refuses instead of guessing', () => {
	for (const follower of KEYS) {
		for (const leader of KEYS) {
			if (follower.at(-1) === leader.at(-1)) continue;
			assert.throws(() => mod.keySyncSemitoneDelta(follower, leader, 'exact'), /relative/i, `${follower}->${leader}`);
		}
	}
});

test('relative: all 24x24 pairs land on the leader Camelot number, follower mode kept, shortest path', () => {
	let checked = 0;
	for (const follower of KEYS) {
		for (const leader of KEYS) {
			const shift = mod.keySyncSemitoneDelta(follower, leader, 'relative');
			assert.ok(Number.isInteger(shift) && shift >= -6 && shift <= 6, `${follower}->${leader}: ${shift}`);
			const landed = mod.effectiveCamelotKey(follower, shift);
			assert.equal(landed, `${leader.slice(0, -1)}${follower.at(-1)}`, `${follower}->${leader} landed ${landed}`);
			if (follower.at(-1) === leader.at(-1)) {
				assert.equal(shift, mod.keySyncSemitoneDelta(follower, leader, 'exact'), 'same mode: relative equals exact');
			}
			checked += 1;
		}
	}
	assert.equal(checked, 576);
	assert.equal(mod.keySyncSemitoneDelta('8A', '11B', 'relative'), -3, 'A minor onto A major -> F# minor (11A)');
});

test('an unparseable key or an unknown mode fails loudly', () => {
	assert.throws(() => mod.keySyncSemitoneDelta(null, '8A', 'exact'), /key/i);
	assert.throws(() => mod.keySyncSemitoneDelta('8A', 'nope', 'exact'), /key/i);
	assert.throws(() => mod.keySyncSemitoneDelta('8A', '8A', 'compatible'), /mode/i);
	assert.deepEqual([...mod.KEY_SYNC_MODES], ['exact', 'relative', 'compatible']);
});

test('the engine target derivation uses the mode and accounts for the leader audible shift', () => {
	// Follower 8A at shift 0, leader 3A already nudged +2 (audibly 5A).
	assert.equal(mod.deriveKeySyncTargetManualShift('8A', '3A', 0, 2, 0, 'exact'), 3);
	// A follower already at +5 retargets absolutely, never stacking past -6..+6.
	assert.equal(mod.deriveKeySyncTargetManualShift('8A', '3A', 5, 0, 5, 'exact'), 1);
	assert.equal(mod.deriveKeySyncTargetManualShift('8A', '11B', 0, 0, 0, 'relative'), -3);
	// compatible keeps the Pioneer family-root behaviour unchanged.
	assert.equal(
		mod.deriveKeySyncTargetManualShift('8A', '3A', 0, 0, 0, 'compatible'),
		mod.deriveKeySyncTargetManualShift('8A', '3A', 0, 0, 0, 'compatible')
	);
	assert.throws(() => mod.deriveKeySyncTargetManualShift('8A', '3B', 0, 0, 0, 'exact'), /relative/i);
});

test('KEYSYNC-01 target tempo pitch: 8A at +6% MT off is audible 3A; MT on stays 8A', () => {
	const r = 1.06;
	assert.equal(Math.round(mod.composeStretchSemitones(r, false, 0)), 1);
	assert.equal(mod.keySyncAudibleCamelotKey('8A', r, false, 0), '3A');
	assert.equal(mod.keySyncAudibleCamelotKey('8A', r, true, 0), '8A');
	assert.equal(mod.deriveKeySyncTargetManualShift('8A', '8A', 0, 1, 0, 'exact'), 1);
	assert.equal(mod.deriveKeySyncTargetManualShift('8A', '8A', 0, 0, 0, 'exact'), 0);
	const ladderMtOff = mod.keyShiftLadder('8A', mod.keySyncAudibleCamelotKey('8A', r, false, 0));
	const byShiftOff = Object.fromEntries(ladderMtOff.map((row) => [row.shift, row]));
	assert.deepEqual(byShiftOff[1], { shift: 1, key: '3A', pattern: 'exact', desirable: true });
	const ladderMtOn = mod.keyShiftLadder('8A', mod.keySyncAudibleCamelotKey('8A', r, true, 0));
	const byShiftOn = Object.fromEntries(ladderMtOn.map((row) => [row.shift, row]));
	assert.deepEqual(byShiftOn[0], { shift: 0, key: '8A', pattern: 'exact', desirable: true });
	assert.deepEqual(byShiftOn[1], { shift: 1, key: '3A', pattern: '+7 dominant', desirable: true });
});

test('KEYSYNC-01 wiring: ladder and DeckHeader track tempo + key lock for audible target', () => {
	const engineSrc = readFileSync(join(_frontendRoot, 'src/lib/rb/audio-engine.svelte.ts'), 'utf8');
	assert.match(engineSrc, /keySyncAudibleCamelotKey/);
	assert.match(engineSrc, /leader\.pitch/);
	assert.match(engineSrc, /leader\.master_tempo_enabled/);
	const headerSrc = readFileSync(join(_frontendRoot, 'src/lib/components/rb/deck/DeckHeader.svelte'), 'utf8');
	assert.match(headerSrc, /void target\.pitch/);
	assert.match(headerSrc, /void target\.master_tempo_enabled/);
});

function decks(overrides) {
	const base = (id) => ({
		stable_id: `sid-${id}`,
		key: '8A',
		key_guess: null,
		key_sync_enabled: false,
		key_sync_mode: 'exact',
		key_sync_target: null,
		is_master: false
	});
	return Object.fromEntries([1, 2, 3, 4].map((id) => [id, { ...base(id), ...(overrides[id] ?? {}) }]));
}

test('status: exact mode over a mode mismatch waits, relative follows', () => {
	const armed = { key_sync_enabled: true, key: '8A' };
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true, key: '3B' }, 2: armed })),
		'waiting-mode-mismatch'
	);
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true, key: '3B' }, 2: { ...armed, key_sync_mode: 'relative' } })),
		'following'
	);
	assert.match(mod.keySyncStatusTitle('waiting-mode-mismatch'), /relative/i);
});

test('status: a target deck is followed instead of MASTER, and a guessed key counts', () => {
	const armed = { key_sync_enabled: true, key_sync_target: 3 };
	assert.equal(mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true }, 2: armed })), 'following');
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 2: { ...armed, is_master: true } })),
		'following',
		'a deck that is MASTER can still follow an explicit target'
	);
	assert.equal(mod.deriveKeySyncStatus(2, decks({ 2: armed, 3: { stable_id: null } })), 'waiting-no-master');
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true }, 2: { key_sync_enabled: true, key: null, key_guess: '8A' } })),
		'following'
	);
	assert.equal(mod.keySyncKey({ key: null, key_guess: '8A' }), '8A');
	assert.equal(mod.keySyncKeyIsGuess({ key: null, key_guess: '8A' }), true);
	assert.equal(mod.keySyncKeyIsGuess({ key: '8A', key_guess: null }), false);
});

// ------------------------------------------------------------ KEYSYNC ladder (the maintainer, Wed 4 Nov)
// - if the ladder for 8A onto 3A does not list -6..+6 with the expected landed keys and Mixed In Key patterns then the chevron menu misleads mid-set
// - if a desirable flag disagrees with the Camelot pattern table then a thumbs-up marks a clashing key

test('ladder: follower 8A onto target 3A lists every shift, the key it lands on and its Mixed In Key pattern', () => {
	const ladder = mod.keyShiftLadder('8A', '3A');
	assert.deepEqual(ladder.map((row) => row.shift), [6, 5, 4, 3, 2, 1, 0, -1, -2, -3, -4, -5, -6], 'up shifts above, down below');
	const byShift = Object.fromEntries(ladder.map((row) => [row.shift, row]));
	assert.deepEqual(byShift[1], { shift: 1, key: '3A', pattern: 'exact', desirable: true });
	assert.deepEqual(byShift[0], { shift: 0, key: '8A', pattern: '+5 subdominant', desirable: true });
	assert.deepEqual(byShift[-6], { shift: -6, key: '2A', pattern: '-1', desirable: true }, '+6 and -6 land on the same key');
	assert.deepEqual(byShift[-1], { shift: -1, key: '1A', pattern: null, desirable: false });
	const patterns = Object.fromEntries(ladder.filter((row) => row.desirable).map((row) => [row.key, row.pattern]));
	assert.deepEqual(patterns, {
		'3A': 'exact',
		'4A': '+1 energy boost',
		'2A': '-1',
		'5A': '+2 energy',
		'10A': '+7 dominant',
		'8A': '+5 subdominant'
	}, 'shift 0 lands 8A = 3A +5, so it is desirable too');
});

test('ladder: across modes it offers relative and the mood shift', () => {
	const ladder = mod.keyShiftLadder('8A', '3B');
	const desirable = ladder.filter((row) => row.desirable).map((row) => `${row.key} ${row.pattern}`).sort();
	assert.deepEqual(desirable, ['12A mood shift A<->B', '3A relative'].sort());
});

test('ladder: every desirable flag matches the pattern table for all 24x24 pairs', () => {
	for (const follower of KEYS) {
		for (const target of KEYS) {
			const t = mod.parseCamelotKey(target);
			for (const row of mod.keyShiftLadder(follower, target)) {
				const landed = mod.parseCamelotKey(row.key);
				const match = mod.KEY_LADDER_PATTERNS.find(
					(p) =>
						(landed.mode === t.mode) === (p.to === 'same') &&
						pc(landed.number - t.number) === pc(p.to === 'other' && t.mode === 'B' ? -p.step : p.step)
				);
				assert.equal(row.pattern, match?.label ?? null, `${follower}->${target} shift ${row.shift}`);
				assert.equal(row.desirable, match !== undefined);
				assert.equal(row.key, mod.effectiveCamelotKey(follower, row.shift));
			}
		}
	}
});
