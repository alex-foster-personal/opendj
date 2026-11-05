import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import {
	ADVANCED_OPTIONS_LABEL,
	CHIP_HREF,
	FORCE_SYNC_LABEL,
	REFRESH_STATUS_LABEL,
	SYNC_NOW_LABEL
} from './cloudsync-view';

import CloudSyncQuickActionsView from './CloudSyncQuickActionsView.svelte';

const post = {
	kind: 'post',
	body: { hub_url: 'http://box.example-tailnet.ts.net:8686', name: 'gig-laptop', force: false }
} as const;

/**
 * The status chip's quick-actions popover (issue #3531). The container
 * loads the gate data and makes every request; here the decisions and the
 * notice are args. The popover is position:fixed and clamps to its trigger,
 * so with no trigger it sits at the top left.
 */
const meta = {
	title: 'Components/CloudSync/CloudSyncQuickActionsView',
	component: CloudSyncQuickActionsView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		getTrigger: () => null,
		onclose: fn(),
		syncing: false,
		refreshing: false,
		loadError: null,
		notice: null,
		syncDecision: post,
		forceSyncDecision: { ...post, body: { ...post.body, force: true } },
		showForceSync: false,
		syncNowLabel: SYNC_NOW_LABEL,
		forceSyncLabel: FORCE_SYNC_LABEL,
		refreshStatusLabel: REFRESH_STATUS_LABEL,
		advancedLabel: ADVANCED_OPTIONS_LABEL,
		advancedHref: CHIP_HREF,
		onSyncNow: fn(),
		onForceSyncNow: fn(),
		onRefreshStatus: fn()
	}
} satisfies Meta<typeof CloudSyncQuickActionsView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Prep posture, nothing playing: Sync now is live. */
export const Ready: Story = {};

/** A deck is playing: Sync now is refused with its reason, and Force sync is offered. */
export const DeferredWhilePlaying: Story = {
	args: {
		syncDecision: {
			kind: 'refuse',
			reason:
				'CloudSync sync deferred (deck_playing): a playing deck is active. Use Force sync to bypass for one round.'
		},
		showForceSync: true
	}
};

/** A sync in flight. */
export const Syncing: Story = { args: { syncing: true } };

/** The last sync succeeded. */
export const SyncedNotice: Story = {
	args: { notice: { kind: 'ok', text: 'Sync ok: pushed 12, pulled 3.' } }
};

/** The last sync failed, with technical details. */
export const FailedNotice: Story = {
	args: {
		notice: {
			kind: 'error',
			text: 'Sync failed: the hub did not answer.',
			details: 'ConnectError: connect ECONNREFUSED box.example-tailnet.ts.net:8686'
		}
	}
};

/** The gate data could not be loaded: Sync now is disabled. */
export const GateLoadError: Story = { args: { loadError: '502 Bad Gateway' } };
