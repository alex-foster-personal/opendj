/**
 * @pytest.mark.requirement UX-TOAST-03
 * [if] browser pane load fails [then] classification is browser-selection [else stop].
 * [if] beat sync context is present [then] settings summary names modes [else stop].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let classifyToastError;

before(async () => {
	const mod = await loadTypeScriptModule('src/lib/toast-error-classification.ts');
	classifyToastError = mod.classifyToastError;
});

test('browser pane load maps to browser-selection with playlist context', () => {
	const diagnostic = classifyToastError({
		kind: 'error',
		message: 'playlist load failed: timeout',
		context: {
			source: 'browser-pane-load',
			playlist_id: 'pl-1',
			playlist_name: 'Warmup',
			pane_kind: 'playlist'
		}
	});
	assert.equal(diagnostic.classification, 'browser-selection');
	assert.match(diagnostic.settingsSummary ?? '', /playlist_id: pl-1/);
	assert.equal(diagnostic.feature, 'Library selection');
});

test('beat sync errors include mode in settings summary', () => {
	const diagnostic = classifyToastError({
		kind: 'warn',
		message: 'Beat sync fold to half tempo',
		context: { beat_sync_mode: 'BAR', beat_sync_max: true, master_deck: 1, follower_deck: 2 }
	});
	assert.equal(diagnostic.classification, 'beat-sync');
	assert.match(diagnostic.settingsSummary ?? '', /Beat Sync: BAR/);
	assert.match(diagnostic.settingsSummary ?? '', /Beat Sync Max: on/);
});

test('RangeError on cue jump classifies as cue-transport', () => {
	const diagnostic = classifyToastError({
		kind: 'error',
		message: 'cue.Jump failed',
		cause: new RangeError('index out of range'),
		context: { beat_sync_mode: 'BEAT', deck_id: 2 }
	});
	assert.equal(diagnostic.classification, 'cue-transport');
});
