import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import type { CloudSyncStatus } from '$lib/api-cloudsync';

import {
	CLOUDSYNC_TECHNICAL_DETAILS_LABEL,
	FORCE_SYNC_LABEL,
	envOverrideNotes,
	forceSyncNowRequest,
	identityBacklogNote,
	presentCloudSyncResultError,
	syncNowRequest
} from './cloudsync-view';

import CloudSyncStatusTabView from './CloudSyncStatusTabView.svelte';

type Config = NonNullable<ComponentProps<typeof CloudSyncStatusTabView>['config']>;

const HUB = 'http://box.example-tailnet.ts.net:8686';

const config = (over: Partial<Config['effective']> = {}): Config => ({
	path: '/srv/Open DJ/cloudsync.json',
	file: { enabled: true, hub_url: HUB, machine_name: 'gig-laptop' },
	effective: {
		configured: true,
		enabled: true,
		enabled_source: 'file',
		hub_url: HUB,
		hub_url_source: 'file',
		machine_name: 'gig-laptop',
		...over
	}
});

const status = (over: Partial<CloudSyncStatus> = {}): CloudSyncStatus => ({
	enabled: true,
	configured: true,
	running: true,
	heartbeat_at: '2026-10-14T21:04:00Z',
	enabled_source: 'file',
	endpoint_source: 'file',
	reason: null,
	signed_in_as: 'dj@example.com',
	last_push_at: '2026-10-14T21:03:40Z',
	last_pull_at: '2026-10-14T21:03:41Z',
	last_result: { status: 'ok', message: 'pushed 12, pulled 3' },
	rows_pending: 0,
	hash_pending: 0,
	quarantined: 0,
	excluded_total: 4,
	endpoint: HUB,
	recent_results: [
		{
			finished_at: '2026-10-14T21:03:41Z',
			status: 'ok',
			message: 'pushed 12, pulled 3',
			pushed: 12,
			pulled: 3
		},
		{
			finished_at: '2026-10-14T20:33:12Z',
			status: 'error',
			message: 'ConnectError: hub unreachable',
			pushed: 0,
			pulled: 0
		}
	],
	update_required: null,
	...over
});

/**
 * Args for one status and config, derived by the app's own cloudsync-view
 * rules exactly as the container derives them, so a story cannot show a
 * headline or decision the app would not.
 */
const derive = (s: CloudSyncStatus | null, c: Config | null, playing = false) => {
	const gate = {
		appPosture: 'prep' as const,
		uiMirror: playing ? { decks: { '1': { playing: true } } } : null
	};
	return {
		status: s,
		config: c,
		form: {
			enabled: c?.effective.enabled ?? false,
			hubUrl: c?.effective.hub_url ?? '',
			machineName: c?.effective.machine_name ?? ''
		},
		syncDecision: syncNowRequest(c, gate),
		forceSyncDecision: forceSyncNowRequest(c),
		showForceSync: playing && c?.effective.hub_url != null
	};
};

/**
 * The /cloudsync Status tab: status panel, recent results and the config
 * form. The container makes every request; Sync now, Force sync, Refresh
 * and Save raise callbacks. Statuses, hosts and paths are invented.
 */
const meta = {
	title: 'Components/CloudSync/CloudSyncStatusTabView',
	component: CloudSyncStatusTabView,
	tags: ['autodocs'],
	args: {
		...derive(status(), config()),
		loadError: null,
		notice: null,
		saving: false,
		syncing: false,
		backlogNote: null,
		technicalDetailsLabel: CLOUDSYNC_TECHNICAL_DETAILS_LABEL,
		forceSyncLabel: FORCE_SYNC_LABEL,
		presentCloudSyncResultError,
		envOverrideNotes,
		syncNow: fn(),
		forceSyncNow: fn(),
		refresh: fn(),
		saveConfig: fn(async () => {})
	}
} satisfies Meta<typeof CloudSyncStatusTabView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Running and healthy, with one failed attempt in the journal. */
export const Healthy: Story = {};

/** A deck is playing: Sync now is refused and Force sync is offered. */
export const DeferredWhilePlaying: Story = { args: derive(status(), config(), true) };

/** The last result failed: a summary plus a technical-details disclosure. */
export const LastResultError: Story = {
	args: derive(
		status({
			last_result: { status: 'error', message: 'HTTP 409: another sync is already running' }
		}),
		config()
	)
};

/** Hub URL from the environment: the form says it overrides the file. */
export const EnvOverride: Story = {
	args: derive(status(), config({ hub_url_source: 'env', enabled_source: 'env' }))
};

/** Tracks waiting on a content hash: enrichment note sits in the secondary block, not the headline strip. */
export const IdentityBacklog: Story = {
	args: { ...derive(status({ hash_pending: 37 }), config()), backlogNote: identityBacklogNote(37) }
};

/** A Sync now result with its counts in the hover. */
export const SyncedNotice: Story = {
	args: {
		notice: {
			kind: 'ok',
			text: 'Sync ok: pushed 12, pulled 3.',
			title:
				'Rows this Sync now pushed to the hub (12) and pulled from it (3); 1 round(s), hub sequence 4182'
		}
	}
};

/** Not configured: no hub URL, so Sync now says to set one. */
export const NotConfigured: Story = {
	args: derive(
		status({
			enabled: false,
			configured: false,
			running: false,
			last_result: null,
			recent_results: [],
			endpoint: null
		}),
		config({ configured: false, enabled: false, hub_url: null, machine_name: null })
	)
};

/** First load in flight. */
export const Loading: Story = { args: derive(null, null) };

/** The status read failed. */
export const LoadError: Story = { args: { ...derive(null, null), loadError: '502 Bad Gateway' } };
