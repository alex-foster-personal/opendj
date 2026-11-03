import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import TrackActionsView from './TrackActionsView.svelte';

/**
 * The track page's action row. Show in Finder raises `onReveal` (Actions
 * panel); the container is what calls the reveal endpoint. Each hand-off
 * button is enabled only when the engine found its app on this machine; the
 * container computes that state and passes the rows in.
 */
const meta = {
	title: 'Components/TrackActionsView',
	component: TrackActionsView,
	tags: ['autodocs'],
	args: {
		revealTitle: 'Show this track in Finder',
		onReveal: fn(),
		buttons: [
			{
				app: 'rekordbox',
				label: 'Open in Rekordbox',
				disabled: false,
				title: 'Rekordbox is installed on this machine'
			},
			{
				app: 'djay',
				label: 'Open in djay',
				disabled: true,
				title: 'djay is not installed'
			}
		],
		onOpenIn: fn(),
		outcome: null
	}
} satisfies Meta<typeof TrackActionsView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The row with one app available and one absent. */
export const Default: Story = {};

/** A hand-off outcome line after a click that the engine refused. */
export const WithError: Story = {
	args: { outcome: { text: 'djay did not open the track', error: true } }
};
