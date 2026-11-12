import { expect, fn, within } from 'storybook/test';

import type { DeletedPlaylistOut } from '$lib/rb/playlist-deleted';

type Args = {
	open: boolean;
	rows: DeletedPlaylistOut[] | null;
	error: string | null;
	ontoggle: () => void;
	onrestore: (id: string, name: string) => void;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const callbacks = { ontoggle: fn(), onrestore: fn() };

const deleted = (playlist_id: string, name: string, track_count: number): DeletedPlaylistOut => ({
	playlist_id,
	name,
	track_count,
	deleted_at: '2026-10-12T21:04:00Z',
	updated_at: '2026-10-12T21:04:00Z',
	vendor: 'rekordbox',
	vendor_pl_id: playlist_id
});

const sampleRows = [deleted('pl-7', 'Old Warmup', 32), deleted('pl-9', 'Test Crate', 4)];

const errorMessage = 'HTTP 503 from /api/v1/playlists/deleted';

export const contracts = {
	/** Open while the list is still loading: inert loading row, not empty. */
	Loading: {
		args: { open: true, rows: null, error: null, ...callbacks } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			const row = canvas.getByTestId('playlist-recently-deleted-loading');
			await expect(row).toHaveClass('row', 'child', 'rb-inert');
			await expect(canvas.getByTestId('playlist-recently-deleted')).toHaveTextContent('...');
			await expect(canvas.queryByTestId('playlist-recently-deleted-empty')).toBeNull();
			await expect(canvas.queryByTestId('playlist-deleted-row')).toBeNull();
		}
	},
	/** List fetch failed: inert error row with failure text, not empty. */
	Error: {
		args: { open: true, rows: null, error: errorMessage, ...callbacks } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			const row = canvas.getByTestId('playlist-recently-deleted-error');
			await expect(row).toHaveClass('row', 'child', 'rb-inert');
			await expect(row).toHaveTextContent('deleted playlists unavailable');
			await expect(row).toHaveAttribute('title', errorMessage);
			await expect(canvas.getByTestId('playlist-recently-deleted')).toHaveTextContent('!');
			await expect(canvas.queryByTestId('playlist-recently-deleted-empty')).toBeNull();
			await expect(canvas.queryByTestId('playlist-deleted-row')).toBeNull();
		}
	},
	/** Successful load with no rows: empty state only. */
	EmptyAfterSuccess: {
		args: { open: true, rows: [], error: null, ...callbacks } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			const row = canvas.getByTestId('playlist-recently-deleted-empty');
			await expect(row).toHaveClass('row', 'child', 'rb-inert');
			await expect(row).toHaveTextContent('no deleted playlists');
			await expect(canvas.queryByTestId('playlist-recently-deleted-error')).toBeNull();
			await expect(canvas.queryByTestId('playlist-recently-deleted-loading')).toBeNull();
		}
	},
	/** Collapsed: inner states are not mounted. */
	Collapsed: {
		args: { open: false, rows: sampleRows, error: null, ...callbacks } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(canvas.getByTestId('playlist-recently-deleted')).toBeVisible();
			await expect(canvas.queryByTestId('playlist-recently-deleted-empty')).toBeNull();
			await expect(canvas.queryByTestId('playlist-recently-deleted-error')).toBeNull();
			await expect(canvas.queryByTestId('playlist-recently-deleted-loading')).toBeNull();
		}
	},
	/** Open with deleted playlists listed. */
	OpenWithRows: {
		args: { open: true, rows: sampleRows, error: null, ...callbacks } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			const rows = canvas.getAllByTestId('playlist-deleted-row');
			await expect(rows).toHaveLength(2);
			await expect(canvas.getAllByTestId('restore-playlist')).toHaveLength(2);
			await expect(canvas.queryByTestId('playlist-recently-deleted-empty')).toBeNull();
			await expect(canvas.queryByTestId('playlist-recently-deleted-error')).toBeNull();
			await expect(canvas.queryByTestId('playlist-recently-deleted-loading')).toBeNull();
		}
	}
};
