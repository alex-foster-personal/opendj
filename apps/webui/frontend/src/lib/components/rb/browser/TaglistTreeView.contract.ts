import { expect, fn, within } from 'storybook/test';

import type { MyTagCatalog } from '$lib/rb/api-edit-suite';
import type { PlaylistNode } from '$lib/rb/library-types';

/**
 * TaglistTreeView UI contract (TaglistTreeView.stories.ts). The container
 * clears error and sets catalog on a successful refresh; these plays assert
 * the view branch order at the prop boundary.
 */

type Args = {
	catalog: MyTagCatalog | null;
	error: string | null;
	selectedId: string | null;
	onselect: (node: PlaylistNode) => void;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const base = { catalog: null, error: null, selectedId: null, onselect: fn() } satisfies Args;

const sampleTags = [
	{ name: 'Warmup', track_count: 48 },
	{ name: 'Vocal', track_count: 213 }
];

const sampleCatalog: MyTagCatalog = { catalog_revision: 'r2', tags: sampleTags };

export const contracts = {
	/** Catalog not loaded yet: only the inert loading row. */
	Loading: {
		args: { ...base } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const root = within(canvasElement);
			const row = root.getByTestId('taglists-loading');
			await expect(row).toHaveClass('row', 'rb-inert');
			await expect(row).toHaveTextContent('...');
			await expect(canvasElement.querySelectorAll('.taglist-root .row')).toHaveLength(1);
			await expect(canvasElement.querySelector('[data-testid="taglists-error"]')).toBeNull();
			await expect(canvasElement.querySelector('[data-testid="taglists-empty"]')).toBeNull();
			await expect(canvasElement.querySelector('[data-testid="taglist-row"]')).toBeNull();
		}
	},
	/** Load failed: error row wins over a non-empty catalog and is not empty-state copy. */
	ErrorNotEmpty: {
		args: {
			...base,
			error: 'mytags_unavailable',
			catalog: sampleCatalog
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const root = within(canvasElement);
			const row = root.getByTestId('taglists-error');
			await expect(row).toHaveClass('row', 'rb-inert');
			await expect(row).toHaveTextContent('taglists unavailable');
			await expect(row).toHaveAttribute('title', 'mytags_unavailable');
			await expect(root.queryByText('No tags yet')).toBeNull();
			await expect(canvasElement.querySelector('[data-testid="taglist-row"]')).toBeNull();
		}
	},
	/** Loaded catalog with zero tags: distinct from error. */
	EmptyLoaded: {
		args: {
			...base,
			catalog: { catalog_revision: 'r1', tags: [] }
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const row = within(canvasElement).getByTestId('taglists-empty');
			await expect(row).toHaveClass('row', 'rb-inert');
			await expect(row).toHaveTextContent('No tags yet');
			await expect(canvasElement.querySelector('[data-testid="taglists-error"]')).toBeNull();
		}
	},
	/** After TaglistTree refresh: error cleared and tag rows replace the failure row. */
	RecoveredAfterError: {
		args: {
			...base,
			catalog: sampleCatalog
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const root = within(canvasElement);
			await expect(canvasElement.querySelector('[data-testid="taglists-error"]')).toBeNull();
			await expect(canvasElement.querySelector('[data-testid="taglists-loading"]')).toBeNull();
			await expect(canvasElement.querySelector('[data-testid="taglists-empty"]')).toBeNull();
			const warmup = root.getByRole('button', { name: /Warmup/ });
			await expect(warmup).toHaveAttribute('data-testid', 'taglist-row');
			await expect(warmup).toHaveTextContent('48');
			await expect(canvasElement.querySelectorAll('[data-testid="taglist-row"]')).toHaveLength(2);
		}
	}
};
