import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
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
});

describe('auto-play deck pick', () => {
	it('prefers playing master, else lowest playing deck', () => {
		const { pickSourceDeck } = mod;
		const decks = [
			deck({ id: 1, stable_id: 'a', playing: true, is_master: false }),
			deck({ id: 2, stable_id: 'b', playing: true, is_master: true }),
			deck({ id: 3, stable_id: 'c', playing: false })
		];
		assert.equal(pickSourceDeck(decks)?.id, 2);
		assert.equal(
			pickSourceDeck([
				deck({ id: 3, stable_id: 'c', playing: true }),
				deck({ id: 1, stable_id: 'a', playing: true })
			])?.id,
			1
		);
		assert.equal(pickSourceDeck([deck({ id: 1, playing: false })]), null);
	});

	it('picks empty follower before stopped; never a playing deck', () => {
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
			3
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

describe('auto-play track pick', () => {
	const playlist = [
		row('a', '8A', 120),
		row('b', '8A', 122),
		row('c', '3A', 120),
		row('d', '8B', 125),
		row('e', '8A', 160)
	];

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
			null
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
		// nothing left in range
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
			null
		);
	});

	it('bpmWithinPhaseLockRange rejects half/double folds', () => {
		const { bpmWithinPhaseLockRange } = mod;
		assert.equal(bpmWithinPhaseLockRange(120, 120, 0.84, 1.16), true);
		assert.equal(bpmWithinPhaseLockRange(160, 120, 0.84, 1.16), false);
		assert.equal(bpmWithinPhaseLockRange(60, 120, 0.84, 1.16), false);
	});

	it('publishes browser feed getters; epoch only on membership identity change', () => {
		const {
			setAutoPlayTrackFeed,
			getAutoPlayPlaylist,
			getAutoPlayPlaylistIds,
			getAutoPlayFeedEpoch
		} = mod;
		const before = getAutoPlayFeedEpoch();
		setAutoPlayTrackFeed([
			{ stable_id: 'p1', key: '1A', bpm: 120, file_exists: true },
			{ stable_id: 'p2', key: '2A', bpm: 124, file_exists: false }
		]);
		const afterId = getAutoPlayFeedEpoch();
		assert.equal(afterId > before, true);
		setAutoPlayTrackFeed([
			{ stable_id: 'p1', key: '9A', bpm: 128, file_exists: true },
			{ stable_id: 'p2', key: '2A', bpm: 124, file_exists: true }
		]);
		assert.equal(getAutoPlayFeedEpoch(), afterId);
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
		const { pickNextMaximizingReach, AUTO_PLAY_REACH_MAX_STEPS } = mod;
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
			decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: true }),
			'enable'
		);
		assert.equal(
			decideAutoPlayBeatSync({ source_beat_sync_enabled: true, phase_lock_ok: false }),
			'disable'
		);
		assert.equal(
			decideAutoPlayBeatSync({ source_beat_sync_enabled: false, phase_lock_ok: true }),
			'disable'
		);
		assert.equal(
			decideAutoPlayBeatSync({ source_beat_sync_enabled: false, phase_lock_ok: false }),
			'disable'
		);
	});

	it('never auto-selects BEAT; toast points at BAR pitch window or BEAT tip', () => {
		const { formatAutoPlaySyncSkipToast } = mod;
		const outOfRange = formatAutoPlaySyncSkipToast({
			follower_deck: 2,
			mode: 'bar',
			plan_error:
				'follower grid has no phase-capable bar anchor with tempo ratio within [0.84, 1.16] for beat n=2',
			min_ratio: 0.84,
			max_ratio: 1.16
		});
		assert.match(outOfRange, /Beat Sync skipped \(bar\)/);
		assert.match(outOfRange, /BAR needs a twin within pitch range/);
		assert.doesNotMatch(outOfRange, /Select BEAT mode/);

		const halfDouble = formatAutoPlaySyncSkipToast({
			follower_deck: 3,
			mode: 'bar',
			plan_error:
				'strict BAR sync requires tempoNormalization=1 to preserve raw PQTZ cadence; ' +
				'the available anchor requires tempoNormalization=0.5. ' +
				'Select BEAT mode for half/double tempo matching or widen the follower tempo range.',
			min_ratio: 0.84,
			max_ratio: 1.16
		});
		assert.match(halfDouble, /Select BEAT mode for half\/double/);
		assert.doesNotMatch(halfDouble, /auto-switch/);
	});
});
