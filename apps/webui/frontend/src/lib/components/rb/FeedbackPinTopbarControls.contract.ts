import { expect, userEvent, waitFor, within } from 'storybook/test';

import { readPinsVisible, writePinsVisible } from '$lib/rb/feedback-pin-visibility';
import { PINS_VISIBLE_KEY } from '$lib/rb/feedback';

/**
 * FeedbackPinTopbarControls UI contract, shared by the Svelte story
 * (FeedbackPinTopbarControls.stories.ts) and its React twin
 * (FeedbackPinTopbarControls.stories.tsx). The component takes no props; its
 * state is the persisted pins-visible preference. Each play first resets
 * that preference through the module, which also proves the same-tab sync.
 */

type Ctx = { canvasElement: HTMLElement };

const box = (root: HTMLElement): HTMLInputElement =>
	within(root).getByRole('checkbox', { name: 'Show feedback comment pins' }) as HTMLInputElement;

export const contracts = {
	/** Clicking the label flips the box and persists "1" then "0". */
	Toggle: {
		args: {},
		play: async ({ canvasElement }: Ctx) => {
			writePinsVisible(window.localStorage, false);
			await waitFor(() => expect(box(canvasElement)).not.toBeChecked());
			const label = canvasElement.querySelector('label.fb-shell-pins-toggle');
			await expect(label).toHaveAttribute('title', 'Show or hide feedback comment pins on every page');
			await userEvent.click(box(canvasElement));
			await expect(box(canvasElement)).toBeChecked();
			await expect(window.localStorage.getItem(PINS_VISIBLE_KEY)).toBe('1');
			await userEvent.click(within(canvasElement).getByText('Show feedback comment pins'));
			await expect(box(canvasElement)).not.toBeChecked();
			await expect(readPinsVisible(window.localStorage)).toBe(false);
		}
	},
	/** Another surface writing the preference in the same tab updates the box. */
	ExternalSync: {
		args: {},
		play: async ({ canvasElement }: Ctx) => {
			writePinsVisible(window.localStorage, false);
			await waitFor(() => expect(box(canvasElement)).not.toBeChecked());
			writePinsVisible(window.localStorage, true);
			await waitFor(() => expect(box(canvasElement)).toBeChecked());
		}
	}
};
