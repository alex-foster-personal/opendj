import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import ToolTrace from './ToolTrace.svelte';

const meta = {
	title: 'Components/ChatKit/ToolTrace',
	component: ToolTrace,
	tags: ['autodocs'],
	args: { steps: [], streaming: false }
} satisfies Meta<typeof ToolTrace>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Empty: Story = {};

export const AllStatuses: Story = {
	args: {
		steps: [
			{ id: 'a', name: 'library_read', status: 'running' },
			{ id: 'b', name: 'app_state', status: 'ok' },
			{ id: 'c', name: 'command', status: 'failed' }
		]
	}
};

/** Running tool row while the assistant NDJSON stream is still open (no `done` yet). */
export const MidStream: Story = {
	args: {
		streaming: true,
		steps: [{ id: 'mid-1', name: 'library_read', status: 'running' }]
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		const trace = canvas.getByTestId('chat-tool-trace');
		await expect(trace).toHaveAttribute('data-streaming', 'true');
		const row = canvas.getByTitle('Tool running');
		await expect(row).toHaveAttribute('data-step-id', 'mid-1');
		await expect(row).toHaveAttribute('data-status', 'running');
	}
};
