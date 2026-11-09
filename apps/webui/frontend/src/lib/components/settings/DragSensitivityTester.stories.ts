import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fireEvent, fn, waitFor, within } from 'storybook/test';

import DragSensitivityTester from './DragSensitivityTester.svelte';

/**
 * Settings drag-sensitivity tester: two test knobs and two test faders on
 * the mixer's drag code. It reads the live drag-sensitivity store, so it is
 * a contract story.
 */
const meta = {
	title: 'Contracts/Settings/DragSensitivityTester',
	component: DragSensitivityTester,
	tags: ['autodocs'],
	args: { onresetsensitivity: fn() }
} satisfies Meta<typeof DragSensitivityTester>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Both test faders and the sensitivity readout render. */
export const Default: Story = {
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByTestId('drag-sensitivity-tester')).toBeVisible();
		await expect(canvas.getByRole('slider', { name: 'Test fader A' })).toBeVisible();
		await expect(canvas.getByRole('slider', { name: 'Test fader B' })).toBeVisible();
		await expect(canvas.getByText(/x, half at \d+ px/)).toBeVisible();
	}
};

/** Reset calls the reset callback and recenters a dragged fader. */
export const ResetRecenters: Story = {
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		const fader = canvas.getByRole('slider', { name: 'Test fader A' });
		const box = fader.getBoundingClientRect();
		const x = box.left + box.width / 2;
		const y = box.top + box.height / 2;
		await fireEvent.pointerDown(fader, { pointerId: 1, button: 0, clientX: x, clientY: y });
		for (let i = 1; i <= 10; i++) {
			await fireEvent.pointerMove(fader, { pointerId: 1, buttons: 1, clientX: x, clientY: y + i * 20 });
		}
		await fireEvent.pointerUp(fader, { pointerId: 1, button: 0, clientX: x, clientY: y + 200 });
		await waitFor(() => expect(Number(fader.getAttribute('aria-valuenow'))).toBeLessThan(0.5));
		await fireEvent.click(canvas.getByRole('button', { name: 'Reset drag sensitivity' }));
		await expect(args.onresetsensitivity).toHaveBeenCalledTimes(1);
		await waitFor(() => expect(fader).toHaveAttribute('aria-valuenow', '0.5'));
	}
};
