import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const CHIP = source('lib/components/CloudSyncStatusChip.svelte');
const QUICK = source('lib/components/cloudsync/CloudSyncQuickActions.svelte');
const VIEW = source('lib/components/cloudsync/cloudsync-view.ts');
const TAB = source('lib/components/cloudsync/CloudSyncStatusTab.svelte');
const LAYOUT = source('routes/+layout.svelte');
const TOPBAR = source('lib/components/rb/TopBar.svelte');

test('the status chip has off, syncing, ok, error, inconclusive and update_required render states', () => {
	/** if the shared chip state function loses a render state then broken */
	for (const state of ['off', 'syncing', 'ok', 'error', 'inconclusive', 'update_required']) {
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
	assert.match(CHIP, /const title = \$derived\(chipTitle\(status, loadError\)\)/);
	assert.match(CHIP, /title=\{title\}/);
	assert.match(VIEW, /CHIP_QUICK_ACTIONS_CTA/);
	assert.doesNotMatch(VIEW, /Click to open recent results\./);
	assert.doesNotMatch(VIEW, /Click after it loads to see details/);
	assert.doesNotMatch(VIEW, /Click to retry details/);
	assert.match(VIEW, /still loading from the daemon\. \$\{CHIP_QUICK_ACTIONS_CTA\}/);
	assert.match(VIEW, /last_push_at/);
});

// requirement: CSSTATUS-04
// [if] the chip is in error [then] it binds a derived aria-label helper and never interpolates raw last_result.message, [else stop]
test('the chip binds a derived aria-label and does not render raw diagnostics', () => {
	assert.match(CHIP, /const ariaLabel = \$derived\(chipAriaLabel\(status, loadError\)\)/);
	assert.match(CHIP, /aria-label=\{ariaLabel\}/);
	assert.doesNotMatch(CHIP, /last_result\.message/);
	assert.match(CHIP, /class:error=\{chipState\(\) === 'error'\}/);
	assert.match(CHIP, /class:update-required=\{chipState\(\) === 'update_required'\}/);
	assert.match(CHIP, /\.chip\.update-required/);
});

// requirement: CSSTATUS-06
// [if] update_required is set [then] chip and status headline mention App update required
test('update_required uses warn styling and App update required copy', () => {
	assert.match(VIEW, /updateRequiredHeadline/);
	assert.match(VIEW, /App update required to sync/);
	assert.match(TAB, /statusHeadline\(status\)/);
	assert.doesNotMatch(CHIP, /\.chip\.update-required[\s\S]*var\(--danger\)/);
});

// requirement: CSSTATUS-04
// [if] CloudSync errors are surfaced in the status tab [then] the shared formatter and labelled technical details are used, [else stop]
test('the status tab uses shared error presentation and technical details disclosures', () => {
	assert.match(TAB, /presentCloudSyncError/);
	assert.match(TAB, /presentCloudSyncResultError/);
	assert.match(TAB, /data-testid="cloudsync-notice-details"/);
	assert.match(TAB, /data-testid="cloudsync-last-result-details"/);
	assert.match(TAB, /<summary>\{CLOUDSYNC_TECHNICAL_DETAILS_LABEL\}<\/summary>/);
	assert.doesNotMatch(TAB, /Sync failed: \$\{message\(exc\)\}/);
	assert.doesNotMatch(
		TAB,
		/`\$\{status\.last_result\.status\}: \$\{status\.last_result\.message\}`/
	);
	assert.match(TAB, /presentCloudSyncResultError\(status\.last_result, status\.update_required\)\.summary/);
	assert.match(TAB, /<pre class="technical-details">\{status\.last_result\.message\}<\/pre>/);
	assert.match(TAB, /<pre class="technical-details">\{row\.message\}<\/pre>/);
	assert.match(
		VIEW,
		/CloudSync conflict: another sync is already running/
	);
	assert.match(VIEW, /HTTP 409/);
});

// requirement: CSUI-02
// [if] the CloudSync chip is activated [then] it opens quick actions without navigating, [else stop]
test('the chip is a button trigger with an in-place quick-actions popover', () => {
	assert.match(CHIP, /type="button"/);
	assert.match(CHIP, /data-testid="cloudsync-status-chip"/);
	assert.match(CHIP, /aria-haspopup="dialog"/);
	assert.match(CHIP, /CloudSyncQuickActions/);
	assert.doesNotMatch(CHIP, /<a[\s\S]*href=\{CHIP_HREF\}/);
	assert.doesNotMatch(CHIP, /detailsOpen/);
	assert.doesNotMatch(CHIP, /recent_results/);
	assert.match(VIEW, /export const CHIP_HREF = '\/cloudsync'/);
	assert.match(QUICK, /data-testid="cloudsync-quick-actions-popover"/);
	assert.match(QUICK, /data-testid="cloudsync-quick-advanced"[\s\S]*href=\{CHIP_HREF\}/);
	assert.match(QUICK, /runCloudSyncNow/);
	assert.match(QUICK, /syncNowRequest/);
	assert.match(QUICK, /forceSyncNowRequest/);
	assert.match(QUICK, /fetchUiMirrorForGate/);
	assert.match(CHIP, /onpointerdown=\{onWindowPointerDown\}/);
	assert.match(CHIP, /event\.key !== 'Escape'/);
	assert.match(CHIP, /onkeydown=\{onWindowKeydown\}/);
});

test('the chip uses compact single-line layout CSS for small screens', () => {
	/** if the chip can wrap into a two-line circle on narrow viewports then broken */
	assert.match(CHIP, /@media \(max-width: 1024px\)/);
	assert.match(CHIP, /white-space: nowrap/);
	assert.match(CHIP, /max-height: 24px/);
	assert.match(CHIP, /\.chip-label-short/);
	assert.match(CHIP, /<svg/);
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
	assert.equal((TAB.match(/announceStatusChanged\(\);/g) ?? []).length, 3, 'after save, Sync now, and Force sync');
});

test('the chip is rendered immediately beside the account bauble', () => {
	/** if the chip leaves the app shell or moves away from the bauble then broken */
	const chipAt = LAYOUT.indexOf('<CloudSyncStatusChip />');
	const baubleAt = LAYOUT.indexOf('<UserBauble />');
	assert.ok(chipAt >= 0, 'the app shell must render the CloudSync status chip');
	assert.ok(baubleAt > chipAt, 'the CloudSync chip must be beside and before the account bauble');
	// Match the tag, not one exact attribute list: the top bar passes props to
	// the bauble (`size`, plus `showLabel` since issue #2357), so requiring the
	// literal `<UserBauble size={20} />` makes indexOf return -1 and the
	// comparison pass for the wrong reason. The invariant under test is ORDER.
	const chipInTopbar = TOPBAR.indexOf('<CloudSyncStatusChip');
	const baubleInTopbar = TOPBAR.indexOf('<UserBauble');
	assert.ok(chipInTopbar >= 0, 'the performance top bar must render the CloudSync status chip');
	assert.ok(baubleInTopbar > chipInTopbar, 'the chip must sit beside and before the bauble');
});
