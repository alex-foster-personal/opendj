import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import { contracts } from './ContextMenu.contract';
import ContextMenu from './ContextMenu.svelte';

/**
 * Right-click menu. Items without a `run` render inert (aria-disabled, hover
 * title from plannedTitle). `x`/`y` are the click point; the menu clamps
 * itself into the viewport.
 */
const meta = {
	title: 'Contracts/Root/ContextMenu',
	component: ContextMenu,
	tags: ['autodocs'],
	args: { x: 80, y: 60, onclose: fn() }
} satisfies Meta<typeof ContextMenu>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Items: Story = contracts.Items;
export const Activate: Story = contracts.Activate;
export const Keyboard: Story = contracts.Keyboard;
export const Clamped: Story = contracts.Clamped;

/** Every item is wired to a handler. */
export const Default: Story = {
	args: {
		items: [
			{ id: 'load-a', label: 'Load to deck A', run: fn() },
			{ id: 'load-b', label: 'Load to deck B', run: fn() },
			{ id: 'reveal', label: 'Reveal in Finder', run: fn() }
		]
	},
	play: async ({ canvasElement }) => {
		const items = within(canvasElement).getAllByRole('menuitem');
		await expect(items.map((el) => el.textContent?.trim())).toEqual([
			'Load to deck A',
			'Load to deck B',
			'Reveal in Finder'
		]);
	}
};

/** Items with no `run` render inert with the planned-feature title. */
export const WithInertItems: Story = {
	args: {
		items: [
			{ id: 'load-a', label: 'Load to deck A', run: fn() },
			{ id: 'send-usb', label: 'Send to USB' },
			{ id: 'history', label: 'Show play history' }
		]
	},
	play: async ({ canvasElement }) => {
		const menu = within(canvasElement).getByRole('menu');
		await expect(menu.querySelector('[aria-disabled="true"]')).not.toBeNull();
		await expect(within(canvasElement).getByText('Send to USB').closest('[role="menuitem"]')).toHaveAttribute(
			'aria-disabled',
			'true'
		);
		await expect(within(canvasElement).getByText('Load to deck A').closest('[role="menuitem"]')).not.toHaveAttribute(
			'aria-disabled'
		);
	}
};

/** Items with a boolean `checked` become menuitemcheckbox entries. */
export const CheckableItems: Story = {
	args: {
		items: [
			{ id: 'show-art', label: 'Show artwork', checked: true, run: fn() },
			{ id: 'show-bpm', label: 'Show BPM column', checked: false, run: fn() }
		]
	},
	play: async ({ canvasElement }) => {
		const boxes = within(canvasElement).getAllByRole('menuitemcheckbox');
		await expect(boxes).toHaveLength(2);
		await expect(boxes[0]).toHaveAttribute('aria-checked', 'true');
		await expect(boxes[1]).toHaveAttribute('aria-checked', 'false');
	}
};

/** Click point near the bottom-right corner: the menu clamps into view. */
export const NearViewportEdge: Story = {
	args: {
		x: 5000,
		y: 5000,
		items: [{ id: 'one', label: 'Load to deck A', run: fn() }]
	},
	play: async ({ canvasElement }) => {
		const menu = within(canvasElement).getByRole('menu');
		const box = menu.getBoundingClientRect();
		await expect(box.right).toBeLessThanOrEqual(window.innerWidth);
		await expect(box.bottom).toBeLessThanOrEqual(window.innerHeight);
	}
};

export const LongLabel: Story = {
	args: {
		items: [
			{
				id: 'long',
				label: 'Add "Night Drive (Extended Mix)" by Lumen Fields to the Friday Warmup smartlist',
				run: fn()
			}
		]
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByRole('menuitem')).toHaveTextContent('Friday Warmup smartlist');
	}
};

/**
 * A track row's menu as TrackContextMenu binds it: one collection track with
 * audio on disk, every item wired.
 */
export const TrackRow: Story = {
	args: {
		items: [
			{ id: 'load-1', label: 'Load to deck 1', title: 'Load this track on deck 1', run: fn() },
			{ id: 'load-2', label: 'Load to deck 2', title: 'Load this track on deck 2', run: fn() },
			{ id: 'add-playlist', label: 'Add to playlist...', title: 'Add to a playlist', run: fn() },
			{ id: 'show-in-playlists', label: 'Show in playlists', title: 'Playlists holding this track', run: fn() },
			{ id: 'finder', label: 'Reveal in Finder', title: 'Show the audio file', run: fn() },
			{ id: 'analyze', label: 'Analyze', title: 'Re-run analysis', run: fn() }
		]
	},
	play: async ({ canvasElement }) => {
		const items = within(canvasElement).getAllByRole('menuitem');
		await expect(items.map((el) => el.textContent?.trim())).toEqual([
			'Load to deck 1',
			'Load to deck 2',
			'Add to playlist...',
			'Show in playlists',
			'Reveal in Finder',
			'Analyze'
		]);
	}
};

/** Several tracks selected: deck loads are gone and unwired items say why. */
export const TrackManySelected: Story = {
	args: {
		items: [
			{ id: 'add-playlist', label: 'Add 12 tracks to playlist...', title: 'Add to a playlist', run: fn() },
			{ id: 'bulk-edit', label: 'Bulk edit...', title: 'not available in this view - Edit tags on every selected track' },
			{ id: 'copy-path', label: 'Copy paths', title: 'Copy the file paths', run: fn() },
			{ id: 'remove-library', label: 'Remove from library', title: 'Remove the selected tracks', run: fn() }
		]
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getAllByRole('menuitem')).toHaveLength(4);
		await expect(canvas.getByText('Bulk edit...').closest('[role="menuitem"]')).toHaveAttribute(
			'aria-disabled',
			'true'
		);
	}
};

/** A playlist tree row's menu as TreeContextMenu binds it: every item wired. */
export const TreePlaylist: Story = {
	args: {
		items: [
			{ id: 'new-playlist', label: 'New playlist', title: 'Create a playlist', run: fn() },
			{ id: 'new-smartlist', label: 'New smartlist', title: 'Create a smartlist', run: fn() },
			{ id: 'rename', label: 'Rename', title: 'Rename this playlist', run: fn() },
			{ id: 'duplicate', label: 'Duplicate', title: 'Copy this playlist', run: fn() },
			{
				id: 'forbid-duplicates',
				label: 'Forbid duplicates',
				title: 'Reject a track already in this playlist',
				checked: true,
				run: fn()
			},
			{ id: 'delete', label: 'Delete', title: 'Delete this playlist', run: fn() }
		]
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getAllByRole('menuitem')).toHaveLength(5);
		const boxes = canvas.getAllByRole('menuitemcheckbox');
		await expect(boxes).toHaveLength(1);
		await expect(boxes[0]).toHaveAttribute('aria-checked', 'true');
	}
};

/** The tree's Playlists header, from a host that did not wire smartlist create. */
export const TreeRootUnwiredItem: Story = {
	args: {
		items: [
			{ id: 'new-playlist', label: 'New playlist', title: 'Create a playlist', run: fn() },
			{
				id: 'new-smartlist',
				label: 'New smartlist',
				title: 'not available in this view - Create a smartlist'
			}
		]
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getAllByRole('menuitem')).toHaveLength(2);
		await expect(canvas.getByText('New smartlist').closest('[role="menuitem"]')).toHaveAttribute(
			'aria-disabled',
			'true'
		);
	}
};
