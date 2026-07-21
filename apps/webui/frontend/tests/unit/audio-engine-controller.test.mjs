import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 }
];
let audio;
let computeFollowerSyncPlan;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts');
	({ computeFollowerSyncPlan } = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts'));
});

test('controller defaults enable quantize, Beat Sync, and Master Tempo with no static master', () => {
	for (const deck of audio.DECK_IDS) {
		const state = audio.getDeckState(deck);
		assert.equal(state.quantize_enabled, true);
		assert.equal(state.beat_sync_enabled, true);
		assert.equal(state.master_tempo_enabled, true);
		assert.equal(state.audible, false);
		assert.equal(state.transport_pending, false);
		assert.equal(state.sync_mode, 'bar');
		assert.equal(state.sync_error, null);
		assert.equal(state.processor_error, null);
		assert.equal(state.is_master, false);
	}
});

test('Master Tempo preserves pitch while the disabled mode follows playback rate', () => {
	assert.equal(audio.masterTempoSemitones(1.1, true), 0);
	assert.ok(Math.abs(audio.masterTempoSemitones(1.1, false) - 12 * Math.log2(1.1)) < 1e-12);
	assert.throws(() => audio.masterTempoSemitones(0, true), /tempo ratio/i);
});

test('central seek quantization snaps to real PQTZ and missing grids fail explicitly', () => {
	assert.equal(audio.quantizedPositionMs(REAL_PQTZ_BEATS, 590, true), 608);
	assert.equal(audio.quantizedPositionMs(REAL_PQTZ_BEATS, 590, false), 590);
	assert.throws(() => audio.quantizedPositionMs([], 590, true), /beat grid/i);
});

test('paused seek produces one frozen UI and runtime clock position', () => {
	assert.deepEqual(audio.pausedSeekClock(608, 4000), {
		position_ms: 608,
		start_offset_sec: 0.608
	});
	assert.throws(() => audio.pausedSeekClock(4001, 4000), /duration/i);
});

test('decoded audio duration is the canonical waveform and transport duration', () => {
	assert.equal(audio.decodedTransportDurationMs(123.4567), 123456.7);
	assert.throws(() => audio.decodedTransportDurationMs(0), /positive/i);
	assert.throws(() => audio.decodedTransportDurationMs(Number.NaN), /finite/i);
});

test('audible Beat-Synced follower seeks route back through the selected master', () => {
	assert.equal(audio.seekSyncMaster(2, true, true, 1), 1);
	assert.equal(audio.seekSyncMaster(1, true, true, 1), null);
	assert.equal(audio.seekSyncMaster(2, false, true, 1), null);
	assert.equal(audio.seekSyncMaster(2, true, false, 1), null);
});

test('central loop quantization snaps both endpoints and rejects collapsed loops', () => {
	assert.deepEqual(
		audio.quantizedLoopEndpointsMs(REAL_PQTZ_BEATS, { in_ms: 590, out_ms: 1090 }, true),
		{ in_ms: 608, out_ms: 1080 }
	);
	assert.throws(
		() => audio.quantizedLoopEndpointsMs(REAL_PQTZ_BEATS, { in_ms: 590, out_ms: 610 }, true),
		/collapsed/i
	);
});

test('future-scheduled transport projection starts from the scheduled epoch', () => {
	assert.equal(
		audio.projectedTransportPosition({
			now: 10,
			startContextTime: 12,
			startPositionSec: 4,
			tempoRatio: 1.1,
			projectAt: 12.5
		}),
		4.55
	);
});

test('presented timeline supersedes an unpresented schedule at the same boundary', () => {
	const timeline = audio.createPresentedTransportTimeline(2);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 10,
		startPositionSec: 4,
		tempoRatio: 1
	});
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: true,
		loop: null,
		startContextTime: 10,
		startPositionSec: 8,
		tempoRatio: 1
	});

	assert.equal(timeline.schedules.length, 2, 'every acknowledged schedule remains mirrored');
	const before = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 9, performanceTime: 9000 },
		20
	);
	assert.deepEqual(
		{
			position_sec: before.position_sec,
			audible: before.audible,
			transport_pending: before.transport_pending,
			presented_revision: before.presented_revision,
			desired_revision: before.desired_revision
		},
		{
			position_sec: 2,
			audible: false,
			transport_pending: true,
			presented_revision: 0,
			desired_revision: 2
		}
	);

	const presented = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 10.25, performanceTime: 10250 },
		20
	);
	assert.equal(presented.position_sec, 8.25);
	assert.equal(presented.audible, true);
	assert.equal(presented.transport_pending, false);
	assert.equal(presented.presented_revision, 2);
});

test('presented timeline retains intermediate boundaries and clears pending only at latest revision', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 1,
		startPositionSec: 1,
		tempoRatio: 1
	});
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: false,
		loop: null,
		startContextTime: 2,
		startPositionSec: 2,
		tempoRatio: 1
	});

	const between = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 1.5, performanceTime: 1500 },
		10
	);
	assert.equal(between.audible, true);
	assert.equal(between.position_sec, 1.5);
	assert.equal(between.presented_revision, 1);
	assert.equal(between.transport_pending, true);

	const latest = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 2.1, performanceTime: 2100 },
		10
	);
	assert.equal(latest.audible, false);
	assert.equal(latest.position_sec, 2);
	assert.equal(latest.presented_revision, 2);
	assert.equal(latest.transport_pending, false);
});

test('presented timeline evaluates tempo and engaged loops at the output clock', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: { in_ms: 1000, out_ms: 3000, engaged: true, beat_length: 4 },
		startContextTime: 5,
		startPositionSec: 1,
		tempoRatio: 2
	});

	const beforeWrap = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 5.75, performanceTime: 5750 },
		20
	);
	assert.equal(beforeWrap.position_sec, 2.5);
	const afterWrap = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 6.25, performanceTime: 6250 },
		20
	);
	assert.equal(afterWrap.position_sec, 1.5);
});

test('revisioned natural-end stop retires the active schedule before replay cursor movement', () => {
	const timeline = audio.createPresentedTransportTimeline(9);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 1,
		startPositionSec: 9,
		tempoRatio: 1
	});
	const ended = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 2.1, performanceTime: 2100 },
		10
	);
	assert.equal(ended.audible, false);
	assert.equal(ended.position_sec, 10);
	assert.equal(
		audio.naturalEndNeedsRevisionedStop(true, ended, 10, 0),
		true,
		'an active revision first presented at duration still requires cleanup'
	);
	assert.equal(
		audio.naturalEndNeedsRevisionedStop(false, ended, 10, 0),
		false,
		'an explicitly inactive pause at duration must not add another stop'
	);

	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: false,
		loop: null,
		startContextTime: 3,
		startPositionSec: 10,
		tempoRatio: 1
	});
	const pendingStop = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 2.1, performanceTime: 2100 },
		10
	);
	assert.equal(pendingStop.accepted, true);
	assert.equal(pendingStop.transport_pending, true);
	const stopped = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 3, performanceTime: 3000 },
		10
	);
	assert.equal(stopped.presented_revision, 2);
	assert.equal(stopped.transport_pending, false);

	audio.setPausedTransportTimelineCursor(timeline, 0, 10);
	const replayCursor = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 3.1, performanceTime: 3100 },
		10
	);
	assert.equal(replayCursor.position_sec, 0);
	assert.equal(replayCursor.audible, false);
});

test('zero output timestamp preserves the frozen cursor and pending start', () => {
	const timeline = audio.createPresentedTransportTimeline(0.5);
	audio.setPausedTransportTimelineCursor(timeline, 0.75, 10);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 1,
		startPositionSec: 0.75,
		tempoRatio: 1
	});

	const observation = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 0, performanceTime: 0 },
		10
	);
	assert.equal(observation.accepted, false);
	assert.equal(observation.output_started, false);
	assert.equal(observation.presentation_context_time_s, null);
	assert.equal(observation.position_sec, 0.75);
	assert.equal(observation.audible, false);
	assert.equal(observation.transport_pending, true);
	assert.equal(timeline.presented_revision, 0);
});

test('stale output observations and late old revisions never rewind presented state', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: true,
		loop: null,
		startContextTime: 2,
		startPositionSec: 2,
		tempoRatio: 1
	});
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 1,
		startPositionSec: 9,
		tempoRatio: 1
	});

	const current = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 3, performanceTime: 3000 },
		20
	);
	assert.equal(current.position_sec, 3);
	assert.equal(current.presented_revision, 2);
	const repeated = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 3, performanceTime: 3000 },
		20
	);
	assert.equal(repeated.accepted, true);
	assert.equal(repeated.position_sec, 3);
	assert.equal(repeated.presented_revision, 2);
	const regressingContext = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 2.5, performanceTime: 3500 },
		20
	);
	assert.equal(regressingContext.accepted, false);
	assert.equal(regressingContext.position_sec, 3);
	assert.equal(regressingContext.presented_revision, 2);
	const regressingPerformance = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 4, performanceTime: 2500 },
		20
	);
	assert.equal(regressingPerformance.accepted, false);
	assert.equal(regressingPerformance.position_sec, 3);
	assert.throws(
		() =>
			audio.observePresentedTransportTimeline(
				timeline,
				{ contextTime: Number.NaN, performanceTime: 4000 },
				20
			),
		/output timestamp/i
	);
	assert.throws(
		() =>
			audio.observePresentedTransportTimeline(
				timeline,
				{ contextTime: 0, performanceTime: 4000 },
				20
			),
		/zero timestamp/i
	);
});

test('transport scheduling horizon is strictly future and latency-aware', () => {
	assert.ok(Math.abs(audio.safeTransportScheduleTime(10, 0.2, 0.1) - 10.3) < 1e-12);
	assert.throws(() => audio.safeTransportScheduleTime(10, -0.1), /latency/i);
	assert.equal(
		audio.supersedingScheduleTime(10.5, 10.2, 10.3),
		10.5,
		'an unsafe pending boundary must not backdate a replacement schedule'
	);
	assert.equal(
		audio.supersedingScheduleTime(10.5, 10.4, 10.3),
		10.4,
		'a still-safe pending boundary may be superseded in place'
	);
});

test('deck transport clock exposes the paused cursor and revision diagnostics', () => {
	assert.deepEqual(audio.deckTransportClock(1), {
		source: 'paused_cursor',
		presentation_context_time_s: null,
		desired_revision: 0,
		presented_revision: 0
	});
});

test('existing pending follower work waits before recomputing a fresh safe sync horizon', () => {
	const waitTarget = audio.pendingSyncWaitTarget(10.5, [10.2, 10.6]);
	assert.equal(waitTarget, 10.2);
	const recomputedSyncAt = audio.safeSyncScheduleTime(waitTarget, 0.2, waitTarget, 0.1);
	assert.ok(Math.abs(recomputedSyncAt - 10.5) < 1e-12);
	assert.ok(recomputedSyncAt >= waitTarget + 0.2 + 0.1);
	assert.equal(audio.pendingSyncWaitTarget(10.5, [10.6]), null);
	assert.equal(audio.supersedingScheduleTime(10.5, null), 10.5);
	assert.throws(() => audio.pendingSyncWaitTarget(10.5, [Number.NaN]), /pending sync/);
});

test('master tempo and every follower receive exactly one common schedule horizon', () => {
	assert.deepEqual(audio.commonSyncScheduleTimes(12.4, 3), [12.4, 12.4, 12.4]);
	assert.throws(() => audio.commonSyncScheduleTimes(12.4, 0), /participant/i);
});

test('loop-aware projection wraps once across a future synchronization lead', () => {
	const position = audio.projectedLoopAwareTransportPosition({
		active: true,
		loop: { in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 2 },
		startContextTime: 10,
		startPositionSec: 1.9,
		tempoRatio: 1,
		projectAt: 10.2,
		durationSec: 10
	});
	assert.ok(Math.abs(position - 1.1) < 1e-12);
});

test('paused play normalizes positions on either side of an engaged loop with full modulo', () => {
	const loop = { in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 2 };
	assert.ok(Math.abs(audio.normalizeEngagedLoopPositionSec(0.5, loop) - 1.5) < 1e-12);
	assert.ok(Math.abs(audio.normalizeEngagedLoopPositionSec(4.2, loop) - 1.2) < 1e-12);
	assert.equal(audio.normalizeEngagedLoopPositionSec(1.4, loop), 1.4);
	assert.equal(audio.normalizeEngagedLoopPositionSec(4.2, { ...loop, engaged: false }), 4.2);
});

test('live loop and seek scheduling normalize below and above loop positions with exact modulo', () => {
	const loop = { in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 2 };
	const liveLoopProjectedPositionSec = 0.5;
	const liveSeekPositionSec = 4.2;

	assert.ok(
		Math.abs(
			audio.normalizeScheduledTransportEntrySec(liveLoopProjectedPositionSec, 10, loop, true) -
				1.5
		) < 1e-12
	);
	assert.ok(
		Math.abs(
			audio.normalizeScheduledTransportEntrySec(liveSeekPositionSec, 10, loop, true) - 1.2
		) < 1e-12
	);
});

test('inactive CUE return preserves its requested frozen position outside an engaged loop', () => {
	const loop = { in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 2 };

	assert.equal(audio.normalizeScheduledTransportEntrySec(4.2, 10, loop, false), 4.2);
	assert.throws(
		() => audio.normalizeScheduledTransportEntrySec(10.1, 10, loop, false),
		/within 0\.\.10/i
	);
});

test('canceling a pending looped start preserves the requested paused position', () => {
	const pendingLoop = { in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 2 };

	assert.equal(audio.normalizeScheduledTransportEntrySec(0.5, 10, pendingLoop, false), 0.5);
	assert.equal(audio.normalizeScheduledTransportEntrySec(4.2, 10, pendingLoop, false), 4.2);
});

test('synced follower region and phase are chosen before final loop-entry normalization', () => {
	const regularGrid = Array.from({ length: 12 }, (_, index) => ({
		n: (index % 4) + 1,
		bpm: 120,
		t: index * 0.5
	}));
	const plan = computeFollowerSyncPlan({
		masterGrid: regularGrid,
		followerGrid: regularGrid,
		masterPositionAtSyncSec: 0.3,
		masterTempoRatio: 1,
		followerPositionSec: 5,
		currentContextTimeSec: 30,
		syncAtContextTimeSec: 30.2,
		minFollowerTempoRatio: 0.9,
		maxFollowerTempoRatio: 1.1,
		mode: 'beat'
	});
	const scheduledPositionSec = audio.normalizeScheduledTransportEntrySec(
		plan.followerPositionSec,
		10,
		{ in_ms: 1000, out_ms: 2000, engaged: true, beat_length: 2 },
		true
	);

	assert.ok(plan.followerPositionSec > 4.5);
	assert.ok(Math.abs(plan.beatPhase - 0.6) < 1e-12);
	assert.ok(Math.abs(scheduledPositionSec - 1.8) < 1e-12);
});

test('exact beat-loop resize preserves the supplied real PQTZ loop-in anchor', () => {
	assert.deepEqual(audio.exactBeatLoopRangeMs(REAL_PQTZ_BEATS, 1080, 2, 608), {
		in_ms: 608,
		out_ms: 1553
	});
	assert.throws(() => audio.exactBeatLoopRangeMs(REAL_PQTZ_BEATS, 1080, 4, 1080), /do not fit/i);
});

test('only engaged loops suppress end-of-track and the final playing master clears', () => {
	const loop = { in_ms: 1000, out_ms: 2000, engaged: false, beat_length: 2 };
	assert.equal(audio.deckReachedEnd(10, 10, loop), true);
	assert.equal(audio.deckReachedEnd(10, 10, { ...loop, engaged: true }), false);
	assert.equal(audio.nextPlayingMaster([]), null);
	assert.equal(audio.nextPlayingMaster([3]), 3);
});

test('load-state invariant rejects playable-looking ghost decks', () => {
	assert.doesNotThrow(() => audio.assertDeckLoadConsistency(null, 0, false));
	assert.doesNotThrow(() => audio.assertDeckLoadConsistency('stable', 120, true));
	assert.throws(
		() => audio.assertDeckLoadConsistency('ghost', 0, false),
		/inconsistent deck load state/
	);
});

test('deck replacement requires a fully presented stop before candidate preparation', () => {
	const stopped = {
		playing: false,
		audible: false,
		transportPending: false,
		controlActive: false,
		pendingScheduleCount: 0,
		scheduleIntentCount: 0
	};
	assert.doesNotThrow(() => audio.assertDeckReplacementAllowed(1, stopped));

	for (const active of [
		{ playing: true },
		{ audible: true },
		{ transportPending: true },
		{ controlActive: true },
		{ pendingScheduleCount: 1 },
		{ scheduleIntentCount: 1 }
	]) {
		assert.throws(
			() => audio.assertDeckReplacementAllowed(1, { ...stopped, ...active }),
			/fully stopped/i
		);
	}
});

test('only the latest prepared load candidate may publish and replace the incumbent', () => {
	assert.equal(audio.loadCandidateCanPublish(7, 7), true);
	assert.equal(audio.loadCandidateCanPublish(6, 7), false);
	assert.throws(() => audio.loadCandidateCanPublish(0, 1), /positive integer/i);
});

test('a paused MASTER selection rejects while another deck is live or scheduled', () => {
	assert.doesNotThrow(() => audio.assertPausedMasterSelectionAllowed(2, false, []));
	assert.doesNotThrow(() => audio.assertPausedMasterSelectionAllowed(2, true, [1]));
	assert.throws(
		() => audio.assertPausedMasterSelectionAllowed(2, false, [1]),
		/cannot select paused deck 2/i
	);
});

test('an audible pending stop remains a paused MASTER selection blocker', () => {
	const activity = {
		1: { audible: true, playing: false },
		2: { audible: false, playing: false },
		3: { audible: false, playing: false },
		4: { audible: false, playing: false }
	};

	assert.deepEqual(audio.pausedMasterSelectionBlockers(2, activity), [1]);
	assert.throws(
		() =>
			audio.assertPausedMasterSelectionAllowed(
				2,
				activity[2].audible,
				audio.pausedMasterSelectionBlockers(2, activity)
			),
		/cannot select paused deck 2/i
	);
});

test('MASTER switching includes Beat-Synced pending starts at the next common horizon', () => {
	const activity = {
		1: { audible: true, playing: true, beat_sync_enabled: true },
		2: { audible: false, playing: true, beat_sync_enabled: true },
		3: { audible: true, playing: false, beat_sync_enabled: true },
		4: { audible: true, playing: true, beat_sync_enabled: false }
	};

	assert.deepEqual(audio.masterSwitchFollowers(1, activity), [2]);
	assert.deepEqual(audio.commonSyncScheduleTimes(12.4, 2), [12.4, 12.4]);
	assert.equal(audio.supersedingScheduleTime(12.4, 12.6), 12.4);
});

test('public controller exposes semantic tempo, sync, master, quantize, and analysis APIs', async () => {
	for (const method of [
		'setTempoRatio',
		'setQuantize',
		'setBeatSync',
		'setMasterTempo',
		'setSyncMode',
		'setDeckMaster',
		'quantizedSeek',
		'captureDeckAudio'
	]) {
		assert.equal(typeof audio.engine[method], 'function', `${method} must be public`);
	}

	audio.engine.setQuantize(1, false);
	assert.equal(audio.getDeckState(1).quantize_enabled, false);
	audio.engine.setQuantize(1, true);
	await audio.engine.setBeatSync(1, false);
	assert.equal(audio.getDeckState(1).beat_sync_enabled, false);
	await audio.engine.setBeatSync(1, true);
	audio.engine.setMasterTempo(1, false);
	assert.equal(audio.getDeckState(1).master_tempo_enabled, false);
	audio.engine.setMasterTempo(1, true);
	audio.engine.setSyncMode(1, 'bar');
	assert.equal(audio.getDeckState(1).sync_mode, 'bar');
	audio.engine.setSyncMode(1, 'beat');
	assert.throws(() => audio.engine.setSyncMode(1, 'phrase'), /sync mode/i);
	await assert.rejects(audio.engine.setDeckMaster(1), /no track loaded/i);
	assert.throws(() => audio.engine.captureDeckAudio(1), /no track loaded/i);
});
