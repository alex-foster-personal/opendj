import { expect } from 'storybook/test';
import { prefersReducedMotion } from '$lib/motion/reduced-motion';

/**
 * SpinnerIcon UI contract, shared by the Svelte story (SpinnerIcon.stories.ts)
 * and its React twin (SpinnerIcon.stories.tsx). See src/react/README.md.
 */

type Args = { size?: number };
type Ctx = { canvasElement: HTMLElement; args: Args };

const svg = (root: HTMLElement): SVGSVGElement => {
	const el = root.querySelector<SVGSVGElement>('svg.spinner-icon');
	if (el === null) throw new Error('svg.spinner-icon not rendered');
	return el;
};

export const contracts = {
	/** No size: a 10px ring with its spoke, hidden from assistive tech, spinning. */
	Default: {
		args: {} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = svg(canvasElement);
			await expect(el).toHaveAttribute('width', '10');
			await expect(el).toHaveAttribute('height', '10');
			await expect(el).toHaveAttribute('viewBox', '0 0 16 16');
			await expect(el).toHaveAttribute('aria-hidden', 'true');
			await expect(el.querySelector('circle')).toHaveAttribute('r', '6');
			await expect(el.querySelector('path')).toHaveAttribute('d', 'M8 8V3');
			// Spinning, unless the viewer asked for reduced motion, which stops it.
			// (Svelte hashes the keyframes name, so match on its suffix.)
			const name = getComputedStyle(el).animationName;
			if (prefersReducedMotion()) await expect(name).toBe('none');
			else await expect(name).toMatch(/spinner-icon-spin$/);
		}
	},
	/** A size sets both dimensions. */
	Large: {
		args: { size: 24 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = svg(canvasElement);
			await expect(el).toHaveAttribute('width', '24');
			await expect(el).toHaveAttribute('height', '24');
		}
	}
};
