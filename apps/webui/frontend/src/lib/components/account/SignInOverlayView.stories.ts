import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import SignInOverlayView from './SignInOverlayView.svelte';

/**
 * The full-screen Google sign-in blocker. The container reads the sign-in
 * overlay state and the status text for its phase; Cancel (offered only
 * while waiting on the system browser) raises `onCancel`.
 */
const meta = {
	title: 'Components/Account/SignInOverlayView',
	component: SignInOverlayView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: { open: true, phase: 'starting', statusText: 'Starting sign-in...', onCancel: fn() }
} satisfies Meta<typeof SignInOverlayView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The first moment of an attempt: spinner and status, no Cancel. */
export const Starting: Story = {};

/** Waiting on the system browser: Cancel is offered and Escape cancels too. */
export const Waiting: Story = {
	args: { phase: 'waiting', statusText: 'Waiting for Google sign-in...' }
};

/** Not signing in: nothing renders. */
export const Closed: Story = { tags: ['renders-nothing'], args: { open: false, phase: 'idle', statusText: '' } };
