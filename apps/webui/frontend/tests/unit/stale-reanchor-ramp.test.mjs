// Issue #1071: stale re-anchor ramp tails must not override newer transport
// commands (pause, beat-sync disable, master promotion, overlapping re-anchors).
//
// Regression lines:
// - if _bumpReanchorOperation is not called from pause, setBeatSync disable, or
//   _scheduleReanchoredFollower then a superseding command cannot invalidate
//   an in-flight ramp tail
// - if _continueTempoRamp stops checking generation inside the step loop and
//   after await _scheduleDeck then a stale step can publish active transport
// - if reanchorRampOwnerGeneration is cleared without matching the captured
//   generation then an older tail can clear a newer ramp's pending state
// - if reanchorRampActive remains then the boolean pending flag is back
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	engineBlockAfter,
	readFrontendSource,
	SCHEDULE_DECK_SERIAL_ANCHOR
} from './engine-source.mjs';

const AUDIO_ENGINE = 'src/lib/rb/audio-engine.svelte.ts';

test('re-anchor operation generation supersedes stale ramp tails', () => {
	const text = readFrontendSource(AUDIO_ENGINE);
	assert.equal(text.includes('reanchorRampActive'), false, 'boolean pending flag must be gone');
	assert.match(text, /function _bumpReanchorOperation\(/);
	assert.match(text, /function _reanchorOperationIsCurrent\(/);
	assert.match(text, /function _claimReanchorRampOwner\(/);
	assert.match(text, /function _releaseReanchorRampOwner\(/);
	assert.match(text, /function _reanchorRampPending\(/);
});

test('superseding commands bump re-anchor operation generation', () => {
	const pauseBody = engineBlockAfter('pause(deck: DeckId, pressT0Ms?: number): Promise<void> {');
	assert.match(pauseBody, /_bumpReanchorOperation\(deck\)/);

	const beatSyncBody = engineBlockAfter('setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {');
	const disableAt = beatSyncBody.indexOf('if (!enabled) {');
	assert.notEqual(disableAt, -1);
	const disableBranch = beatSyncBody.slice(disableAt, disableAt + 400);
	assert.match(disableBranch, /_bumpReanchorOperation\(deck\)/);

	const reanchorText = readFrontendSource(AUDIO_ENGINE);
	const reanchorFn = reanchorText.indexOf('async function _scheduleReanchoredFollower(');
	assert.notEqual(reanchorFn, -1);
	const reanchorSlice = reanchorText.slice(reanchorFn, reanchorFn + 1200);
	assert.match(reanchorSlice, /const generation = _bumpReanchorOperation\(deck\)/);
	assert.match(reanchorSlice, /_claimReanchorRampOwner\(deck, generation\)/);

	const masterBody = engineBlockAfter(
		'async setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void> {'
	);
	assert.match(masterBody, /if \(previousMaster !== deck\) _bumpReanchorOperation\(deck\)/);
});

test('the ramp tail checks generation before and after each schedule await', () => {
	const body = engineBlockAfter('async function _continueTempoRamp(\n\tdeck: DeckId,\n\tsyncAt: number,\n\tremainingSteps: readonly TempoRampStep[],\n\tmasterTempoEnabled: boolean,\n\tgeneration: number\n): Promise<void> {');
	const loopAt = body.indexOf('for (const step of remainingSteps)');
	assert.notEqual(loopAt, -1);
	const preSchedule = body.indexOf('_reanchorOperationIsCurrent(deck, generation)');
	assert.notEqual(preSchedule, -1);
	assert.ok(preSchedule > loopAt && preSchedule < body.indexOf('await _scheduleDeck'));
	const scheduleAt = body.indexOf('await _scheduleDeck');
	const postSchedule = body.indexOf('_reanchorOperationIsCurrent(deck, generation)', scheduleAt);
	assert.notEqual(postSchedule, -1);
	assert.ok(postSchedule > scheduleAt);
});

test('post-await schedule ack is guarded by re-anchor generation', () => {
	const body = engineBlockAfter(SCHEDULE_DECK_SERIAL_ANCHOR);
	assert.match(
		body,
		/reanchorGeneration !== undefined[\s\S]*_reanchorOperationIsCurrent\(deck, reanchorGeneration\)[\s\S]*recordPerfTiming/
	);
});

test('ramp owner is released only for the captured generation', () => {
	const releaseBody = engineBlockAfter('function _releaseReanchorRampOwner(deck: DeckId, generation: number): void {');
	assert.match(releaseBody, /if \(rt\.reanchorRampOwnerGeneration === generation\)/);
	const rampBody = engineBlockAfter('async function _continueTempoRamp(\n\tdeck: DeckId,\n\tsyncAt: number,\n\tremainingSteps: readonly TempoRampStep[],\n\tmasterTempoEnabled: boolean,\n\tgeneration: number\n): Promise<void> {');
	assert.match(rampBody, /_releaseReanchorRampOwner\(deck, generation\)/);
});
