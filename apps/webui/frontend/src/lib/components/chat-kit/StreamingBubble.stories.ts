import type { Meta, StoryObj } from '@storybook/sveltekit';

import StreamingBubble from './StreamingBubble.svelte';

const meta = {
	title: 'Components/ChatKit/StreamingBubble',
	component: StreamingBubble,
	tags: ['autodocs'],
	args: { text: '', delta: '', streaming: false }
} satisfies Meta<typeof StreamingBubble>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Empty: Story = {};

export const Settled: Story = {
	args: { text: 'The library has 1,284 tracks with artwork.', streaming: false }
};

export const PartialDelta: Story = {
	args: { text: 'Beat sync locks deck 2 to ', delta: 'deck 1 bar phase.', streaming: true }
};
