import type { Meta, StoryObj } from '@storybook/sveltekit';

import OjGlassGraphic from './OjGlassGraphic.svelte';
import { ojGlassClasses } from './oj-glass-gestures';
import { OJ_MOTION_FULL } from './oj-glass-motion';

/** The OJ glass SVG drawing on its own, with the classes its parent derives. */
const meta = {
	title: 'Components/OjGlassGraphic',
	component: OjGlassGraphic,
	tags: ['autodocs'],
	args: {
		classes: ojGlassClasses('idle', null, OJ_MOTION_FULL).join(' '),
		state: 'idle',
		bubbles: true,
		effective: OJ_MOTION_FULL,
		size: 96,
		clipId: 'oj-glass-story-graphic'
	}
} satisfies Meta<typeof OjGlassGraphic>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = {};
export const Slosh: Story = {
	args: { state: 'slosh', classes: ojGlassClasses('slosh', null, OJ_MOTION_FULL).join(' ') }
};
