/**
 * Issue #11326 / pin 894af5672c3b recording half.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

const recorder = await loadTypeScriptModule('src/lib/sets/performance-recorder.ts');

test('[if] the operator clicks the performance record control while idle [then] only the picker opens, not capture', () => {
	const chip = source('src/lib/components/rb/AppPostureChip.svelte');
	const picker = source('src/lib/components/rb/browser/RecordInputPicker.svelte');
	assert.equal(recorder.recordIdleClickOpensPicker(false), true);
	assert.equal(recorder.recordIdleClickOpensPicker(true), false);
	assert.match(chip, /recordIdleClickOpensPicker\(chrome\.recorder\.active\)/);
	assert.match(chip, /await chrome\.openRecordPanel\(\)/);
	assert.match(picker, /const status = await startPerformanceRecorder\(choice, mode\)/);
	assert.doesNotMatch(chip, /startPerformanceRecorder/);
});

test('[if] the pointer hovers the record control [then] the explainer stays open while interactive', () => {
	const chip = source('src/lib/components/rb/AppPostureChip.svelte');
	assert.equal(recorder.RECORD_CONTROL_EXPLAINER_DISMISS, 'stay');
	assert.match(chip, /dismiss=\{RECORD_CONTROL_EXPLAINER_DISMISS\}/);
	assert.match(chip, /pinOnClick=\{true\}/);
});

test('[if] recording is idle [then] REC is unlit and shows the idle title', () => {
	const idle = recorder.recordRailState({
		active: false,
		session_id: null,
		pid: null,
		owned: false,
		recoverable: false,
		capture: 'none'
	});
	assert.equal(idle.recording, false);
	assert.equal(idle.waiting, false);
	assert.match(recorder.recordControlIdleTitle(), /click to configure/i);
});

test('[if] the library source tabs are shown [then] Recordings sits immediately right of USBs', () => {
	const tabs = source('src/lib/components/rb/browser/LibrarySourceTabs.svelte');
	assert.match(tabs, /libraryRecordingsTabEnabled\(\)/);
	assert.match(tabs, /id: 'recordings', label: 'Recordings'/);
	const usbs = tabs.indexOf("id: 'usbs'");
	const recordings = tabs.indexOf("id: 'recordings'");
	assert.ok(usbs >= 0 && recordings > usbs);
});

test('[if] the recordings tab is selected and files exist in the recordings dir [then] rows list segment files', () => {
	const nav = source('src/lib/components/rb/browser/LibraryNav.svelte');
	const rows = recorder.recordingRowsFromSegments(
		[{ session_id: '2026-01-01T12-00-00' }],
		new Map([['2026-01-01T12-00-00', [{ name: 'audio_1.wav', size_bytes: 100 }]]])
	);
	assert.deepEqual(rows, [
		{ session_id: '2026-01-01T12-00-00', file_name: 'audio_1.wav', size_bytes: 100 }
	]);
	assert.match(nav, /recordingRowsFromSegments/);
	assert.match(nav, /data-testid="library-recordings-list"/);
	assert.match(nav, /recordings_dir/);
});
