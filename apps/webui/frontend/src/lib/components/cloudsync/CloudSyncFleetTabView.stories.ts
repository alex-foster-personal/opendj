import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import CloudSyncFleetTabView from './CloudSyncFleetTabView.svelte';

type Fleet = NonNullable<ComponentProps<typeof CloudSyncFleetTabView>['fleet']>;
type Machine = Fleet['machines'][number];

const machine = (over: Partial<Machine>): Machine => ({
	machine_id: 'm-0000',
	name: 'studio-mini',
	state: 'owned',
	owner_email: 'owner@example.com',
	owner_google_sub: 'sub-0001',
	enrolled_at: '2026-10-02T18:04:11Z',
	enrolled_via: 'grant',
	row_hub_machine_id: 'm-hub',
	...over
});

/**
 * The /cloudsync Fleet tab: enrollment state of every machine the state DB
 * knows. Args have the shape of GET /api/v1/cloudsync/fleet; machine names
 * and ids are invented. Refresh raises `onRefresh`; the enrollment-grant form
 * raises `onCreateGrant` and `onCopyToken`.
 */
const meta = {
	title: 'Components/CloudSync/CloudSyncFleetTabView',
	component: CloudSyncFleetTabView,
	tags: ['autodocs'],
	args: {
		fleet: null,
		loadError: null,
		onRefresh: fn(),
		grantEmail: '',
		grantBusy: false,
		grantError: null,
		grant: null,
		copyState: 'idle',
		expiryTitle: '',
		enrollHint:
			'python -m apps.sync_hub enroll --data-dir <its data dir> --hub <this hub url> --name <its name> --grant-file -',
		onCreateGrant: fn(),
		onCopyToken: fn()
	}
} satisfies Meta<typeof CloudSyncFleetTabView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Three machines, one in each enrollment state. */
export const Typical: Story = {
	args: {
		fleet: {
			hub_machine_id: 'm-hub',
			owned: 1,
			unowned: 1,
			foreign: 1,
			machines: [
				machine({ machine_id: 'm-0001', name: 'studio-mini' }),
				machine({
					machine_id: 'm-0002',
					name: 'gig-laptop',
					state: 'unowned',
					owner_email: null,
					owner_google_sub: null,
					enrolled_at: null,
					enrolled_via: null
				}),
				machine({
					machine_id: 'm-0003',
					name: 'club-booth',
					state: 'foreign',
					row_hub_machine_id: 'm-other'
				})
			]
		}
	}
};

/** The registry is empty. */
export const NoMachines: Story = {
	args: { fleet: { hub_machine_id: 'm-hub', owned: 0, unowned: 0, foreign: 0, machines: [] } }
};

/** First load in flight. */
export const Loading: Story = {};

/** The fleet read failed. */
export const LoadError: Story = { args: { loadError: '403 Forbidden: local operator only' } };

/** An enrollment grant has been minted: its token shows once, with the join command. */
export const WithGrant: Story = {
	args: {
		grantEmail: 'owner@example.com',
		grant: {
			created_at: '2026-10-17T17:04:11Z',
			owner_email: 'owner@example.com',
			expires_at: '2026-10-17T18:04:11Z',
			token: 'grant-token-example-0001'
		},
		expiryTitle: 'Single-use grant; it expires 17 Oct 2026, 18:04 UTC.'
	}
};
