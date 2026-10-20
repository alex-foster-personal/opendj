import { expect, waitFor } from 'storybook/test';

/**
 * BrandLaunch UI contract, shared by the Svelte story (BrandLaunch.stories.ts)
 * and its React twin (BrandLaunch.stories.tsx), both titled
 * 'Contracts/Root/BrandLaunch'. See src/react/README.md.
 *
 * The OPS-10 identity launch plays once per browser: it shows when the
 * completion marker is absent, ignores the halves' own slide animationend
 * events, and only on the overlay's fade end persists the marker and unmounts.
 * Each story seeds the marker in beforeEach and clears it afterwards.
 */

const STORAGE_KEY = 'odj.brand-launch.v1';
type Ctx = { canvasElement: HTMLElement };

const overlay = (root: HTMLElement): HTMLElement | null =>
	root.ownerDocument.querySelector<HTMLElement>('[aria-label="Open DJ launch animation"]');
const forget = (): void => window.localStorage.removeItem(STORAGE_KEY);

export const contracts = {
	/** First launch: the overlay and both halves show, survive the slide's end, then fade out and record completion. */
	FirstLaunch: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: {},
		beforeEach: () => {
			forget();
			return forget;
		},
		play: async ({ canvasElement }: Ctx) => {
			await waitFor(() => expect(overlay(canvasElement)).not.toBeNull());
			const el = overlay(canvasElement)!;
			await expect(el).toHaveClass('brand-launch');
			await expect(el.style.getPropertyValue('--brand-slide-ms').trim()).toBe('900ms');
			await expect(el.style.getPropertyValue('--brand-hold-ms').trim()).toBe('200ms');
			await expect(el.style.getPropertyValue('--brand-fade-ms').trim()).toBe('500ms');
			await expect(el.style.getPropertyValue('--brand-total-ms').trim()).toBe('1600ms');
			await expect(el.querySelector('.brand-mark')).toHaveAttribute('aria-hidden', 'true');
			const dark = el.querySelector('[data-testid="brand-launch-half-dark"]');
			const light = el.querySelector('[data-testid="brand-launch-half-light"]');
			await expect(dark).toHaveAttribute('fill', '#9c4b34');
			await expect(dark).toHaveClass('brand-half-dark');
			await expect(light).toHaveAttribute('fill', '#D97757');
			await expect(light).toHaveClass('brand-half-light');
			await expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
			// Past the 900 ms slide, before the 1100 ms fade start: the halves'
			// bubbling animationend must not end the launch.
			await new Promise((r) => setTimeout(r, 1000));
			await expect(overlay(canvasElement)).not.toBeNull();
			await expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
			await waitFor(() => expect(overlay(canvasElement)).toBeNull(), { timeout: 4000 });
			await expect(window.localStorage.getItem(STORAGE_KEY)).toBe('complete');
		}
	},
	/** Already played in this browser: nothing renders and the marker stays. */
	AlreadyPlayed: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: {},
		beforeEach: () => {
			window.localStorage.setItem(STORAGE_KEY, 'complete');
			return forget;
		},
		play: async ({ canvasElement }: Ctx) => {
			await new Promise((r) => setTimeout(r, 50));
			await expect(overlay(canvasElement)).toBeNull();
			await expect(canvasElement.ownerDocument.querySelector('.brand-launch')).toBeNull();
			await expect(window.localStorage.getItem(STORAGE_KEY)).toBe('complete');
		}
	}
};
