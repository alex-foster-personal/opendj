import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import type { PlaylistNode } from '$lib/rb/library-types';

import { contracts } from './AddToPlaylistPicker.contract';
import AddToPlaylistPicker from './AddToPlaylistPicker.svelte';

const node = (id: number, name: string, count: number): PlaylistNode => ({
	playlist_id: String(id),
	name,
	track_count: count,
	broken_count: 0,
	kind: 'playlist',
	children: []
});

const few: PlaylistNode[] = [
	node(1, 'Warmup Set', 24),
	node(2, 'Sunrise Closers', 18),
	node(3, 'Peak Hour', 61)
];

const many: PlaylistNode[] = Array.from({ length: 30 }, (_, i) =>
	node(100 + i, `Crate ${String(i + 1).padStart(2, '0')} - Quiet Kiln Session`, 10 + i * 3)
);

const meta = {
	title: 'Contracts/Browser/AddToPlaylistPicker',
	component: AddToPlaylistPicker,
	tags: ['autodocs'],
	argTypes: { open: { control: 'boolean' } },
	args: { open: true, playlists: few, onpick: fn(), onclose: fn() }
} satisfies Meta<typeof AddToPlaylistPicker>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Few: Story = contracts.Few;
export const ManyWithFilter: Story = contracts.ManyWithFilter;
export const Empty: Story = contracts.Empty;
export const Closed: Story = contracts.Closed;

/** A short list: no filter box, focus lands on the first playlist. */
export const FewPlaylists: Story = {
	play: async ({ canvasElement }) => {
		const picker = within(canvasElement).getByTestId('add-to-playlist-picker');
		await expect(picker).toHaveAttribute('aria-label', 'Add to playlist');
		await expect(picker.querySelectorAll('.m-name').length).toBe(3);
		await expect(within(canvasElement).getByText('Sunrise Closers')).toBeInTheDocument();
	}
};

/** Past the threshold: the filter box shows and takes focus; type to narrow. */
export const ManyPlaylists: Story = { args: { playlists: many },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('.m-name').length).toBe(30);
	}
};

/** Very long names ellipsize while the count stays visible. */
export const LongNames: Story = {
	args: {
		playlists: [
			node(
				1,
				'Late Night Deep Cuts and Extended Mixes from the Quiet Kiln Residency 2026 - Part Two',
				1204
			),
			node(2, 'Short', 3)
		]
	},
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('.m-name')).toHaveLength(2);
	}
};
