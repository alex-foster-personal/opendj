import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, userEvent, waitFor, within } from 'storybook/test';

import LoopSafetyControls from './LoopSafetyControls.svelte';
import LoopSafetyControlsStoryDock from './LoopSafetyControlsStoryDock.svelte';

const meta = {
	title: 'Contracts/Deck/LoopSafetyControls',
	component: LoopSafetyControls,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	render: (args) => ({ Component: LoopSafetyControlsStoryDock, props: args }),
	args: {
		deckId: 1,
		pending: false,
		safety: { in_ms: 64_000, out_ms: 71_500, beat_length: 16, armed: false },
		canSaveSafety: true,
		safetyTip: 'save the engaged loop as the safety loop',
		onSafetySave: fn(async () => {}),
		onSafetyArm: fn(async () => {}),
		onSafetyClear: fn(async () => {})
	}
} satisfies Meta<typeof LoopSafetyControls>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Disarmed: arm click fires once with true and the control reads armed. */
export const Disarmed: Story = {
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		const arm = canvas.getByTestId('safety-loop-deck-1');
		await expect(arm).toHaveAttribute('aria-pressed', 'false');
		await expect(arm).toHaveAttribute('data-state', 'off');
		await expect(arm).toHaveTextContent('SAFE 16');
		await userEvent.click(arm);
		await expect(args.onSafetyArm).toHaveBeenCalledTimes(1);
		await expect(args.onSafetyArm).toHaveBeenCalledWith(true);
		await waitFor(() => expect(arm).toHaveAttribute('aria-pressed', 'true'));
		await expect(arm).toHaveAttribute('data-state', 'on');
	}
};

export const Armed: Story = {
	args: { safety: { in_ms: 64_000, out_ms: 71_500, beat_length: 16, armed: true } },
	play: async ({ canvasElement, args }) => {
		const arm = within(canvasElement).getByTestId('safety-loop-deck-1');
		await expect(arm).toHaveAttribute('aria-pressed', 'true');
		await userEvent.click(arm);
		await expect(args.onSafetyArm).toHaveBeenCalledTimes(1);
		await expect(args.onSafetyArm).toHaveBeenCalledWith(false);
		await waitFor(() => expect(arm).toHaveAttribute('aria-pressed', 'false'));
	}
};

export const ClearSaved: Story = {
	play: async ({ canvasElement, args }) => {
		const clear = within(canvasElement).getByTestId('clear-safety-loop-deck-1');
		await userEvent.click(clear);
		await expect(args.onSafetyClear).toHaveBeenCalledTimes(1);
	}
};

export const NoSafetyLoop: Story = {
	args: { safety: null },
	play: async ({ canvasElement, args }) => {
		const canvas = within(canvasElement);
		const save = canvas.getByTestId('save-safety-loop-deck-1');
		await expect(save).toBeEnabled();
		await userEvent.click(save);
		await expect(args.onSafetySave).toHaveBeenCalledTimes(1);
	}
};

export const Pending: Story = {
	args: { pending: true },
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByTestId('safety-loop-deck-1')).toBeDisabled();
		await expect(canvas.getByTestId('clear-safety-loop-deck-1')).toBeDisabled();
	}
};
