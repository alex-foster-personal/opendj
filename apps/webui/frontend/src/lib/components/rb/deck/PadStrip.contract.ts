import { expect } from 'storybook/test';

/**
 * PadStrip UI contract, shared by the Svelte story (PadStrip.stories.ts) and
 * its React twin (PadStrip.stories.tsx). See src/react/README.md.
 * The strip takes no props; theme.css hides it on .perf-root by default.
 */

type Ctx = { canvasElement: HTMLElement };

export const contracts = {
	/** Eight letters A-H with one separator between D and E, aria-hidden, display:none from the theme var. */
	Default: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: {},
		play: async ({ canvasElement }: Ctx) => {
			const strip = canvasElement.querySelector<HTMLElement>('div.pad-strip');
			await expect(strip).not.toBeNull();
			await expect(strip).toHaveAttribute('aria-hidden', 'true');
			const letters = Array.from(canvasElement.querySelectorAll('span.pad-letter')).map((el) => el.textContent);
			await expect(letters).toEqual(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']);
			const seps = canvasElement.querySelectorAll('span.pad-sep');
			await expect(seps).toHaveLength(1);
			await expect(seps[0]).toHaveTextContent('|');
			await expect(seps[0].previousElementSibling).toHaveTextContent('D');
			await expect(seps[0].nextElementSibling).toHaveTextContent('E');
			await expect(getComputedStyle(strip as HTMLElement).display).toBe('none');
		}
	}
};
