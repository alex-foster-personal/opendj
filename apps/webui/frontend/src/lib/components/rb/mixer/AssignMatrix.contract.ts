import { expect, fn, userEvent } from 'storybook/test';

import type { CrossfaderAssign, MixerChannelState } from '$lib/rb/mixer-types';

type DeckId = MixerChannelState['deck_id'];

/**
 * AssignMatrix UI contract, shared by the Svelte story (AssignMatrix.stories.ts)
 * and its React twin (AssignMatrix.stories.tsx). See src/react/README.md.
 */

type Args = {
	bus: 'A' | 'B';
	assigns: Record<DeckId, CrossfaderAssign>;
	onassign: (deck: DeckId, assign: CrossfaderAssign) => void;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const cells = (root: HTMLElement): HTMLButtonElement[] =>
	Array.from(root.querySelectorAll<HTMLButtonElement>('div.matrix button.cell'));
const assigns: Record<DeckId, CrossfaderAssign> = { 1: 'A', 2: 'B', 3: 'THRU', 4: 'A' };

export const contracts = {
	/** Bus A: channels on A are lit; click releases a lit one to THRU and pulls others onto A. */
	BusA: {
		args: { bus: 'A', assigns, onassign: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(canvasElement.querySelector('div.matrix')).toHaveAttribute('aria-label', 'crossfader A assign matrix');
			const c = cells(canvasElement);
			await expect(c.map((b) => b.textContent?.trim())).toEqual(['1', '2', '3', '4']);
			await expect(c.map((b) => b.classList.contains('lit'))).toEqual([true, false, false, true]);
			await expect(c[0]).toHaveAttribute('title', 'Channel 1 is on left (A) now. Click to release to THRU.');
			await expect(c[1]).toHaveAttribute('title', 'Channel 2 is on B now. Click to move it to left (A).');
			await expect(c[2]).toHaveAttribute(
				'title',
				'Channel 3 is THRU (not on the crossfader) now. Click to assign to left (A).'
			);
			await userEvent.click(c[0]);
			await expect(args.onassign).toHaveBeenLastCalledWith(1, 'THRU');
			await userEvent.click(c[1]);
			await expect(args.onassign).toHaveBeenLastCalledWith(2, 'A');
			await userEvent.click(c[2]);
			await expect(args.onassign).toHaveBeenLastCalledWith(3, 'A');
		}
	},
	/** Bus B over the same assignments: only channel 2 is lit. */
	BusB: {
		args: { bus: 'B', assigns, onassign: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(canvasElement.querySelector('div.matrix')).toHaveAttribute('aria-label', 'crossfader B assign matrix');
			const c = cells(canvasElement);
			await expect(c.map((b) => b.classList.contains('lit'))).toEqual([false, true, false, false]);
			await expect(c[1]).toHaveAttribute('title', 'Channel 2 is on right (B) now. Click to release to THRU.');
			await expect(c[3]).toHaveAttribute('title', 'Channel 4 is on A now. Click to move it to right (B).');
			await userEvent.click(c[1]);
			await expect(args.onassign).toHaveBeenLastCalledWith(2, 'THRU');
			await userEvent.click(c[3]);
			await expect(args.onassign).toHaveBeenLastCalledWith(4, 'B');
		}
	}
};
