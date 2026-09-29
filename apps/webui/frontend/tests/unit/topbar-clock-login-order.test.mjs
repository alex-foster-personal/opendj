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

test('a failed chip refresh publishes the retained status, not off', () => {
	// [if] a poll fails after a good one [then] the clock keeps following the
	// state the chip still shows, rather than hiding as if CloudSync were off.
	const chip = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/CloudSyncStatusChip.svelte', import.meta.url)),
		'utf8'
	);
	const load = chip.slice(chip.indexOf('async function load()'), chip.indexOf('function closePopover()'));
	assert.ok(load.includes('} catch ('), 'load() must still have its failure branch');
	const [ok, failed] = load.split('} catch (');
	// Control: the success path still publishes the fresh status.
	assert.match(ok, /status = await getStatus\(\);[\s\S]*publishCloudSyncChipState\(status\)/);
	assert.match(failed, /publishCloudSyncChipState\(status\)/);
	assert.doesNotMatch(chip, /publishCloudSyncChipState\(null\)/);
});

test('signed-out bauble lists login-gated features via ControlExplainer hover', () => {
	assert.match(cluster, /ControlExplainer/);
	assert.match(cluster, /Sign in required/);
	assert.match(cluster, /loginGatedBullets/);
	assert.match(cluster, /Google sign-in for account panel/);
});

test('signed-in login cluster uses green styling for the gated-feature list', () => {
	assert.match(cluster, /class:signed-in=\{auth\.user !== null\}/);
	assert.match(cluster, /\.login-cluster\.signed-in[\s\S]*var\(--rb-green\)/);
});

test('signed-in account button precedes login cluster', () => {
	// TopBar passes the bauble into TopBarAccountCluster, which renders the
	// account button ahead of it.
	const accountAt = cluster.indexOf('class="account-btn"');
	const baubleSlotAt = cluster.indexOf('{@render children()}');
	assert.ok(accountAt >= 0 && accountAt < baubleSlotAt);
	const clusterAt = topbar.indexOf('<TopBarAccountCluster ');
	const baubleAt = topbar.indexOf('<UserBauble');
	const clusterEndAt = topbar.indexOf('</TopBarAccountCluster>');
	assert.ok(clusterAt >= 0 && clusterAt < baubleAt && baubleAt < clusterEndAt);
});

test('topbar MIDI slot removed in favor of tray and I/O', () => {
	assert.equal(topbar.includes('topbar-slot-midi'), false);
});
