import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fireEvent, fn } from 'storybook/test';

import DragTestFader from './DragTestFader.svelte';

/**
 * The settings test fader: the channel fader's drag path with no deck
 * wiring. It registers with the shared knob selection, so it is a contract
 * story. No story shift-clicks (that writes the global selection).
 */
const meta = {
	title: 'Contracts/Settings/DragTestFader',
	component: DragTestFader,
	tags: ['autodocs'],
	args: { controlId: 'story-fader', label: 'Test fader', value: 0.5, onchange: fn() }
} satisfies Meta<typeof DragTestFader>;

export default meta;
type Story = StoryObj<typeof meta>;

function slider(root: HTMLElement): HTMLElement {
	return root.querySelector<HTMLElement>('[data-testid="drag-test-story-fader"]')!;
}

function thumbTop(root: HTMLElement): string {
	return root.querySelector<HTMLElement>('.dtf-thumb')!.style.top;
}

/** Fader at the bottom. */
export const AtBottom: Story = {
	args: { value: 0 },
	play: async ({ canvasElement }) => {
		await expect(slider(canvasElement)).toHaveAttribute('aria-valuenow', '0');
		await expect(thumbTop(canvasElement)).toBe('96px');
	}
};

/** Fader centered. */
export const Centered: Story = {
	args: { value: 0.5 },
	play: async ({ canvasElement }) => {
		await expect(slider(canvasElement)).toHaveAttribute('aria-valuenow', '0.5');
		await expect(thumbTop(canvasElement)).toBe('48px');
	}
};

/** Fader at the top. */
export const AtTop: Story = {
	args: { value: 1 },
	play: async ({ canvasElement }) => {
		await expect(slider(canvasElement)).toHaveAttribute('aria-valuenow', '1');
		await expect(thumbTop(canvasElement)).toBe('0px');
	}
};

/** A press at the center jumps to 0.5, then a long downward drag reaches the bottom. */
export const DragToBottom: Story = {
	args: { value: 0.5 },
	play: async ({ canvasElement, args }) => {
		const el = slider(canvasElement);
		const box = el.getBoundingClientRect();
		const x = box.left + box.width / 2;
		const y = box.top + box.height / 2;
		await fireEvent.pointerDown(el, { pointerId: 1, button: 0, clientX: x, clientY: y });
		for (let i = 1; i <= 20; i++) {
			await fireEvent.pointerMove(el, { pointerId: 1, buttons: 1, clientX: x, clientY: y + i * 20 });
		}
		await fireEvent.pointerUp(el, { pointerId: 1, button: 0, clientX: x, clientY: y + 400 });
		const onchange = args.onchange as ReturnType<typeof fn>;
		await expect(onchange).toHaveBeenCalled();
		await expect(onchange.mock.calls[0][0]).toBeCloseTo(0.5, 5);
		await expect(Math.abs(onchange.mock.lastCall![0] as number)).toBeLessThanOrEqual(0.02);
	}
};
