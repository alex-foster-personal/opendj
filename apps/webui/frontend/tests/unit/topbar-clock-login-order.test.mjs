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
const cluster = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBarAccountCluster.svelte', import.meta.url)),
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
	// TopBar passes the bauble into TopBarAccountCluster, which renders the
	// account button ahead of it.
	const accountAt = cluster.indexOf('class="account-btn"');
	const baubleSlotAt = cluster.indexOf('{@render children()}');
	assert.ok(accountAt >= 0 && accountAt < baubleSlotAt);
	const clusterAt = topbar.indexOf('<TopBarAccountCluster>');
	const baubleAt = topbar.indexOf('<UserBauble');
	const clusterEndAt = topbar.indexOf('</TopBarAccountCluster>');
	assert.ok(clusterAt >= 0 && clusterAt < baubleAt && baubleAt < clusterEndAt);
});

test('topbar MIDI slot removed in favor of tray and I/O', () => {
	assert.equal(topbar.includes('topbar-slot-midi'), false);
});
