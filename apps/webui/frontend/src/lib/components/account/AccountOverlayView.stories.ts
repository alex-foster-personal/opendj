import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import AccountOverlayView from './AccountOverlayView.svelte';

type Props = ComponentProps<typeof AccountOverlayView>;
type Account = NonNullable<Props['account']>;

// Synthesized, not captured: this hand-written AccountOut payload is invented.
// Its paths, plan text and response shape are not a real daemon response.
const SIGNED_IN: Account = {
	authorisation_enforced: false,
	authorisation_note:
		'Plans are recorded but not enforced in this build: every feature works whatever the plan says.',
	signed_in: true,
	privacy_policy_url: 'https://example.com/privacy',
	plan: {
		plan_id: 'free',
		label: 'Free',
		note: 'The default plan. No payment provider is configured.',
		provider: null
	},
	user: {
		google_sub: 'sub-0001',
		name: 'Sample DJ',
		email: 'dj@example.com',
		avatar_url: null,
		created_at: '2026-01-15T12:00:00+00:00'
	},
	local_data: [
		{
			label: 'Account row',
			location: '/srv/openDJ/state.db',
			contents: 'Your Google id, name, email and avatar URL.',
			delete_with: 'Delete my account data'
		},
		{
			label: 'Sign-in sessions',
			location: '/srv/openDJ/state.db',
			contents: 'One row per signed-in browser, with its Google tokens.',
			delete_with: 'Sign out'
		}
	]
};

const FLAG_BASE = {
	default: true,
	overridden: false,
	owner: 'webui',
	retire_by: '2027-01-01',
	refusal: null
};

/**
 * The account panel body: identity, plan, what this build can do, the
 * local-data disclosure and the danger zone. The container loads the account,
 * entitlements and build flags and owns sign-out and delete.
 */
const meta = {
	title: 'Components/Account/AccountOverlayView',
	component: AccountOverlayView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		open: true,
		error: null,
		account: SIGNED_IN,
		deleted: null,
		sessionAvatarUrl: null,
		features: [],
		planRefusal: () => null,
		buildFlags: { error: null, loaded: true, profile: 'full', sandboxed: false },
		absentInThisBuild: [],
		disabledLocally: [],
		everythingAvailable: true,
		confirmingDelete: false,
		busy: false,
		onPanelKeydown: fn(),
		onClose: fn(),
		onSignOut: fn(),
		onDelete: fn(),
		onAskDelete: fn(),
		onCancelDelete: fn(),
		hostedBilling: { eligible: false, provider: null, standings: [], loading: false, error: null },
		billingBusy: false,
		onSubscribe: fn(),
		onManage: fn()
	}
} satisfies Meta<typeof AccountOverlayView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Signed in on a build where nothing is switched off. */
export const SignedIn: Story = {};

/** Not signed in: the identity section says so and the danger zone is hidden. */
export const SignedOut: Story = {
	args: { account: { ...SIGNED_IN, signed_in: false, user: null } }
};

/** The account is still being read from the daemon. */
export const Loading: Story = { args: { account: null } };

/** The account read failed; the failure is shown verbatim. */
export const LoadFailed: Story = {
	args: { account: null, error: 'GET /api/v1/account failed: 503 daemon starting' }
};

/** Plan-gated features, one included with a quota and one refused. */
export const WithFeatures: Story = {
	args: {
		features: [
			{
				feature_id: 'cloud.stems',
				label: 'Cloud stem separation',
				note: '',
				entitled: true,
				quota: 50,
				server_side: true
			},
			{
				feature_id: 'cloud.sync',
				label: 'CloudSync',
				note: '',
				entitled: false,
				quota: null,
				server_side: true
			}
		],
		planRefusal: (featureId: string) =>
			featureId === 'cloud.sync' ? 'Not on the Free plan.' : null
	}
};

/** A store build with one capability removed and one flag turned off locally. */
export const SomeFlagsOff: Story = {
	args: {
		everythingAvailable: false,
		buildFlags: { error: null, loaded: true, profile: 'app_store', sandboxed: true },
		absentInThisBuild: [
			{
				flag: {
					...FLAG_BASE,
					flag_id: 'usb.export',
					enabled: false,
					note: 'ON everywhere except the Mac App Store build.'
				},
				refusal: 'This build of openDJ does not include USB export.'
			}
		],
		disabledLocally: [
			{ ...FLAG_BASE, flag_id: 'library.watch', enabled: false, note: '', overridden: true }
		]
	}
};

/** The build capabilities could not be read. */
export const FlagsFailed: Story = {
	args: {
		buildFlags: {
			error: 'GET /api/v1/flags failed: 500',
			loaded: false,
			profile: 'full',
			sandboxed: false
		}
	}
};

/** Step two of deletion: the irreversible confirm. */
export const ConfirmingDelete: Story = { args: { confirmingDelete: true } };

/** After a delete, the daemon's confirmation is shown. */
export const Deleted: Story = {
	args: {
		account: { ...SIGNED_IN, signed_in: false, user: null },
		deleted: 'Account data deleted from this machine.'
	}
};

/** Closed: nothing renders. */
export const Closed: Story = { tags: ['renders-nothing'], args: { open: false } };

/** The hosted hub is reachable: the CloudSync plan section lists each feature's standing. */
export const HostedBilling: Story = {
	args: {
		hostedBilling: {
			eligible: true,
			provider: 'polar',
			standings: [{ feature_id: 'cloudsync', state: 'active' }],
			loading: false,
			error: null
		}
	}
};
