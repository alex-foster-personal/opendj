import { expect, within } from 'storybook/test';

/**
 * LibraryLoadIndicator UI contract, shared by the Svelte story
 * (LibraryLoadIndicator.stories.ts) and its React twin
 * (LibraryLoadIndicator.stories.tsx). See src/react/README.md.
 *
 * The indeterminate state (loading with no known total) runs an infinite
 * CSS sweep, so it has no story here: a pixel parity screenshot of a moving
 * bar cannot be deterministic. Every state below is static.
 */

type Progress = { loaded: number; total: number | null } | null;
type Args = { loading: boolean; progress: Progress; searching?: boolean };
type Ctx = { canvasElement: HTMLElement; args: Args };

const root = (el: HTMLElement): HTMLElement => el.querySelector<HTMLElement>('.lli-root')!;

export const contracts = {
	/** A known total: the bar, aria values, title and label all say 50%, never a guess. */
	Determinate: {
		args: { loading: true, progress: { loaded: 450, total: 900 }, searching: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(canvas.getByRole('status')).toHaveAttribute('aria-live', 'polite');
			const bar = canvas.getByRole('progressbar', { name: 'loading... 450 of 900 rows' });
			await expect(bar).toHaveAttribute('aria-valuemin', '0');
			await expect(bar).toHaveAttribute('aria-valuemax', '100');
			await expect(bar).toHaveAttribute('aria-valuenow', '50');
			await expect(bar).toHaveAttribute('title', '50%');
			await expect(bar).not.toHaveClass('lli-indeterminate');
			await expect(bar.querySelector<HTMLElement>('.lli-bar')!.style.width).toBe('50%');
			await expect(canvasElement.querySelector('.lli-label')).toHaveTextContent('loading... 450 of 900 rows');
			// No rows/s figure until the measuring window is at least a second long.
			await expect(canvasElement.querySelector('.lli-rate')).toBeNull();
			await expect(canvasElement.querySelector('.lli-search')).toBeNull();
			await expect(canvasElement.querySelector('.lli-mark')).toHaveAttribute('aria-hidden', 'true');
		}
	},
	/** Not loading but progress still reported: the verb is "loading more...", the percent is capped at 100. */
	LoadingMoreWhileSearching: {
		args: { loading: false, progress: { loaded: 950, total: 900 }, searching: true } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(canvas.getByText('searching whole collection...')).toHaveClass('lli-search');
			const bar = canvas.getByRole('progressbar', { name: 'loading more... 950 of 900 rows' });
			await expect(bar).toHaveAttribute('aria-valuenow', '100');
			await expect(bar).toHaveAttribute('title', '100%');
		}
	},
	/** Searching only: the mark and the search text, no progress bar. */
	SearchingOnly: {
		args: { loading: false, progress: null, searching: true } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(canvas.getByText('searching whole collection...')).toBeVisible();
			await expect(canvas.queryByRole('progressbar')).toBeNull();
			await expect(canvasElement.querySelector('.lli-label')).toBeNull();
			await expect(canvasElement.querySelectorAll('.lli-mark')).toHaveLength(1);
		}
	},
	/** Idle: the reserved strip stays in the layout, empty. */
	Idle: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { loading: false, progress: null, searching: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(root(canvasElement)).toHaveAttribute('role', 'status');
			await expect(root(canvasElement).children).toHaveLength(0);
			await expect(root(canvasElement).getBoundingClientRect().height).toBe(18);
		}
	}
};
