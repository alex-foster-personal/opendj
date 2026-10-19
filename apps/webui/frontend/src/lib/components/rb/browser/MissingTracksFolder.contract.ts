import { expect, fn, userEvent, within } from 'storybook/test';

/**
 * MissingTracksFolder UI contract, shared by the Svelte story
 * (MissingTracksFolder.stories.ts) and its React twin
 * (MissingTracksFolder.stories.tsx). See src/react/README.md.
 */

type Args = { brokenCount: number | null; error: string | null; selected: boolean; onselect: () => void };
type Ctx = { canvasElement: HTMLElement; args: Args };

const row = (root: HTMLElement): HTMLElement => within(root).getByTestId('playlist-missing-tracks');
const count = (root: HTMLElement): HTMLElement => row(root).querySelector('.count') as HTMLElement;

export const contracts = {
	/** A known count renders with its hover title; click and Enter both select. */
	Counted: {
		args: { brokenCount: 42, error: null, selected: false, onselect: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const r = canvas.getByRole('button', { name: /Missing Tracks/ });
			await expect(r).toHaveAttribute('data-menu-target', 'tree-readonly');
			await expect(r).toHaveAttribute('tabindex', '0');
			await expect(r).not.toHaveClass('selected');
			await expect(r.querySelector('.name')).toHaveTextContent('Missing Tracks');
			await expect(count(canvasElement)).toHaveTextContent('42');
			await expect(count(canvasElement)).toHaveAttribute('title', '42 broken tracks');
			await userEvent.click(r);
			await expect(args.onselect).toHaveBeenCalledTimes(1);
			r.focus();
			await userEvent.keyboard('{Enter}');
			await expect(args.onselect).toHaveBeenCalledTimes(2);
			await userEvent.keyboard('{Space}');
			await expect(args.onselect).toHaveBeenCalledTimes(2);
		}
	},
	/** Still loading: the count reads '...' and says so on hover; selected adds .selected. */
	LoadingSelected: {
		args: { brokenCount: null, error: null, selected: true, onselect: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(row(canvasElement)).toHaveClass('row', 'selected');
			await expect(count(canvasElement)).toHaveTextContent('...');
			await expect(count(canvasElement)).toHaveAttribute('title', 'loading broken track count');
		}
	},
	/** An error wins over any count: '!' with the error in the title. */
	Errored: {
		args: { brokenCount: 7, error: 'daemon offline', selected: false, onselect: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(count(canvasElement)).toHaveTextContent('!');
			await expect(count(canvasElement)).toHaveAttribute('title', 'broken count unavailable: daemon offline');
		}
	}
};
