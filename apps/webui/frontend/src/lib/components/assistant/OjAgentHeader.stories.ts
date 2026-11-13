import type { Meta, StoryObj } from '@storybook/sveltekit';

import OjAgentHeader from './OjAgentHeader.svelte';
import { OJ_AGENT_IDLE } from './oj-agent-header';
import { OJ_MOTION_FULL } from './oj-glass-motion';

/** OJ's glass, heading and juice fill as the agent sidebar header draws them (#13711). */
const meta = {
	title: 'Components/OjAgentHeader',
	component: OjAgentHeader,
	tags: ['autodocs'],
	args: { look: OJ_AGENT_IDLE, mode: null }
} satisfies Meta<typeof OjAgentHeader>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = {};
export const Streaming: Story = {
	args: {
		look: {
			glass: 'think',
			glassBubbles: true,
			motion: OJ_MOTION_FULL,
			fillPhase: 'streaming',
			fillMode: 'animated',
			streamedChars: 240
		},
		mode: 'gateway'
	}
};
export const Landed: Story = {
	args: { look: { ...OJ_AGENT_IDLE, glass: 'smile', fillPhase: 'done', fillMode: 'animated' } }
};
/** Mood beats (#13937): a failed or declined call sloshes; a write that lands smiles with bubbles. */
export const Slosh: Story = {
	args: { look: { ...OJ_AGENT_IDLE, glass: 'slosh', motion: OJ_MOTION_FULL } }
};
export const WriteLanded: Story = {
	args: { look: { ...OJ_AGENT_IDLE, glass: 'smile', glassBubbles: true, motion: OJ_MOTION_FULL } }
};
