import type { Meta, StoryObj } from '@storybook/sveltekit';
import BpmDisputeCard from './BpmDisputeCard.svelte';

// GRID-OCTAVE-02: the hover card on a disputed BPM; one click stores the other tempo.
const meta = {
	title: 'Components/BpmDisputeCard',
	component: BpmDisputeCard,
	tags: ['autodocs'],
	args: {
		alternatives: [
			{ source: 'own', bpm: 87 },
			{ source: 'mik', bpm: 174 }
		],
		shownBpm: 87,
		confidence: 0.62,
		note: null,
		anchor: { left: 120, top: 80, width: 48, height: 18 },
		onpick: async () => {},
		onerror: () => {},
		onenter: () => {},
		onleave: () => {},
		onDismiss: () => {}
	}
} satisfies Meta<typeof BpmDisputeCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const OursShown: Story = {};

export const WithCellNote: Story = {
	args: { shownBpm: 174, note: 'from Mixed In Key tag' }
};
