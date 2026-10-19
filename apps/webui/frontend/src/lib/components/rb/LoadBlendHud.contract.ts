import { expect, within } from 'storybook/test';

/**
 * LoadBlendHud UI contract, shared by the Svelte story (LoadBlendHud.stories.ts)
 * and its React twin (LoadBlendHud.stories.tsx). See src/react/README.md.
 */

type Args = { t: number; fader: number; scrubDeltaMs: number };
type Ctx = { canvasElement: HTMLElement; args: Args };

const parts = (root: HTMLElement): string[] =>
	Array.from(within(root).getByTestId('load-blend-hud').querySelectorAll('span')).map((s) => s.textContent ?? '');

export const contracts = {
	/** Forward scrub: t and fader to 2 places, scrub rounded to whole ms with an explicit '+'. */
	Forward: {
		args: { t: 0.5, fader: 0.25, scrubDeltaMs: 120.4 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const hud = within(canvasElement).getByTestId('load-blend-hud');
			await expect(hud).toHaveClass('load-blend-hud');
			await expect(getComputedStyle(hud).position).toBe('fixed');
			await expect(parts(canvasElement)).toEqual(['t 0.50', 'scrub +120 ms / fader 0.25']);
		}
	},
	/** Backward scrub: the minus comes from the number, with no doubled sign. */
	Backward: {
		args: { t: 0.875, fader: 1, scrubDeltaMs: -37.6 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(parts(canvasElement)).toEqual(['t 0.88', 'scrub -38 ms / fader 1.00']);
		}
	},
	/** Zero scrub counts as forward. */
	Zero: {
		args: { t: 0, fader: 0, scrubDeltaMs: 0 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(parts(canvasElement)).toEqual(['t 0.00', 'scrub +0 ms / fader 0.00']);
		}
	}
};
