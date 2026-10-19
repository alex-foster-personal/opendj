import type { Meta, StoryObj } from '@storybook/sveltekit';
import { http, HttpResponse } from 'msw';
import { expect, fn, userEvent, waitFor, within } from 'storybook/test';

import type { components } from '$lib/api-types';
import { browserRows } from '../../../../sandbox/fixtures/browser-rows';
import { makeClientRowProvider } from './pane-contract.svelte';
import TrackTable from './TrackTable.svelte';

/**
 * TrackTable UI contracts. The real 3k-line table, fed through its typed
 * RowProvider seam instead of BrowserPanel's listing fetch, so it renders
 * with no daemon. Each story's `play` is a saved behavior; run them with
 * `pnpm test:contracts src/lib/components/rb/browser/TrackTable.stories.ts`.
 */
const FORTY = browserRows(40);

/** The table asks `POST /library/row-assets` for strip-less rows in view
 * (LIBM-172). `assets` holds only ids that are library tracks, and no fixture
 * id is one, so the daemon's own answer for these rows is empty with nothing
 * pending: every row settles and none is asked for again. */
const NO_LIBRARY_ASSETS: components['schemas']['RowAssetsOut'] = { assets: {}, pending: [] };

const meta = {
	title: 'Contracts/Browser/TrackTable',
	component: TrackTable,
	parameters: {
		msw: { handlers: [http.post('/api/v1/library/row-assets', () => HttpResponse.json(NO_LIBRARY_ASSETS))] }
	},
	args: {
		provider: makeClientRowProvider(
			() => FORTY,
			() => false
		),
		selectedIds: [],
		selectedOrders: [],
		loadedIds: new Set<string>(),
		vocalsById: {},
		markerAnlzById: {},
		previewStripById: {},
		stripLoadingById: {},
		sortKey: null,
		sortDir: 1,
		emptyMessage: null,
		restoreKey: 0,
		scrollTop: 0,
		onscrollcursor: fn(),
		onsort: fn(),
		onselectrow: fn(),
		onloadrow: fn(),
		onrate: fn(),
		onrowvisible: fn()
	}
} satisfies Meta<typeof TrackTable>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Every provider row renders as a table row, in provider order. */
export const RendersEveryRow: Story = {
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(await canvas.findByText('Track 1')).toBeVisible();
		await waitFor(() => expect(canvas.getAllByText(/^Track \d+$/)).toHaveLength(40));
	}
};

/** A single click selects: the parent hears onselectrow with that row. */
export const ClickSelectsRow: Story = {
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		await userEvent.click(await canvas.findByText('Track 3'));
		await expect(args.onselectrow).toHaveBeenCalledTimes(1);
		await expect(args.onselectrow).toHaveBeenCalledWith(
			expect.objectContaining({ stable_id: 'fixture-3' }),
			expect.anything()
		);
		await expect(args.onloadrow).not.toHaveBeenCalled();
	}
};

/** Double-clicking a title never loads silently: it asks "Load+play CH1?"
 * first, and only Yes loads deck 1 with play. */
export const DoubleClickAsksBeforeLoading: Story = {
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		await userEvent.dblClick(await canvas.findByText('Track 5'));
		const dialog = await within(document.body).findByRole('dialog', {
			name: 'Load and play CH1?'
		});
		await expect(args.onloadrow).not.toHaveBeenCalled();
		await userEvent.click(within(dialog).getByRole('button', { name: /yes/i }));
		await expect(args.onloadrow).toHaveBeenCalledWith(
			expect.objectContaining({ stable_id: 'fixture-5' }),
			1,
			expect.objectContaining({ play: true })
		);
	}
};

/** An empty result states the parent's reason instead of a blank table. */
export const EmptyStatesTheReason: Story = {
	args: {
		provider: makeClientRowProvider(
			() => [],
			() => false
		),
		emptyMessage: 'No tracks in this playlist yet.'
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(await canvas.findByText('No tracks in this playlist yet.')).toBeVisible();
	}
};
