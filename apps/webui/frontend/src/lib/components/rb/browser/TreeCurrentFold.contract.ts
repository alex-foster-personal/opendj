import { expect, fn, userEvent, within } from 'storybook/test';

/**
 * TreeCurrentFold UI contract, shared by the Svelte story (TreeCurrentFold.stories.ts)
 * and its React twin (TreeCurrentFold.stories.tsx). See src/react/README.md.
 */

type Args = { fold: 'above' | 'below' | null; onjump: () => void };
type Ctx = { canvasElement: HTMLElement; args: Args };

export const contracts = {
	/** Current playlist scrolled above: an up-arrow button pinned top, click jumps. */
	Above: {
		args: { fold: 'above', onjump: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const btn = within(canvasElement).getByRole('button', { name: '▲ CURRENT' });
			await expect(btn).toHaveClass('current-fold', 'above');
			await expect(btn).not.toHaveClass('below');
			await expect(btn).toHaveAttribute('type', 'button');
			await expect(btn).toHaveAttribute('title', 'Current playlist is above - click to jump');
			await userEvent.click(btn);
			await expect(args.onjump).toHaveBeenCalledOnce();
		}
	},
	/** Current playlist scrolled below: a down-arrow button pinned bottom. */
	Below: {
		args: { fold: 'below', onjump: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const btn = within(canvasElement).getByRole('button', { name: '▼ CURRENT' });
			await expect(btn).toHaveClass('current-fold', 'below');
			await expect(btn).not.toHaveClass('above');
			await expect(btn).toHaveAttribute('title', 'Current playlist is below - click to jump');
		}
	},
	/** Current playlist in view: nothing renders. */
	InView: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { fold: null, onjump: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('button')).toBeNull();
		}
	}
};
