import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fireEvent, fn, waitFor } from 'storybook/test';

import Crossfader from './Crossfader.svelte';

/**
 * #14330 (epic #9365): the mixer crossfader at full A, centre and full B.
 * Each story checks where the thumb sits on the track and what the
 * accessible value names; the keyboard story drives the thumb with an
 * arrow key and checks onchange gets a number in 0..1.
 */

const meta = {
	title: 'Contracts/Mixer/Crossfader',
	component: Crossfader,
	args: { value: 0.5, onchange: fn() }
} satisfies Meta<typeof Crossfader>;
export default meta;
type Story = StoryObj<typeof meta>;

const slider = (root: HTMLElement): HTMLElement => {
	const el = root.querySelector<HTMLElement>('[role="slider"][aria-label="crossfader"]');
	if (el === null) throw new Error('no crossfader');
	return el;
};

function sizeCanvas(canvasElement: HTMLElement): void {
	canvasElement.style.width = '300px';
	canvasElement.style.display = 'flex';
}

/** Thumb centre as a fraction of the track width (0 = left edge, 1 = right edge). */
function thumbFraction(root: HTMLElement): number {
	const track = slider(root).getBoundingClientRect();
	const thumb = slider(root).querySelector<HTMLElement>('.thumb-hit');
	if (thumb === null) throw new Error('no thumb');
	const box = thumb.getBoundingClientRect();
	return (box.left + box.width / 2 - track.left) / track.width;
}

export const FullA: Story = {
	args: { value: 0 },
	play: async ({ canvasElement }) => {
		sizeCanvas(canvasElement);
		await waitFor(() => expect(thumbFraction(canvasElement)).toBeLessThan(0.2));
		await expect(slider(canvasElement).getAttribute('aria-valuetext')).toBe('100% left');
	}
};

export const Centre: Story = {
	args: { value: 0.5 },
	play: async ({ canvasElement }) => {
		sizeCanvas(canvasElement);
		await waitFor(() => expect(Math.abs(thumbFraction(canvasElement) - 0.5)).toBeLessThan(0.05));
		await expect(slider(canvasElement).getAttribute('aria-valuetext')).toBe('centre');
	}
};

export const FullB: Story = {
	args: { value: 1 },
	play: async ({ canvasElement }) => {
		sizeCanvas(canvasElement);
		await waitFor(() => expect(thumbFraction(canvasElement)).toBeGreaterThan(0.8));
		await expect(slider(canvasElement).getAttribute('aria-valuetext')).toBe('100% right');
	}
};

export const KeyboardArrowCallsOnchange: Story = {
	args: { value: 0.5 },
	play: async ({ canvasElement, args }) => {
		sizeCanvas(canvasElement);
		const el = slider(canvasElement);
		el.focus();
		await expect(document.activeElement).toBe(el);
		await fireEvent.keyDown(el, { key: 'ArrowRight' });
		await expect(args.onchange).toHaveBeenCalledTimes(1);
		const v = (args.onchange as ReturnType<typeof fn>).mock.calls[0][0] as number;
		await expect(typeof v).toBe('number');
		await expect(v).toBeGreaterThanOrEqual(0);
		await expect(v).toBeLessThanOrEqual(1);
	}
};
