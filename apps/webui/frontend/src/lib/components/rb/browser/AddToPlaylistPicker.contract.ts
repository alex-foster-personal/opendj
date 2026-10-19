import { expect, fireEvent, fn, userEvent, waitFor, within } from 'storybook/test';

import type { PlaylistNode } from '$lib/rb/library-types';

/**
 * AddToPlaylistPicker UI contract, shared by the Svelte story
 * (AddToPlaylistPicker.stories.ts) and its React twin
 * (AddToPlaylistPicker.stories.tsx). See src/react/README.md.
 *
 * The picker is a fixed scrim over the whole viewport, so queries go through
 * the canvas root; focus is checked with waitFor because both renderers move
 * it after the first paint.
 */

type Args = {
	open: boolean;
	playlists: PlaylistNode[];
	onpick: (node: PlaylistNode) => void;
	onclose: () => void;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const node = (i: number, name: string, track_count: number): PlaylistNode => ({
	playlist_id: `pl-${i}`,
	name,
	track_count,
	broken_count: 0,
	kind: 'playlist',
	children: []
});

const FEW: PlaylistNode[] = [node(1, 'Warm-up', 42), node(2, 'Peak time', 7), node(3, 'Closing', 120)];
// One over PLAYLIST_PICKER_SEARCH_THRESHOLD (15), which turns the filter on.
const MANY: PlaylistNode[] = Array.from({ length: 16 }, (_, i) =>
	node(100 + i, i === 9 ? 'Deep house' : `Set ${String(i + 1).padStart(2, '0')}`, i * 3)
);

const rows = (root: HTMLElement): HTMLButtonElement[] => Array.from(root.querySelectorAll<HTMLButtonElement>('button.m-row'));

export const contracts = {
	/** Few playlists: no filter, focus on the first row, click picks, scrim and Escape close, the modal does not. */
	Few: {
		args: { open: true, playlists: FEW, onpick: fn(), onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const dialog = canvas.getByRole('dialog', { name: 'Add to playlist' });
			await expect(dialog).toHaveAttribute('aria-modal', 'true');
			await expect(dialog).toHaveAttribute('data-testid', 'add-to-playlist-picker');
			await expect(canvas.getByText('Add to playlist')).toHaveClass('m-title');
			await expect(canvas.queryByRole('searchbox')).toBeNull();
			await expect(rows(canvasElement).map((r) => r.querySelector('.m-name')?.textContent)).toEqual([
				'Warm-up',
				'Peak time',
				'Closing'
			]);
			const count = rows(canvasElement)[2].querySelector('.m-count')!;
			await expect(count).toHaveTextContent('120');
			await expect(count).toHaveAttribute('title', 'Track count in this playlist');
			await waitFor(() => expect(document.activeElement).toBe(rows(canvasElement)[0]));

			await userEvent.keyboard('{ArrowDown}');
			await expect(document.activeElement).toBe(rows(canvasElement)[1]);
			await userEvent.keyboard('{ArrowUp}');
			await expect(document.activeElement).toBe(rows(canvasElement)[0]);

			await userEvent.click(rows(canvasElement)[1]);
			await expect(args.onpick).toHaveBeenCalledWith(FEW[1]);
			await userEvent.click(canvas.getByText('Add to playlist'));
			await expect(args.onclose).not.toHaveBeenCalled();
			await fireEvent.click(canvasElement.querySelector<HTMLElement>('.scrim')!);
			await expect(args.onclose).toHaveBeenCalledTimes(1);
			// Escape from inside the dialog closes exactly once, not once per listener.
			rows(canvasElement)[0].focus();
			await userEvent.keyboard('{Escape}');
			await expect(args.onclose).toHaveBeenCalledTimes(2);
		}
	},
	/** Many playlists: a focused filter box narrows by name; ArrowDown and ArrowUp move between it and the rows. */
	ManyWithFilter: {
		args: { open: true, playlists: MANY, onpick: fn(), onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const search = canvas.getByRole('searchbox', { name: 'Filter playlists' });
			await expect(search).toHaveClass('m-search');
			await expect(search).toHaveAttribute('autocomplete', 'off');
			await expect(search).toHaveAttribute('spellcheck', 'false');
			await waitFor(() => expect(document.activeElement).toBe(search));
			await expect(rows(canvasElement)).toHaveLength(16);

			await userEvent.type(search, 'DEEP');
			await expect(rows(canvasElement)).toHaveLength(1);
			await expect(rows(canvasElement)[0]).toHaveTextContent('Deep house');
			await userEvent.keyboard('{ArrowDown}');
			await expect(document.activeElement).toBe(rows(canvasElement)[0]);
			await userEvent.keyboard('{ArrowUp}');
			await expect(document.activeElement).toBe(search);
			await userEvent.keyboard('{Enter}');
			await expect(args.onpick).not.toHaveBeenCalled();

			await userEvent.type(search, 'zzz');
			await expect(rows(canvasElement)).toHaveLength(0);
			await expect(canvas.getByText('No matching playlists')).toHaveClass('m-empty');
		}
	},
	/** No writable playlists: the empty line, and focus falls back to the dialog itself. */
	Empty: {
		args: { open: true, playlists: [], onpick: fn(), onclose: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(canvas.getByText('No writable playlists')).toHaveClass('m-empty');
			const dialog = canvas.getByRole('dialog', { name: 'Add to playlist' });
			await waitFor(() => expect(document.activeElement).toBe(dialog));
		}
	},
	/** Closed: nothing renders and Escape is ignored. */
	Closed: {
		args: { open: false, playlists: FEW, onpick: fn(), onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(canvasElement.querySelector('.scrim')).toBeNull();
			await userEvent.keyboard('{Escape}');
			await expect(args.onclose).not.toHaveBeenCalled();
		}
	}
};
