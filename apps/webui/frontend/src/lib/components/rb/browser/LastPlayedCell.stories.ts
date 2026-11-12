import type { Meta, StoryObj } from '@storybook/sveltekit';

import LastPlayedCell from './LastPlayedCell.svelte';
import type { PlayFacts } from './last-played';

/**
 * Performance table last-played cell. Stories pass `wire` play facts and keep
 * `playCount` at 0 so the cell does not enqueue a live plays fetch.
 */
const meta = {
	title: 'Components/Browser/LastPlayedCell',
	component: LastPlayedCell,
	tags: ['autodocs']
} satisfies Meta<typeof LastPlayedCell>;

export default meta;
type Story = StoryObj<typeof meta>;

const wire: PlayFacts = {
	counts: { rekordbox: 3, gig: 0, rekordbox_usb: 0, serato: 0, djay: 0, traktor: 0 },
	at: {
		rekordbox: '2026-11-10T12:00:00.000Z',
		gig: null,
		rekordbox_usb: null,
		serato: null,
		djay: null,
		traktor: null
	}
};

export const RekordboxDaysAgo: Story = {
	args: { stableId: 'lib-track-1', playCount: 0, wire }
};

export const NoPlays: Story = {
	args: { stableId: 'lib-track-2', playCount: 0, wire: null }
};
