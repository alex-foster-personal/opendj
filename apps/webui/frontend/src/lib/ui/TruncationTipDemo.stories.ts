import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, userEvent, waitFor, within } from 'storybook/test';

import TruncationTipDemo from './TruncationTipDemo.svelte';
import { TRUNCATION_TIP_DELAY_MS, TRUNCATION_TIP_ID } from './truncation-tip';

/**
 * LIBUX-57: a track name's hover tip shows only when the name is cut off,
 * after a rest of TRUNCATION_TIP_DELAY_MS, and a quick pass shows nothing.
 * The track names are invented.
 */
const LONG = 'Extended Club Mix of a Very Long Track Name That Cannot Fit';

const meta = {
	title: 'Contracts/UI/TruncationTip',
	component: TruncationTipDemo,
	args: { text: 'Strobe', width: 240 }
} satisfies Meta<typeof TruncationTipDemo>;

export default meta;
type Story = StoryObj<typeof meta>;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function tipShowing(): boolean {
	const tip = document.getElementById(TRUNCATION_TIP_ID);
	return tip !== null && !tip.hidden;
}

/** A name that fits its box never shows a tip, however long the rest. */
export const FitsShowsNoTip: Story = {
	play: async ({ canvasElement }) => {
		const label = within(canvasElement).getByText('Strobe');
		await userEvent.hover(label);
		await sleep(TRUNCATION_TIP_DELAY_MS + 300);
		await expect(tipShowing()).toBe(false);
		await userEvent.unhover(label);
	}
};

/** A cut-off name shows its full text after the delay, just below and aligned with the text start. */
export const TruncatedShowsTipAfterDelay: Story = {
	args: { text: LONG, width: 120 },
	play: async ({ canvasElement }) => {
		const label = within(canvasElement).getByText(LONG);
		await userEvent.hover(label);
		await sleep(TRUNCATION_TIP_DELAY_MS / 2);
		await expect(tipShowing()).toBe(false);
		await waitFor(() => expect(tipShowing()).toBe(true), { timeout: TRUNCATION_TIP_DELAY_MS + 500 });
		const tip = document.getElementById(TRUNCATION_TIP_ID)!;
		await expect(tip.textContent).toBe(LONG);
		const labelBox = label.getBoundingClientRect();
		const tipBox = tip.getBoundingClientRect();
		await expect(Math.abs(tipBox.left - labelBox.left)).toBeLessThanOrEqual(1);
		await expect(tipBox.top).toBeGreaterThanOrEqual(labelBox.bottom);
		await expect(label.getAttribute('aria-describedby')).toBe(TRUNCATION_TIP_ID);
		await userEvent.unhover(label);
		await expect(tipShowing()).toBe(false);
	}
};

/** Sweeping across a cut-off name (leave before the delay) shows nothing. */
export const QuickPassShowsNoTip: Story = {
	args: { text: LONG, width: 120 },
	play: async ({ canvasElement }) => {
		const label = within(canvasElement).getByText(LONG);
		await userEvent.hover(label);
		await sleep(150);
		await userEvent.unhover(label);
		await sleep(TRUNCATION_TIP_DELAY_MS + 300);
		await expect(tipShowing()).toBe(false);
	}
};
