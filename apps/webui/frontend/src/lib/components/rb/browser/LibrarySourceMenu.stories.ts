import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import LibrarySourceMenu from './LibrarySourceMenu.svelte';

const children = createRawSnippet(() => ({
	render: () =>
		'<div style="padding:4px 8px;font-family:var(--rb-font);font-size:var(--rb-fs-label)">Library sources (right-click for menu)</div>'
}));

const meta = {
	title: 'Components/Browser/LibrarySourceMenu',
	component: LibrarySourceMenu,
	tags: ['autodocs'],
	args: { children }
} satisfies Meta<typeof LibrarySourceMenu>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
	args: { children }
};
