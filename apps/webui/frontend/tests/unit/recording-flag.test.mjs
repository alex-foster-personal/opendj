/**
 * SET-12 v1 fallback (`sets.recording`): REC is hidden only when the daemon
 * declares the flag off. Unread or undeclared flags keep the control, and the
 * start route then refuses with the reason (403), so it is never silently gone.
 *
 * [if] the flag is declared off [then] both REC entry points are hidden, [else stop].
 * [if] the flags are unread or the flag is on [then] REC shows, [else stop].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { recordingEnabledIn, SET_RECORDING_FLAG_ID } = await loadTypeScriptModule('src/lib/sets/recording-flag.ts');

test('REC shows unless the daemon declares sets.recording off', () => {
	assert.equal(SET_RECORDING_FLAG_ID, 'sets.recording');
	assert.equal(recordingEnabledIn([]), true, 'flags unread: show, the API refuses with the reason');
	assert.equal(recordingEnabledIn([{ flag_id: 'sets.recording', enabled: true }]), true);
	assert.equal(recordingEnabledIn([{ flag_id: 'usb.export', enabled: false }]), true, 'another flag off changes nothing');
	assert.equal(recordingEnabledIn([{ flag_id: 'sets.recording', enabled: false }]), false);
});

test('both REC entry points are gated on the flag', () => {
	const read = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8');
	assert.match(read('lib/components/rb/browser/IconRail.svelte'), /entry\.action !== 'record' \|\| setRecordingEnabled\(\)/);
	assert.match(read('routes/sets/+page.svelte'), /\{:else if setRecordingEnabled\(\)\}\s*<button class="record"/);
});
