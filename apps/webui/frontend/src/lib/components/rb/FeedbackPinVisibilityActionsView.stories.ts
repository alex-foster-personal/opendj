import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import FeedbackPinVisibilityActionsView from './FeedbackPinVisibilityActionsView.svelte';

/**
 * Comment-pin toggles from FeedbackWidget's explainer. The third row
 * ("other users' pins") is always inert - it has no data source yet.
 */
const meta = {
	title: 'Components/FeedbackPinVisibilityActions',
	component: FeedbackPinVisibilityActionsView,
	tags: ['autodocs'],
	args: {
		pinsVisible: false,
		showAgentPins: false,
		ontoggle: fn(),
		ontoggleagentpins: fn(),
		pinVisibleDays: 0,
		onvisibledays: fn()
	}
} satisfies Meta<typeof FeedbackPinVisibilityActionsView>;

export default meta;
type Story = StoryObj<typeof meta>;

export const AllOff: Story = {};

export const PinsVisible: Story = { args: { pinsVisible: true } };

export const AllOn: Story = { args: { pinsVisible: true, showAgentPins: true } };

/** Pins older than two weeks are hidden. */
export const FourteenDays: Story = { args: { pinsVisible: true, pinVisibleDays: 14 } };
