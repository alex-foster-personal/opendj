// issue #8631 / #8581: barrel line-count ceiling + runtime export snapshot (no API drift).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { readFrontendSource } from './engine-source.mjs';

globalThis.location ??= { search: '' };

// Ratchet toward 1_500 as further #8581 slices land.
const AUDIO_ENGINE_SVELTE_MAX_LINES = 4789;

/**
 * Sorted runtime Object.keys of the barrel on this merge, matching origin/main's public names (the
 * extract still re-exports upgradeDeckStemsForTest; that name is already on the list).
 */
const AUDIO_ENGINE_EXPORT_NAMES = [
	'DECK_IDS',
	'DeckReplacementBlockedError',
	'KEY_SHIFT_RAMP_DURATION_SEC',
	'KEY_SHIFT_RAMP_MAX_CENTS',
	'KEY_SHIFT_RAMP_STEP_SEC',
	'PITCH_RANGES',
	'REAL_CONTEXT_WAIT_CLOCK',
	'_configureGraphRebuildRetriesForTests',
	'_fidelityTapForTests',
	'acknowledgePresentedTransportSchedule',
	'applyPausedDeckControlSettings',
	'assertDeckLoadAllowed',
	'assertDeckLoadConsistency',
	'assertDeckReplacementAllowed',
	'assertPausedMasterSelectionAllowed',
	'audioContextNowSec',
	'audioContextState',
	'beatSyncFold',
	'beatSyncMasterDeck',
	'beatSyncMaxFollowers',
	'camelotKeysAreCompatible',
	'clampSeekTargetMs',
	'commitDueQuantizedLaunches',
	'commonSyncScheduleTimes',
	'composeStretchSemitones',
	'createPresentedTransportTimeline',
	'createSlipAnchor',
	'deckAudioClockPositionMs',
	'deckAutoGain',
	'deckEffectiveBpm',
	'deckHandoverToken',
	'deckHandoverUntilSec',
	'deckLoadErrors',
	'deckMasterAtContextSec',
	'deckMasterBusGain',
	'deckMixBuffer',
	'deckPcmEstimatedBytes',
	'deckReachedEnd',
	'deckRemainingMs',
	'deckStates',
	'deckTransportClock',
	'decodedTransportDurationMs',
	'deriveKeySyncNudge',
	'deriveKeySyncSemitones',
	'deriveKeySyncTargetManualShift',
	'detachProcessorForDisposal',
	'drillGraphRebuild',
	'effectiveCamelotKey',
	'engine',
	'ensureAudioGraphForCue',
	'exactBeatLoopRangeMs',
	'getDeckState',
	'getMasterMode',
	'getMasterReason',
	'gigDeckGraphIsPresent',
	'installAutomaticRejoinRunner',
	'installScopedSyncRunner',
	'isDeckExternallyRouted',
	'isMasterMuted',
	'keyShiftOptions',
	'keySyncAudibleCamelotKey',
	'keySyncEffectiveAudibleSemitones',
	'keySyncManualShiftBaseline',
	'keySyncPreview',
	'keySyncStatus',
	'loadCandidateCanPublish',
	'loopEndpointsWithinDurationMs',
	'masterDelayNode',
	'masterMixTapPoint',
	'masterSwitchFollowers',
	'masterTempoSemitones',
	'metersUnavailable',
	'mixerState',
	'naturalEndNeedsRevisionedStop',
	'nextPlayingMaster',
	'normalizeEngagedLoopPositionSec',
	'normalizeScheduledTransportEntrySec',
	'observePresentedTransportTimeline',
	'onMetersUnavailableChange',
	'parseCamelotKey',
	'pausedMasterSelectionBlockers',
	'pausedSeekClock',
	'peekDeckFaderGain',
	'peekDeckMeterReading',
	'peekMasterMeterReading',
	'pendingSyncWaitTarget',
	'pitchRanges',
	'planKeyShiftMutation',
	'planKeyShiftRamp',
	'planSeekSync',
	'playResumePositionSec',
	'presentationTickFailure',
	'presentedKeyShiftSemitonesAt',
	'presentedSlipAnchor',
	'processorOnsetLeadSec',
	'projectedLoopAwareTransportPosition',
	'projectedTransportPosition',
	'quantizedLoopEndpointsMs',
	'quantizedPositionMs',
	'rateDeckTrack',
	'reattachingDecks',
	'rebaseSlipAnchor',
	'safeSyncScheduleTime',
	'safeTransportScheduleTime',
	'scheduleDeckHandover',
	'scheduleOffsetStages',
	'seekSyncMaster',
	'setAutoGainLiveGuard',
	'setMasterMuted',
	'setPausedTransportTimelineCursor',
	'shouldActivateSlip',
	'slipHiddenPositionSec',
	'slipHiddenPositionWithTempoBoundaries',
	'slipTempoBoundariesAfterAnchor',
	'startDeckAlignedTo',
	'stretchScheduleChange',
	'supersedingScheduleTime',
	'syncChangeRequiresReschedule',
	'syncMayWriteTempo',
	'transportNeedsScheduledMutation',
	'upgradeDeckStemsForTest',
	'waitForAdvancingContextTime',
	'waitForContextSec'
];

test('audio-engine.svelte.ts line count stays under the ratchet', () => {
	const text = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const lines = text.trimEnd().split('\n').length;
	assert.ok(
		lines <= AUDIO_ENGINE_SVELTE_MAX_LINES,
		`audio-engine.svelte.ts is ${lines} lines; ratchet is ${AUDIO_ENGINE_SVELTE_MAX_LINES}`
	);
});

test('audio-engine.svelte.ts runtime exports match the frozen list', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: 'https://audio-engine.example.test'
	});
	assert.deepEqual(Object.keys(mod).sort(), AUDIO_ENGINE_EXPORT_NAMES);
});
