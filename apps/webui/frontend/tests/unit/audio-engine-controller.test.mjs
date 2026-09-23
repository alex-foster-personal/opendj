import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { readFrontendSource } from './engine-source.mjs';

const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 }
];
let audio;
let presentation;
let headphones;
let computeFollowerSyncPlan;
// The pure SLIP hidden-timeline math lives in the player's pure leaf
// (transport/schedule-math.ts); the two timeline-shaped helpers
// (presentedSlipAnchor, slipTempoBoundariesAfterAnchor) live in
// transport/presentation.ts and reach here through the engine barrel.
let slip;
let disposeAudioResources;
let beatLoopFitsWithinDuration;
let shiftLiveBeatLoopRangeMs;
let targetWithinShiftedLiveLoopMs;
let loopExitOnSeekMs;
let quantizedSeekDecisionMs;

// The engine reaches the daemon through the generated OpenAPI client, which
// builds a `new Request(url)` before any stub sees it. Node has no document to
// resolve a root-relative URL against (the browser, where `ssr = false` means
// this code only ever runs, does), so the module is loaded with an explicit
// base. Every URL check below is a suffix match and is unaffected by it.
const API_BASE = 'https://audio-engine.example.test';

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: API_BASE
	});
	presentation = await loadTypeScriptModule('src/lib/player/transport/presentation.ts');
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	({ disposeAudioResources } = await loadTypeScriptModule(
		'src/lib/rb/audio-resource-disposal.ts'
	));
	({ computeFollowerSyncPlan } = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts'));
	slip = await loadTypeScriptModule('src/lib/player/transport/slip-anchor.ts');
	({
		beatLoopFitsWithinDuration,
		shiftLiveBeatLoopRangeMs,
		targetWithinShiftedLiveLoopMs,
		loopExitOnSeekMs,
		quantizedSeekDecisionMs
	} = await loadTypeScriptModule('src/lib/player/transport/loops.ts'));
});

test('controller defaults enable quantize, Beat Sync, and Master Tempo with no static master', () => {
	for (const deck of audio.DECK_IDS) {
		const state = audio.getDeckState(deck);
		assert.equal(state.quantize_enabled, true);
		assert.equal(state.beat_sync_enabled, true);
		assert.equal(state.master_tempo_enabled, true);
		assert.equal(state.slip_enabled, false);
		assert.equal(state.slip_active, false);
		assert.equal(state.slip_position_ms, null);
		assert.equal(state.audible, false);
		assert.equal(state.transport_pending, false);
		assert.equal(state.sync_mode, 'bar');
		assert.equal(state.sync_error, null);
		assert.equal(state.processor_error, null);
		assert.equal(state.is_master, false);
	}
});

test('route teardown cancels animation, disconnects every graph resource, and closes context', async () => {
	const calls = [];
	const disconnectable = (name) => ({ disconnect: () => calls.push(`disconnect:${name}`) });
	const processor = (name) => ({
		...disconnectable(name),
		dispose: async () => calls.push(`dispose:${name}`)
	});
	const context = {
		state: 'running',
		close: async () => {
			calls.push('close:context');
		}
	};

	await disposeAudioResources(
		{
			rafId: 17,
			processors: [processor('processor-1'), processor('processor-2')],
			nodes: [disconnectable('deck-nodes')],
			masterGain: disconnectable('master'),
			context
		},
		(rafId) => calls.push(`cancel:${rafId}`)
	);

	assert.deepEqual(calls, [
		'cancel:17',
		'disconnect:processor-1',
		'disconnect:processor-2',
		'disconnect:deck-nodes',
		'disconnect:master',
		'dispose:processor-1',
		'dispose:processor-2',
		'close:context'
	]);
});

test('route teardown detaches old processor ownership before async disposal', () => {
	const processor = { disconnect() {} };
	const owner = { processor };

	assert.equal(audio.detachProcessorForDisposal(owner), processor);
	assert.equal(owner.processor, null);
});

test('route teardown propagates AudioContext close failures after audio is disconnected', async () => {
	const calls = [];
	const failure = new Error('close failed');
	const context = {
		state: 'running',
		close: async () => {
			calls.push('close');
			throw failure;
		}
	};

	await assert.rejects(
		disposeAudioResources(
			{
				rafId: null,
				processors: [{
					disconnect: () => calls.push('disconnect'),
					dispose: async () => calls.push('dispose')
				}],
				nodes: [],
				masterGain: null,
				context
			},
			() => calls.push('unexpected cancel')
		),
		failure
	);
	assert.deepEqual(calls, ['disconnect', 'dispose', 'close']);
});

test('one teardown failure cannot prevent later audio resources from being silenced', async () => {
	const calls = [];
	const failure = new Error('processor disconnect failed');

	await assert.rejects(
		disposeAudioResources(
			{
				rafId: null,
				processors: [
					{
							disconnect: () => {
								calls.push('disconnect:failed');
								throw failure;
							},
							dispose: async () => calls.push('dispose:failed')
						},
						{
							disconnect: () => calls.push('disconnect:later'),
							dispose: async () => calls.push('dispose:later')
						}
				],
				nodes: [{ disconnect: () => calls.push('disconnect:node') }],
				masterGain: { disconnect: () => calls.push('disconnect:master') },
				context: {
					state: 'running',
					close: async () => calls.push('close')
				}
			},
			() => calls.push('unexpected cancel')
		),
		failure
	);
	assert.deepEqual(calls, [
		'disconnect:failed',
		'disconnect:later',
		'disconnect:node',
		'disconnect:master',
		'dispose:failed',
		'dispose:later',
		'close'
	]);
});

test('engine disposal resets all route-owned reactive state and is safe without a graph', async () => {
	audio.engine.setQuantize(1, false);
	audio.engine.setTrim(1, 0.7);
	audio.engine.setCrossfader(0.2);
	audio.engine.setMaster(0.4);

	await audio.engine.dispose();

	assert.equal(audio.getDeckState(1).quantize_enabled, true);
	assert.equal(audio.getDeckState(1).stable_id, null);
	assert.equal(audio.mixerState.channels[1].trim, 0.5);
	assert.equal(audio.mixerState.crossfader, 0.5);
	assert.equal(audio.mixerState.master, 1);
});

test('Master Tempo preserves pitch while the disabled mode follows playback rate', () => {
	assert.equal(audio.masterTempoSemitones(1.1, true), 0);
	assert.ok(Math.abs(audio.masterTempoSemitones(1.1, false) - 12 * Math.log2(1.1)) < 1e-12);
	assert.throws(() => audio.masterTempoSemitones(0, true), /tempo ratio/i);
});

test('headphone cue/master mix uses equal-power gains and validates serializable output state', () => {
	assert.deepEqual(headphones.headphoneMixGains(0), { cue: 1, master: 0 });
	assert.deepEqual(headphones.headphoneMixGains(1), { cue: 0, master: 1 });
	const center = headphones.headphoneMixGains(0.5);
	assert.ok(Math.abs(center.cue - Math.SQRT1_2) < 1e-12);
	assert.ok(Math.abs(center.master - Math.SQRT1_2) < 1e-12);
	assert.throws(() => headphones.headphoneMixGains(1.1), /within 0\.\.1/i);
	assert.doesNotThrow(() =>
		headphones.assertHeadphoneOutputSelection('usb-headphones', [
			{ id: 'usb-headphones', label: 'USB Headphones' }
		])
	);
	assert.throws(
		() => headphones.assertHeadphoneOutputSelection('missing', [{ id: 'usb-headphones', label: '' }]),
		/enumerated headphone output/i
	);
	assert.deepEqual(
		headphones.mergeHeadphoneOutput([{ id: 'default', label: 'Default' }], {
			deviceId: 'usb-headphones',
			label: 'USB Headphones'
		}),
		[
			{ id: 'default', label: 'Default' },
			{ id: 'usb-headphones', label: 'USB Headphones' }
		]
	);
	assert.deepEqual(
		headphones.mergeHeadphoneOutput([{ id: 'usb-headphones', label: '' }], {
			deviceId: 'usb-headphones',
			label: 'USB Headphones'
		}),
		[{ id: 'usb-headphones', label: 'USB Headphones' }]
	);
});

test('headphone output refresh fails closed when browser device IDs rotate', () => {
	assert.deepEqual(
		headphones.reconcileHeadphoneOutputRefresh(
			true,
			'rotated-device-id',
			[{ id: 'current-device-id', label: 'USB Headphones' }]
		),
		{ active: false, selected_output_device_id: null }
	);
	assert.deepEqual(
		headphones.reconcileHeadphoneOutputRefresh(
			true,
			'current-device-id',
			[{ id: 'current-device-id', label: 'USB Headphones' }]
		),
		{ active: true, selected_output_device_id: 'current-device-id' }
	);
});

test('headphone selection declares sink, stream attach, play, then publish and rejects stale ownership', async () => {
	assert.deepEqual(headphones.headphoneSelectionStages(), ['setSinkId', 'attachStream', 'play', 'publish']);
	assert.equal(headphones.headphoneOwnershipIsCurrent(4, 4, true), true);
	assert.equal(headphones.headphoneOwnershipIsCurrent(4, 5, true), false);
	assert.equal(headphones.headphoneOwnershipIsCurrent(4, 4, false), false);
	assert.throws(() => headphones.assertHeadphoneOwnership(4, 5, true), /stale headphone operation/i);
	await assert.rejects(
		headphones.withHeadphoneOperationTimeout('enumerateDevices', new Promise(() => {}), 1),
		/enumerateDevices timed out/i
	);
	assert.equal(
		await headphones.withHeadphoneOperationTimeout('setSinkId', Promise.resolve('accepted'), 20),
		'accepted'
	);
});

test('headphone reselection keeps the previous monitor until a candidate commits', () => {
	assert.deepEqual(headphones.headphoneReselectionStages(), [
		'createCandidate',
		'setSinkId',
		'attachStream',
		'play',
		'replaceAndPublish',
		'detachPrevious'
	]);
	assert.deepEqual(headphones.headphoneReselectionResult(false), {
		replaceCurrentElement: false,
		publishSelection: false,
		detachPrevious: false
	});
	assert.deepEqual(headphones.headphoneReselectionResult(true), {
		replaceCurrentElement: true,
		publishSelection: true,
		detachPrevious: true
	});
});

test('paused tempo and Master Tempo settings persist into the next requested Signalsmith play', () => {
	const afterTempo = audio.applyPausedDeckControlSettings(
		{ tempoRatio: 1, masterTempoEnabled: true, keyShiftSemitones: 2 },
		{ tempoRatio: 1.08 }
	);
	const afterMasterTempo = audio.applyPausedDeckControlSettings(afterTempo, {
		masterTempoEnabled: false
	});

	assert.deepEqual(afterMasterTempo, {
		tempoRatio: 1.08,
		masterTempoEnabled: false,
		keyShiftSemitones: 2
	});
	const nextPlay = audio.stretchScheduleChange(
		0,
		true,
		afterMasterTempo.tempoRatio,
		afterMasterTempo.masterTempoEnabled,
		afterMasterTempo.keyShiftSemitones,
		null
	);
	assert.equal(nextPlay.rate, 1.08);
	assert.ok(Math.abs(nextPlay.semitones - (12 * Math.log2(1.08) + 2)) < 1e-12);
});

test('arming SLIP alone does not alter the paused transport read model', async () => {
	const before = { ...audio.getDeckState(1) };
	await audio.engine.setSlip(1, true);
	const armed = audio.getDeckState(1);
	assert.equal(armed.slip_enabled, true);
	assert.equal(armed.slip_active, false);
	assert.equal(armed.slip_position_ms, null);
	assert.equal(armed.position_ms, before.position_ms);
	assert.equal(armed.transport_pending, before.transport_pending);
	await audio.engine.setSlip(1, false);
});

test('Camelot Key Sync models all AlphaTheta least-change compatibility families', () => {
	assert.deepEqual(audio.parseCamelotKey('8A'), { number: 8, mode: 'A', root: 9 });
	assert.equal(audio.parseCamelotKey('13A'), null);
	assert.equal(audio.parseCamelotKey('8C'), null);

	// Same-mode: same, clockwise, anti-clockwise. Cross-mode: same, clockwise,
	// anti-clockwise. Numbering is circular at the 1/12 boundary.
	for (const [deck, master] of [
		['8A', '8A'],
		['8A', '9A'],
		['8A', '7A'],
		['8A', '8B'],
		['8A', '9B'],
		['8A', '7B'],
		['1A', '12B']
	]) {
		assert.equal(audio.camelotKeysAreCompatible(deck, master), true, `${deck}/${master}`);
		assert.equal(audio.deriveKeySyncNudge(deck, master, 0, 0, 0), 0, `${deck}/${master}`);
	}
	assert.equal(audio.camelotKeysAreCompatible('8A', '10B'), false);

	// A deck two wheel numbers away requires the least transposition.
	assert.equal(audio.deriveKeySyncNudge('8A', '10A', 0, 0, 0), 2);
	assert.throws(() => audio.deriveKeySyncNudge('not-a-key', '8B', 0, 0, 0), /Camelot/i);
});

test('KEY SYNC compares effective audible Signalsmith offsets when Master Tempo is off', () => {
	const deckEffective = audio.composeStretchSemitones(1.1, false, 0);
	const masterEffective = audio.composeStretchSemitones(0.9, false, 2);
	const nudge = audio.deriveKeySyncNudge('8A', '10A', deckEffective, masterEffective, 0);
	assert.equal(Number.isInteger(nudge), true);
	assert.ok(nudge >= -12 && nudge <= 12);
	assert.equal(
		audio.deriveKeySyncNudge('8A', '8A', 0, 0, 12),
		0,
		'already-compatible manual shift remains untouched'
	);
	assert.throws(
		() => audio.deriveKeySyncNudge('8A', '8A', Number.NaN, 0, 0),
		/effective audible semitones/i
	);
});

test('key shift state remains on its presented revision until output crosses its schedule', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 10,
		startPositionSec: 0,
		tempoRatio: 1,
		masterTempoEnabled: true,
		keyShiftSemitones: 0
	});
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: true,
		loop: null,
		startContextTime: 20,
		startPositionSec: 10,
		tempoRatio: 1,
		masterTempoEnabled: true,
		keyShiftSemitones: 3
	});
	assert.equal(audio.presentedKeyShiftSemitonesAt(timeline, 15), 0);
	assert.equal(audio.presentedKeyShiftSemitonesAt(timeline, 20), 3);

	// Existing output-clock evaluation still owns publication, not command time.
	audio.observePresentedTransportTimeline(timeline, { contextTime: 15, performanceTime: 15_000 }, 60);
	assert.equal(audio.presentedKeyShiftSemitonesAt(timeline, 15), 0);
});

test('KEY SYNC reads effective offsets from the last output-presented schedule only', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 4,
		startPositionSec: 0,
		tempoRatio: 1.1,
		masterTempoEnabled: false,
		keyShiftSemitones: 2
	});
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: true,
		loop: null,
		startContextTime: 10,
		startPositionSec: 6,
		tempoRatio: 0.9,
		masterTempoEnabled: false,
		keyShiftSemitones: -1
	});
	audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 5, performanceTime: 5_000 },
		60
	);

	assert.ok(
		Math.abs(presentation.presentedEffectiveAudibleSemitones(timeline) - (12 * Math.log2(1.1) + 2)) <
			1e-12,
		'the unpresented revision at contextTime 10 must not affect KEY SYNC'
	);
	assert.throws(
		() => presentation.presentedEffectiveAudibleSemitones(audio.createPresentedTransportTimeline(0)),
		/presentation truth/i
	);
});

test('KEY SYNC uses desired controls for a fresh paused target and output truth for its audible master', () => {
	const freshPaused = audio.createPresentedTransportTimeline(0);
	const audibleMaster = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(audibleMaster, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 2,
		startPositionSec: 0,
		tempoRatio: 0.9,
		masterTempoEnabled: false,
		keyShiftSemitones: -1
	});
	audio.observePresentedTransportTimeline(
		audibleMaster,
		{ contextTime: 3, performanceTime: 3_000 },
		60
	);

	const targetEffective = audio.keySyncEffectiveAudibleSemitones({
		audible: false,
		transportPending: false,
		pendingMutation: false,
		control: { tempoRatio: 1.1, masterTempoEnabled: false, keyShiftSemitones: 2 },
		presentation: freshPaused
	});
	const masterEffective = audio.keySyncEffectiveAudibleSemitones({
		audible: true,
		transportPending: false,
		pendingMutation: false,
		control: { tempoRatio: 1.2, masterTempoEnabled: false, keyShiftSemitones: 7 },
		presentation: audibleMaster
	});
	assert.ok(Math.abs(targetEffective - (12 * Math.log2(1.1) + 2)) < 1e-12);
	assert.ok(
		Math.abs(masterEffective - (12 * Math.log2(0.9) - 1)) < 1e-12,
		'the audible master must ignore its render/control settings'
	);
	assert.equal(Number.isInteger(audio.deriveKeySyncNudge('8A', '9B', targetEffective, masterEffective, 2)), true);
	const targetManualShift = audio.deriveKeySyncTargetManualShift(
		'8A',
		'10A',
		targetEffective,
		masterEffective,
		2
	);
	assert.equal(Number.isInteger(targetManualShift), true);
	assert.ok(
		targetManualShift >= -12 && targetManualShift <= 12,
		'authoritative target keeps an existing manual shift within the real DSP range'
	);
});

test('KEY SYNC preview is explicitly unavailable with no elected master', () => {
	assert.equal(audio.keySyncPreview(1), null);
});

test('KEY SYNC supports two fresh paused loaded decks but rejects live or pending decks without presentation truth', () => {
	const freshDeck = (control) =>
		audio.keySyncEffectiveAudibleSemitones({
			audible: false,
			transportPending: false,
			pendingMutation: false,
			control,
			presentation: audio.createPresentedTransportTimeline(0)
		});
	const deckEffective = freshDeck({
		tempoRatio: 1.08,
		masterTempoEnabled: false,
		keyShiftSemitones: 1
	});
	const masterEffective = freshDeck({
		tempoRatio: 0.96,
		masterTempoEnabled: false,
		keyShiftSemitones: -2
	});
	assert.ok(Math.abs(deckEffective - (12 * Math.log2(1.08) + 1)) < 1e-12);
	assert.ok(Math.abs(masterEffective - (12 * Math.log2(0.96) - 2)) < 1e-12);
	assert.equal(Number.isInteger(audio.deriveKeySyncNudge('8A', '8A', deckEffective, masterEffective, 1)), true);

	for (const [label, activity] of [
		['live', { audible: true, transportPending: false, pendingMutation: false }],
		['pending', { audible: false, transportPending: true, pendingMutation: true }]
	]) {
		assert.throws(
			() =>
				audio.keySyncEffectiveAudibleSemitones({
					...activity,
					control: { tempoRatio: 1, masterTempoEnabled: true, keyShiftSemitones: 0 },
					presentation: audio.createPresentedTransportTimeline(0)
				}),
			/presentation truth/i,
			label
		);
	}
});

test('legacy Key Sync helper remains a zero-offset convenience wrapper', () => {
	assert.equal(audio.deriveKeySyncSemitones('8A', '8B'), 0);
	assert.equal(audio.deriveKeySyncSemitones('8A', '10A'), 2);
});

test('key shift composes with Master Tempo compensation in the native Signalsmith semitones field', () => {
	assert.equal(
		audio.stretchScheduleChange(4, true, 1.1, true, 3, null).semitones,
		3
	);
	assert.ok(
		Math.abs(
			audio.stretchScheduleChange(4, true, 1.1, false, -2, null).semitones -
				(12 * Math.log2(1.1) - 2)
		) <
			1e-12
	);
	assert.throws(() => audio.composeStretchSemitones(1, true, 1.5), /integer/i);
	assert.throws(() => audio.composeStretchSemitones(1, true, 13), /-12\.\.12/i);

	// composeStretchSemitones(1, true, n) returns exactly n for n in -12..12
	for (let n = -12; n <= 12; n++) {
		assert.equal(audio.composeStretchSemitones(1, true, n), n);
	}

	// composeStretchSemitones(ratio, false, n) adds 12*log2(ratio)
	const ratio = 1.08;
	const baseMT = 12 * Math.log2(ratio);
	for (let n = -12; n <= 12; n++) {
		const composed = audio.composeStretchSemitones(ratio, false, n);
		assert.ok(Math.abs(composed - (baseMT + n)) < 1e-12);

		if (n < 12) {
			assert.ok(
				Math.abs(
					audio.composeStretchSemitones(ratio, false, n + 1) -
						audio.composeStretchSemitones(ratio, false, n) -
						1
				) < 1e-12
			);
		}
	}

	// stretchScheduleChange carries that composed semitones value into the worklet change object
	const change = audio.stretchScheduleChange(1.5, true, ratio, false, 2, null);
	assert.ok(Math.abs(change.semitones - (baseMT + 2)) < 1e-12);
});

test('Slip hidden playhead advances linearly from its acknowledged loop schedule without wrapping', () => {
	const anchor = slip.createSlipAnchor({
		startContextTime: 10,
		startPositionSec: 30,
		tempoRatio: 1.25,
		durationSec: 120
	});
	assert.equal(slip.slipHiddenPositionSec(anchor, 10), 30);
	assert.equal(slip.slipHiddenPositionSec(anchor, 14), 35);
	assert.equal(slip.slipHiddenPositionSec(anchor, 200), 120);
	assert.throws(
		() => slip.createSlipAnchor({ startContextTime: 1, startPositionSec: 2, tempoRatio: 0, durationSec: 3 }),
		/tempoRatio must be positive/i
	);
});

test('SLIP activation carries acknowledged future tempo boundaries through loop release without duplicates', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: { in_ms: 10_000, out_ms: 12_000, engaged: true, beat_length: 4 },
		startContextTime: 10,
		startPositionSec: 10,
		tempoRatio: 1,
		masterTempoEnabled: true,
		keyShiftSemitones: 0
	});
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: true,
		loop: { in_ms: 10_000, out_ms: 12_000, engaged: true, beat_length: 4 },
		startContextTime: 15,
		startPositionSec: 15,
		tempoRatio: 2,
		masterTempoEnabled: true,
		keyShiftSemitones: 0
	});
	// A same-boundary supersession must replace, rather than double-apply, the prior rate.
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 3,
		active: true,
		loop: { in_ms: 10_000, out_ms: 12_000, engaged: true, beat_length: 4 },
		startContextTime: 15,
		startPositionSec: 15,
		tempoRatio: 1.5,
		masterTempoEnabled: true,
		keyShiftSemitones: 0
	});
	audio.observePresentedTransportTimeline(timeline, { contextTime: 11, performanceTime: 11_000 }, 120);
	const anchor = audio.presentedSlipAnchor(timeline, 120);
	const boundaries = audio.slipTempoBoundariesAfterAnchor(timeline, anchor);
	assert.deepEqual(boundaries, [{ startContextTime: 15, tempoRatio: 1.5 }]);
	assert.equal(
		slip.slipHiddenPositionWithTempoBoundaries(anchor, boundaries, 17),
		18,
		'loop release at 17s resumes 4s at the old rate plus 2s at 1.5x'
	);
});

test('KEY SYNC uses the same presented manual-shift baseline for pending desired state', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 10,
		startPositionSec: 0,
		tempoRatio: 1,
		masterTempoEnabled: true,
		keyShiftSemitones: 0
	});
	audio.observePresentedTransportTimeline(timeline, { contextTime: 11, performanceTime: 11_000 }, 60);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 2,
		active: true,
		loop: null,
		startContextTime: 15,
		startPositionSec: 5,
		tempoRatio: 1,
		masterTempoEnabled: true,
		keyShiftSemitones: 1
	});
	const source = {
		audible: true,
		transportPending: true,
		pendingMutation: true,
		control: { tempoRatio: 1, masterTempoEnabled: true, keyShiftSemitones: 1 },
		presentation: timeline
	};
	const baseline = audio.keySyncManualShiftBaseline(source);
	const target = audio.deriveKeySyncTargetManualShift(
		'8A',
		'10A',
		audio.keySyncEffectiveAudibleSemitones(source),
		0,
		baseline
	);
	assert.equal(baseline, 0);
	assert.equal(target, 2, 'the second unpresented command must not compound the pending +1 into +3');
});
test('Slip activation is limited to playing decks with SLIP enabled', () => {
	assert.equal(slip.shouldActivateSlip(true, true), true);
	assert.equal(slip.shouldActivateSlip(false, true), false);
	assert.equal(slip.shouldActivateSlip(true, false), false);
});

test('central seek quantization snaps to real PQTZ and missing grids fail explicitly', () => {
	assert.equal(audio.quantizedPositionMs(REAL_PQTZ_BEATS, 590, true), 608);
	assert.equal(audio.quantizedPositionMs(REAL_PQTZ_BEATS, 590, false), 590);
	assert.throws(() => audio.quantizedPositionMs([], 590, true), /beat grid/i);
});

/**
 * Pin a67bafbfc4b0 follow-up (bot review P1): the deck's selected quantize
 * grid (1/4/8 beats) must actually change what a seek/cue/loop snaps to, not
 * just sit in deckStates unread. A regular 120bpm, 8-bar grid (32 beats)
 * gives every-beat, every-bar-downbeat, and every-OTHER-bar-downbeat targets
 * that all differ at position 5.4s: nearest beat 5.5, nearest downbeat 6.0,
 * nearest 2-bar downbeat 4.0. If gridBeats stopped being threaded through
 * (e.g. a caller reverting to the 1-only default) this test would see grid 4
 * and grid 8 collapse back onto the grid-1 answer.
 */
function _regularGrid(bpm, beatCount) {
	const beatIntervalSec = 60 / bpm;
	return Array.from({ length: beatCount }, (_, index) => ({
		n: (index % 4) + 1,
		bpm,
		t: index * beatIntervalSec
	}));
}
const GRID_120BPM_8BAR = _regularGrid(120, 32);

test('quantizedPositionMs snaps to a different beat depending on the selected grid (1 vs 4 vs 8)', () => {
	assert.equal(audio.quantizedPositionMs(GRID_120BPM_8BAR, 5400, true, 1), 5500);
	assert.equal(audio.quantizedPositionMs(GRID_120BPM_8BAR, 5400, true, 4), 6000);
	assert.equal(audio.quantizedPositionMs(GRID_120BPM_8BAR, 5400, true, 8), 4000);
	// Unchanged when quantize is off, regardless of which grid is selected.
	assert.equal(audio.quantizedPositionMs(GRID_120BPM_8BAR, 5400, false, 8), 5400);
	// Omitting gridBeats still defaults to the old grid-1 behaviour.
	assert.equal(audio.quantizedPositionMs(GRID_120BPM_8BAR, 5400, true), 5500);
});

test('quantizedLoopEndpointsMs snaps both endpoints to the selected grid, not always grid-1', () => {
	const loop = { in_ms: 5400, out_ms: 9900 };
	assert.deepEqual(audio.quantizedLoopEndpointsMs(GRID_120BPM_8BAR, loop, true, 1), {
		in_ms: 5500,
		out_ms: 10000
	});
	assert.deepEqual(audio.quantizedLoopEndpointsMs(GRID_120BPM_8BAR, loop, true, 4), {
		in_ms: 6000,
		out_ms: 10000
	});
	assert.deepEqual(audio.quantizedLoopEndpointsMs(GRID_120BPM_8BAR, loop, true, 8), {
		in_ms: 4000,
		out_ms: 8000
	});
});

/**
 * Pin 334a50710ef0 defect A: a beat-jump target landing exactly on a shifted
 * live loop's EXCLUSIVE out boundary must not disengage the loop. This is
 * the exact composition `quantizedSeek` runs for beatJump's in-loop branch:
 * shift the loop, pull the target off the exclusive boundary
 * (targetWithinShiftedLiveLoopMs), then decide whether to exit
 * (loopExitOnSeekMs) using the FINAL seek target quantizedSeek would use.
 *
 * A regular 4/4, 500ms/beat, 4-bar grid (16 beats): a 4-beat loop shifted by
 * 8 beats lands at {in:4000, out:6000}; the beat jump's raw target is chosen
 * to equal that new out (6000) exactly, so targetWithinShiftedLiveLoopMs
 * pulls it back one real beat to 5500.
 */
const GRID_4BAR_500MS = _regularGrid(120, 16);

test('a beat jump exactly at a shifted loop out boundary must not disengage the loop', () => {
	const shiftedLoop = shiftLiveBeatLoopRangeMs(GRID_4BAR_500MS, { in_ms: 0, out_ms: 2000 }, 8, 10_000);
	assert.deepEqual(shiftedLoop, { in_ms: 4000, out_ms: 6000 });
	const inLoopTargetMs = targetWithinShiftedLiveLoopMs(GRID_4BAR_500MS, 6000, shiftedLoop);
	assert.equal(inLoopTargetMs, 5500, 'exclusive out must resolve to the preceding real beat');
	const engagedLoop = { ...shiftedLoop, engaged: true };

	// This is the exact call quantizedSeek makes (production quantizedSeekDecisionMs,
	// not a reimplementation): gridBeats=4 is the deck's own coarser quantize
	// grid (nearest bar downbeat). Without skipGridQuantize, 5500ms is nearer
	// the 6000ms downbeat than the 4000ms one, so the deck's own coarser grid
	// would resnap the already-safe in-loop target right back onto the
	// excluded out boundary and exit the loop - the reproduction of the bug.
	const withoutFix = quantizedSeekDecisionMs(GRID_4BAR_500MS, inLoopTargetMs, 4, false, engagedLoop);
	assert.equal(withoutFix.targetMs, 6000, 'the coarse grid resnaps the safe target back onto the out boundary');
	assert.equal(withoutFix.exitLoop, true, 'the pre-fix composition disengages the loop - defect A reproduction');

	// beatJump's in-loop branch calls quantizedSeek with skipGridQuantize=true,
	// so the coarse-grid re-quantization above never runs and the exact
	// in-loop target reaches the exit decision unchanged.
	const withFix = quantizedSeekDecisionMs(GRID_4BAR_500MS, inLoopTargetMs, 4, true, engagedLoop);
	assert.equal(withFix.targetMs, 5500, 'skipGridQuantize passes the exact beat-jump target through unchanged');
	assert.equal(withFix.exitLoop, false, 'skipping the coarse re-quantize keeps the beat jump inside the shifted loop');

	// quantizedSeekDecisionMs delegates its exit call to loopExitOnSeekMs - assert
	// the two agree directly on both targets rather than trusting it silently.
	assert.equal(loopExitOnSeekMs(withoutFix.targetMs, engagedLoop), withoutFix.exitLoop);
	assert.equal(loopExitOnSeekMs(withFix.targetMs, engagedLoop), withFix.exitLoop);
});

// Blinded-reviewer P0, pin 334a50710ef0: defect A resurfaces for exactly the
// loops defect B exists to preserve. beatJumpTargetMs always returns a
// grid-EXACT time, but a preserved manual endpoint can be off-grid - an
// exact-equality check against loop.out_ms (the pre-fix contract) can never
// fire for an off-grid out, and the mirrored case (an off-grid in) was never
// checked at all. Real PQTZ grid, drifting BPM, same fixture as
// deck-loop-beatjump.test.mjs: beats at 135/608/1080/1553/2040/2530/3030/3540/4060ms.
const DRIFTING_GRID_FOR_BOUNDARY = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 126, t: 2.04 },
	{ n: 2, bpm: 126, t: 2.53 },
	{ n: 3, bpm: 126, t: 3.03 },
	{ n: 4, bpm: 126, t: 3.54 },
	{ n: 1, bpm: 125, t: 4.06 }
];

test('a forward beat jump onto an off-grid loop-out must not disengage the loop', () => {
	// out_ms sits 10ms EARLY of its nearest real beat (3540ms) - a manual
	// nudge preserved by defect B's fix. The beat jump's own target is always
	// grid-exact, so it lands on 3540 itself, never on the off-grid 3530.
	const shiftedLoop = { in_ms: 1553, out_ms: 3530 };

	// Pre-fix contract: only an EXACT match against loop.out_ms pulled the
	// target back. 3540 !== 3530, so the exact-equality check never fires and
	// the grid-exact target passes straight through.
	assert.equal(
		loopExitOnSeekMs(3540, { ...shiftedLoop, engaged: true }),
		true,
		'an uncorrected grid-exact target 10ms past an off-grid out disengages the loop - the P0 reproduction'
	);

	// Fixed contract: targetWithinShiftedLiveLoopMs compares against the real
	// (possibly off-grid) out_ms with < / >=, not equality, so it still pulls
	// back to the preceding real beat (3030ms).
	const correctedMs = targetWithinShiftedLiveLoopMs(DRIFTING_GRID_FOR_BOUNDARY, 3540, shiftedLoop);
	assert.equal(correctedMs, 3030, 'must pull back to the last real beat before the off-grid out, not 3540');
	assert.equal(loopExitOnSeekMs(correctedMs, { ...shiftedLoop, engaged: true }), false);
});

test('a backward beat jump onto an off-grid loop-in must not disengage the loop', () => {
	// in_ms sits 10ms LATE of its nearest real beat (1553ms) - the symmetric
	// manual nudge. The beat jump's own target is grid-exact, so a backward
	// jump lands on 1553 itself, never on the off-grid 1563.
	const shiftedLoop = { in_ms: 1563, out_ms: 3540 };

	// Pre-fix contract had no in-side correction at all: 1553 passes straight
	// through and is strictly less than the off-grid in (1563).
	assert.equal(
		loopExitOnSeekMs(1553, { ...shiftedLoop, engaged: true }),
		true,
		'an uncorrected grid-exact target 10ms before an off-grid in disengages the loop - the symmetric P0 case'
	);

	const correctedMs = targetWithinShiftedLiveLoopMs(DRIFTING_GRID_FOR_BOUNDARY, 1553, shiftedLoop);
	assert.equal(correctedMs, 2040, 'must pull forward to the first real beat at or after the off-grid in, not 1553');
	assert.equal(loopExitOnSeekMs(correctedMs, { ...shiftedLoop, engaged: true }), false);
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

test('sync changes reschedule desired pending starts before they become audible', () => {
	assert.equal(audio.syncChangeRequiresReschedule(2, true, true, 1), true);
	assert.equal(audio.syncChangeRequiresReschedule(2, false, true, 1), false);
	assert.equal(audio.syncChangeRequiresReschedule(2, true, false, 1), false);
	assert.equal(audio.syncChangeRequiresReschedule(1, true, true, 1), false);
	assert.equal(audio.syncChangeRequiresReschedule(2, true, true, null), false);
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

test('loop endpoints clamp to decoded duration near EOF without hiding an empty loop', () => {
	assert.deepEqual(
		audio.loopEndpointsWithinDurationMs({ in_ms: 9_000, out_ms: 13_000 }, 10_000),
		{ in_ms: 9_000, out_ms: 10_000 }
	);
	assert.deepEqual(
		audio.loopEndpointsWithinDurationMs({ in_ms: 1_000, out_ms: 3_000 }, 10_000),
		{ in_ms: 1_000, out_ms: 3_000 }
	);
	assert.throws(
		() => audio.loopEndpointsWithinDurationMs({ in_ms: 10_000, out_ms: 13_000 }, 10_000),
		/empty at decoded duration/i
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

	assert.equal(timeline.schedules.length, 1, 'superseded schedules are pruned immediately');
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

test('presented timeline prunes long-session history but retains current and future revisions', () => {
	const timeline = audio.createPresentedTransportTimeline(0);
	for (let revision = 1; revision <= 10_000; revision += 1) {
		audio.acknowledgePresentedTransportSchedule(timeline, {
			revision,
			active: true,
			loop: null,
			startContextTime: revision,
			startPositionSec: revision,
			tempoRatio: 1
		});
		audio.observePresentedTransportTimeline(
			timeline,
			{ contextTime: revision + 0.25, performanceTime: (revision + 0.25) * 1000 },
			20_000
		);
		assert.ok(timeline.schedules.length <= 1, `revision ${revision} leaked schedule history`);
	}

	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 10_001,
		active: false,
		loop: null,
		startContextTime: 10_002,
		startPositionSec: 10_001.25,
		tempoRatio: 1
	});
	assert.deepEqual(
		timeline.schedules.map(({ revision }) => revision),
		[10_000, 10_001],
		'the effective current revision and needed future revision remain mirrored'
	);
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

test('zero context output timestamps preserve the frozen cursor and pending start', () => {
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

	const warmupObservation = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 0, performanceTime: 4123.5 },
		10
	);
	assert.equal(warmupObservation.accepted, false);
	assert.equal(warmupObservation.output_started, false);
	assert.equal(warmupObservation.presentation_context_time_s, null);
	assert.equal(warmupObservation.position_sec, 0.75);
	assert.equal(warmupObservation.audible, false);
	assert.equal(warmupObservation.transport_pending, true);
	assert.equal(timeline.presented_revision, 0);
});

test('positive context output timestamp presents even before performance correlation is available', () => {
	const timeline = audio.createPresentedTransportTimeline(0.5);
	audio.acknowledgePresentedTransportSchedule(timeline, {
		revision: 1,
		active: true,
		loop: null,
		startContextTime: 1,
		startPositionSec: 0.5,
		tempoRatio: 1
	});

	const observation = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 1.25, performanceTime: 0 },
		10
	);
	assert.equal(observation.accepted, true);
	assert.equal(observation.output_started, true);
	assert.equal(observation.presentation_context_time_s, 1.25);
	assert.equal(observation.position_sec, 0.75);
	assert.equal(observation.audible, true);
	assert.equal(observation.transport_pending, false);
	assert.equal(observation.presented_revision, 1);
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
	const laterContextWithResetCorrelation = audio.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 4, performanceTime: 0 },
		20
	);
	assert.equal(laterContextWithResetCorrelation.accepted, true);
	assert.equal(laterContextWithResetCorrelation.output_started, true);
	assert.equal(laterContextWithResetCorrelation.position_sec, 4);
	assert.equal(laterContextWithResetCorrelation.presented_revision, 2);
	assert.throws(
		() =>
			audio.observePresentedTransportTimeline(
				timeline,
				{ contextTime: Number.NaN, performanceTime: 4000 },
				20
			),
		/output timestamp/i
	);
});

test('transport scheduling horizon is strictly future and latency-aware', () => {
	assert.ok(Math.abs(audio.safeTransportScheduleTime(10, 0.2, 0.1) - 10.3) < 1e-12);
	assert.throws(() => audio.safeTransportScheduleTime(10, -0.1), /processorLeadSec/);
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

test('pending pause transport mutations stay scheduled instead of touching the frozen cursor', () => {
	assert.equal(
		audio.transportNeedsScheduledMutation({
			playing: false,
			audible: true,
			controlActive: true,
			pendingScheduleCount: 1,
			presentationPending: false,
			scheduleIntentCount: 0
		}),
		true,
		'pause then play or seek must supersede the pending stop'
	);
	assert.equal(
		audio.transportNeedsScheduledMutation({
			playing: false,
			audible: false,
			controlActive: false,
			pendingScheduleCount: 0,
			presentationPending: false,
			scheduleIntentCount: 0
		}),
		false,
		'a fully paused deck may move its frozen cursor directly'
	);
	assert.throws(
		() =>
			audio.transportNeedsScheduledMutation({
				playing: false,
				audible: false,
				controlActive: false,
				pendingScheduleCount: -1,
				presentationPending: false,
				scheduleIntentCount: 0
			}),
		/pendingScheduleCount/i
	);
});

test('KEY nudge keeps an acknowledged pending stop on the scheduled output path', () => {
	const playingPlan = audio.planKeyShiftMutation(
		{
			playing: true,
			audible: true,
			controlActive: true,
			pendingScheduleCount: 0,
			presentationPending: false,
			scheduleIntentCount: 0
		},
		true,
		1
	);
	assert.deepEqual(playingPlan, {
		kind: 'scheduled',
		active: true,
		publishedKeyShiftSemitones: null
	});

	const pendingStop = audio.planKeyShiftMutation(
		{
			playing: false,
			audible: true,
			controlActive: true,
			pendingScheduleCount: 1,
			presentationPending: false,
			scheduleIntentCount: 0
		},
		false,
		3
	);
	assert.deepEqual(pendingStop, {
		kind: 'scheduled',
		active: false,
		publishedKeyShiftSemitones: null
	});

	assert.deepEqual(
		audio.planKeyShiftMutation(
			{
				playing: false,
				audible: false,
				controlActive: false,
				pendingScheduleCount: 0,
				presentationPending: false,
				scheduleIntentCount: 0
			},
			false,
			3
		),
		{ kind: 'immediate', active: false, publishedKeyShiftSemitones: 3 }
	);
});

/**
 * A clock whose only motion comes from the wait's own `sleep` calls, so
 * virtual time advances by exactly the delays the wait schedules and by
 * nothing else. The bounded-interval assertions below then measure the wait's
 * scheduling arithmetic rather than how punctually a loaded machine delivers
 * a `setTimeout` callback: the real 20ms poll was measured overshooting to
 * 395ms in a full-file run on a saturated host, which is a fact about the
 * host, not about the code under test.
 *
 * `sleepBudget` is the fail-fast fuse. Virtual sleeps cost no real time, so a
 * wait that has lost its give-up condition would spin forever instead of
 * failing; the budget turns that into a named error.
 */
function virtualContextWaitClock({ sleepBudget = 4096 } = {}) {
	let nowMs = 0;
	let sleeps = 0;
	return {
		nowMs: () => nowMs,
		sleep: async (ms) => {
			sleeps += 1;
			if (sleeps > sleepBudget) {
				throw new Error(
					`virtual clock exhausted after ${sleepBudget} sleeps; the wait never gave up`
				);
			}
			nowMs += ms;
		},
		elapsedMs: () => nowMs,
		sleepCount: () => sleeps
	};
}

test('context-time waits reject suspended, stale, and stalled clocks within a bounded interval', async () => {
	// The default clock is the real one, and these three reject before any
	// sleep, so they exercise it end to end without waiting on a timer.
	await assert.rejects(
		audio.waitForAdvancingContextTime({ currentTime: 0, state: 'suspended' }, 1),
		/not running/i
	);
	await assert.rejects(
		audio.waitForAdvancingContextTime({ currentTime: Number.NaN, state: 'running' }, 1),
		/finite and non-negative/i
	);
	await assert.rejects(
		audio.waitForAdvancingContextTime(
			{ currentTime: 0, state: 'running' },
			1,
			() => false,
			20
		),
		/state changed/i
	);
	// The one part of the real clock the rejections above cannot reach.
	assert.ok(Number.isFinite(audio.REAL_CONTEXT_WAIT_CLOCK.nowMs()));
	await audio.REAL_CONTEXT_WAIT_CLOCK.sleep(1);

	// A context whose time never moves: the wait must give up inside its stall
	// timeout instead of polling all the way to the target.
	const stalled = virtualContextWaitClock();
	await assert.rejects(
		audio.waitForAdvancingContextTime(
			{ currentTime: 0, state: 'running' },
			1,
			() => true,
			20,
			stalled
		),
		/stalled/i
	);
	assert.ok(
		stalled.elapsedMs() <= 20,
		`stalled context wait exceeded its bounded interval: waited ${stalled.elapsedMs()}ms ` +
			'against a 20ms stall timeout'
	);
	assert.ok(stalled.sleepCount() > 0, 'the stalled wait must poll at least once before giving up');

	// A context that advances with the clock: every poll sees progress, so the
	// wait rides out ten stall timeouts' worth of time and resolves at the
	// target rather than declaring a stall.
	const advancing = virtualContextWaitClock();
	const advancingContext = {
		get currentTime() {
			return advancing.elapsedMs() / 1000;
		},
		state: 'running'
	};
	await audio.waitForAdvancingContextTime(advancingContext, 1, () => true, 100, advancing);
	assert.equal(advancing.elapsedMs(), 1000, 'the wait must stop at the target, not past it');
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

test('beatLoopFitsWithinDuration answers the same fit question without throwing, plus duration', () => {
	// Mirrors the two cases above: a length that fits from the anchor, and
	// one that runs past the end of the grid because too few beats remain.
	assert.equal(beatLoopFitsWithinDuration(REAL_PQTZ_BEATS, 1080, 2, 2000, 608), true);
	assert.equal(beatLoopFitsWithinDuration(REAL_PQTZ_BEATS, 1080, 4, 2000, 1080), false);
	// A grid-only fit that would still get clipped by the decoded duration
	// (the loop's own out_ms of 1553ms lands past a 1000ms duration) must
	// also report false, not just a bare grid-length fit.
	assert.equal(beatLoopFitsWithinDuration(REAL_PQTZ_BEATS, 1080, 2, 1000, 608), false);
	// An unusable grid disables the choice rather than throwing mid-render,
	// matching beatJumpMovesTransportWithinDuration's contract for the same class of call.
	assert.equal(beatLoopFitsWithinDuration([], 0, 4, 1000), false);
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

test('analysis retrieval failure rejects before an unusable deck candidate can publish', async () => {
	const originalFetch = globalThis.fetch;
	const originalSetTimeout = globalThis.setTimeout;
	// Two timers are legitimate on this path: the toast expiry, and the perf
	// ring's coalesced localStorage flush, which replaced the synchronous
	// stringify + setItem that used to run per recorded row. Collected rather
	// than asserted inline so a stray timer names itself in the diff.
	const timerDelays = [];
	globalThis.setTimeout = (_callback, delay) => {
		timerDelays.push(delay);
		return 0;
	};
	globalThis.fetch = async (input) => {
		// Generated-client calls arrive as a Request; api-rb's own raw fetches
		// still arrive as a URL string.
		const url = input instanceof Request ? input.url : String(input);
		if (url.endsWith('/hot-cues')) {
			return new Response(
				JSON.stringify(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({
					slot,
					cue: null,
					revision: `empty-${slot}`
				}))),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			);
		}
		// includes(), not endsWith(): fetchAnlz appends a `gen=` cache-buster
		// (anlz-fetch-generation.ts, PARITY-02 discussion_r3921839825), and
		// sends points= only when a caller or the server-seeded default set
		// one (#3739).
		if (url.includes('/anlz?')) {
			return new Response(
				JSON.stringify({
					detail: { code: 'ANALYSIS_NOT_FOUND', message: 'track has no analysis' }
				}),
				{ status: 404, headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.endsWith('/audio')) return new Response(new Uint8Array([1, 2, 3]));
		if (url.endsWith('/tracks/no-analysis/stems')) {
			return new Response(
				JSON.stringify({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }),
				{ status: 404, headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.endsWith('/tracks/no-analysis')) {
			return new Response(JSON.stringify({ stable_id: 'no-analysis' }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		throw new Error(`unexpected request ${url}`);
	};

	try {
		await assert.rejects(
			audio.engine.load(1, 'no-analysis'),
			(error) =>
				error?.name === 'RbApiError' &&
				error.code === 'ANALYSIS_NOT_FOUND' &&
				/track has no analysis/.test(error.message)
		);
		assert.equal(audio.getDeckState(1).stable_id, null);
		assert.equal(audio.getDeckState(1).anlz, null);
		assert.match(audio.deckLoadErrors[1], /ANALYSIS_NOT_FOUND/);
		assert.ok(timerDelays.includes(5_000), 'the failure toast must still be raised');
		assert.deepEqual(
			timerDelays.filter((delay) => delay !== 5_000 && delay !== 250),
			[],
			'only the toast expiry and the perf ring flush may schedule work on the ' +
				'load-failure path; anything else is a stray timer on a failing load'
		);
	} finally {
		globalThis.fetch = originalFetch;
		globalThis.setTimeout = originalSetTimeout;
	}
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
		'dispose',
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

test('setPitchRange (pitch-slider range switcher) rejects a range the current pitch no longer fits', () => {
	const deck = 4;
	assert.equal(audio.pitchRanges[deck], 16);

	audio.engine.setPitchRange(deck, 8);
	assert.equal(audio.pitchRanges[deck], 8);

	audio.deckStates[deck].pitch = 1.1;
	assert.throws(() => audio.engine.setPitchRange(deck, 8), /exceeds/);
	assert.equal(audio.pitchRanges[deck], 8, 'a rejected range switch must not mutate state');

	audio.deckStates[deck].pitch = 1;
	audio.engine.setPitchRange(deck, 100);
	assert.equal(audio.pitchRanges[deck], 100);

	assert.throws(() => audio.engine.setPitchRange(deck, 12), /invalid range/i);
});

test('master election uses electMaster instead of audible lowest-id handoff', async () => {
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	assert.match(source, /electMaster\(_electionInput\(\)\)/, '_electPlayingMaster must call electMaster');
	assert.doesNotMatch(
		source,
		/nextPlayingMaster\(DECK_IDS\.filter\(\(deck\) => deckStates\[deck\]\.audible\)\)/
	);
});

test('fresh engine exposes AUTO master mode', () => {
	assert.equal(audio.getMasterMode(), 'auto');
});
