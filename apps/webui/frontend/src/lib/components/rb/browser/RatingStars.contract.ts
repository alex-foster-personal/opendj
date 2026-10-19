import { expect, fn, userEvent, within } from 'storybook/test';

/**
 * RatingStars UI contract, shared by the Svelte story (RatingStars.stories.ts)
 * and its React twin (RatingStars.stories.tsx). Both stories use this title,
 * these args and these play functions, so the twin passes only when it
 * behaves exactly like the Svelte original. Both stories set the same literal
 * title, 'Contracts/Browser/RatingStars' (Storybook indexes titles statically),
 * so their story ids match for `pnpm story:parity`. See src/react/README.md.
 */

type Args = { rating: number | null; onrate: (next: number) => void; disabledReason?: string | null };
type Ctx = { canvasElement: HTMLElement; args: Args };

const stars = (root: HTMLElement): HTMLButtonElement[] =>
	Array.from(root.querySelectorAll<HTMLButtonElement>('button.rb-star'));
const filled = (root: HTMLElement): number => root.querySelectorAll('button.rb-star.filled').length;

export const contracts = {
	/** Three stars lit; clicking star 5 asks for 5, clicking the current star 3 clears to 0. */
	Rated: {
		args: { rating: 3, onrate: fn(), disabledReason: null } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(canvas.getByRole('radiogroup', { name: 'Rating' })).toBeVisible();
			await expect(stars(canvasElement)).toHaveLength(5);
			await expect(filled(canvasElement)).toBe(3);
			await expect(stars(canvasElement)[2]).toHaveAttribute('aria-checked', 'true');
			await expect(stars(canvasElement)[3]).toHaveAttribute('aria-checked', 'false');
			await userEvent.click(canvas.getByRole('radio', { name: 'Set rating 5' }));
			await expect(args.onrate).toHaveBeenLastCalledWith(5);
			await userEvent.click(canvas.getByRole('radio', { name: 'Set rating 3' }));
			await expect(args.onrate).toHaveBeenLastCalledWith(0);
		}
	},
	/** No rating: nothing lit, the group carries .unrated, hover previews up to the hovered star. */
	Unrated: {
		args: { rating: null, onrate: fn(), disabledReason: null } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(filled(canvasElement)).toBe(0);
			await expect(canvasElement.querySelector('.rb-stars')).toHaveClass('unrated');
			await userEvent.hover(stars(canvasElement)[1]);
			await expect(filled(canvasElement)).toBe(2);
			await expect(canvasElement.querySelectorAll('button.rb-star.preview')).toHaveLength(2);
		}
	},
	/** A disabled reason makes every star inert and titles it with the reason. */
	Disabled: {
		args: { rating: 4, onrate: fn(), disabledReason: 'Read-only library' } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			for (const star of stars(canvasElement)) {
				await expect(star).toBeDisabled();
				await expect(star).toHaveAttribute('title', 'Read-only library');
			}
			await expect(filled(canvasElement)).toBe(4);
			await expect(args.onrate).not.toHaveBeenCalled();
		}
	}
};
