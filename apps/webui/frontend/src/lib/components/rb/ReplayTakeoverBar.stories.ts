import type { Meta, StoryObj } from '@storybook/sveltekit';
import ReplayTakeoverBar from './ReplayTakeoverBar.svelte';
import type { ReplayLiveControl } from '$lib/rb/set-replay-takeover.svelte';

const meta = {
	title: 'Components/ReplayTakeoverBar',
	component: ReplayTakeoverBar,
	tags: ['autodocs']
} satisfies Meta<typeof ReplayTakeoverBar>;

export default meta;
type Story = StoryObj<typeof meta>;

const twoLive: ReplayLiveControl[] = [
	{ control: 'deck.1.eq.low', source: 'user', at_ms: 0 },
	{ control: 'mixer.crossfader', source: 'agent', at_ms: 1 }
];

export const Replaying: Story = {
	args: {
		live: [],
		takenOverAll: false,
		onTakeOverAll: () => {}
	}
};

export const PerControlLive: Story = {
	args: {
		live: twoLive,
		takenOverAll: false,
		onTakeOverAll: () => {}
	}
};

export const AllLive: Story = {
	args: {
		live: twoLive,
		takenOverAll: true,
		onTakeOverAll: () => {}
	}
};
