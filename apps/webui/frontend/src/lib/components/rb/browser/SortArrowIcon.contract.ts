import { expect } from 'storybook/test';

/**
 * SortArrowIcon UI contract, shared by the Svelte story (SortArrowIcon.stories.ts)
 * and its React twin (SortArrowIcon.stories.tsx). See src/react/README.md.
 */

type Args = { asc: boolean };

// Literal known-good glyphs, written out rather than read from sortArrowPath()
// so a swapped or corrupted helper fails here instead of moving with the
// assertion. Up: apex (6,3) above a base at y=9. Down: apex (6,11) below y=5.
const UP_ARROW = 'M6 3 L10 9 L2 9 Z';
const DOWN_ARROW = 'M6 11 L2 5 L10 5 Z';

type Ctx = { canvasElement: HTMLElement; args: Args };

const svg = (root: HTMLElement): SVGSVGElement => {
	const el = root.querySelector<SVGSVGElement>('svg.sort-arrow');
	if (el === null) throw new Error('svg.sort-arrow not rendered');
	return el;
};

export const contracts = {
	/** Ascending draws the up arrow (apex at the top), 8px, hidden from assistive tech. */
	Ascending: {
		args: { asc: true } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = svg(canvasElement);
			await expect(el).toHaveAttribute('aria-hidden', 'true');
			await expect(el).toHaveAttribute('viewBox', '0 0 12 12');
			await expect(el).toHaveAttribute('width', '8');
			await expect(el).toHaveAttribute('height', '8');
			const path = el.querySelector('path');
			await expect(path).toHaveAttribute('d', UP_ARROW);
			await expect(path).toHaveAttribute('fill', 'currentColor');
		}
	},
	/** Descending draws the down arrow. */
	Descending: {
		args: { asc: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(svg(canvasElement).querySelector('path')).toHaveAttribute('d', DOWN_ARROW);
		}
	}
};
