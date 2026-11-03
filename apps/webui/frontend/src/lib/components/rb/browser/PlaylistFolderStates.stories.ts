import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import { contracts } from './PlaylistFolderStates.contract';
import PlaylistFolderStates from './PlaylistFolderStates.svelte';

const meta = {
	title: 'Contracts/Browser/PlaylistFolderStates',
	component: PlaylistFolderStates,
	tags: ['autodocs'],
	args: {
		nodeCount: 0,
		playlistsLoading: false,
		playlistsError: null,
		hiddenBrokenPlaylistCount: 0,
		oncreate: fn()
	}
} satisfies Meta<typeof PlaylistFolderStates>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Loading: Story = contracts.Loading;
export const Error: Story = contracts.Error;
export const EmptyCreate: Story = contracts.EmptyCreate;
export const EmptyInert: Story = contracts.EmptyInert;

/** Empty tree with creation wired: a clickable call to action. */
export const EmptyWithCreate: Story = {
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTestId('playlists-empty-cta')).toHaveTextContent('Create your first playlist');
	}
};

/** Tree has nodes, so nothing renders. */
export const HasNodes: Story = {
	tags: ['renders-nothing'],
	args: { nodeCount: 12 },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="playlists-empty-cta"]')).toBeNull();
		await expect(canvasElement.querySelector('[data-testid="playlists-empty"]')).toBeNull();
	}
};

/** Empty but broken playlists are hidden: the empty prompt is suppressed. */
export const OnlyHiddenBroken: Story = {
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="playlists-empty-cta"]')).toBeNull();
		await expect(canvasElement.querySelector('[data-testid="playlists-empty"]')).toBeNull();
	},
	tags: ['renders-nothing'],
	args: { hiddenBrokenPlaylistCount: 3 }
};
