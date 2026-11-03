import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import UserBaubleView from './UserBaubleView.svelte';

type Auth = ComponentProps<typeof UserBaubleView>['auth'];

const signedIn: Auth = {
	loading: false,
	user: {
		google_sub: 'sub-0001',
		email: 'dj@example.com',
		name: 'Sample DJ',
		avatar_url: null,
		created_at: '2026-09-01T10:00:00Z'
	}
};

/**
 * The account bauble: a circle in the app shell, a labeled pill on
 * /performance (`showLabel`). The container owns sign-in, sign-out and the
 * daemon's OAuth bit; the user is invented and has no picture, so the
 * initial shows and nothing is fetched.
 */
const meta = {
	title: 'Components/UserBaubleView',
	component: UserBaubleView,
	tags: ['autodocs'],
	args: {
		auth: { user: null, loading: false },
		busy: false,
		signInUnavailable: false,
		menuOpen: false,
		onBaubleClick: fn(),
		onOpenAccount: fn(),
		onSignOut: fn()
	}
} satisfies Meta<typeof UserBaubleView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Signed out: the generic avatar outline. */
export const SignedOut: Story = {};

/** Signed out on /performance: the labeled pill at the top bar's size. */
export const SignedOutLabeled: Story = { args: { showLabel: true, size: 20 } };

/** Signed in, no picture: the first letter of the name. */
export const SignedIn: Story = { args: { auth: signedIn } };

/** Signed in, labeled with the account name. */
export const SignedInLabeled: Story = { args: { auth: signedIn, showLabel: true, size: 20 } };

/** The account menu open. */
export const MenuOpen: Story = { args: { auth: signedIn, menuOpen: true } };

/** A sign-in attempt in flight: the control is disabled. */
export const Busy: Story = { args: { busy: true, showLabel: true } };

/** The daemon has no OAuth client: disabled, and the title says how to fix it. */
export const SignInUnavailable: Story = { args: { signInUnavailable: true, showLabel: true } };
