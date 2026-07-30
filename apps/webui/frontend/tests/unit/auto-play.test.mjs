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
	it('prefers suggest-next, else next playlist row after current', () => {
		const { pickNextStableId } = mod;
		assert.equal(
			pickNextStableId({
				suggest_ids: ['s1', 's2'],
				playlist_ids: ['a', 'b', 'c'],
				current_stable_id: 'a',
				exclude_ids: new Set()
			}),
			's1'
		);
		assert.equal(
			pickNextStableId({
				suggest_ids: ['a', 's1'],
				playlist_ids: ['a', 'b', 'c'],
				current_stable_id: 'a',
				exclude_ids: new Set(['s1'])
			}),
			'b'
		);
		assert.equal(
			pickNextStableId({
				suggest_ids: [],
				playlist_ids: ['a', 'b', 'c'],
				current_stable_id: 'b',
				exclude_ids: new Set(['c'])
			}),
			null
		);
		assert.equal(
			pickNextStableId({
				suggest_ids: [],
				playlist_ids: ['a', 'b'],
				current_stable_id: 'missing',
				exclude_ids: new Set()
			}),
			null
		);
	});

	it('publishes browser feed getters', () => {
		const { setAutoPlayTrackFeed, getAutoPlaySuggestIds, getAutoPlayPlaylistIds } = mod;
		setAutoPlayTrackFeed(['s1'], ['p1', 'p2']);
		assert.deepEqual([...getAutoPlaySuggestIds()], ['s1']);
		assert.deepEqual([...getAutoPlayPlaylistIds()], ['p1', 'p2']);
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
