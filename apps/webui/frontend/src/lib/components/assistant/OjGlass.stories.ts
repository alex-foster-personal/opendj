import type { Meta, StoryObj } from '@storybook/sveltekit';

import OjGlass from './OjGlass.svelte';

/** The OJ glass character in each of its states, at full motion. */
const meta = {
	title: 'Components/OjGlass',
	component: OjGlass,
	tags: ['autodocs'],
	args: { state: 'idle', bubbles: true, size: 96 }
} satisfies Meta<typeof OjGlass>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = {};
export const Think: Story = { args: { state: 'think' } };
