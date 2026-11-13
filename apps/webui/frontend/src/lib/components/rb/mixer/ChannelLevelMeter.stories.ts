import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import ChannelLevelMeter from './ChannelLevelMeter.svelte';

/** Ten discrete segments the meter always renders, lit or not. */
const SEGMENT_COUNT = 10;

/**
 * Post-EQ, post-channel-fader level meter for one channel. Stopped, every
 * segment stays unlit; playing, it polls the deck's meter tap each frame.
 */
const meta = {
	title: 'Contracts/Mixer/ChannelLevelMeter',
	component: ChannelLevelMeter,
	tags: ['autodocs'],
	args: { deckId: 1, playing: false },
	argTypes: {
		deckId: { control: 'inline-radio', options: [1, 2] },
		playing: { control: 'boolean' }
	}
} satisfies Meta<typeof ChannelLevelMeter>;
export default meta;
type Story = StoryObj<typeof meta>;

const expectOneMeter = (canvasElement: HTMLElement): HTMLElement => {
	const meters = canvasElement.querySelectorAll<HTMLElement>('.rb-channel-level-meter');
	expect(meters).toHaveLength(1);
	expect(meters[0].querySelectorAll('.rb-channel-level-meter-segment')).toHaveLength(SEGMENT_COUNT);
	return meters[0];
};

/** Deck 1 stopped: full segment column, nothing lit. */
export const StoppedDeck1: Story = {
	play: async ({ canvasElement }) => {
		const meter = expectOneMeter(canvasElement);
		await expect(meter.querySelectorAll('.rb-channel-level-meter-segment.lit')).toHaveLength(0);
		await expect(meter.getAttribute('aria-valuetext')).toBe('silent');
	}
};

/** Deck 2 stopped: full segment column, nothing lit. */
export const StoppedDeck2: Story = {
	args: { deckId: 2 },
	play: async ({ canvasElement }) => {
		const meter = expectOneMeter(canvasElement);
		await expect(meter.querySelectorAll('.rb-channel-level-meter-segment.lit')).toHaveLength(0);
	}
};

/** Deck 1 playing: the meter mounts and polls its tap. */
export const PlayingDeck1: Story = {
	args: { playing: true },
	play: async ({ canvasElement }) => {
		const meter = expectOneMeter(canvasElement);
		await expect(meter.getAttribute('aria-label')).toContain('channel 1');
	}
};

/** Deck 2 playing: the meter mounts with its full segment column. */
export const PlayingDeck2: Story = {
	args: { deckId: 2, playing: true },
	play: async ({ canvasElement }) => {
		const meter = expectOneMeter(canvasElement);
		await expect(meter.getAttribute('aria-label')).toContain('channel 2');
	}
};
