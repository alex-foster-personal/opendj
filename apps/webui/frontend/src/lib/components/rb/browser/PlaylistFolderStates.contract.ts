import { expect, fn, userEvent, within } from 'storybook/test';

/**
 * PlaylistFolderStates UI contract, shared by the Svelte story
 * (PlaylistFolderStates.stories.ts) and its React twin
 * (PlaylistFolderStates.stories.tsx). See src/react/README.md.
 */

type Args = {
	nodeCount: number;
	playlistsLoading: boolean;
	playlistsError: string | null;
	hiddenBrokenPlaylistCount: number;
	oncreate: (() => void) | undefined;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const base = { nodeCount: 0, playlistsLoading: false, playlistsError: null, hiddenBrokenPlaylistCount: 0 };

export const contracts = {
	/** Empty tree still loading: inert loading row, loading wins over an error. */
	Loading: {
		args: { ...base, playlistsLoading: true, playlistsError: 'stale', oncreate: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const row = within(canvasElement).getByTestId('playlists-loading');
			await expect(row).toHaveClass('row', 'child', 'rb-inert');
			await expect(row).toHaveTextContent('Loading playlists...');
			await expect(canvasElement.querySelectorAll('.row')).toHaveLength(1);
		}
	},
	/** Load failed: inert row with the error text on hover. */
	Error: {
		args: { ...base, playlistsError: 'HTTP 503 from /api/v1/playlists', oncreate: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const row = within(canvasElement).getByTestId('playlists-error');
			await expect(row).toHaveClass('row', 'child', 'rb-inert');
			await expect(row).toHaveTextContent('Playlist load failed');
			await expect(row).toHaveAttribute('title', 'HTTP 503 from /api/v1/playlists');
		}
	},
	/** Truly empty with creation wired: a create button that calls oncreate. */
	EmptyCreate: {
		args: { ...base, oncreate: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const btn = within(canvasElement).getByRole('button', { name: '+ Create your first playlist' });
			await expect(btn).toHaveClass('row', 'child', 'empty-cta');
			await expect(btn).toHaveAttribute('data-testid', 'playlists-empty-cta');
			await expect(btn).toHaveAttribute('type', 'button');
			await userEvent.click(btn);
			await expect(args.oncreate).toHaveBeenCalledOnce();
		}
	},
	/** Empty without creation wired: inert text, no button. */
	EmptyInert: {
		args: { ...base, oncreate: undefined } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const row = within(canvasElement).getByTestId('playlists-empty');
			await expect(row).toHaveClass('row', 'child', 'rb-inert');
			await expect(row).toHaveTextContent('no playlists yet');
			await expect(canvasElement.querySelector('button')).toBeNull();
		}
	}
};
