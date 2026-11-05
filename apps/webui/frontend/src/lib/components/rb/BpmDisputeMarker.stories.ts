import type { Meta, StoryObj } from '@storybook/sveltekit';
import BpmDisputeMarker from './BpmDisputeMarker.svelte';

// GRID-OCTAVE-02: the glyph beside a BPM whose tempo our analysis and Mixed In Key disagree on.
const meta = {
	title: 'Components/BpmDisputeMarker',
	component: BpmDisputeMarker,
	tags: ['autodocs']
} satisfies Meta<typeof BpmDisputeMarker>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
