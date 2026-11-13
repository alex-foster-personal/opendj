import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { Item } from '$lib/assistant/agent-session';

import AgentSidebarConversationLog from './AgentSidebarConversationLog.svelte';
import AgentSidebarStoryDock from './AgentSidebarStoryDock.svelte';

const USER: Item = { kind: 'user', text: 'Find techno around 128 BPM I have not played lately' };
const TEXT: Item = {
	kind: 'text',
	text: 'Three techno tracks near **128 BPM** you have not played since spring.'
};
const TIER: Item = {
	kind: 'tier',
	tier: 'fast',
	model: 'google/gemini-3.7-flash',
	reason: 'library question'
};

const meta = {
	title: 'Components/AgentSidebarConversationLog',
	component: AgentSidebarConversationLog,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	decorators: [() => ({ Component: AgentSidebarStoryDock })],
	args: {
		items: [],
		busy: false,
		done: null,
		availability: 'ready',
		suggestions: [],
		decisions: {},
		open: {},
		canAnswerHeld: false,
		nextHeld: undefined,
		thinking: false,
		onsend: fn(),
		onapprove: fn(),
		ondecline: fn(),
		ontoggle: fn()
	}
} satisfies Meta<typeof AgentSidebarConversationLog>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Empty: Story = {
	args: {
		suggestions: [
			'Find techno around 128 BPM I have not played lately',
			"What's loaded on each deck?",
			'Turn the system volume down a bit'
		]
	}
};

export const Transcript: Story = {
	args: { items: [USER, TIER, TEXT] }
};

export const Checking: Story = {
	args: { availability: 'checking' }
};
