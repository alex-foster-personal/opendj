import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const CHIP = source('lib/components/CloudSyncStatusChip.svelte');
const VIEW = source('lib/components/cloudsync/cloudsync-view.ts');
const LAYOUT = source('routes/+layout.svelte');
const TOPBAR = source('lib/components/rb/TopBar.svelte');

test('the status chip has off, syncing, ok and error render states', () => {
	/** if the shared chip state function loses a render state then broken */
	for (const state of ['off', 'syncing', 'ok', 'error', 'inconclusive']) {
		assert.match(VIEW, new RegExp(`return '${state}'`), `missing ${state} state`);
	}
});

test('the chip derives its state from the shared heartbeat-gated rule', () => {
	/** if the chip keeps a private copy of the state rule (which drifted to env-only before) then broken */
	assert.match(CHIP, /chipState as chipStateOf,[\s\S]*?\} from '\$lib\/components\/cloudsync\/cloudsync-view'/);
	assert.match(CHIP, /return chipStateOf\(status\)/);
	assert.doesNotMatch(CHIP, /status\.enabled\)/);
	assert.match(VIEW, /!status\.configured \|\| !status\.running\) return 'off'/);
});

test('the chip carries an explanatory hover title derived from status', () => {
	/** if the chip loses its hover title or its relative-time readout then broken */
	assert.match(CHIP, /const title = \$derived\.by\(/);
	assert.match(CHIP, /title=\{title\}/);
	assert.match(CHIP, /Click to open recent results\./);
	assert.match(CHIP, /status\?\.last_push_at/);
});

test('clicking the chip opens the five-result detail instead of a dead badge', () => {
	/** if the chip becomes a dead badge with no result detail then broken */
	assert.match(CHIP, /onclick=\{\(\) => \(detailsOpen = !detailsOpen\)\}/);
	assert.match(CHIP, /status\.recent_results/);
	assert.match(CHIP, /No sync attempts have completed yet\./);
});

test('the chip re-reads status on a poll under the heartbeat stale window', () => {
	/** if the chip reads status only once, so a heartbeat gone stale after load stays lit, then broken */
	const poll = VIEW.match(/export const CHIP_POLL_MS = ([\d_]+);/);
	assert.ok(poll, 'CHIP_POLL_MS must be exported from cloudsync-view');
	const pollMs = Number(poll[1].replaceAll('_', ''));
	assert.ok(pollMs > 0 && pollMs < 45_000, `poll ${pollMs} ms must be under STALE_AFTER_S (45 s)`);
	assert.match(CHIP, /setInterval\(\(\) => \{\s*if \(!document\.hidden\) void load\(\);\s*\}, CHIP_POLL_MS\)/);
	assert.match(CHIP, /clearInterval\(timer\)/);
});

test('Sync now and a config save tell the chip to re-read at once', () => {
	/** if the chip only learns of a Sync now or config save on its next poll then broken */
	const TAB = source('lib/components/cloudsync/CloudSyncStatusTab.svelte');
	assert.match(CHIP, /window\.addEventListener\(STATUS_CHANGED_EVENT, onStatusChanged\)/);
	assert.match(TAB, /window\.dispatchEvent\(new CustomEvent\(STATUS_CHANGED_EVENT\)\)/);
	assert.equal((TAB.match(/announceStatusChanged\(\);/g) ?? []).length, 2, 'after save and after Sync now');
});

test('the chip is rendered immediately beside the account bauble', () => {
	/** if the chip leaves the app shell or moves away from the bauble then broken */
	const chipAt = LAYOUT.indexOf('<CloudSyncStatusChip />');
	const baubleAt = LAYOUT.indexOf('<UserBauble />');
	assert.ok(chipAt >= 0, 'the app shell must render the CloudSync status chip');
	assert.ok(baubleAt > chipAt, 'the CloudSync chip must be beside and before the account bauble');
	assert.ok(TOPBAR.indexOf('<CloudSyncStatusChip />') < TOPBAR.indexOf('<UserBauble size={20} />'));
});
