import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import QuitConfirmOverlayView from './QuitConfirmOverlayView.svelte';

/**
 * The desktop shell's quit confirmation. The container follows the quit
 * gate; Cancel and Confirm raise callbacks (Actions panel) and quit nothing.
 */
const meta = {
	title: 'Components/Shell/QuitConfirmOverlayView',
	component: QuitConfirmOverlayView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: { open: true, onCancel: fn(), onConfirm: fn() }
} satisfies Meta<typeof QuitConfirmOverlayView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The dialog over its backdrop. */
export const Open: Story = {};

/** The gate is closed: nothing renders. */
export const Closed: Story = { tags: ['renders-nothing'], args: { open: false } };
