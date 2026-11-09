import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn, userEvent, within } from 'storybook/test';

import { RELATED_CRITERIA_DEFAULTS } from '$lib/rb/related-criteria-prefs';

import RelatedCriteriaPopover from './RelatedCriteriaPopover.svelte';

/**
 * The Related tab's criteria cog. Props only: the parent saves prefs and
 * refetches, so these stories never touch ui-prefs or the daemon.
 */
const meta = {
	title: 'Components/Browser/RelatedCriteriaPopover',
	component: RelatedCriteriaPopover,
	tags: ['autodocs'],
	args: {
		criteria: RELATED_CRITERIA_DEFAULTS,
		onchange: fn(),
		onreset: fn()
	}
} satisfies Meta<typeof RelatedCriteriaPopover>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Resting: closed cog beside the Related tab label. */
export const Resting: Story = {};

/** Open panel with the default #9753 criteria set. */
export const Open: Story = {
	play: async ({ canvasElement }) => {
		await userEvent.click(within(canvasElement).getByTestId('related-criteria-cog'));
	}
};
