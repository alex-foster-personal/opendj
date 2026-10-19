import { expect, fn, userEvent, waitFor, within } from 'storybook/test';

/**
 * ContextMenu UI contract, shared by the Svelte story (ContextMenu.stories.ts)
 * and its React twin (ContextMenu.stories.tsx), both titled
 * 'Contracts/Root/ContextMenu'. See src/react/README.md.
 *
 * An item with no `run` is unavailable: aria-disabled, explained by the
 * planned-control text both as its title and as a visually hidden
 * description, and choosing it does nothing (the menu stays open).
 */

type Item = { id: string; label: string; run?: (() => void | Promise<void>) | undefined; title?: string; testId?: string; checked?: boolean };
type Args = { items: Item[]; x: number; y: number; onclose: () => void };
type Ctx = { canvasElement: HTMLElement; args: Args };

const UNAVAILABLE =
	'Library action - this menu command will act on the selected tracks or playlist. Its handler is not connected yet, so choosing it changes nothing. (Not built yet.)';

const makeItems = (): Item[] => [
	{ id: 'load', label: 'Load to Deck 1', run: fn(), testId: 'ctx-load' },
	{ id: 'analyze', label: 'Analyze Track', run: fn(), title: 'Run beatgrid and key analysis on this track' },
	{ id: 'export', label: 'Export to Device' },
	{ id: 'show-artwork', label: 'Show Artwork', run: fn(), checked: true }
];

const menu = (root: HTMLElement): HTMLElement => within(root).getByRole('menu');
const buttons = (root: HTMLElement): HTMLButtonElement[] => Array.from(menu(root).querySelectorAll('button'));

export const contracts = {
	/** Every item shape: runnable, custom title, unavailable (aria-disabled + description), checkbox; placed at x/y and focused. */
	Items: {
		args: { items: makeItems(), x: 40, y: 30, onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const m = menu(canvasElement);
			await expect(m).toHaveClass('context-menu');
			await expect(m).toHaveAttribute('data-testid', 'context-menu');
			await expect(m).toHaveAttribute('tabindex', '-1');
			await waitFor(() => expect(m.style.left).toBe('40px'));
			await expect(m.style.top).toBe('30px');
			await waitFor(() => expect(m).toHaveFocus());

			const [load, analyze, exp, artwork] = buttons(canvasElement);
			await expect(buttons(canvasElement).map((b) => b.textContent)).toEqual([
				'Load to Deck 1',
				'Analyze Track',
				'Export to Device',
				'Show Artwork'
			]);
			await expect(load).toHaveAttribute('role', 'menuitem');
			await expect(load).toHaveAttribute('data-testid', 'ctx-load');
			await expect(load).toHaveAttribute('title', 'Load to Deck 1');
			await expect(load).not.toHaveAttribute('aria-disabled');
			await expect(load).not.toHaveAttribute('aria-describedby');
			await expect(analyze).toHaveAttribute('title', 'Run beatgrid and key analysis on this track');
			await expect(analyze).not.toHaveAttribute('data-testid');

			await expect(exp).toHaveAttribute('aria-disabled', 'true');
			await expect(exp).toHaveAttribute('title', UNAVAILABLE);
			await expect(exp).toHaveAttribute('aria-describedby', 'ctx-menu-desc-export');
			const desc = canvasElement.querySelector('#ctx-menu-desc-export');
			await expect(desc).toHaveClass('visually-hidden');
			await expect(desc).toHaveTextContent(UNAVAILABLE);
			await expect(canvasElement.querySelectorAll('.visually-hidden')).toHaveLength(1);

			await expect(artwork).toHaveAttribute('role', 'menuitemcheckbox');
			await expect(artwork).toHaveAttribute('aria-checked', 'true');
			await expect(load).not.toHaveAttribute('aria-checked');
			await expect(args.onclose).not.toHaveBeenCalled();
		}
	},
	/** Choosing a runnable item closes the menu, then runs it; an unavailable one does neither. */
	Activate: {
		args: { items: makeItems(), x: 40, y: 30, onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			await userEvent.click(canvas.getByRole('menuitem', { name: 'Export to Device' }));
			await expect(args.onclose).not.toHaveBeenCalled();
			await userEvent.click(canvas.getByRole('menuitem', { name: 'Analyze Track' }));
			await expect(args.onclose).toHaveBeenCalledTimes(1);
			await expect(args.items[1].run).toHaveBeenCalledTimes(1);
			await expect(args.items[0].run).not.toHaveBeenCalled();
			await userEvent.click(canvas.getByRole('menuitemcheckbox', { name: 'Show Artwork' }));
			await expect(args.items[3].run).toHaveBeenCalledTimes(1);
			await expect(args.onclose).toHaveBeenCalledTimes(2);
		}
	},
	/** Arrow keys walk and wrap the items, Home/End jump, Enter on an unavailable item is swallowed, Escape closes. */
	Keyboard: {
		args: { items: makeItems(), x: 40, y: 30, onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const m = menu(canvasElement);
			await waitFor(() => expect(m).toHaveFocus());
			const [load, analyze, exp, artwork] = buttons(canvasElement);
			await userEvent.keyboard('{ArrowDown}');
			await expect(load).toHaveFocus();
			await userEvent.keyboard('{ArrowDown}');
			await expect(analyze).toHaveFocus();
			await userEvent.keyboard('{ArrowUp}{ArrowUp}');
			await expect(artwork).toHaveFocus();
			await userEvent.keyboard('{ArrowDown}');
			await expect(load).toHaveFocus();
			await userEvent.keyboard('{End}');
			await expect(artwork).toHaveFocus();
			await userEvent.keyboard('{Home}');
			await expect(load).toHaveFocus();
			exp.focus();
			await userEvent.keyboard('{Enter}');
			await expect(args.onclose).not.toHaveBeenCalled();
			await userEvent.keyboard('{Escape}');
			await expect(args.onclose).toHaveBeenCalledTimes(1);
		}
	},
	/** A point past the viewport corner is clamped 8px inside it; a press outside the menu closes it. */
	Clamped: {
		args: { items: makeItems(), x: 100000, y: 100000, onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const m = menu(canvasElement);
			const win = canvasElement.ownerDocument.defaultView!;
			await waitFor(() => expect(m.style.left).not.toBe('0px'));
			const r = m.getBoundingClientRect();
			await expect(m.style.left).toBe(`${win.innerWidth - r.width - 8}px`);
			await expect(m.style.top).toBe(`${win.innerHeight - r.height - 8}px`);
			await userEvent.click(m);
			await expect(args.onclose).not.toHaveBeenCalled();
			await userEvent.click(canvasElement.ownerDocument.body);
			await expect(args.onclose).toHaveBeenCalledTimes(1);
		}
	}
};
