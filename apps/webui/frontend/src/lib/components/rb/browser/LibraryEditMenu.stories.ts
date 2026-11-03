import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import { contracts } from './LibraryEditMenu.contract';
import LibraryEditMenu from './LibraryEditMenu.svelte';

const meta = {
	title: 'Contracts/Browser/LibraryEditMenu',
	component: LibraryEditMenu,
	tags: ['autodocs'],
	args: { hasSelection: true, onpick: fn() }
} satisfies Meta<typeof LibraryEditMenu>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Closed: Story = contracts.Closed;
export const WithSelection: Story = contracts.WithSelection;
export const NoSelection: Story = contracts.NoSelection;
export const Dismiss: Story = contracts.Dismiss;
