import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const LAYOUT = source('routes/+layout.svelte');
const APP_CSS = source('app.css');

test('the header status strip groups health readouts with middle-dot separators', () => {
	/** if lock/sync/bind sit in .topbar as sibling spans with no · then broken */
	assert.match(LAYOUT, /data-testid="header-status-strip"/);
	assert.match(LAYOUT, /class="status-strip"/);
});

test('tracks and playlists are numeric readouts with hover titles', () => {
	/** if tracks and playlists lose their hover title then broken */
	const numericMatches = LAYOUT.match(/readout-numeric[^>]*title=\{/g) ?? [];
	assert.equal(numericMatches.length, 2, 'tracks and playlists each need readout-numeric with title');
	assert.doesNotMatch(
		LAYOUT,
		/\{health\.data\.state_db\.tracks\} tracks · \{health\.data\.state_db\.playlists\} playlists/
	);
});

test('every health readout is joined by a literal middle-dot separator', () => {
	/** if .status-strip can neither wrap nor ellipsis then readouts concatenate then broken */
	const sepMatches = LAYOUT.match(/class="sep"[^>]*> · </g) ?? [];
	assert.ok(sepMatches.length >= 4, `expected at least 4 · separators, got ${sepMatches.length}`);
});

test('the status strip wraps or ellipsizes on overflow', () => {
	assert.match(LAYOUT, /\.status-strip \{[^}]*flex-wrap:\s*wrap/);
	assert.match(LAYOUT, /text-overflow:\s*ellipsis/);
});

test('the topbar no longer uses space-between so the chip stays right-aligned', () => {
	/** if .topbar keeps justify-content: space-between after the health spans are grouped then the CloudSync chip centers then broken */
	assert.doesNotMatch(APP_CSS, /\.topbar \{[^}]*justify-content:\s*space-between/);
	assert.match(APP_CSS, /\.topbar \{[^}]*gap:/);
});

test('the CloudSync chip stays immediately before the account bauble', () => {
	const chipAt = LAYOUT.indexOf('<CloudSyncStatusChip />');
	const baubleAt = LAYOUT.indexOf('<UserBauble />');
	assert.ok(chipAt >= 0, 'the app shell must render the CloudSync status chip');
	assert.ok(baubleAt > chipAt, 'the CloudSync chip must be beside and before the account bauble');
});

test('the status strip never labels syncthing as sync:', () => {
	/** if syncthing steals the sync: prefix from CloudSync then broken */
	assert.doesNotMatch(LAYOUT, /sync: n\/a/);
	assert.doesNotMatch(LAYOUT, />sync:/);
	assert.match(
		LAYOUT,
		/syncthing: \{health\.data\.syncthing\.peers_connected\} peers - \{health\.data\.syncthing\.folder_state\}/
	);
	assert.doesNotMatch(LAYOUT, /PARITY-TODO \(syncthing not configured\)/);
	const syncthingIf = LAYOUT.match(/\{#if health\.data\.syncthing\}[\s\S]*?\{\/if\}/);
	assert.ok(syncthingIf, 'syncthing block must exist');
	assert.match(syncthingIf[0], /class="sep"/, 'syncthing block must include a separator');
	const sepMatches = LAYOUT.match(/class="sep"[^>]*> · </g) ?? [];
	assert.ok(sepMatches.length >= 4, `expected at least 4 · separators, got ${sepMatches.length}`);
});
