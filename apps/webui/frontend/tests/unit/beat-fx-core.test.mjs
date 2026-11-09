// requirement: BEATFX-9949
// [if] BEAT_FX_TARGETS or BEAT_FX_BEATS drift from 1..4 + master and 1/16..16 with 3/4 [then] broken
// [if] parseBeatFxCommand accepts an unknown field, an out-of-range value or an empty command [then] broken
// [if] parseBeatFxReleaseCommand accepts a kind other than echo [then] broken
// [if] beatFxTempoSource reads the wrong deck for master, or returns a tempo outside 40..300 BPM [then] broken
// [if] nextQuantizeBoundarySec waits more than one beat, or returns a boundary with no grid [then] broken
// [if] xpadToBeats does not walk the division list in order across 0..1 [then] broken
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let fx;

before(async () => {
	fx = await loadTypeScriptModule('src/lib/rb/beat-fx.ts');
});

describe('beat fx constants', () => {
	test('targets and beat divisions are exported in order', () => {
		assert.deepEqual([...fx.BEAT_FX_TARGETS], ['1', '2', '3', '4', 'master']);
		assert.deepEqual([...fx.BEAT_FX_BEATS], [1 / 16, 1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 4, 8, 16]);
	});
});

describe('parseBeatFxCommand', () => {
	test('accepts every field, one field alone, and xpad null as the release', () => {
		const full = {
			type: 'beat_fx',
			target: '3',
			effect: 'echo',
			beats: 3 / 4,
			level: 0.4,
			enabled: true,
			quantize: false,
			xpad: 0.25
		};
		assert.deepEqual(fx.parseBeatFxCommand(full), full);
		assert.deepEqual(fx.parseBeatFxCommand({ type: 'beat_fx', enabled: false }), {
			type: 'beat_fx',
			enabled: false
		});
		assert.deepEqual(fx.parseBeatFxCommand({ type: 'beat_fx', xpad: null }), {
			type: 'beat_fx',
			xpad: null
		});
		assert.deepEqual(fx.parseBeatFxCommand({ type: 'beat_fx', target: 'master' }), {
			type: 'beat_fx',
			target: 'master'
		});
	});

	test('refuses unknown fields, out-of-range values and an empty command by name', () => {
		const cases = [
			[{ type: 'beat_fx', enabled: true, deck: 1 }, /unknown field deck/],
			[{ type: 'beat_fx' }, /at least one/],
			[{ type: 'filter', enabled: true }, /type/],
			[{ type: 'beat_fx', target: '5' }, /target/],
			[{ type: 'beat_fx', target: 1 }, /target/],
			[{ type: 'beat_fx', effect: 'flanger' }, /effect/],
			[{ type: 'beat_fx', beats: 32 }, /beats/],
			[{ type: 'beat_fx', beats: 3 }, /beats/],
			[{ type: 'beat_fx', level: 1.01 }, /level/],
			[{ type: 'beat_fx', level: Number.NaN }, /level/],
			[{ type: 'beat_fx', enabled: 'yes' }, /enabled/],
			[{ type: 'beat_fx', quantize: 1 }, /quantize/],
			[{ type: 'beat_fx', xpad: -0.1 }, /xpad/],
			[{ type: 'beat_fx', xpad: 2 }, /xpad/]
		];
		for (const [record, reason] of cases) {
			assert.throws(() => fx.parseBeatFxCommand(record), reason, JSON.stringify(record));
		}
	});
});

describe('parseBeatFxReleaseCommand', () => {
	test('accepts held with optional beats and kind echo', () => {
		assert.deepEqual(fx.parseBeatFxReleaseCommand({ type: 'beat_fx_release', held: true }), {
			type: 'beat_fx_release',
			held: true
		});
		const full = { type: 'beat_fx_release', held: false, beats: 2, kind: 'echo' };
		assert.deepEqual(fx.parseBeatFxReleaseCommand(full), full);
	});

	test('refuses any other kind by name, a missing held and unknown fields', () => {
		assert.throws(
			() => fx.parseBeatFxReleaseCommand({ type: 'beat_fx_release', held: true, kind: 'spiral' }),
			/kind .*spiral/
		);
		assert.throws(() => fx.parseBeatFxReleaseCommand({ type: 'beat_fx_release' }), /held/);
		assert.throws(
			() => fx.parseBeatFxReleaseCommand({ type: 'beat_fx_release', held: true, level: 1 }),
			/unknown field level/
		);
		assert.throws(
			() => fx.parseBeatFxReleaseCommand({ type: 'beat_fx_release', held: true, beats: 0.3 }),
			/beats/
		);
	});
});

describe('beatFxTempoSource', () => {
	const decks = [
		{ deck: '1', effectiveBpm: 124, playing: true, isMaster: false },
		{ deck: '2', effectiveBpm: 128.5, playing: true, isMaster: true },
		{ deck: '3', effectiveBpm: 310, playing: false, isMaster: false },
		{ deck: '4', effectiveBpm: null, playing: false, isMaster: false }
	];

	test('a deck target reads that deck; master reads the master deck', () => {
		assert.equal(fx.beatFxTempoSource('1', decks), 124);
		assert.equal(fx.beatFxTempoSource('2', decks), 128.5);
		assert.equal(fx.beatFxTempoSource('master', decks), 128.5);
	});

	test('master with no master deck falls to the first playing deck in deck order', () => {
		const noMaster = [
			{ deck: '4', effectiveBpm: 100, playing: true, isMaster: false },
			{ deck: '2', effectiveBpm: 140, playing: true, isMaster: false },
			{ deck: '1', effectiveBpm: 90, playing: false, isMaster: false }
		];
		assert.equal(fx.beatFxTempoSource('master', noMaster), 140);
		assert.equal(
			fx.beatFxTempoSource(
				'master',
				noMaster.map((d) => ({ ...d, playing: false }))
			),
			null
		);
	});

	test('null outside 40..300 BPM or with no tempo', () => {
		assert.equal(fx.beatFxTempoSource('3', decks), null);
		assert.equal(fx.beatFxTempoSource('4', decks), null);
		assert.equal(
			fx.beatFxTempoSource('1', [{ deck: '1', effectiveBpm: 39.9, playing: true, isMaster: true }]),
			null
		);
		assert.equal(
			fx.beatFxTempoSource('1', [{ deck: '1', effectiveBpm: 40, playing: true, isMaster: true }]),
			40
		);
		assert.equal(
			fx.beatFxTempoSource('1', [{ deck: '1', effectiveBpm: 300, playing: true, isMaster: true }]),
			300
		);
		assert.throws(() => fx.beatFxTempoSource('5', decks), /target/);
	});
});

describe('nextQuantizeBoundarySec', () => {
	// 120 BPM, 0.5 s per beat from 1.0 s.
	const grid = Array.from({ length: 64 }, (_unused, i) => 1 + i * 0.5);

	test('a division of a beat or more waits for the next beat, never more than one beat', () => {
		for (const beats of [1, 2, 4, 8, 16]) {
			assert.equal(fx.nextQuantizeBoundarySec(grid, 3.1, beats), 3.5);
			assert.equal(fx.nextQuantizeBoundarySec(grid, 3.0, beats), 3.0, 'on a beat fires at once');
		}
		for (let now = 1; now < 32; now += 0.0731) {
			const at = fx.nextQuantizeBoundarySec(grid, now, 16);
			assert.ok(at >= now && at - now <= 0.5 + 1e-12, `now ${now} at ${at}`);
		}
	});

	test('a division under a beat waits for the next sub-beat boundary', () => {
		assert.equal(fx.nextQuantizeBoundarySec(grid, 3.1, 1 / 4), 3.125);
		assert.equal(fx.nextQuantizeBoundarySec(grid, 3.1, 1 / 2), 3.25);
		assert.equal(fx.nextQuantizeBoundarySec(grid, 3.26, 1 / 2), 3.5);
		assert.ok(Math.abs(fx.nextQuantizeBoundarySec(grid, 3.01, 1 / 16) - 3.03125) < 1e-12);
		assert.equal(fx.nextQuantizeBoundarySec(grid, 3.1, 3 / 4), 3.375);
	});

	test('null with no grid or off the grid', () => {
		assert.equal(fx.nextQuantizeBoundarySec([], 3, 1), null);
		assert.equal(fx.nextQuantizeBoundarySec([2], 3, 1), null);
		assert.equal(fx.nextQuantizeBoundarySec(grid, 0.5, 1), null);
		assert.equal(fx.nextQuantizeBoundarySec(grid, 100, 1), null);
		assert.throws(() => fx.nextQuantizeBoundarySec(grid, Number.NaN, 1), /nowSec/);
		assert.throws(() => fx.nextQuantizeBoundarySec(grid, 3, 3), /beats/);
	});
});

describe('xpadToBeats', () => {
	test('maps 0..1 across the division list in order', () => {
		const n = fx.BEAT_FX_BEATS.length;
		const walked = Array.from({ length: n }, (_unused, i) => fx.xpadToBeats((i + 0.5) / n));
		assert.deepEqual(walked, [...fx.BEAT_FX_BEATS]);
		assert.equal(fx.xpadToBeats(0), 1 / 16);
		assert.equal(fx.xpadToBeats(1), 16);
		let last = 0;
		for (let x = 0; x <= 1; x += 0.01) {
			const beats = fx.xpadToBeats(x);
			assert.ok(beats >= last, `monotonic at ${x}`);
			last = beats;
		}
		assert.throws(() => fx.xpadToBeats(1.2), /xpad/);
		assert.throws(() => fx.xpadToBeats(Number.NaN), /xpad/);
	});
});

test('no U+2014 or U+2013 characters in the new files', () => {
	const banned = new RegExp(`[${String.fromCharCode(0x2014)}${String.fromCharCode(0x2013)}]`);
	for (const rel of ['../../src/lib/rb/beat-fx.ts', './beat-fx-core.test.mjs']) {
		const text = readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
		assert.equal(banned.test(text), false, rel);
	}
});
