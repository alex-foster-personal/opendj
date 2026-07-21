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

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts');
});

test('controller defaults enable quantize, Beat Sync, and Master Tempo with no static master', () => {
	for (const deck of audio.DECK_IDS) {
		const state = audio.getDeckState(deck);
		assert.equal(state.quantize_enabled, true);
		assert.equal(state.beat_sync_enabled, true);
		assert.equal(state.master_tempo_enabled, true);
		assert.equal(state.audible, false);
		assert.equal(state.transport_pending, false);
		assert.equal(state.sync_mode, 'beat');
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
