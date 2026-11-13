import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import AgentSidebarComposer from './AgentSidebarComposer.svelte';
import AgentSidebarStoryDock from './AgentSidebarStoryDock.svelte';

const meta = {
	title: 'Components/AgentSidebarComposer',
	component: AgentSidebarComposer,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	decorators: [() => ({ Component: AgentSidebarStoryDock })],
	args: {
		draft: '',
		contexts: [],
		tierChoice: 'auto',
		composerOpen: true,
		held: false,
		busy: false,
		availability: 'ready',
		outcome: 'send',
		composerNote: null,
		instantCheckNote: null,
		ontier: fn(),
		onsubmit: fn(),
		onstop: fn(),
		onkeydown: fn()
	}
} satisfies Meta<typeof AgentSidebarComposer>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Ready: Story = {
	args: {
		draft: 'Find techno around 128 BPM',
		contexts: [{ id: 'playlist', label: 'This playlist', title: 'Playlist: Warm-up (42 tracks)' }]
	}
};

export const KeylessRefuse: Story = {
	args: {
		availability: 'keyless',
		outcome: 'refuse',
		draft: 'find me something dark',
		composerNote: 'Keyless mode sends instant engine commands only',
		instantCheckNote: null
	}
};

export const HeldStop: Story = {
	args: {
		held: true,
		busy: true,
		outcome: 'ignore',
		draft: 'play deck 2'
	}
};
