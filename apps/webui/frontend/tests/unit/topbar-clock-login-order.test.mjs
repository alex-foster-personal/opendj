/**
 * requirement: CHROME-04, CHROME-05, CHROME-06
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const topbar = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);

test('UserBauble appears before the clock in TopBar source order', () => {
	const baubleAt = topbar.indexOf('<UserBauble');
	const clockAt = topbar.indexOf('class="clock"');
	assert.ok(baubleAt >= 0 && clockAt > baubleAt, 'clock must follow UserBauble');
});

test('clock is hidden when signed out and CloudSync is off', () => {
	assert.match(topbar, /\{#if showClock\}/);
	assert.match(topbar, /cloudSyncChipState\.value !== 'off'/);
});

test('signed-in account button precedes login cluster', () => {
	const accountAt = topbar.indexOf('class="account-btn"');
	const baubleAt = topbar.indexOf('<UserBauble');
	assert.ok(accountAt >= 0 && accountAt < baubleAt);
});

test('topbar MIDI slot removed in favor of tray and I/O', () => {
	assert.equal(topbar.includes('topbar-slot-midi'), false);
});
