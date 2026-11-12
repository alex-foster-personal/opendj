import { expect, userEvent, waitFor, within } from 'storybook/test';

import { BEAT_FX_UNIT_NOT_WIRED_REASON } from '$lib/rb/beat-fx-unit-port.svelte';

/**
 * PadModeMenu UI contract, shared by the Svelte story (PadModeMenu.stories.ts)
 * and its React twin (PadModeMenu.stories.tsx), both titled
 * 'Contracts/Deck/PadModeMenu'. See src/react/README.md.
 *
 * Hot Cue, Beat Jump (DECKUX-9956) and Pad FX 1/2 are built. Pad FX is disabled with the not-wired
 * reason when no beat FX unit port is installed (as in Storybook); every other
 * mode is listed for hardware parity and renders inert with the not-built tooltip.
 */

type Args = { deckId: 1 | 2 | 3 | 4; stacked?: boolean };
type Ctx = { canvasElement: HTMLElement; args: Args };

const TRIGGER_TITLE =
	"Pad mode menu - lists the pad modes a controller's performance-pad selector offers; modes marked not-built-yet do nothing yet";
const NOT_BUILT_TIP =
	'Pad mode - this performance-pad mode (for example slicer, sampler or keyboard) will turn the eight pads into its own controls. It is listed so the menu matches the hardware, but it is not built yet, so picking it changes nothing. (Not built yet.)';
const LABELS = [
	'Hot Cue',
	'Beat Loop / Roll (not-built-yet)',
	'Beat Jump',
	'Sampler (not-built-yet)',
	'Pad FX 1',
	'Pad FX 2',
	'Key Shift (not-built-yet)',
	'Keyboard (not-built-yet)'
];

const menu = (root: HTMLElement): HTMLElement | null => root.querySelector<HTMLElement>('.pad-menu');

export const contracts = {
	/** Closed: the HOT CUE trigger, titled and collapsed, and no menu in the DOM. */
	Closed: {
		args: { deckId: 1, stacked: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const trigger = within(canvasElement).getByRole('button', { name: 'hot cue menu deck 1' });
			await expect(trigger).toHaveTextContent(/^HOT CUE$/);
			await expect(trigger).toHaveClass('rb-lit-button', 'dropdown');
			await expect(trigger).not.toHaveClass('stacked');
			await expect(trigger).toHaveAttribute('aria-expanded', 'false');
			await expect(trigger).toHaveAttribute('title', TRIGGER_TITLE);
			await expect(trigger).toHaveAttribute('data-testid', 'hot-cue-menu-deck-1');
			await expect(menu(canvasElement)).toBeNull();
		}
	},
	/** Open: eight modes, Hot Cue live, Pad FX inert with the not-wired reason, the rest with the not-built tip, placed fixed below the trigger. */
	Open: {
		args: { deckId: 3, stacked: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			const trigger = canvas.getByRole('button', { name: 'hot cue menu deck 3' });
			await userEvent.click(trigger);
			await expect(trigger).toHaveAttribute('aria-expanded', 'true');
			const list = canvas.getByRole('menu');
			const items = within(list).getAllByRole('menuitem');
			await expect(items.map((i) => i.textContent?.trim())).toEqual(LABELS);
			await expect(items[0]).toBeEnabled();
			await expect(items[0]).not.toHaveAttribute('title');
			await expect(items[0]).toHaveAttribute('data-built', 'true');
			await expect(items[0]).not.toHaveClass('rb-inert');
			await expect(items[2]).toBeEnabled();
			await expect(items[2]).toHaveAttribute('data-built', 'true');
			for (const [i, item] of items.entries()) {
				if (i === 0 || i === 2) continue;
				const padFx = i === 4 || i === 5;
				await expect(item).toBeDisabled();
				await expect(item).toHaveClass('pad-menu-item', 'rb-inert');
				await expect(item).toHaveAttribute('data-built', padFx ? 'true' : 'false');
				await expect(item).toHaveAttribute('title', padFx ? BEAT_FX_UNIT_NOT_WIRED_REASON : NOT_BUILT_TIP);
			}
			await expect(items[2]).toHaveAttribute('data-pad-mode', 'beat-jump');
			await waitFor(() => expect(list.style.position).toBe('fixed'));
			const t = trigger.getBoundingClientRect();
			await expect(list.style.top).toBe(`${Math.round(t.top + t.height + 4)}px`);
			await expect(list.style.left).toBe(`${Math.round(t.left)}px`);
		}
	},
	/** Picking Hot Cue, Escape (focus back on the trigger) and an outside press each close the menu. */
	Dismiss: {
		args: { deckId: 2, stacked: false } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const canvas = within(canvasElement);
			const trigger = canvas.getByRole('button', { name: 'hot cue menu deck 2' });
			await userEvent.click(trigger);
			await userEvent.click(canvas.getByRole('menuitem', { name: 'Hot Cue' }));
			await expect(menu(canvasElement)).toBeNull();
			await expect(trigger).toHaveAttribute('aria-expanded', 'false');

			await userEvent.click(trigger);
			await expect(menu(canvasElement)).not.toBeNull();
			(canvas.getByRole('menuitem', { name: 'Hot Cue' }) as HTMLElement).focus();
			await userEvent.keyboard('{Escape}');
			await expect(menu(canvasElement)).toBeNull();
			await expect(trigger).toHaveFocus();

			await userEvent.click(trigger);
			await expect(menu(canvasElement)).not.toBeNull();
			await userEvent.click(canvasElement.ownerDocument.body);
			await expect(menu(canvasElement)).toBeNull();

			// A second click on the trigger toggles it shut as well.
			await userEvent.click(trigger);
			await userEvent.click(trigger);
			await expect(menu(canvasElement)).toBeNull();
		}
	},
	/** Stacked: the narrow grid-column variant carries .stacked; the text stays HOT CUE. */
	Stacked: {
		args: { deckId: 4, stacked: true } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const trigger = within(canvasElement).getByRole('button', { name: 'hot cue menu deck 4' });
			await expect(trigger).toHaveClass('rb-lit-button', 'dropdown', 'stacked');
			await expect(trigger).toHaveTextContent(/^HOT CUE$/);
		}
	}
};
