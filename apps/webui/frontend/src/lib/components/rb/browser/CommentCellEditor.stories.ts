import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, userEvent, waitFor, within } from 'storybook/test';

import CommentCellEditor from './CommentCellEditor.svelte';

/**
 * LIBUX-56: the library Comment cell's inline editor. Enter or blur saves,
 * Escape cancels, an unchanged draft saves nothing. `oncommit` is a spy, so
 * these stories never write a track. The comment text is invented.
 */
const meta = {
	title: 'Contracts/Browser/CommentCellEditor',
	component: CommentCellEditor,
	args: {
		initial: 'old note',
		oncommit: fn(async () => {}),
		oncancel: fn()
	}
} satisfies Meta<typeof CommentCellEditor>;

export default meta;
type Story = StoryObj<typeof meta>;

/** It opens focused with the current comment selected, so typing replaces it. */
export const OpensFocusedAndSelected: Story = {
	play: async ({ canvasElement }) => {
		const input = await within(canvasElement).findByRole('textbox', { name: 'Edit comment' });
		await waitFor(() => expect(input).toHaveFocus());
		await expect(input).toHaveValue('old note');
		await userEvent.keyboard('peak time');
		await expect(input).toHaveValue('peak time');
	}
};

/** Enter saves the draft once, even though the blur that follows also fires. */
export const EnterSaves: Story = {
	play: async ({ canvasElement, args }) => {
		const input = await within(canvasElement).findByRole('textbox', { name: 'Edit comment' });
		await waitFor(() => expect(input).toHaveFocus());
		await userEvent.clear(input);
		await userEvent.type(input, 'big room opener{Enter}');
		input.blur();
		await expect(args.oncommit).toHaveBeenCalledTimes(1);
		await expect(args.oncommit).toHaveBeenCalledWith('big room opener');
		await expect(args.oncancel).not.toHaveBeenCalled();
	}
};

/** Escape cancels: nothing is saved, not even by the blur after it. */
export const EscapeCancels: Story = {
	play: async ({ canvasElement, args }) => {
		const input = await within(canvasElement).findByRole('textbox', { name: 'Edit comment' });
		await waitFor(() => expect(input).toHaveFocus());
		await userEvent.type(input, ' changed{Escape}');
		input.blur();
		await expect(args.oncancel).toHaveBeenCalledTimes(1);
		await expect(args.oncommit).not.toHaveBeenCalled();
	}
};

/** Clicking away (blur) saves the draft; an emptied draft saves an empty comment. */
export const BlurSaves: Story = {
	play: async ({ canvasElement, args }) => {
		const input = await within(canvasElement).findByRole('textbox', { name: 'Edit comment' });
		await waitFor(() => expect(input).toHaveFocus());
		await userEvent.clear(input);
		input.blur();
		await expect(args.oncommit).toHaveBeenCalledWith('');
	}
};

/** An unchanged draft is a cancel, not a write. */
export const UnchangedSavesNothing: Story = {
	play: async ({ canvasElement, args }) => {
		const input = await within(canvasElement).findByRole('textbox', { name: 'Edit comment' });
		await waitFor(() => expect(input).toHaveFocus());
		await userEvent.keyboard('{Enter}');
		await expect(args.oncommit).not.toHaveBeenCalled();
		await expect(args.oncancel).toHaveBeenCalledTimes(1);
	}
};
