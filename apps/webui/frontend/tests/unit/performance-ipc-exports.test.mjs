/**
 * #8629: runtime export surface of the performance IPC barrel must stay stable
 * when types and parsers move to sibling modules (Object.keys only; type-only
 * re-exports are invisible here).
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const EXPECTED = [
	'PERFORMANCE_PRESET_COMMAND_SCOPES',
	'PERFORMANCE_RESCUE_COMMAND_SCOPES',
	'abortPreparedPerformancePreset',
	'activePerformanceCommandSession',
	'claimPerformanceMount',
	'commitPerformanceLaunches',
	'currentPerformanceMount',
	'deckCommandDispatchCount',
	'deckFaderWriteCount',
	'deckLoadDispatchCount',
	'dismissPerformanceDeckError',
	'dispatchCallerFromStack',
	'dispatchPerformanceCommand',
	'getDeckState',
	'installPairingSnapshotForTest',
	'installPerformanceBrowserIpc',
	'installPerformanceHotCueDriver',
	'installPerformanceHotCueDriverForTest',
	'installPerformanceQuantizedLaunchDriverForTest',
	'installRescueRingWriterHooks',
	'operatorMasterVolume',
	'parsePerformanceCommandForTest',
	'performanceCommandQueueScopes',
	'performanceCommandStatus',
	'performanceContextTimeNowSec',
	'performanceDecksAwaitingPresentation',
	'performanceForgetStep',
	'performancePositionAnchor',
	'performancePresetLifecycle',
	'performanceStepState',
	'preparePerformancePresetTransaction',
	'pushToast',
	'queryMasterMode',
	'queryPerformanceState',
	'queryQuantizedLaunchArmed',
	'queryWaveformSeekArmed',
	'recordDeckLoadRefusal',
	'registerAutoPlayNextController',
	'registerCmdkAdapter',
	'registerPerformanceBrowserAdapter',
	'registerPlaylistHistoryAdapter',
	'rescueRestoreLocksControls',
	'resetQuantizedLaunchArmedForTest',
	'retirePerformanceBrowserIpc',
	'runAudioIoTest',
	'runCoordinatedFromUi',
	'runPerformanceCommandFromUi',
	'runSyncScopedWork',
	'setRescueRestorePhaseForTest',
	'silencePreviewForUserLoad',
	'startPerformancePresetTransaction',
	'stopPerformancePresetTransaction',
	'uiPrefs',
	'uninstallRescueRingWriterHooks'
];

test('performance-ipc.svelte.ts runtime export names are unchanged', async () => {
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	assert.deepEqual(Object.keys(ipc).sort(), EXPECTED);
});
