import type { Meta, StoryObj } from '@storybook/sveltekit';

import OjGlassClient from './OjGlassClient.svelte';
import { OJ_MOTION_FULL } from './oj-glass-motion';

/** The client-side OJ glass: picks idle gestures and honors the motion policy. */
const meta = {
	title: 'Components/OjGlassClient',
	component: OjGlassClient,
	tags: ['autodocs'],
	args: {
		state: 'idle',
		bubbles: true,
		size: 96,
		policy: OJ_MOTION_FULL,
		clipId: 'oj-glass-story-client'
	}
} satisfies Meta<typeof OjGlassClient>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = {};
export const Smile: Story = { args: { state: 'smile' } };
