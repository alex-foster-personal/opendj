import type { Meta, StoryObj } from '@storybook/sveltekit';

import VersionStackPanelLoader from './VersionStackPanelLoader.svelte';

const meta = {
	title: 'Browser/VersionStackPanelLoader',
	component: VersionStackPanelLoader,
	tags: ['autodocs'],
	args: { playlistId: null, rowPosition: undefined, top: 40, onclose: () => {} }
} satisfies Meta<typeof VersionStackPanelLoader>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Closed: Story = { args: { stableId: null } };
export const Open: Story = { args: { stableId: 'stack-1' } };
