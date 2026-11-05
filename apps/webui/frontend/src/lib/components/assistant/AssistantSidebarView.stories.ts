import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import AssistantSidebarView from './AssistantSidebarView.svelte';

/**
 * The setup assistant sidebar shown while the library imports. The
 * container probes GET /api/v1/assistant/status and streams replies; here
 * the conversation is args. The messages are invented.
 */
const meta = {
	title: 'Components/Assistant/AssistantSidebarView',
	component: AssistantSidebarView,
	tags: ['autodocs'],
	args: {
		visible: true,
		mode: 'chat',
		status: { configured: true, model: 'example/model-small' },
		statusError: null,
		messages: [],
		streaming: null,
		sendError: null,
		canSend: false,
		draft: '',
		send: fn(async () => {}),
		onStop: fn()
	}
} satisfies Meta<typeof AssistantSidebarView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Ready, nothing asked yet. */
export const Ready: Story = {};

/** A question typed: Send is enabled. */
export const Drafting: Story = {
	args: { draft: 'How many tracks have no key yet?', canSend: true }
};

/** One turn answered and a reply streaming in, with its character count. */
export const Streaming: Story = {
	args: {
		messages: [
			{ role: 'user', content: 'Which playlists are biggest?' },
			{ role: 'assistant', content: 'Warehouse Warmups has 128 tracks, Deep Cuts 311.' },
			{ role: 'user', content: 'And which are missing artwork?' }
		],
		streaming: 'Looking at the import so far, 14 tracks in Deep Cuts'
	}
};

/** The last reply failed. */
export const SendError: Story = {
	args: {
		messages: [{ role: 'user', content: 'Which playlists are biggest?' }],
		sendError: 'The assistant could not answer right now. Try again.'
	}
};

/** The status probe failed: the human note plus the agent-only detail. */
export const Unavailable: Story = {
	args: { mode: 'unavailable', status: null, statusError: 'HTTP 502 from /api/v1/assistant/status' }
};

/** No key configured: only a hidden marker for agents. */
export const Unconfigured: Story = {
	tags: ['renders-nothing'],
	args: { mode: 'hidden', status: { configured: false, model: '' } }
};
