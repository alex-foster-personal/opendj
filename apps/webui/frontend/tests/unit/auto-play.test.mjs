import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
/** The chain/pick algebra lives in its own module; auto-play.ts re-exports only
 * the part with static importers, so reach internals are read from the owner. */
let chain;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	chain = await loadTypeScriptModule('src/lib/rb/auto-play-chain.ts');
});

function deck(partial) {
	return {
		id: 1,
		stable_id: null,
		playing: false,
		position_ms: 0,
		duration_ms: null,
		is_master: false,
		beat_sync_enabled: false,
		...partial
	};
}

function row(stable_id, key, bpm, file_exists = true) {
	return { stable_id, key, bpm, file_exists };
}

describe('auto-play remaining / trigger', () => {
	it('computes remaining from presentation position + duration', () => {
		const { remainingMs } = mod;
		assert.equal(remainingMs(90_000, 100_000), 10_000);
		assert.equal(remainingMs(100_000, 100_000), 0);
		assert.equal(remainingMs(0, null), null);
		assert.equal(remainingMs(-1, 100_000), null);
		assert.equal(remainingMs(10, 0), null);
	});

	it('triggers once inside threshold, not while in-flight or already armed', () => {
		const { shouldTriggerAutoPlay, AUTO_PLAY_THRESHOLD_MS } = mod;
		const base = {
			enabled: true,
			remaining_ms: AUTO_PLAY_THRESHOLD_MS,
			threshold_ms: AUTO_PLAY_THRESHOLD_MS,
			source_stable_id: 'a',
			already_triggered_for: null,
			in_flight: false
		};
		assert.equal(shouldTriggerAutoPlay(base), true);
		assert.equal(shouldTriggerAutoPlay({ ...base, remaining_ms: AUTO_PLAY_THRESHOLD_MS + 1 }), false);
		assert.equal(shouldTriggerAutoPlay({ ...base, enabled: false }), false);
		assert.equal(shouldTriggerAutoPlay({ ...base, in_flight: true }), false);
		assert.equal(shouldTriggerAutoPlay({ ...base, already_triggered_for: 'a' }), false);
		assert.equal(shouldTriggerAutoPlay({ ...base, source_stable_id: null }), false);
		assert.equal(shouldTriggerAutoPlay({ ...base, remaining_ms: null }), false);
	});

	it('scenario 19: the window clamps to duration/2 so a short track never arms at t=0', () => {
		// Rule from the maintainer, Tue 1 Sep 2026. Before the clamp, a track shorter
		// than the constant window was in-window the moment play started, so
		// AutoPlay loaded the next track over its own first beat.
		const { effectiveAutoPlayThresholdMs, AUTO_PLAY_THRESHOLD_MS } = mod;

		// Long track: constant window unchanged.
		assert.equal(effectiveAutoPlayThresholdMs(300_000), AUTO_PLAY_THRESHOLD_MS);
		// Exactly 2x the constant window: the two rules meet, still 16s.
		assert.equal(effectiveAutoPlayThresholdMs(32_000), AUTO_PLAY_THRESHOLD_MS);
		// Short track: half the track, not the constant.
		assert.equal(effectiveAutoPlayThresholdMs(12_000), 6_000);
		// The incident shape: a 15s sting used to arm at t=0.
		assert.equal(effectiveAutoPlayThresholdMs(15_000), 7_500);
		// Unknown/degenerate durations must never trigger (scenario row 3).
		assert.equal(effectiveAutoPlayThresholdMs(null), null);
		assert.equal(effectiveAutoPlayThresholdMs(0), null);
		assert.equal(effectiveAutoPlayThresholdMs(-5), null);
		assert.equal(effectiveAutoPlayThresholdMs(Number.NaN), null);
	});

	it('scenario 19: composed - a 12s track triggers after 6s played, not at start', () => {
		const { shouldTriggerAutoPlay, remainingMs, effectiveAutoPlayThresholdMs } = mod;
		const duration = 12_000;
		const windowMs = effectiveAutoPlayThresholdMs(duration);
		const at = (position_ms) => ({
			enabled: true,
			remaining_ms: remainingMs(position_ms, duration),
			threshold_ms: windowMs,
			source_stable_id: 'sting',
			already_triggered_for: null,
			in_flight: false
		});
		assert.equal(shouldTriggerAutoPlay(at(0)), false, 'play start must not arm');
		assert.equal(shouldTriggerAutoPlay(at(5_999)), false, 'first half must not arm');
		assert.equal(shouldTriggerAutoPlay(at(6_000)), true, 'half played - window opens');
		assert.equal(shouldTriggerAutoPlay(at(11_000)), true, 'still armed near the end');
	});

	it('duplicate stable_ids in the feed: playing one copy retires every copy', () => {
		// The production library holds 785 paths that resolve to more than one
		// row (found Tue 1 Sep 2026), so duplicate stable_ids in a published
		// feed are a real input, not a hypothetical. The invariant: exclusion
		// and played-history key on stable_id, so one played/excluded id must
		// retire EVERY row carrying it - a duplicate row must never let the
		// same track play twice.
		const { pickNextStableId } = mod;
		const playlist = [
			{ stable_id: 'dup', key: '8A', bpm: 124, file_exists: true },
			{ stable_id: 'dup', key: '8A', bpm: 124, file_exists: true },
			{ stable_id: 'other', key: '8A', bpm: 126, file_exists: true }
		];
		const base = {
			playlist,
			current_stable_id: 'dup',
			current_key: '8A',
			current_bpm: 124,
			exclude_ids: new Set(),
			played_ids: new Set(['dup']),
			enforce_play_order: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16
		};
		assert.equal(pickNextStableId(base), 'other', 'the second dup row must not be picked');
		assert.equal(
			pickNextStableId({ ...base, played_ids: new Set(['dup', 'other']) }),
			null,
			'with both ids retired nothing is pickable, dup rows included'
		);
		assert.equal(
			pickNextStableId({ ...base, enforce_play_order: true }),
			'other',
			'play-order mode skips the duplicate row the same way'
		);
	});
});

describe('auto-play deck pick', () => {
	it('only the playing master arms a handoff - never a non-master playing deck', () => {
		// Regression: two decks playing with no master pick used to fall back
		// to "lowest playing deck id", so BOTH could independently arm a
		// handoff. With two decks finishing ~1m apart that staggers new
		// loads 1m apart forever instead of following one master track.
		const { pickSourceDeck } = mod;
		const decks = [
			deck({ id: 1, stable_id: 'a', playing: true, is_master: false }),
			deck({ id: 2, stable_id: 'b', playing: true, is_master: true }),
			deck({ id: 3, stable_id: 'c', playing: false })
		];
		assert.equal(pickSourceDeck(decks)?.id, 2);
		// No master among the playing decks (or at all) - must not fall back
		// to picking any playing deck by id; that is the staggering bug.
		assert.equal(
			pickSourceDeck([
				deck({ id: 3, stable_id: 'c', playing: true }),
				deck({ id: 1, stable_id: 'a', playing: true })
			]),
			null
		);
		// A master that isn't currently playing (e.g. paused) is not a source.
		assert.equal(
			pickSourceDeck([
				deck({ id: 1, stable_id: 'a', playing: true, is_master: false }),
				deck({ id: 2, stable_id: 'b', playing: false, is_master: true })
			]),
			null
		);
		assert.equal(pickSourceDeck([deck({ id: 1, playing: false })]), null);
	});

	it('partner first, then empty before stopped; never a playing deck', () => {
		const { pickFollowerDeck } = mod;
		assert.equal(
			pickFollowerDeck(
				[
					deck({ id: 1, stable_id: 'src', playing: true }),
					deck({ id: 2, stable_id: 'x', playing: false }),
					deck({ id: 3, stable_id: null }),
					deck({ id: 4, stable_id: 'y', playing: true })
				],
				1
			),
			2
		);
		assert.equal(
			pickFollowerDeck(
				[
					deck({ id: 1, stable_id: 'src', playing: true }),
					deck({ id: 2, stable_id: 'x', playing: false }),
					deck({ id: 3, stable_id: 'y', playing: true }),
					deck({ id: 4, stable_id: 'z', playing: true })
				],
				1
			),
			2
		);
		assert.equal(
			pickFollowerDeck(
				[
					deck({ id: 1, stable_id: 'src', playing: true }),
					deck({ id: 2, stable_id: 'a', playing: true }),
					deck({ id: 3, stable_id: 'b', playing: true }),
					deck({ id: 4, stable_id: 'c', playing: true })
				],
				1
			),
			null
		);
	});
});

describe('[PLAY-22] auto-play prefers the partner deck (1<->2, 3<->4)', () => {
	const pick = (decks, source) => mod.pickFollowerDeck(decks.map((d) => deck(d)), source);
	it('source 1, deck 2 loaded and stopped, 3/4 empty: deck 2 (cleared by the handoff)', () => {
		assert.equal(
			pick([{ id: 1, stable_id: 'src', playing: true }, { id: 2, stable_id: 'x' }, { id: 3 }, { id: 4 }], 1),
			2
		);
	});
	it('source 1, deck 2 empty: deck 2', () => {
		assert.equal(pick([{ id: 1, stable_id: 'src', playing: true }, { id: 2 }, { id: 3 }, { id: 4 }], 1), 2);
	});
	it('source 2: deck 1; source 3: deck 4; source 4: deck 3', () => {
		assert.equal(pick([{ id: 1, stable_id: 'x' }, { id: 2, stable_id: 'src', playing: true }, { id: 3 }, { id: 4 }], 2), 1);
		assert.equal(pick([{ id: 1 }, { id: 2 }, { id: 3, stable_id: 'src', playing: true }, { id: 4, stable_id: 'y' }], 3), 4);
		assert.equal(pick([{ id: 1 }, { id: 2 }, { id: 3, stable_id: 'y' }, { id: 4, stable_id: 'src', playing: true }], 4), 3);
	});
	it('partner playing: the old rule, empty first, then stopped, never a playing deck', () => {
		assert.equal(
			pick([{ id: 1, stable_id: 'src', playing: true }, { id: 2, stable_id: 'p', playing: true }, { id: 3, stable_id: 'x' }, { id: 4 }], 1),
			4
		);
		assert.equal(
			pick([{ id: 1, stable_id: 'src', playing: true }, { id: 2, stable_id: 'p', playing: true }, { id: 3, stable_id: 'x' }, { id: 4, stable_id: 'z', playing: true }], 1),
			3
		);
	});
	it('a two-track AutoPlay set ping-pongs 1<->2 and never touches 3/4', () => {
		const decks = [{ id: 1, stable_id: 'a', playing: true }, { id: 2 }, { id: 3 }, { id: 4 }];
		let source = 1;
		for (let i = 0; i < 4; i++) {
			const follower = pick(decks, source);
			assert.ok(follower === 1 || follower === 2, `handoff ${i} reached deck ${follower}`);
			decks[follower - 1] = { id: follower, stable_id: `t${i}`, playing: true };
			decks[source - 1] = { id: source, stable_id: decks[source - 1].stable_id, playing: false };
			source = follower;
		}
	});
});

describe('auto-play track pick', () => {
	const playlist = [
		row('a', '8A', 120),
		row('b', '8A', 122),
		row('c', '3A', 120),
		row('d', '8B', 125),
		row('e', '8A', 160)
	];

	it('falls back to the earliest unplayed playable row when compatibility candidates are exhausted', () => {
		const { pickNextStableId } = mod;
		assert.equal(
			pickNextStableId({
				playlist: [row('source', '8A', 120), row('incompatible', '1A', 90), row('broken', '8A', 122, false)],
				current_stable_id: 'source',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'incompatible'
		);
	});

	it('enforce order: next membership row after current, skipping played/excluded', () => {
		const { pickNextStableId } = mod;
		assert.equal(
			pickNextStableId({
				playlist,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: true,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'b'
		);
		assert.equal(
			pickNextStableId({
				playlist,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(['b']),
				played_ids: new Set(['c']),
				enforce_play_order: true,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'd'
		);
		// Pin 0e5fa1 (playlist switch) deliberately supersedes the previous
		// "unknown current id => null" contract. The deck's playing track is
		// NOT a member of the playlist the user just switched to, and stopping
		// there is exactly the stall that left the old playlist's queue in
		// charge. Enforced order now resumes at the head of the new playlist.
		assert.equal(
			pickNextStableId({
				playlist,
				current_stable_id: 'missing',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: true,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'a',
			'when the source is absent after a playlist switch, ordered mode begins at the new feed start'
		);
		assert.equal(
			pickNextStableId({
				playlist: [row('first', '8A', 120), row('last', '8A', 122)],
				current_stable_id: 'last',
				current_key: '8A',
				current_bpm: 122,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: true,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			null,
			'ordered playback never wraps after the true final membership row'
		);
	});

	it('skips file_exists=false (missing / iCloud stubs) in both modes', () => {
		const { pickNextStableId } = mod;
		const withStub = [
			row('a', '8A', 120),
			row('b', '8A', 122, false),
			row('d', '8B', 125)
		];
		assert.equal(
			pickNextStableId({
				playlist: withStub,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: true,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'd'
		);
		assert.equal(
			pickNextStableId({
				playlist: withStub,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'd'
		);
	});

	it('smart: earliest unplayed key+-1 within phase-lock BPM ratio', () => {
		const { pickNextStableId } = mod;
		// b is first after a that matches key+BPM; c is wrong key; e is out of ratio
		assert.equal(
			pickNextStableId({
				playlist,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'b'
		);
		// skip played b; d is 8B (compatible) and 125 in range
		assert.equal(
			pickNextStableId({
				playlist,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(['b']),
				enforce_play_order: false,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'd'
		);
		// No compatible rows remain; keep playback moving with the first
		// unplayed row, as requested by pin 2e6a9258927c.
		assert.equal(
			pickNextStableId({
				playlist,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(['b', 'd']),
				enforce_play_order: false,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'c'
		);
	});

	it('pin 0a047b: smart mode continues from the loaded track\'s own position, never the list top', () => {
		// the maintainer, pin 0a047b8a4e4d: a double-clicked (instant-loaded) track must
		// have its successor computed from ITS position in the current view,
		// not from row 0 onward. Every row here is mutually compatible (same
		// key, same BPM), so the ONLY thing that can decide the pick is
		// position - if 'early' (unplayed, sits before 'mid') is ever picked
		// over 'late' (sits after 'mid'), AutoPlay just walked back to the top
		// of the list instead of continuing from the loaded track.
		const { pickNextStableId } = mod;
		const viewOrder = [row('early', '8A', 120), row('mid', '8A', 120), row('late', '8A', 120)];
		for (const maximize_reach of [false, true]) {
			assert.equal(
				pickNextStableId({
					playlist: viewOrder,
					current_stable_id: 'mid',
					current_key: '8A',
					current_bpm: 120,
					exclude_ids: new Set(),
					played_ids: new Set(),
					enforce_play_order: false,
					maximize_reach,
					min_tempo_ratio: 0.84,
					max_tempo_ratio: 1.16
				}),
				'late',
				`maximize_reach=${maximize_reach}: must continue forward from 'mid', not back to 'early'`
			);
		}
	});

	it('pin 0a047b: smart mode wraps to a row before the loaded track only when nothing compatible follows it', () => {
		// The forward-first rule must not turn into a dead end: if nothing
		// after the loaded track is compatible, AutoPlay still has to keep
		// playing rather than stall, so it falls back to a compatible row
		// before it.
		const { pickNextStableId } = mod;
		const viewOrder = [row('before', '8A', 120), row('mid', '8A', 120), row('after', '1A', 120)];
		assert.equal(
			pickNextStableId({
				playlist: viewOrder,
				current_stable_id: 'mid',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			}),
			'before'
		);
	});

	it('bpmWithinPhaseLockRange rejects half/double folds', () => {
		const { bpmWithinPhaseLockRange } = mod;
		assert.equal(bpmWithinPhaseLockRange(120, 120, 0.84, 1.16), true);
		assert.equal(bpmWithinPhaseLockRange(160, 120, 0.84, 1.16), false);
		assert.equal(bpmWithinPhaseLockRange(60, 120, 0.84, 1.16), false);
	});

	it('publishes browser feed getters; epoch follows playlist scope and membership identity', () => {
		const {
			setAutoPlayTrackFeed,
			getAutoPlayPlaylist,
			getAutoPlayPlaylistIds,
			getAutoPlayFeedEpoch
		} = mod;
		const before = getAutoPlayFeedEpoch();
		setAutoPlayTrackFeed('playlist-a', [
			{ stable_id: 'p1', key: '1A', bpm: 120, file_exists: true },
			{ stable_id: 'p2', key: '2A', bpm: 124, file_exists: false }
		]);
		const afterId = getAutoPlayFeedEpoch();
		assert.equal(afterId > before, true);
		setAutoPlayTrackFeed('playlist-a', [
			{ stable_id: 'p1', key: '9A', bpm: 128, file_exists: true },
			{ stable_id: 'p2', key: '2A', bpm: 124, file_exists: true }
		]);
		assert.equal(getAutoPlayFeedEpoch(), afterId);
		setAutoPlayTrackFeed('playlist-b', [
			{ stable_id: 'p1', key: '9A', bpm: 128, file_exists: true },
			{ stable_id: 'p2', key: '2A', bpm: 124, file_exists: true }
		]);
		assert.equal(
			getAutoPlayFeedEpoch() > afterId,
			true,
			'a different playlist with identical ordered members must advance the feed epoch'
		);
		assert.deepEqual(
			[...getAutoPlayPlaylist()],
			[
				{ stable_id: 'p1', key: '9A', bpm: 128, file_exists: true },
				{ stable_id: 'p2', key: '2A', bpm: 124, file_exists: true }
			]
		);
		assert.deepEqual([...getAutoPlayPlaylistIds()], ['p1', 'p2']);
	});
});

describe('auto-play fold-lock preference', () => {
	const wide = { min_tempo_ratio: 0.84, max_tempo_ratio: 1.16 };

	function pickInput(playlist, current, key, bpm, extra = {}) {
		return {
			playlist,
			current_stable_id: current,
			current_key: key,
			current_bpm: bpm,
			exclude_ids: new Set(),
			played_ids: new Set(),
			enforce_play_order: false,
			...wide,
			...extra
		};
	}

	function assertBoth(playlist, current, key, bpm, expected, bounds = wide) {
		const { pickNextStableId } = chain;
		assert.equal(
			pickNextStableId(pickInput(playlist, current, key, bpm, { maximize_reach: false, ...bounds })),
			expected
		);
		assert.equal(
			pickNextStableId(pickInput(playlist, current, key, bpm, { maximize_reach: true, ...bounds })),
			expected
		);
	}

	it('tempoLockClass table: exact, fold, and still-not-a-lock pairs', () => {
		const { tempoLockClass } = chain;
		assert.equal(tempoLockClass(120, 120, 0.84, 1.16), 'exact');
		assert.equal(tempoLockClass(64, 128, 0.84, 1.16), 'fold');
		assert.equal(tempoLockClass(256, 128, 0.84, 1.16), 'fold');
		assert.equal(tempoLockClass(160, 120, 0.84, 1.16), null);
		assert.equal(tempoLockClass(220, 128, 0.84, 1.16), null);
	});

	it('exact always wins even when a fold sits earlier in membership', () => {
		const playlist = [row('cur', '8A', 128), row('fold', '8A', 64), row('exact', '8A', 124)];
		assertBoth(playlist, 'cur', '8A', 128, 'exact');
	});

	it('fold when nothing exact: 128→64 and 128→256', () => {
		assertBoth([row('cur', '8A', 128), row('half', '8A', 64)], 'cur', '8A', 128, 'half');
		assertBoth([row('cur', '8A', 128), row('double', '8A', 256)], 'cur', '8A', 128, 'double');
	});

	it('still not a lock: 160 vs 120, 220 vs 128, 40 vs 128', () => {
		assertBoth([row('cur', '8A', 120), row('off', '8A', 160)], 'cur', '8A', 120, null);
		assertBoth([row('cur', '8A', 128), row('demo4', '8A', 220)], 'cur', '8A', 128, null);
		assertBoth([row('cur', '8A', 128), row('low', '8A', 40)], 'cur', '8A', 128, null);
	});

	it('key still gates folds', () => {
		assertBoth([row('cur', '8A', 128), row('wrong', '2A', 64)], 'cur', '8A', 128, null);
	});

	it('pitch range still hard: 70 vs 128 folds at ±16% and misses at ±8%', () => {
		const { tempoBoundsFromPitchRange } = mod;
		const at16 = tempoBoundsFromPitchRange(16);
		const at8 = tempoBoundsFromPitchRange(8);
		const playlist = [row('cur', '8A', 128), row('seventy', '8A', 70)];
		assertBoth(playlist, 'cur', '8A', 128, 'seventy', {
			min_tempo_ratio: at16.min,
			max_tempo_ratio: at16.max
		});
		assertBoth(playlist, 'cur', '8A', 128, null, {
			min_tempo_ratio: at8.min,
			max_tempo_ratio: at8.max
		});
	});
});

describe('auto-play maximize reach (slack path)', () => {
	// Tight BPM window: greedy earliest (b) is a dead end; c unlocks d.
	const strand = [
		row('a', '8A', 120),
		row('b', '8A', 121),
		row('c', '8A', 119),
		row('d', '8A', 118)
	];
	const tight = { min_tempo_ratio: 0.985, max_tempo_ratio: 1.015 };

	it('slack picks fewer-outward non-dead-end over greedy earliest', () => {
		const { pickNextStableId } = mod;
		assert.equal(
			pickNextStableId({
				playlist: strand,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				maximize_reach: false,
				...tight
			}),
			'b'
		);
		assert.equal(
			pickNextStableId({
				playlist: strand,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				maximize_reach: true,
				...tight
			}),
			'c'
		);
	});

	it('Warnsdorff unchanged when a fold row sits beside exact matches', () => {
		const { pickNextStableId } = mod;
		const withFold = [...strand, row('fold64', '8A', 64)];
		assert.equal(
			pickNextStableId({
				playlist: withFold,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				maximize_reach: false,
				...tight
			}),
			'b'
		);
		assert.equal(
			pickNextStableId({
				playlist: withFold,
				current_stable_id: 'a',
				current_key: '8A',
				current_bpm: 120,
				exclude_ids: new Set(),
				played_ids: new Set(),
				enforce_play_order: false,
				maximize_reach: true,
				...tight
			}),
			'c'
		);
	});

	it('simulated chain: slack last track is last accessible; longer than greedy', () => {
		const { simulateAutoPlayChain } = mod;
		const greedy = simulateAutoPlayChain({
			playlist: strand,
			start_stable_id: 'a',
			enforce_play_order: false,
			maximize_reach: false,
			...tight
		});
		const slack = simulateAutoPlayChain({
			playlist: strand,
			start_stable_id: 'a',
			enforce_play_order: false,
			maximize_reach: true,
			...tight
		});
		assert.deepEqual([...greedy], ['a', 'b']);
		assert.deepEqual([...slack], ['a', 'c', 'd']);
		assert.equal(slack[slack.length - 1], 'd');
		assert.equal(slack.length > greedy.length, true);
	});

	it('charted replay includes the live fallback after compatible tracks are exhausted', () => {
		for (const [maximize_reach, expected] of [
			[false, ['a', 'b', 'c', 'd']],
			[true, ['a', 'c', 'd', 'b']]
		]) {
			const result = mod.simulateAutoPlayChain({
				playlist: strand,
				start_stable_id: 'a',
				enforce_play_order: false,
				maximize_reach,
				select_next: mod.pickNextStableId,
				...tight
			});
			assert.deepEqual([...result], expected);
			assert.equal(new Set(result).size, result.length, 'fallback must never repeat a row');
		}
	});

	it('charted fallback retains exclusions, played rows, and the visible horizon', () => {
		const input = {
			playlist: strand,
			start_stable_id: 'a',
			enforce_play_order: false,
			maximize_reach: false,
			select_next: mod.pickNextStableId,
			...tight
		};
		assert.deepEqual([...mod.simulateAutoPlayChain({
			...input, exclude_ids: new Set(['c']), played_ids: new Set(['d'])
		})], ['a', 'b']);
		assert.deepEqual([...mod.simulateAutoPlayChain({ ...input, max_chain_length: 3 })], ['a', 'b', 'c']);
		assert.deepEqual([...mod.simulateAutoPlayChain({ ...input, enforce_play_order: true })], ['a', 'b', 'c', 'd']);
	});

	it('charts a switched playlist from the external source metadata', () => {
		const result = chain.simulateAutoPlayChain({
			playlist: [row('b', '8A', 120), row('c', '8A', 120)],
			start_stable_id: 'external', start_key: '8A', start_bpm: 120,
			enforce_play_order: false, maximize_reach: false,
			min_tempo_ratio: 0.84, max_tempo_ratio: 1.16
		});
		assert.deepEqual([...result], ['external', 'b', 'c']);
	});

	it('never substitutes external-source metadata for an existing unknown row', () => {
		for (const [key, bpm] of [[null, 120], ['8A', null]]) {
			const result = chain.simulateAutoPlayChain({
				playlist: [row('source', key, bpm), row('b', '8A', 120)],
				start_stable_id: 'source', start_key: '8A', start_bpm: 120,
				enforce_play_order: false, maximize_reach: false,
				min_tempo_ratio: 0.84, max_tempo_ratio: 1.16
			});
			assert.deepEqual([...result], ['source']);
		}
	});

	it('no-stranding fixture: greedy and slack produce identical order', () => {
		const { simulateAutoPlayChain } = mod;
		const linear = [
			row('a', '8A', 120),
			row('b', '8A', 120),
			row('c', '8A', 120),
			row('d', '8A', 120)
		];
		const opts = {
			playlist: linear,
			start_stable_id: 'a',
			enforce_play_order: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16
		};
		assert.deepEqual(
			[...simulateAutoPlayChain({ ...opts, maximize_reach: false })],
			[...simulateAutoPlayChain({ ...opts, maximize_reach: true })]
		);
	});

	it('over budget falls back to greedy earliest with fell_back', () => {
		const { pickNextMaximizingReach, AUTO_PLAY_REACH_MAX_STEPS } = chain;
		assert.equal(AUTO_PLAY_REACH_MAX_STEPS >= 50_000, true);
		const many = [row('a', '8A', 120), row('b', '8A', 121), row('c', '8A', 119)];
		const pick = pickNextMaximizingReach({
			candidates: many.slice(1),
			current_key: '8A',
			current_bpm: 120,
			min_tempo_ratio: 0.985,
			max_tempo_ratio: 1.015,
			max_steps: 1
		});
		assert.equal(pick.fell_back, true);
		assert.equal(pick.next, 'b');
	});

	it('broken rows never appear in either chain', () => {
		const { simulateAutoPlayChain } = mod;
		const withBroken = [
			row('a', '8A', 120),
			row('x', '8A', 120, false),
			row('b', '8A', 120)
		];
		for (const maximize of [false, true]) {
			const chain = simulateAutoPlayChain({
				playlist: withBroken,
				start_stable_id: 'a',
				enforce_play_order: false,
				maximize_reach: maximize,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			});
			assert.equal(chain.includes('x'), false);
		}
	});
});

describe('auto-play Beat Sync handoff policy', () => {
	it('maps pitch range to closed tempo bounds (default +-16%)', () => {
		const { tempoBoundsFromPitchRange } = mod;
		assert.deepEqual(tempoBoundsFromPitchRange(16), { min: 0.84, max: 1.16 });
		assert.deepEqual(tempoBoundsFromPitchRange(8), { min: 0.92, max: 1.08 });
	});

	it('enables Beat Sync only when source wants sync and phase-lock ok', () => {
		const { decideAutoPlayBeatSync } = mod;
		assert.equal(
			decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: true, beat_sync_max: false }),
			'enable'
		);
		assert.equal(
			decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: false, beat_sync_max: false }),
			'disable'
		);
		assert.equal(
			decideAutoPlayBeatSync({ source_beat_sync_enabled: false, phase_lock_ok: true, beat_sync_max: false }),
			'disable'
		);
		assert.equal(
			decideAutoPlayBeatSync({ source_beat_sync_enabled: false, phase_lock_ok: false, beat_sync_max: false }),
			'disable'
		);
	});

	// PLAY-23 (the maintainer: "Beatsync turns off despite BeatSyncMax being on during Autoplay.")
	it('[PLAY-23] with Beat Sync Max on, AutoPlay never decides to turn the follower sync off', () => {
		const { decideAutoPlayBeatSync } = mod;
		for (const source_beat_sync_enabled of [true, false]) {
			for (const phase_lock_ok of [true, false]) {
				const decision = decideAutoPlayBeatSync({ source_beat_sync_enabled, phase_lock_ok, beat_sync_max: true });
				assert.notEqual(decision, 'disable', `source sync ${source_beat_sync_enabled}, lock ${phase_lock_ok}`);
				assert.equal(decision, phase_lock_ok ? 'enable' : 'keep');
			}
		}
		assert.throws(
			() => decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: true }),
			/beat_sync_max must be boolean/
		);
	});

	it('[PLAY-23] SHAPE GUARD: the handoff passes the live Beat Sync Max pref into the decision', async () => {
		const { readFile } = await import('node:fs/promises');
		const src = await readFile('src/lib/rb/auto-play-phase-lock.ts', 'utf8');
		assert.match(src, /const beatSyncMax = uiPrefs\.beat_sync_max;/);
		assert.match(
			src,
			/decideAutoPlayBeatSync\(\{[^}]*beat_sync_max: beatSyncMax[^}]*\}\)/,
			'if the handoff decides sync without Beat Sync Max then Max turns off mid-AutoPlay again - broken'
		);
		assert.match(src, /source\.beat_sync_enabled \|\| beatSyncMax/, 'with Max on the lock is probed even when the source is not synced');
	});

	// The error this toast formats is DERIVED from the real planner, never
	// hand-written. The previous version built the string it wanted to see,
	// which is how it went on asserting a "Select BEAT mode for half/double"
	// branch for a message `computeFollowerSyncPlan` had stopped being able to
	// produce: pin 9bf12adccb45 made BAR fold rather than throw in that case,
	// so the only surviving BAR throw is pitch-range exhaustion. A synthetic
	// fixture cannot notice that. Blinded review, Thu 10 Sep 2026.
	it('formats the toast from the error the real planner actually throws', async () => {
		const { formatAutoPlaySyncSkipToast } = mod;
		const math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
		const grid = (bpm, count) => {
			const beats = [];
			let t = 0;
			for (let index = 0; index < count; index++) {
				beats.push({ n: (index % 4) + 1, bpm, t });
				t += 60 / bpm;
			}
			return beats;
		};
		// 128 against 40 is out of range raw (3.2), halved (1.6) and doubled
		// (6.4), so no normalization rescues it and BAR has nothing to fold to.
		let planError = null;
		try {
			math.computeFollowerSyncPlan({
				masterGrid: grid(128, 400),
				followerGrid: grid(40, 400),
				masterPositionAtSyncSec: 20,
				followerPositionSec: 20,
				currentContextTimeSec: 0.5,
				syncAtContextTimeSec: 1,
				masterTempoRatio: 1,
				minFollowerTempoRatio: 0.84,
				maxFollowerTempoRatio: 1.16,
				mode: 'bar'
			});
		} catch (error) {
			planError = error.message;
		}
		assert.ok(planError !== null, 'a 128:40 pair must still be refused outright');
		// Guard against the trap this test fell into while being written: an
		// incomplete request throws a RangeError from argument validation, and
		// every assertion below would then be inspecting the wrong message
		// while looking green.
		assert.match(
			planError,
			/no phase-capable bar anchor/,
			`the refusal must be the real pitch-range one, not a validation error: ${planError}`
		);
		// The load-bearing assertion, and the one the deleted branch needed:
		// BAR's surviving refusal carries no `tempoNormalization` substring, so
		// a tip branching on it could never fire.
		assert.doesNotMatch(
			planError,
			/tempoNormalization/,
			"BAR's only remaining refusal must not mention normalization - if it does, " +
				'the half/double tip removed from formatAutoPlaySyncSkipToast belongs back'
		);

		const toast = formatAutoPlaySyncSkipToast({
			follower_deck: 2,
			mode: 'bar',
			plan_error: planError,
			min_ratio: 0.84,
			max_ratio: 1.16
		});
		assert.match(toast, /Beat Sync skipped \(bar\)/);
		assert.match(toast, /BAR needs a twin within pitch range \[0.84, 1.16\]/);
		assert.doesNotMatch(toast, /Select BEAT mode/);
		assert.doesNotMatch(toast, /auto-switch/);
	});

	// The other half of the same claim: a pair that CAN fold no longer reaches
	// this toast at all, because BAR locks it instead of throwing.
	it('a foldable pair never reaches the skip toast, because BAR locks it', async () => {
		const math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
		const grid = (bpm, count) => {
			const beats = [];
			let t = 0;
			for (let index = 0; index < count; index++) {
				beats.push({ n: (index % 4) + 1, bpm, t });
				t += 60 / bpm;
			}
			return beats;
		};
		const plan = math.computeFollowerSyncPlan({
			masterGrid: grid(128, 400),
			followerGrid: grid(64, 400),
			masterPositionAtSyncSec: 20,
			followerPositionSec: 20,
			currentContextTimeSec: 0.5,
			syncAtContextTimeSec: 1,
			masterTempoRatio: 1,
			minFollowerTempoRatio: 0.84,
			maxFollowerTempoRatio: 1.16,
			mode: 'bar'
		});
		assert.equal(plan.mode, 'bar');
		assert.equal(plan.tempoNormalization, 0.5, '128 against 64 must lock as a half-tempo fold');
	});
});

// ---------------------------------------------------------------------------
// Live-set regression, Mon 31 Aug 2026. Two operator-visible failures:
//   1. "autoplay loaded SAME song into two decks"
//   2. "contact and acid rain both loaded after hypnosis - shortly after each
//      other, and got an error re set deck master which I then couldn't dismiss"
// The invariant: AutoPlay loads a new track only when the MASTER reaches the
// end, exactly once per master track. Scenario numbers below map to the matrix
// in the auto-play.ts module docstring.
// ---------------------------------------------------------------------------

describe('auto-play handoff commit point (scenarios 16, 17)', () => {
	it('re-arms only when nothing has been committed to the follower deck', () => {
		const { handoffFailureIsRetryable } = mod;
		// Scenario 16: unload/load rejected - deck untouched, try another pick.
		assert.equal(handoffFailureIsRetryable('load'), true);
		// Scenario 17: beat_sync/play/master rejected AFTER the track landed.
		// Re-arming here is what loaded "contact" and then "acid rain".
		assert.equal(handoffFailureIsRetryable('commit'), false);
		// PLAY-22: the follower's old track would not clear; the candidate is not to blame.
		assert.equal(handoffFailureIsRetryable('follower-unload'), true);
	});

	it('throws on an unknown phase rather than guessing a default', () => {
		const { handoffFailureIsRetryable } = mod;
		assert.throws(() => handoffFailureIsRetryable('nonsense'), /unhandled handoff phase/);
	});
});

describe('auto-play exclusion set (scenarios 13, 14)', () => {
	it('excludes tracks on other decks but never the source deck track', () => {
		const { autoPlayExcludedIds } = mod;
		const decks = [
			deck({ id: 1, stable_id: 'hypnosis', playing: true, is_master: true }),
			deck({ id: 2, stable_id: 'contact' }),
			deck({ id: 3, stable_id: null }),
			deck({ id: 4, stable_id: 'acid-rain' })
		];
		const out = autoPlayExcludedIds({
			decks,
			source_deck: 1,
			claimed_ids: new Set(),
			unplayable_ids: new Set()
		});
		assert.equal(out.has('contact'), true);
		assert.equal(out.has('acid-rain'), true);
		assert.equal(out.has('hypnosis'), false);
	});

	// REQ: PLAY-10
	it('excludes every id AutoPlay ever claimed, even after the deck is emptied', () => {
		const { autoPlayExcludedIds } = mod;
		// Scenario 14 / bug 1: the deck that held "contact" was unloaded, so the
		// deck scan no longer sees it. The claim is what stops the second load.
		const decks = [
			deck({ id: 1, stable_id: 'hypnosis', playing: true, is_master: true }),
			deck({ id: 2, stable_id: null }),
			deck({ id: 3, stable_id: null }),
			deck({ id: 4, stable_id: null })
		];
		const out = autoPlayExcludedIds({
			decks,
			source_deck: 1,
			claimed_ids: new Set(['contact']),
			unplayable_ids: new Set(['ghost'])
		});
		assert.equal(out.has('contact'), true);
		assert.equal(out.has('ghost'), true);
	});

	it('a claimed id can never be picked again by the track picker', () => {
		// REGRESSION for bug 1: same stable_id must never reach two decks.
		const { autoPlayExcludedIds, pickNextStableId } = mod;
		const playlist = [
			row('hypnosis', '8A', 124),
			row('contact', '8A', 124),
			row('acid-rain', '8A', 124)
		];
		const decks = [
			deck({ id: 1, stable_id: 'hypnosis', playing: true, is_master: true }),
			deck({ id: 2, stable_id: null }),
			deck({ id: 3, stable_id: null }),
			deck({ id: 4, stable_id: null })
		];
		const claimed = new Set();
		const picked = [];
		for (let pass = 0; pass < 3; pass++) {
			const next = pickNextStableId({
				playlist,
				current_stable_id: 'hypnosis',
				current_key: '8A',
				current_bpm: 124,
				exclude_ids: autoPlayExcludedIds({
					decks,
					source_deck: 1,
					claimed_ids: claimed,
					unplayable_ids: new Set()
				}),
				played_ids: new Set(),
				enforce_play_order: false,
				min_tempo_ratio: 0.84,
				max_tempo_ratio: 1.16
			});
			if (next === null) break;
			claimed.add(next);
			picked.push(next);
		}
		assert.deepEqual(picked, ['contact', 'acid-rain']);
		assert.equal(new Set(picked).size, picked.length, 'no id may be picked twice');
	});
});

describe('auto-play deferred master promotion (scenario 18)', () => {
	it('waits while the freshly started follower is scheduled but not yet audible', () => {
		// This is the exact refusal the maintainer hit: setDeckMaster rejects a
		// not-yet-audible deck while the outgoing master is still audible.
		const { decideMasterPromotion } = mod;
		assert.equal(
			decideMasterPromotion({
				pending_deck: 2,
				pending_stable_id: 'contact',
				deck: deck({ id: 2, stable_id: 'contact', playing: true }),
				audible: false
			}),
			'wait'
		);
	});

	it('promotes once the follower is actually presenting audio', () => {
		const { decideMasterPromotion } = mod;
		assert.equal(
			decideMasterPromotion({
				pending_deck: 2,
				pending_stable_id: 'contact',
				deck: deck({ id: 2, stable_id: 'contact', playing: true }),
				audible: true
			}),
			'promote'
		);
	});

	it('drops the promotion when nothing is pending, it already landed, or the operator took over', () => {
		const { decideMasterPromotion } = mod;
		assert.equal(
			decideMasterPromotion({
				pending_deck: null,
				pending_stable_id: null,
				deck: null,
				audible: false
			}),
			'drop'
		);
		assert.equal(
			decideMasterPromotion({
				pending_deck: 2,
				pending_stable_id: 'contact',
				deck: deck({ id: 2, stable_id: 'contact', playing: true, is_master: true }),
				audible: true
			}),
			'drop'
		);
		assert.equal(
			decideMasterPromotion({
				pending_deck: 2,
				pending_stable_id: 'contact',
				deck: deck({ id: 2, stable_id: 'something-else', playing: true }),
				audible: true
			}),
			'drop'
		);
	});
});

describe('auto-play live-set replay (bugs 1 and 2, Mon 31 Aug 2026)', () => {
	/** Decision half of one _tick, using only the pure policy helpers. */
	function tickDecision(state, source) {
		const rem = mod.remainingMs(source.position_ms, source.duration_ms);
		if (rem !== null && rem > mod.AUTO_PLAY_THRESHOLD_MS) {
			if (state.triggeredFor === source.stable_id) state.triggeredFor = null;
			return null;
		}
		if (
			!mod.shouldTriggerAutoPlay({
				enabled: true,
				remaining_ms: rem,
				threshold_ms: mod.AUTO_PLAY_THRESHOLD_MS,
				source_stable_id: source.stable_id,
				already_triggered_for: state.triggeredFor,
				in_flight: false
			})
		) {
			return null;
		}
		const next = mod.pickNextStableId({
			playlist: state.playlist,
			current_stable_id: source.stable_id,
			current_key: '8A',
			current_bpm: 124,
			exclude_ids: mod.autoPlayExcludedIds({
				decks: state.decks,
				source_deck: source.id,
				claimed_ids: state.claimed,
				unplayable_ids: state.unplayable
			}),
			played_ids: state.played,
			enforce_play_order: false,
			min_tempo_ratio: 0.84,
			max_tempo_ratio: 1.16
		});
		if (next === null) {
			if (state.playlist.length === 0) return null;
			state.triggeredFor = source.stable_id;
			return null;
		}
		// Commit before dispatch - the ordering the fix depends on.
		state.triggeredFor = source.stable_id;
		state.claimed.add(next);
		state.played.add(source.stable_id);
		state.played.add(next);
		return next;
	}

	function freshState() {
		return {
			playlist: [
				row('hypnosis', '8A', 124),
				row('contact', '8A', 124),
				row('acid-rain', '8A', 124)
			],
			decks: [
				deck({ id: 1, stable_id: 'hypnosis', playing: true, is_master: true }),
				deck({ id: 2, stable_id: null }),
				deck({ id: 3, stable_id: null }),
				deck({ id: 4, stable_id: null })
			],
			claimed: new Set(),
			played: new Set(),
			unplayable: new Set(),
			triggeredFor: null
		};
	}

	it('bug 2: a refused master handover no longer loads a second track', () => {
		const state = freshState();
		const master = deck({
			id: 1,
			stable_id: 'hypnosis',
			playing: true,
			is_master: true,
			position_ms: 290_000,
			duration_ms: 300_000
		});

		const first = tickDecision(state, master);
		assert.equal(first, 'contact');

		// setDeckMaster refuses the not-yet-audible follower. This is a 'commit'
		// phase failure: "contact" is already loaded and playing on deck 2.
		assert.equal(mod.handoffFailureIsRetryable('commit'), false);
		// So the trigger is NOT rolled back. Old code did `_triggeredFor = null`
		// here, which is what loaded "acid rain" 250 ms later.
		state.decks[1].stable_id = 'contact';

		for (let poll = 0; poll < 20; poll++) {
			assert.equal(tickDecision(state, master), null, `poll ${poll} must not load`);
		}
		assert.deepEqual([...state.claimed], ['contact']);
	});

	it('bug 1: the same stable_id can never be loaded onto two decks', () => {
		const state = freshState();
		const master = deck({
			id: 1,
			stable_id: 'hypnosis',
			playing: true,
			is_master: true,
			position_ms: 290_000,
			duration_ms: 300_000
		});
		const loaded = [];
		const next = tickDecision(state, master);
		if (next !== null) loaded.push(next);

		// Every way the deck scan can stop seeing "contact": the operator
		// unloads deck 2, and the playlist feed epoch rolls (which retires
		// played history and quarantine). The claim must survive both.
		state.decks[1].stable_id = null;
		state.played = new Set();
		state.unplayable = new Set();
		state.triggeredFor = null;

		for (let poll = 0; poll < 20; poll++) {
			const again = tickDecision(state, master);
			if (again !== null) loaded.push(again);
			state.triggeredFor = null;
		}
		assert.equal(loaded.includes('contact'), true);
		assert.equal(
			loaded.filter((id) => id === 'contact').length,
			1,
			'AutoPlay must never load one stable_id twice'
		);
		assert.equal(new Set(loaded).size, loaded.length, 'no duplicate loads at all');
	});

	it('scenario 6/7: a looped master arms once; a seek back out re-arms once', () => {
		const state = freshState();
		const inWindow = deck({
			id: 1,
			stable_id: 'hypnosis',
			playing: true,
			is_master: true,
			position_ms: 292_000,
			duration_ms: 300_000
		});
		// Row 6: master looping inside the threshold window, polled repeatedly.
		assert.equal(tickDecision(state, inWindow), 'contact');
		for (let poll = 0; poll < 20; poll++) {
			assert.equal(tickDecision(state, inWindow), null);
		}
		// Row 7: operator seeks back out of the window, then plays to the end.
		state.decks[1].stable_id = 'contact';
		const seekedBack = { ...inWindow, position_ms: 100_000 };
		assert.equal(tickDecision(state, seekedBack), null);
		assert.equal(state.triggeredFor, null, 'leaving the window disarms');
		assert.equal(tickDecision(state, inWindow), 'acid-rain', 're-entry arms exactly once');
		for (let poll = 0; poll < 20; poll++) {
			assert.equal(tickDecision(state, inWindow), null);
		}
	});

	it('scenario 3: an unknown duration never triggers a load', () => {
		const state = freshState();
		const unknown = deck({
			id: 1,
			stable_id: 'hypnosis',
			playing: true,
			is_master: true,
			position_ms: 0,
			duration_ms: null
		});
		for (let poll = 0; poll < 10; poll++) {
			assert.equal(tickDecision(state, unknown), null);
		}
		assert.equal(state.claimed.size, 0);
	});
});
