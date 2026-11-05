import type { Meta, StoryObj } from '@storybook/sveltekit';
import { http, HttpResponse } from 'msw';
import { expect, fireEvent, fn, userEvent, waitFor, within } from 'storybook/test';

import type { components } from '$lib/api-types';
import { EMPTY_OVERLAY } from '$lib/rb/library-works';
import { browserRows } from '../../../../sandbox/fixtures/browser-rows';
import { createPaneStore, makeClientRowProvider } from './pane-contract.svelte';
import { applyOptimisticEntriesMove } from '$lib/library/entries-move-local';
import { arrangeSections } from '$lib/library/playlist-sections';
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

/** A fixture row's play counts: never played anywhere. */
function neverPlayed(stableId: string): components['schemas']['TrackPlaysOut'] {
	return { stable_id: stableId, opendj_play_count: 0, rekordbox_play_count: 0, play_count: 0, opendj_last_played_at: null };
}

const meta = {
	title: 'Contracts/Browser/TrackTable',
	component: TrackTable,
	parameters: {
		msw: {
			handlers: [
				http.post('/api/v1/library/row-assets', () => HttpResponse.json(NO_LIBRARY_ASSETS)),
				// The Last Played cell reads each row's play counts after it renders, so
				// a slow story caught the previous story's late reads as unhandled.
				http.get('/api/v1/tracks/:stable_id/plays', ({ params }) =>
					HttpResponse.json(neverPlayed(String(params.stable_id)))
				),
				// Versions (#7901): the table loads the works overlay and its column prefs on mount.
				http.get('/api/v1/works/overlay', () => HttpResponse.json(EMPTY_OVERLAY)),
				http.post('/api/v1/works/suggestion-flags', () => HttpResponse.json({ member_ids: [] })),
				// The grid-quality scan starts once the boot window closes (LIBM-138); a long story outlives it.
				http.post('/api/v1/beatgrid-flags/scan', () => HttpResponse.json({ started: true }, { status: 202 })),
				http.get('/api/v1/ui-prefs', () =>
					HttpResponse.json({ library_optional_columns: [], library_classical_layout: false })
				)
			]
		}
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
		/** Required by TrackTable (PAIR-09): an omitted map must not read as
		 * "no pairings", so the contract stories state the empty graph. */
		pairPartners: new Map<string, ReadonlySet<string>>(),
		pairError: null,
		emptyMessage: null,
		restoreKey: 0,
		scrollTop: 0,
		onscrollcursor: fn(),
		onsort: fn(),
		onselectrow: fn(),
		onloadrow: fn(),
		onrate: fn(),
		onrowvisible: fn(),
		onkeycardfailed: fn()
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

// ------------------------------------------------ drag-to-reorder (REORDER-ROW-01)

/** The rendered row whose order cell reads `order`. */
function rowByOrder(root: HTMLElement, order: number): HTMLElement {
	const row = [...root.querySelectorAll<HTMLElement>('tr[data-testid="track-row"]')].find(
		(tr) => tr.querySelector('td.c-order')?.textContent?.trim().endsWith(String(order))
	);
	if (row === undefined) throw new Error(`no rendered row with order ${order}`);
	return row;
}

/** A playlist in membership order: every row carries its membership id, which
 * is what a move names (sections, #7909). */
const MEMBERS = FORTY.map((row, i) => ({ ...row, item_id: `item-${i + 1}` }));
const memberProvider = () =>
	makeClientRowProvider(
		() => MEMBERS,
		() => false
	);

/**
 * One drag the way WKWebView delivers it: dragstart on the source, then
 * dragenter on the target row's cell and the drop straight after, at the
 * target row's top edge. WebKit takes a target's accept from its dragenter
 * when the pointer crosses into a new element, so a release right after the
 * crossing gets no dragover first; a target that only cancels dragover is
 * never dropped on.
 */
async function webkitDrag(source: HTMLElement, target: HTMLElement): Promise<{ enterAccepted: boolean }> {
	const dataTransfer = new DataTransfer();
	const clientY = target.getBoundingClientRect().top + 1;
	await fireEvent.dragStart(source, { dataTransfer });
	const enter = new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer, clientY });
	target.dispatchEvent(enter);
	if (enter.defaultPrevented) await fireEvent.drop(target, { dataTransfer, clientY });
	await fireEvent.dragEnd(source, { dataTransfer });
	return { enterAccepted: enter.defaultPrevented };
}

/** the maintainer, Wed 4 Nov 2026: "I can't drag tracks' positions around." In a
 * playlist in membership order, pressing a row anywhere and dropping it on
 * another row moves it there, like rekordbox: no hunting for the 8 px grip. */
export const RowDragReordersPlaylist: Story = {
	args: { reorderable: true, provider: memberProvider(), onmoveentries: fn() },
	play: async ({ canvasElement, args }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('.grip').length).toBeGreaterThan(5));
		const source = rowByOrder(canvasElement, 2).querySelector<HTMLElement>('td.c-title')!;
		const target = rowByOrder(canvasElement, 5).querySelector<HTMLElement>('td.c-title')!;
		const { enterAccepted } = await webkitDrag(source, target);
		await expect(enterAccepted, 'the table body accepts on dragenter (WebKit)').toBe(true);
		await expect(args.onmoveentries).toHaveBeenCalledTimes(1);
		await expect(args.onmoveentries).toHaveBeenCalledWith({ item_ids: ['item-2'], section_ids: [], before_item_id: 'item-5' });
	}
};

// ------------------------------------- insertion line + no flash (REORDER-ROW-02)

/** A pointer y inside the table body that opens gap `gap` (gapFromPointer). */
function yForGap(root: HTMLElement, gap: number): number {
	const body = root.querySelector('tbody')!.getBoundingClientRect();
	const rowHeight = root.querySelector<HTMLElement>('tr[data-testid="track-row"]')!.getBoundingClientRect().height;
	return body.top + gap * rowHeight + 2;
}

/** the maintainer, Thu 5 Nov 2026: "there's no thick line indicating the insertion point
 * beforehand." While a move drag is over the list a 3 px accent line marks the
 * gap the drop fills; it follows the pointer and goes on drop, leave or end. */
export const MoveDragDrawsTheInsertionLine: Story = {
	args: { reorderable: true, provider: memberProvider(), onmoveentries: fn() },
	play: async ({ canvasElement, args }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('.grip').length).toBeGreaterThan(5));
		const source = rowByOrder(canvasElement, 2).querySelector<HTMLElement>('td.c-title')!;
		const target = rowByOrder(canvasElement, 5).querySelector<HTMLElement>('td.c-title')!;
		const dataTransfer = new DataTransfer();
		await fireEvent.dragStart(source, { dataTransfer });
		await expect(canvasElement.querySelector('[data-testid="tt-drop-line"]')).toBeNull();
		await fireEvent.dragOver(target, { dataTransfer, clientY: yForGap(canvasElement, 4) });
		const line = await waitFor(() => {
			const el = canvasElement.querySelector<HTMLElement>('[data-testid="tt-drop-line"]');
			expect(el).not.toBeNull();
			return el!;
		});
		await expect(line.dataset.gap).toBe('4');
		await expect(line.getBoundingClientRect().height).toBe(3);
		const firstTop = line.getBoundingClientRect().top;
		await fireEvent.dragOver(target, { dataTransfer, clientY: yForGap(canvasElement, 6) });
		await waitFor(() => expect(line.dataset.gap).toBe('6'));
		await expect(line.getBoundingClientRect().top).toBeGreaterThan(firstTop);
		await fireEvent.drop(target, { dataTransfer, clientY: yForGap(canvasElement, 6) });
		await waitFor(() => expect(canvasElement.querySelector('[data-testid="tt-drop-line"]')).toBeNull());
		await expect(args.onmoveentries).toHaveBeenCalledWith({ item_ids: ['item-2'], section_ids: [], before_item_id: 'item-7' });
		await fireEvent.dragEnd(source, { dataTransfer });
	}
};

/** A drag leaving the list, or ending without a drop, clears the line. */
export const MoveDragLineClearsOnLeaveAndEnd: Story = {
	args: { reorderable: true, provider: memberProvider(), onmoveentries: fn() },
	play: async ({ canvasElement }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('.grip').length).toBeGreaterThan(5));
		const source = rowByOrder(canvasElement, 2).querySelector<HTMLElement>('td.c-title')!;
		const target = rowByOrder(canvasElement, 5).querySelector<HTMLElement>('td.c-title')!;
		const dataTransfer = new DataTransfer();
		await fireEvent.dragStart(source, { dataTransfer });
		await fireEvent.dragOver(target, { dataTransfer, clientY: yForGap(canvasElement, 4) });
		await waitFor(() => expect(canvasElement.querySelector('[data-testid="tt-drop-line"]')).not.toBeNull());
		await fireEvent.dragLeave(target, { dataTransfer, clientX: -50, clientY: -50 });
		await waitFor(() => expect(canvasElement.querySelector('[data-testid="tt-drop-line"]')).toBeNull());
		await fireEvent.dragOver(target, { dataTransfer, clientY: yForGap(canvasElement, 4) });
		await waitFor(() => expect(canvasElement.querySelector('[data-testid="tt-drop-line"]')).not.toBeNull());
		await fireEvent.dragEnd(source, { dataTransfer });
		await waitFor(() => expect(canvasElement.querySelector('[data-testid="tt-drop-line"]')).toBeNull());
	}
};

const SECTIONED = arrangeSections(MEMBERS.slice(0, 12), [{ id: 'S1', title: 'Peak', before_position: 4 }], new Set());

/** Section boundaries: the gap just below a divider is the section's first
 * place, and the line is drawn there, below the divider and inside it. */
export const MoveDragLineInsideASection: Story = {
	args: {
		reorderable: true,
		provider: makeClientRowProvider(
			() => SECTIONED.rows,
			() => false
		),
		sectionHeaders: SECTIONED.headers,
		onmoveentries: fn()
	},
	play: async ({ canvasElement, args }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('.grip').length).toBeGreaterThan(5));
		const header = SECTIONED.headers[0];
		const source = rowByOrder(canvasElement, 1).querySelector<HTMLElement>('td.c-title')!;
		const firstInSection = rowByOrder(canvasElement, 5);
		const dataTransfer = new DataTransfer();
		await fireEvent.dragStart(source, { dataTransfer });
		await fireEvent.dragOver(firstInSection, { dataTransfer, clientY: yForGap(canvasElement, header.slot + 1) });
		const line = await waitFor(() => {
			const el = canvasElement.querySelector<HTMLElement>('[data-testid="tt-drop-line"]');
			expect(el).not.toBeNull();
			return el!;
		});
		const divider = canvasElement.querySelector<HTMLElement>('tbody tr:not([data-testid="track-row"]):not(.tt-spacer)')!;
		await expect(line.getBoundingClientRect().top).toBeGreaterThan(divider.getBoundingClientRect().bottom - 1);
		// The gap opens directly under the divider (the section's first place), and
		// the line sits inside that open slot, not above the divider.
		const dividerBox = divider.getBoundingClientRect();
		await expect(line.dataset.gap).toBe(String(header.slot + 1));
		await expect(line.getBoundingClientRect().bottom).toBeLessThan(dividerBox.bottom + dividerBox.height);
		await fireEvent.drop(firstInSection, { dataTransfer, clientY: yForGap(canvasElement, header.slot + 1) });
		await expect(args.onmoveentries).toHaveBeenCalledWith({ item_ids: ['item-1'], section_ids: [], before_item_id: 'item-5' });
		await fireEvent.dragEnd(source, { dataTransfer });
	}
};

/** the maintainer: "whole library list refreshes flashing on/off." A drop reorders the
 * rows in place: every row keeps its DOM node (keyed by membership item_id), so
 * nothing unmounts, blanks or repaints from empty. */
export const MoveDropKeepsEveryRowNode: Story = {
	args: (() => {
		const pane = createPaneStore();
		pane.rows = MEMBERS.map((row) => ({ ...row }));
		return {
			reorderable: true,
			provider: makeClientRowProvider(
				() => pane.rows,
				() => false
			),
			onmoveentries: fn((move: { item_ids: string[]; section_ids: string[]; before_item_id?: string }) => {
				applyOptimisticEntriesMove(pane, [], () => {}, move);
			})
		};
	})(),
	play: async ({ canvasElement, args }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('.grip').length).toBeGreaterThan(5));
		const nodeById = new Map(
			[...canvasElement.querySelectorAll<HTMLElement>('tr[data-testid="track-row"]')].map((tr) => [tr.dataset.stableId, tr])
		);
		const moved = rowByOrder(canvasElement, 2);
		const movedId = moved.dataset.stableId;
		await webkitDrag(
			moved.querySelector<HTMLElement>('td.c-title')!,
			rowByOrder(canvasElement, 5).querySelector<HTMLElement>('td.c-title')!
		);
		await expect(args.onmoveentries).toHaveBeenCalledWith({ item_ids: ['item-2'], section_ids: [], before_item_id: 'item-5' });
		await waitFor(() => expect(rowByOrder(canvasElement, 4).dataset.stableId).toBe(movedId));
		await expect(rowByOrder(canvasElement, 4), 'the moved row is the same DOM node').toBe(moved);
		for (const tr of canvasElement.querySelectorAll<HTMLElement>('tr[data-testid="track-row"]')) {
			await expect(nodeById.get(tr.dataset.stableId), `row ${tr.dataset.stableId} was not remounted`).toBe(tr);
		}
	}
};

/** The grip still reorders, under WebKit's dragenter-then-drop order too. */
export const GripDragReordersPlaylist: Story = {
	args: { reorderable: true, provider: memberProvider(), onmoveentries: fn() },
	play: async ({ canvasElement, args }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('.grip').length).toBeGreaterThan(5));
		const grip = rowByOrder(canvasElement, 2).querySelector<HTMLElement>('.grip')!;
		const target = rowByOrder(canvasElement, 5).querySelector<HTMLElement>('td.c-title')!;
		await webkitDrag(grip, target);
		await expect(args.onmoveentries).toHaveBeenCalledWith({ item_ids: ['item-2'], section_ids: [], before_item_id: 'item-5' });
	}
};

/** A sorted, searched or read-only pane never reorders: the row drag stays a
 * deck/playlist drag and a drop on another row does nothing. */
export const RowDragNeverReordersWhenNotReorderable: Story = {
	args: { reorderable: false, provider: memberProvider(), onmoveentries: fn() },
	play: async ({ canvasElement, args }) => {
		await expect(await within(canvasElement).findByText('Track 5')).toBeVisible();
		const source = rowByOrder(canvasElement, 2).querySelector<HTMLElement>('td.c-title')!;
		const target = rowByOrder(canvasElement, 5).querySelector<HTMLElement>('td.c-title')!;
		const { enterAccepted } = await webkitDrag(source, target);
		await expect(enterAccepted).toBe(false);
		await expect(args.onmoveentries).not.toHaveBeenCalled();
	}
};

/** A drag that ends anywhere else leaves nothing armed: the next drop on a row
 * (say, a track dragged in from the other pane) is not taken as a reorder. */
export const AbandonedDragDoesNotArmTheNextDrop: Story = {
	args: { reorderable: true, provider: memberProvider(), onmoveentries: fn() },
	play: async ({ canvasElement, args }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('.grip').length).toBeGreaterThan(5));
		const source = rowByOrder(canvasElement, 2).querySelector<HTMLElement>('.grip')!;
		const target = rowByOrder(canvasElement, 5).querySelector<HTMLElement>('td.c-title')!;
		const dataTransfer = new DataTransfer();
		await fireEvent.dragStart(source, { dataTransfer });
		await fireEvent.dragEnd(source, { dataTransfer });
		await fireEvent.drop(target, { dataTransfer: new DataTransfer() });
		await expect(args.onmoveentries).not.toHaveBeenCalled();
	}
};

/** LIBUX-56: double-clicking a Comment cell edits it in place; Enter saves
 * through `oncomment` with that row, and the double-click never loads a deck. */
export const DoubleClickEditsTheComment: Story = {
	args: { oncomment: fn(async () => {}) },
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		await canvas.findByText('Track 2');
		const cell = canvasElement.querySelectorAll<HTMLElement>('tbody tr td.c-comments')[1];
		await userEvent.dblClick(cell);
		const input = await canvas.findByRole('textbox', { name: 'Edit comment' });
		await waitFor(() => expect(input).toHaveFocus());
		await userEvent.type(input, 'warmup only{Enter}');
		await expect(args.oncomment).toHaveBeenCalledWith(
			expect.objectContaining({ stable_id: 'fixture-2' }),
			'warmup only'
		);
		await expect(args.onloadrow).not.toHaveBeenCalled();
		await waitFor(() => expect(canvas.queryByRole('textbox', { name: 'Edit comment' })).toBeNull());
	}
};
