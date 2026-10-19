import { expect } from 'storybook/test';

/**
 * PositionBar UI contract, shared by the Svelte story (PositionBar.stories.ts)
 * and its React twin (PositionBar.stories.tsx). See src/react/README.md.
 */

type Args = { positionMs?: number; durationMs?: number | null; label?: string };
type Ctx = { canvasElement: HTMLElement };

const bar = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>('div.position-bar') as HTMLElement;
const fill = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>('div.position-fill') as HTMLElement;

export const contracts = {
	/** Half way through a known duration: 50% fill, the ms readout on hover, the default label. */
	Halfway: {
		args: { positionMs: 30000, durationMs: 60000 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(bar(canvasElement)).toHaveAttribute('aria-label', 'Track position');
			await expect(bar(canvasElement)).toHaveAttribute('data-testid', 'trackify-position-bar');
			await expect(fill(canvasElement).style.width).toBe('50%');
			await expect(fill(canvasElement)).toHaveAttribute('title', '30000 ms of 60000 ms');
		}
	},
	/** Unknown duration: empty fill, the title rounds the position and says unknown, custom label. */
	UnknownDuration: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { positionMs: 1234.4, durationMs: null, label: 'Deck 2 position' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(bar(canvasElement)).toHaveAttribute('aria-label', 'Deck 2 position');
			await expect(fill(canvasElement).style.width).toBe('0%');
			await expect(fill(canvasElement)).toHaveAttribute('title', '1234 ms of unknown ms');
		}
	},
	/** A position past the end clamps the fill to 100%. */
	PastEnd: {
		args: { positionMs: 90000, durationMs: 60000 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(fill(canvasElement).style.width).toBe('100%');
			await expect(fill(canvasElement)).toHaveAttribute('title', '90000 ms of 60000 ms');
		}
	}
};
