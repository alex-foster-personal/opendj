/**
 * CSUI-02 (issue #3531): activating the CloudSync chip on /performance must
 * open quick actions without navigation or performance transport commands.
 *
 * The frontend unit harness has no jsdom mount for .svelte components, so this
 * pairs the shared playing-deck ui-mirror gate with source-verified chip
 * isolation and the same togglePopover semantics the button uses. Real-audio
 * command-spy coverage lives in performance-cloudsync-quick-actions.spec.ts.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function source(relative) {
	return readFileSync(fileURLToPath(new URL(`../../src/${relative}`, import.meta.url)), 'utf8');
}

const CHIP = source('lib/components/CloudSyncStatusChip.svelte');
const QUICK = source('lib/components/cloudsync/CloudSyncQuickActions.svelte');

let view;

function config(overrides = {}) {
	return {
		effective: {
			enabled: true,
			hub_url: 'http://hub:8686',
			machine_name: 'silver',
			configured: true,
			...overrides
		}
	};
}

before(async () => {
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts');
});

// requirement: CSUI-02
// [if] deck 1 is playing [then] chip activation opens quick actions without route or transport change, [else stop]
test('chip activation opens quick actions while a deck is playing without sync transport side effects', () => {
	const deckBefore = { playing: true, audible: true };
	const playingGate = {
		appPosture: 'prep',
		uiMirror: { decks: { '1': deckBefore } }
	};

	const ordinary = view.syncNowRequest(config(), playingGate);
	assert.equal(ordinary.kind, 'refuse');
	assert.match(ordinary.reason, /deck_playing/);

	assert.doesNotMatch(CHIP, /performance-ipc|dispatchPerformanceCommand|runPerformanceCommandFromUi/);
	assert.doesNotMatch(QUICK, /performance-ipc|dispatchPerformanceCommand|runPerformanceCommandFromUi/);
	assert.doesNotMatch(CHIP, /goto\(|href=/);
	assert.match(CHIP, /onclick=\{togglePopover\}/);
	assert.match(QUICK, /data-testid="cloudsync-quick-actions-popover"/);
});

// requirement: CSUI-02
// [if] chip status is still loading or unavailable [then] hover copy names quick actions, [else stop]
test('loading and unavailable chip titles name quick actions', () => {
	const loadingTitle = view.chipTitle(null, null);
	assert.match(loadingTitle, /still loading from the daemon/);
	assert.ok(loadingTitle.includes(view.CHIP_QUICK_ACTIONS_CTA));
	const unavailableTitle = view.chipTitle(null, 'network error');
	assert.match(unavailableTitle, /CloudSync status unavailable/);
	assert.ok(unavailableTitle.includes(view.CHIP_QUICK_ACTIONS_CTA));
	assert.doesNotMatch(loadingTitle, /see details/);
	assert.doesNotMatch(unavailableTitle, /retry details/);
});
